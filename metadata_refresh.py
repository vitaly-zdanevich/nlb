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
from wikimedia_text import normalize_wikitext


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
	duplicates = item.get('duplicates', [])
	if not isinstance(duplicates, list):
		raise ValueError('Invalid archived Commons duplicate titles')
	return list(dict.fromkeys(title for title in [item.get('commons_title'), *duplicates]
		if isinstance(title, str) and title.startswith('File:')))


def _binary(item: dict) -> tuple[str, int]:
	'''Require a positive saved byte size and a valid SHA-1 for every metadata update.'''
	sha1, size = item.get('sha1', ''), item.get('size_bytes')
	if not isinstance(sha1, str) or not SHA1.fullmatch(sha1) or type(size) is not int or size <= 0:
		raise ValueError('Missing or invalid saved binary identity')
	return sha1.lower(), size


def _archive_snapshots(root: Path) -> tuple[list[dict], list[dict]]:
	'''Read refresh evidence independently, attaching damaged objects to their source records.

	Only descriptions needed for metadata ownership are hashed here. Original PDF
	and raw response integrity are separate from this bounded refresh preflight.
	'''
	snapshots, issues = [], []
	checked: dict[tuple[str, str], str | Exception] = {}
	for path in sorted((root / 'records').glob('*/snapshots/*/manifest.json')):
		record_id = path.parents[2].name
		try:
			manifest = json.loads(path.read_text(encoding='utf-8'))
			if not isinstance(manifest, dict) or manifest.get('record_id') != record_id or not isinstance(manifest.get('files'), dict):
				raise ValueError('Invalid archive manifest identity or files mapping')
		except (OSError, ValueError) as error:
			issues.append({'path': str(path.relative_to(root)), 'record_id': record_id, 'error': str(error)})
			continue
		texts = {}
		for fid, item in manifest['files'].items():
			identity = {'path': str(path.relative_to(root)), 'record_id': record_id, 'key': record_id + ':' + fid}
			try:
				if not isinstance(item, dict):
					raise ValueError('Invalid archived scan entry')
				identity.update({name: item[name] for name in ('sha1', 'size_bytes', 'commons_title', 'duplicates') if name in item})
				_targets(item)
				if not item.get('description'):
					continue
				reference = item['description']
				cache_key = (reference['path'], reference['sha256'])
				if cache_key not in checked:
					try:
						checked[cache_key] = read_description(root, reference)
					except (OSError, ValueError) as error:
						checked[cache_key] = error
				text = checked[cache_key]
				if isinstance(text, Exception):
					raise text
				texts[fid] = text
			except (KeyError, OSError, TypeError, ValueError) as error:
				issues.append({**identity, 'error': str(error)})
		# Source response lists and rights evidence can dwarf the ownership index.
		fields = ('status', 'sha1', 'size_bytes', 'commons_title', 'duplicates', 'description', 'metadata_refresh', 'updated_at', 'verification')
		files = {fid: {key: item[key] for key in fields if key in item} if isinstance(item, dict) else item
			for fid, item in manifest['files'].items()}
		snapshots.append({'manifest': {'record_id': record_id, 'captured_at': manifest.get('captured_at'), 'files': files}, 'texts': texts})
	return snapshots, issues


def inspect_refresh_archive(archive_dir: Path) -> list[dict]:
	'''Report damaged metadata ownership evidence before a long source collection.

	This is read-only and checks manifests and referenced descriptions, excluding
	PDFs and raw catalog responses. Apply repeats the check before editing and
	blocks affected pages while allowing independent pages to continue.
	'''
	return _archive_snapshots(Path(archive_dir))[1]


def _publication_history(snapshots: list[dict], binaries: set[tuple[str, int]]) -> dict:
	'''Index checked upload snapshots; copied statuses are not publication evidence.'''
	history = defaultdict(list)
	for snapshot in snapshots:
		manifest = snapshot['manifest']
		for fid, item in manifest.get('files', {}).items():
			if fid not in snapshot['texts'] or item.get('status') not in ('uploaded', 'verification_failed') or item.get('metadata_refresh'):
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
				'text': snapshot['texts'][fid], 'titles': _targets(item),
				'exact_only': item['status'] != 'uploaded' or not item.get('verification')})
	return history


def _archive_conflicts(issues: list[dict], snapshots: list[dict], page: dict, candidates: list[dict]) -> list[dict]:
	'''Conservatively associate broken evidence with its known records, files and aliases.'''
	if not issues:
		return []
	keys = {item['key'] for item in candidates}
	records = {key.split(':', 1)[0] for key in keys}
	binary, titles = _binary(page), {page['title'], *page.get('aliases', [])}
	for snapshot in snapshots:
		manifest = snapshot['manifest']
		for item in manifest['files'].values():
			try:
				if _binary(item) == binary or titles.intersection(_targets(item)):
					records.add(manifest['record_id'])
			except (AttributeError, TypeError, ValueError):
				continue
	result = []
	for issue in issues:
		try:
			matching_binary = _binary(issue) == binary
		except ValueError:
			matching_binary = False
		try:
			matching_title = bool(titles.intersection(_targets(issue)))
		except ValueError:
			matching_title = isinstance(issue.get('commons_title'), str) and issue['commons_title'] in titles
		if (matching_binary or issue.get('key') in keys or matching_title
			or 'key' not in issue and issue.get('record_id') in records):
			result.append(issue)
	return result


def _page_id(page: dict) -> int | None:
	'''Return a stable positive Commons page identifier when supplied by the reader.'''
	value = page.get('page_id')
	return value if type(value) is int and value > 0 else None


def _receipt_path(root: Path, page: dict) -> Path:
	'''Use stable page IDs for new receipts, retaining compatibility with older readers.'''
	identity = f'page-{_page_id(page)}' if _page_id(page) else text_hash(page['title'])
	return root / 'metadata-refresh' / 'pages' / (identity + '.json')


def _receipt_index(root: Path) -> dict[str, dict | Exception]:
	'''Read receipts once; corrupt unrelated receipts must not abort all page updates.'''
	result = {}
	for path in (root / 'metadata-refresh' / 'pages').glob('*.json'):
		try:
			receipt = json.loads(path.read_text(encoding='utf-8'))
			if not isinstance(receipt, dict):
				raise ValueError('Invalid metadata receipt document')
			result[path.name] = receipt
		except (OSError, ValueError) as error:
			result[path.name] = error
	return result


def _load_receipt(root: Path, page: dict, receipts: dict, reader: Callable) -> dict | None:
	'''Recover ownership across moves using stable identity or verified legacy redirects.

	A matching binary alone does not establish page ownership: two Commons pages
	can contain the same file. Legacy titles must resolve to this canonical page.
	'''
	aliases = {page['title'], *page.get('aliases', [])}
	stable_name = _receipt_path(root, page).name
	names = {stable_name, *(text_hash(title) + '.json' for title in aliases)}
	if stable_name in receipts:
		# This is the latest authoritative receipt after a migration from title keys.
		names = {stable_name}
	matches = []
	for name, receipt in receipts.items():
		direct = name in names
		if isinstance(receipt, Exception):
			if direct:
				raise ValueError(f'Damaged metadata update receipt {name}: {receipt}')
			continue
		try:
			matching_binary = _binary(receipt) == _binary(page)
		except ValueError:
			if direct:
				raise ValueError('Metadata update receipt has invalid binary identity')
			continue
		if not direct and (stable_name in receipts or not matching_binary):
			continue
		if not matching_binary:
			raise ValueError('Metadata update receipt binary differs from Commons')
		if 'page_id' in receipt and _page_id(receipt) is None:
			raise ValueError('Metadata update receipt has invalid Commons page identity')
		if name.startswith('page-') and name != f'page-{_page_id(receipt)}.json':
			raise ValueError('Metadata update receipt filename disagrees with its Commons page identity')
		if _page_id(receipt) is not None:
			matches_page = _page_id(receipt) == _page_id(page)
			if direct and not matches_page:
				raise ValueError('Metadata update receipt belongs to a different Commons page')
			if not matches_page:
				continue
		old_title = receipt.get('title')
		if not isinstance(old_title, str) or not old_title.startswith('File:'):
			if direct or _page_id(receipt) is not None:
				raise ValueError('Invalid metadata update receipt title')
			continue
		if _page_id(receipt) is None and old_title not in aliases:
			try:
				resolved = reader(old_title)
			except (requests.RequestException, KeyError, ValueError) as error:
				if isinstance(error, requests.exceptions.SSLError):
					raise
				# An unresolved prior receipt must not silently reset ownership.
				raise ValueError(f'Cannot verify prior metadata receipt title {old_title}: {error}') from error
			if resolved['title'] != page['title'] or _binary(resolved) != _binary(page):
				continue
			if _page_id(page) is not None and _page_id(resolved) != _page_id(page):
				continue
		if (receipt.get('status') not in ('pending', 'applied') or receipt.get('mode') not in ('information', 'blocks')
			or not isinstance(receipt.get('generated_text'), str) or not isinstance(receipt.get('baseline_text'), str)
			or receipt.get('generated_sha256') != text_hash(receipt['generated_text'])
			or not isinstance(receipt.get('expected_text_sha256'), str)
			or re.fullmatch(r'[0-9a-f]{64}', receipt['expected_text_sha256']) is None
			or 'primary_key' not in receipt
			or receipt.get('mode') == 'information' and not isinstance(receipt.get('primary_key'), str)
			or receipt.get('mode') == 'blocks' and receipt.get('primary_key') is not None
			or not isinstance(receipt.get('aliases', []), list)
			or any(not isinstance(title, str) or not title.startswith('File:') for title in receipt.get('aliases', []))):
			raise ValueError('Metadata update receipt integrity mismatch')
		try:
			updated = datetime.fromisoformat(receipt['updated_at'])
			if updated.tzinfo is None:
				raise ValueError('Receipt time has no timezone')
		except (KeyError, TypeError, ValueError) as error:
			raise ValueError('Metadata update receipt has invalid update time') from error
		matches.append((updated, receipt))
	if not matches:
		return None
	matches.sort(key=lambda item: item[0], reverse=True)
	if len(matches) > 1 and matches[0][0] == matches[1][0] and matches[0][1] != matches[1][1]:
		raise ValueError('Conflicting metadata update receipts')
	receipt = matches[0][1]
	result = {**receipt, 'title': page['title'], 'aliases': sorted(aliases | {receipt['title']} | set(receipt.get('aliases', [])))}
	if _page_id(page) is not None:
		result['page_id'] = _page_id(page)
	return result


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
		owners = [item for item in owners if not item['exact_only']
			or normalize_wikitext(str(information(item['text']))) == normalize_wikitext(str(information(page['text'])))]
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
			if len({normalize_wikitext(str(information(item['text']))) for item in candidates}) != 1:
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
				if (normalize_wikitext(present_blocks[key]) != normalize_wikitext(new_block)
					and normalize_wikitext(present_blocks[key]) not in {normalize_wikitext(block) for block in archived_blocks[key]}):
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
		current_fields, old_fields, new_fields = [{item.name.strip(): normalize_wikitext(str(item.value).strip()) for item in information(text).params}
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
	Progress distinguishes Commons reads, change preparation and attempted edits.
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
		print(f'[{details.get("title", details.get("key", "metadata"))}] {status}: {details.get("error", "")}', flush=True)
	requested = defaultdict(list)
	for snapshot in snapshots:
		path = Path(snapshot).resolve()
		if not path.is_relative_to(root.resolve() / 'records'):
			raise ValueError('Refresh snapshot is outside the archive records')
		try:
			manifest = json.loads(path.read_text(encoding='utf-8'))
			if (not isinstance(manifest, dict) or manifest.get('record_id') != path.parents[2].name
				or not isinstance(manifest.get('files'), dict)):
				raise ValueError('Invalid fresh metadata manifest identity or files mapping')
		except (OSError, ValueError) as error:
			outcome('metadata_source_skipped', key=path.parents[2].name, error=str(error))
			continue
		for fid, item in manifest.get('files', {}).items():
			key = manifest['record_id'] + ':' + fid
			if not isinstance(item, dict):
				outcome('metadata_source_skipped', key=key, error='Invalid fresh scan metadata entry')
				continue
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
	print(f'Reading current Commons metadata for {len(requested)} saved titles.', flush=True)
	for position, (title, candidates) in enumerate(requested.items(), 1):
		print(f'[Commons title {position}/{len(requested)}] Reading current metadata and binary identity: {title}', flush=True)
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
	print('Reading archived publication records to preserve human edits.', flush=True)
	archive_snapshots, archive_issues = _archive_snapshots(root)
	report['archive_issues'] = archive_issues
	history = _publication_history(archive_snapshots, {_binary(group['page']) for group in groups.values()})
	receipts = _receipt_index(root)
	preparation = 'metadata preview' if dry_run else 'metadata changes'
	for position, (title, group) in enumerate(groups.items(), 1):
		print(f'[Commons page {position}/{len(groups)}] Preparing {preparation}: {title}', flush=True)
		page = {**group['page'], 'aliases': group['aliases']}
		candidates = list(group['candidates'].values())
		receipt_path = _receipt_path(root, page)
		try:
			issues = _archive_conflicts(archive_issues, archive_snapshots, page, candidates)
			if issues:
				outcome('metadata_conflict', title=title, error='Damaged archived metadata evidence', archive_issues=issues)
				continue
			receipt = _load_receipt(root, page, receipts, reader)
			new_text, ownership = _proposal(root, page, candidates, history[_binary(page)], receipt)
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
			if any(current.get(key) != page.get(key) for key in ('title', 'page_id', 'sha1', 'size_bytes', 'revid', 'text')):
				raise ValueError('Commons page or binary changed during metadata refresh')
			checkpoint = {**ownership, 'title': title, 'sha1': page['sha1'], 'size_bytes': page['size_bytes'],
				'status': 'pending', 'expected_text_sha256': text_hash(new_text), 'baserevid': page['revid'],
				'aliases': sorted(set(receipt.get('aliases', []) if receipt else []) | group['aliases'] | {title}),
				'updated_at': datetime.now(timezone.utc).isoformat()}
			if _page_id(page) is not None:
				checkpoint['page_id'] = _page_id(page)
			atomic_json(receipt_path, checkpoint)
			print(f'Applying metadata update: {title}', flush=True)
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
