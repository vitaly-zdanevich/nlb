'''Automatically refresh published bibliographic metadata from checked source snapshots.

Publication baselines and per-page receipts establish which generated fields may
change. Fresh source identity, Commons binary identity, and revision checks remain
mandatory. Unrelated or conflicting pages are reported while the run continues.
'''

from collections import defaultdict
from datetime import datetime, timezone
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Callable, Iterable

import requests

from metadata_refresh_merge import information, managed_blocks, merge_generated_metadata, merge_metadata_blocks
from preservation import atomic_json
from reconcile_metadata import _managed_block, _read_description, read_commons_page, edit_commons_page


SHA1 = re.compile(r'[0-9a-fA-F]{40}')


def text_hash(text: str) -> str:
	'''Identify exact UTF-8 wikitext without interpreting its contents.'''
	return hashlib.sha256(text.encode('utf-8')).hexdigest()


def read_description(root: Path, reference: dict) -> str:
	'''Read an immutable archive object only after path and checksum verification.'''
	path = (root / reference['path']).resolve()
	if not path.is_relative_to(root.resolve()):
		raise ValueError('Archive description path escapes archive')
	body = path.read_bytes()
	if hashlib.sha256(body).hexdigest() != reference['sha256']:
		raise ValueError('Archive description checksum mismatch')
	return body.decode('utf-8')


def read_refresh_page(title: str) -> dict:
	'''Bound transient anonymous metadata reads; access and certificate failures propagate.'''
	for attempt in range(3):
		try:
			return read_commons_page(title)
		except (requests.RequestException, ValueError) as error:
			if isinstance(error, requests.exceptions.SSLError):
				raise
			if isinstance(error, requests.HTTPError) and (error.response is None or error.response.status_code not in (429, 500, 502, 503, 504)):
				raise
			if attempt == 2:
				raise
			time.sleep(2 ** (attempt + 1))
	return {}  # Unreachable after the bounded loop.


def _targets(item: dict) -> list[str]:
	'''Retain all known actual Commons targets, including duplicate associations.'''
	return list(dict.fromkeys(title for title in [item.get('commons_title'), *item.get('duplicates', [])]
		if isinstance(title, str) and title.startswith('File:')))


def _binary(item: dict) -> tuple[str, int]:
	'''Require a positive saved byte size and a valid SHA-1 for every metadata update.'''
	sha1, size = item.get('sha1', ''), item.get('size_bytes')
	if not isinstance(sha1, str) or not SHA1.fullmatch(sha1) or type(size) is not int or size <= 0:
		raise ValueError('Missing or invalid saved binary identity')
	return sha1.lower(), size


def _publication_history(root: Path, binaries: set[tuple[str, int]]) -> dict:
	'''Index true upload snapshots; copied uploaded statuses are not publication evidence.'''
	history = defaultdict(list)
	for path in sorted((root / 'records').glob('*/snapshots/*/manifest.json')):
		manifest = json.loads(path.read_text(encoding='utf-8'))
		for fid, item in manifest.get('files', {}).items():
			if item.get('status') not in ('uploaded', 'verification_failed') or not item.get('description') or item.get('metadata_refresh'):
				continue
			try:
				binary = _binary(item)
				published = datetime.fromisoformat(item['updated_at'])
				captured = datetime.fromisoformat(manifest['captured_at'])
				if published < captured:
					continue
			except (KeyError, TypeError, ValueError):
				continue
			if binary not in binaries:
				continue
			history[binary].append({'key': manifest['record_id'] + ':' + fid,
				'text': read_description(root, item['description']), 'titles': _targets(item),
				'exact_only': item['status'] != 'uploaded' or not item.get('verification')})
	return history


def _block(root: Path, candidate: dict) -> str:
	'''Use the existing readable catalog block format for secondary source associations.'''
	record, fid = candidate['key'].split(':', 1)
	return _managed_block('', record, fid, _read_description(root, candidate['item']['description'])).strip()


def _historical_blocks(root: Path, page: dict, candidates: list[dict]) -> dict[str, set[str]]:
	'''Bootstrap older additive metadata sections from exact archived renderer output.'''
	present = managed_blocks(page['text'])
	needed = {item['key'] for item in candidates}.intersection(present)
	result = defaultdict(set)
	for key in needed:
		record, fid = key.split(':', 1)
		for path in (root / 'records' / record / 'snapshots').glob('*/manifest.json'):
			item = json.loads(path.read_text()).get('files', {}).get(fid, {})
			try:
				matches = _binary(item) == _binary(page)
			except ValueError:
				continue
			if matches and item.get('description'):
				result[key].add(_block(root, {'key': key, 'item': item}))
	return result


def _proposal(root: Path, page: dict, candidates: list[dict], history: list[dict], receipt: dict | None) -> tuple[str, dict]:
	'''Choose a proven primary owner, or update separate source blocks on an unowned page.'''
	baseline = None
	primary_key = None
	mode = 'blocks'
	if receipt is not None:
		if receipt.get('title') != page['title'] or _binary(receipt) != _binary(page) or text_hash(receipt['generated_text']) != receipt['generated_sha256']:
			raise ValueError('Metadata update receipt identity/integrity mismatch')
		if receipt['status'] == 'applied' or text_hash(page['text']) == receipt['expected_text_sha256']:
			baseline = receipt['generated_text']
		else:
			baseline = receipt['baseline_text']
		primary_key, mode = receipt['primary_key'], receipt['mode']
	else:
		aliases = {page['title'], *page.get('aliases', [])}
		owners = [item for item in history if aliases.intersection(item['titles'])]
		# Post-upload verification failures may have an unpublished duplicate
		# description. Only exact current template agreement can bootstrap them.
		owners = [item for item in owners if not item['exact_only'] or str(information(item['text'])) == str(information(page['text']))]
		if owners:
			owner = owners[0]
			baseline, primary_key, mode = owner['text'], owner['key'], 'information'
	if baseline is None:
		baseline = ''
	present_blocks = managed_blocks(page['text'])
	old_blocks = managed_blocks(baseline)
	archived_blocks = _historical_blocks(root, page, candidates) if present_blocks else {}
	generated = ''
	if mode == 'information':
		matches = [item for item in candidates if item['key'] == primary_key]
		if not matches:
			if len({str(information(item['text'])) for item in candidates}) != 1:
				raise ValueError('Conflicting replacement catalog associations for the primary description')
			matches = candidates[:1]
		primary = matches[0]
		primary_key = primary['key']
		generated = primary['text']
	for candidate in candidates:
		key = candidate['key']
		if key != primary_key or key in present_blocks:
			new_block = _block(root, candidate)
			generated += '\n' + new_block
			if key in present_blocks and key not in old_blocks:
				if present_blocks[key] != new_block and present_blocks[key] not in archived_blocks[key]:
					raise ValueError('Human-edited or unowned metadata block: ' + key)
				baseline += '\n' + present_blocks[key]
	# A failed or unselected source refresh cannot delete an earlier source's
	# published section. Its metadata remains until that source can be checked.
	for key, old_block in old_blocks.items():
		if key not in {item['key'] for item in candidates}:
			generated += '\n' + old_block
	merger = merge_generated_metadata if mode == 'information' else merge_metadata_blocks
	proposed = merger(page['text'], baseline, generated)
	preserved = []
	if mode == 'information':
		current_fields, old_fields, new_fields = [{item.name.strip(): str(item.value).strip() for item in information(text).params}
			for text in (page['text'], baseline, generated)]
		preserved = [name for name in old_fields if name != 'permission'
			and current_fields.get(name) != old_fields.get(name) and current_fields.get(name) != new_fields.get(name)]
	return proposed, {'mode': mode, 'primary_key': primary_key, 'baseline_text': baseline,
		'generated_text': generated, 'generated_sha256': text_hash(generated), 'preserved_fields': preserved}


def apply_uploaded_metadata(
	archive_dir: Path, snapshots: Iterable[Path], *, dry_run: bool = False,
	report_path: Path | None = None, pywikibot_dir: str | None = None,
	fetch_page: Callable | None = None, edit_page: Callable | None = None,
) -> dict[str, Any]:
	'''Apply fresh eligible metadata automatically, preserving state and recording each outcome.

	Receipts precede edits so an interrupted run can recognize a completed write.
	Running again refreshes sources and skips unchanged pages without requesting
	manual approval. A dry run writes report previews but no edit receipts.
	'''
	root = Path(archive_dir)
	if pywikibot_dir:
		os.environ['PYWIKIBOT_DIR'] = str(pywikibot_dir)
	reader, writer = fetch_page or read_refresh_page, edit_page or edit_commons_page
	report = {'counts': dict.fromkeys(('metadata_updated', 'metadata_unchanged', 'metadata_preview', 'metadata_conflict', 'metadata_edit_failed', 'metadata_source_skipped'), 0), 'items': []}
	output = report_path or root / 'metadata-refresh' / 'report.json'
	def outcome(status: str, **details: Any) -> None:
		'''Checkpoint a bounded per-page result without overwriting the upload state.'''
		report['counts'][status] += 1
		report['items'].append({'status': status, **details})
		atomic_json(output, report)
		print(f'[{details.get("title", details.get("key", "metadata"))}] {status}: {details.get("error", "")}')
	requested = defaultdict(list)
	for snapshot in snapshots:
		path = Path(snapshot).resolve()
		if not path.is_relative_to(root.resolve() / 'records'):
			raise ValueError('Refresh snapshot is outside the archive records')
		manifest = json.loads(path.read_text(encoding='utf-8'))
		for fid, item in manifest.get('files', {}).items():
			key = manifest['record_id'] + ':' + fid
			if item.get('metadata_refresh', {}).get('status') != 'metadata_ready' or (item.get('scan_identity') or {}).get('eligible') is not True:
				outcome('metadata_source_skipped', key=key, error=item.get('error', 'Incomplete or ambiguous source refresh'))
				continue
			try:
				binary = _binary(item)
				text = read_description(root, item['description'])
				if not _targets(item):
					raise ValueError('No recorded Commons target')
			except (KeyError, OSError, ValueError) as error:
				outcome('metadata_source_skipped', key=key, error=str(error))
				continue
			for title in _targets(item):
				requested[title].append({'key': key, 'item': item, 'text': text, 'binary': binary})
	groups = {}
	for title, candidates in requested.items():
		try:
			page = reader(title)
			if any(item['binary'] != _binary(page) for item in candidates):
				raise ValueError('Commons SHA-1 or byte size differs from the archived source')
			group = groups.setdefault(page['title'], {'page': page, 'candidates': {}, 'aliases': set()})
			group['aliases'].add(title)
			group['candidates'].update({item['key']: item for item in candidates})
		except (requests.RequestException, ValueError, KeyError) as error:
			outcome('metadata_conflict', title=title, error=str(error))
			if isinstance(error, requests.exceptions.SSLError):
				raise
	history = _publication_history(root, {_binary(group['page']) for group in groups.values()})
	for title, group in groups.items():
		page = group['page']
		receipt_path = root / 'metadata-refresh' / 'pages' / (text_hash(title) + '.json')
		try:
			receipt = json.loads(receipt_path.read_text()) if receipt_path.exists() else None
			new_text, ownership = _proposal(root, {**page, 'aliases': group['aliases']}, list(group['candidates'].values()), history[_binary(page)], receipt)
			if new_text == page['text']:
				if not dry_run and receipt and receipt['status'] == 'pending' and text_hash(page['text']) == receipt['expected_text_sha256']:
					atomic_json(receipt_path, {**receipt, 'status': 'applied'})
				outcome('metadata_unchanged', title=title)
				continue
			if dry_run:
				diff = ''.join(difflib.unified_diff(page['text'].splitlines(keepends=True), new_text.splitlines(keepends=True), fromfile=title, tofile=title + ' (refreshed)'))
				outcome('metadata_preview', title=title, diff=diff)
				continue
			current = reader(title)
			if any(current.get(key) != page.get(key) for key in ('title', 'sha1', 'size_bytes', 'revid', 'text')):
				raise ValueError('Commons page or binary changed during metadata refresh')
			checkpoint = {**ownership, 'title': title, 'sha1': page['sha1'], 'size_bytes': page['size_bytes'],
				'status': 'pending', 'expected_text_sha256': text_hash(new_text), 'baserevid': page['revid'],
				'updated_at': datetime.now(timezone.utc).isoformat()}
			atomic_json(receipt_path, checkpoint)
			if not writer({'title': title, 'new_text': new_text, 'baserevid': page['revid'], 'sha1': page['sha1'],
				'summary': 'Refresh National Library of Belarus catalog metadata from preserved source records'}):
				raise RuntimeError('Commons rejected the metadata update')
			atomic_json(receipt_path, {**checkpoint, 'status': 'applied'})
			outcome('metadata_updated', title=title, preserved_fields=ownership['preserved_fields'])
		except ValueError as error:
			outcome('metadata_conflict', title=title, error=str(error))
		except Exception as error:
			# An edit may have succeeded before a connection/storage failure. The
			# pending receipt remains available for an idempotent later run.
			outcome('metadata_edit_failed', title=title, error=str(error))
			if (isinstance(error, requests.exceptions.SSLError)
				or getattr(error, 'code', None) in ('badtoken', 'assertuserfailed', 'assertbotfailed', 'notloggedin')
				or type(error).__name__ in ('NoUsernameError', 'UserRightsError')):
				raise
	atomic_json(output, report)
	return report
