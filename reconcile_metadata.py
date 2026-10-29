#!/usr/bin/env python3
'''Plan and explicitly apply metadata additions to hash-verified Commons files.

Plans preserve the existing page outside dedicated per-record metadata blocks.
Generation only performs anonymous reads. Applying a reviewed plan checks the
page revision, original text and current binary hash again before editing.
'''

import argparse
from datetime import datetime, timezone
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Callable

import mwparserfromhell
import requests

from preservation import atomic_json


API_URL = 'https://commons.wikimedia.org/w/api.php'
USER_AGENT = 'nlb-commons-uploader metadata reconciliation (https://github.com/vitaly-zdanevich/nlb)'
IDENTIFIER_RE = re.compile(r'[A-Za-z0-9_-]+')
SHA1_RE = re.compile(r'[0-9a-fA-F]{40}')
COMPLETED_STATUSES = frozenset({'uploaded', 'existing_title', 'duplicate_sha1'})


def _text_hash(text: str) -> str:
	'''Hash exact UTF-8 wikitext bytes for plan and revision integrity checks.'''
	return hashlib.sha256(text.encode('utf-8')).hexdigest()


def read_commons_page(title: str, *, session: Any = None) -> dict[str, Any]:
	'''Read the current file description, revision and binary SHA-1 anonymously.'''
	if not title.startswith('File:'):
		raise ValueError('Reconciliation targets must be File: pages')
	response = (session or requests).get(API_URL, params={
		'action': 'query', 'format': 'json', 'formatversion': '2',
		'titles': title, 'prop': 'revisions|imageinfo',
		'rvprop': 'ids|content', 'rvslots': 'main', 'iiprop': 'sha1',
	}, headers={'User-Agent': USER_AGENT}, timeout=60)
	response.raise_for_status()
	payload = response.json()
	pages = payload.get('query', {}).get('pages', [])
	if payload.get('error') or len(pages) != 1:
		raise ValueError(f'Invalid Commons query response for {title}')
	page = pages[0]
	try:
		revision = page['revisions'][0]
		text = revision['slots']['main']['content']
		sha1 = page['imageinfo'][0]['sha1']
		valid = page['ns'] == 6 and not page.get('missing') and isinstance(text, str) and SHA1_RE.fullmatch(sha1)
		if not valid or not isinstance(revision['revid'], int):
			raise ValueError('Invalid file revision')
		return {'title': page['title'], 'revid': revision['revid'], 'text': text, 'sha1': sha1.lower()}
	except (KeyError, IndexError, TypeError, ValueError) as error:
		raise ValueError(f'Missing or invalid Commons file identity for {title}') from error


def _read_description(root: Path, reference: dict[str, Any]) -> str:
	'''Verify archived metadata and extract bibliographic fields, excluding licensing.'''
	path = (root / reference['path']).resolve()
	if not path.is_relative_to(root.resolve()):
		raise ValueError('Archive object path escapes archive directory')
	body = path.read_bytes()
	if hashlib.sha256(body).hexdigest() != reference.get('sha256'):
		raise ValueError(f'Archive description integrity check failed: {path}')
	parsed = mwparserfromhell.parse(body.decode('utf-8'))
	templates = [template for template in parsed.filter_templates(recursive=False) if str(template.name).strip().casefold() == 'information']
	if len(templates) != 1 or not templates[0].has('description'):
		raise ValueError(f'Archived description has no unique Information template: {path}')
	information = templates[0]
	parts = [str(information.get('description').value).strip()]
	# These are already escaped/generated wikitext from the uploader. Retain them
	# as catalog evidence without replacing existing Commons Information fields.
	for parameter, label in (('date', 'Publication date'), ('author', 'Catalog author'), ('source', 'Catalog source'), ('other_versions', 'Other versions')):
		if information.has(parameter):
			value = str(information.get(parameter).value).strip()
			if value:
				parts.append(f'* {label}: {value}')
	return '\n'.join(parts)


def _managed_block(text: str, record_id: str, file_id: str, description: str) -> str:
	'''Replace one owned block, or insert it before licensing without rewriting peers.'''
	key = f'{record_id}:{file_id}'
	start = f'<!-- NLB-METADATA:{key}:BEGIN -->'
	end = f'<!-- NLB-METADATA:{key}:END -->'
	block = f'{start}\n== NLB catalog metadata: {record_id} / {file_id} ==\n{description}\n{end}'
	if start in text or end in text:
		if text.count(start) != 1 or text.count(end) != 1 or text.index(start) > text.index(end):
			raise ValueError(f'Ambiguous managed metadata block: {key}')
		left = text.index(start)
		right = text.index(end) + len(end)
		return text[:left] + block + text[right:]
	license_heading = re.search(r'^==\s*(?:\{\{\s*int:license-header\s*\}\}|Licensing|License)\s*==\s*$', text, flags=re.IGNORECASE | re.MULTILINE)
	position = license_heading.start() if license_heading else len(text)
	separator = '' if not text[:position] or text[:position].endswith('\n') else '\n'
	return text[:position] + separator + block + '\n\n' + text[position:]


def generate_plan(archive_dir: Path, fetch_page: Callable[[str], dict[str, Any]] | None = None) -> dict[str, Any]:
	'''Build one reviewable edit per actual Commons title from latest record snapshots.'''
	root = Path(archive_dir)
	if not root.is_dir():
		raise ValueError(f'Archive directory does not exist: {root}')
	reader = fetch_page or read_commons_page
	plan: dict[str, Any] = {'schema_version': 1, 'created_at': datetime.now(timezone.utc).isoformat(), 'entries': [], 'skipped': []}
	pages: dict[str, dict[str, Any]] = {}
	pending: dict[str, dict[str, Any]] = {}
	for record_dir in sorted((root / 'records').glob('*')):
		manifests = sorted(record_dir.glob('snapshots/*/manifest.json'))
		if not manifests:
			continue
		manifest = json.loads(manifests[-1].read_text(encoding='utf-8'))
		record_id = manifest.get('record_id', '')
		if not IDENTIFIER_RE.fullmatch(record_id) or record_id != record_dir.name:
			raise ValueError(f'Invalid record identity in {manifests[-1]}')
		if not manifest.get('files'):
			plan['skipped'].append({
				'record_id': record_id,
				'reason': 'Latest snapshot has no scan metadata; refresh this record before planning',
				'manifest': str(manifests[-1].relative_to(root)),
			})
			continue
		for file_id, item in sorted(manifest.get('files', {}).items()):
			if not IDENTIFIER_RE.fullmatch(file_id):
				raise ValueError('Invalid archived file identifier')
			identity = {'record_id': record_id, 'file_id': file_id}
			sha1 = item.get('sha1') or ''
			if item.get('status') not in COMPLETED_STATUSES or not SHA1_RE.fullmatch(sha1):
				plan['skipped'].append({**identity, 'reason': 'No completed, hash-identified Commons association'})
				continue
			titles = item.get('duplicates', []) if item.get('status') == 'duplicate_sha1' else [item.get('commons_title')]
			if not titles:
				plan['skipped'].append({**identity, 'reason': 'No actual Commons target title'})
				continue
			if not item.get('description'):
				plan['skipped'].append({**identity, 'reason': 'No archived description'})
				continue
			description = _read_description(root, item['description'])
			for requested_title in dict.fromkeys(titles):
				if not isinstance(requested_title, str) or not requested_title.startswith('File:'):
					raise ValueError('Invalid Commons target title in archive')
				if requested_title not in pages:
					pages[requested_title] = reader(requested_title)
				current = pages[requested_title]
				if current.get('sha1', '').lower() != sha1.lower():
					plan['skipped'].append({**identity, 'title': requested_title, 'reason': 'Current Commons file SHA-1 differs from archived scan'})
					continue
				title = current['title']
				entry = pending.setdefault(title, {
					'title': title, 'baserevid': current['revid'], 'old_sha256': _text_hash(current['text']),
					'new_text': current['text'], 'sha1': sha1.lower(), 'records': [], 'old_text': current['text'],
				})
				entry['new_text'] = _managed_block(entry['new_text'], record_id, file_id, description)
				entry['records'].append({**identity, 'manifest': str(manifests[-1].relative_to(root)), 'description_sha256': item['description']['sha256']})
	for entry in pending.values():
		old_text = entry.pop('old_text')
		if entry['new_text'] == old_text:
			continue
		entry['new_sha256'] = _text_hash(entry['new_text'])
		entry['diff'] = ''.join(difflib.unified_diff(old_text.splitlines(keepends=True), entry['new_text'].splitlines(keepends=True), fromfile=entry['title'] + ' (current)', tofile=entry['title'] + ' (proposed)'))
		plan['entries'].append(entry)
	return plan


def edit_commons_page(entry: dict[str, Any]) -> bool:
	'''Apply an explicitly selected plan through Pywikibot with revision protection.'''
	import pywikibot
	site = pywikibot.Site('commons', 'commons')
	page = pywikibot.FilePage(site, entry['title'])
	return site.editpage(page, text=entry['new_text'], baserevid=entry['baserevid'], nocreate=True, recreate=False, summary='Preserve additional National Library of Belarus catalog metadata', minor=False)


def apply_plan(
	plan: dict[str, Any],
	fetch_page: Callable[[str], dict[str, Any]] | None = None,
	edit_page: Callable[[dict[str, Any]], bool] | None = None,
) -> int:
	'''Reject stale revisions, changed binaries and modified plan text before edits.'''
	if plan.get('schema_version') != 1:
		raise ValueError('Unsupported reconciliation plan schema')
	reader = fetch_page or read_commons_page
	writer = edit_page or edit_commons_page
	edited = 0
	for entry in plan.get('entries', []):
		if _text_hash(entry['new_text']) != entry['new_sha256']:
			raise ValueError('Reconciliation plan integrity check failed')
		current = reader(entry['title'])
		if not SHA1_RE.fullmatch(entry['sha1']) or current['sha1'].lower() != entry['sha1'].lower():
			raise ValueError(f"Commons binary changed since planning: {entry['title']}")
		current_hash = _text_hash(current['text'])
		if current['title'] == entry['title'] and current_hash == entry['new_sha256']:
			# Resume a partially applied plan without rewriting completed entries.
			continue
		if current['title'] != entry['title'] or current['revid'] != entry['baserevid'] or current_hash != entry['old_sha256']:
			raise ValueError(f"Commons description changed since planning: {entry['title']}")
		if not writer(entry):
			raise RuntimeError(f"Commons rejected metadata edit: {entry['title']}")
		edited += 1
	return edited


def main(argv: list[str] | None = None) -> int:
	'''Write a review plan, or apply a separately and explicitly selected plan file.'''
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument('--archive-dir', type=Path, default=Path('archive'), help='Uploader preservation archive directory.')
	action = parser.add_mutually_exclusive_group(required=True)
	action.add_argument('--plan', type=Path, help='Write a JSON review plan without editing Commons.')
	action.add_argument('--apply', type=Path, help='Apply this reviewed JSON plan; writes to Commons.')
	parser.add_argument('--pywikibot-dir', type=Path, help='Pywikibot configuration directory, used only with --apply.')
	args = parser.parse_args(argv)
	if args.plan:
		plan = generate_plan(args.archive_dir)
		atomic_json(args.plan, plan)
		print(json.dumps({'planned': len(plan['entries']), 'skipped': len(plan['skipped']), 'plan': str(args.plan)}))
	else:
		if args.pywikibot_dir:
			os.environ['PYWIKIBOT_DIR'] = str(args.pywikibot_dir)
		plan = json.loads(args.apply.read_text(encoding='utf-8'))
		print(json.dumps({'edited': apply_plan(plan)}))
	return 0


if __name__ == '__main__':
	raise SystemExit(main())
