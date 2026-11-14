'''Recover missing local originals for saved Commons associations without editing Commons.

Published SHA-1 and size identify the requested bytes. A content-addressed index
also supports older upload histories without SHA-256. Every reused object is
hashed and parsed again; an existing corrupt object is never overwritten.
'''

from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import tempfile
from typing import Any

import requests

from catalog_requests import CatalogRetriesExhausted
from pdf_validation import InvalidPDFError, manifest_page_count, validate_pdf
from preservation import ArchiveWriteError, atomic_json
from publication_history import publication_evidence
from publication_rights import evaluate_publication
from scan_identity import assess_scan_identity
from state_storage import validate_state
from viewer_api import SourceAuthorizationError, ViewerAPIUnavailable, authorization_failure


class ArchiveIntegrityError(ValueError):
	'''An existing archive reference or object disagrees with its saved identity.'''


def _binary_key(item: dict) -> str | None:
	'''Require a saved positive byte size and SHA-1 before selecting any original.'''
	sha1, size = item.get('sha1'), item.get('size_bytes')
	if not isinstance(sha1, str) or re.fullmatch(r'[0-9a-fA-F]{40}', sha1) is None or type(size) is not int or size <= 0:
		return None
	return f'{sha1.lower()}:{size}'


def _object_path(root: Path, reference: dict) -> Path:
	'''Accept only the canonical PDF object path, without following an escaping symlink.'''
	sha256 = reference.get('sha256')
	if not isinstance(sha256, str) or re.fullmatch(r'[0-9a-f]{64}', sha256) is None:
		raise ArchiveIntegrityError('Invalid archived PDF SHA-256')
	expected = f'objects/{sha256}.pdf'
	if reference.get('pdf_path') != expected:
		raise ArchiveIntegrityError('Archived PDF path disagrees with its content address')
	path = root / expected
	if not path.resolve().is_relative_to(root.resolve() / 'objects'):
		raise ArchiveIntegrityError('Archived PDF path escapes the archive objects directory')
	return path


def _load_index(root: Path) -> dict:
	'''Reject malformed backfill history rather than replacing it with an empty index.'''
	path = root / 'pdf-backfill/index.json'
	if not path.exists():
		return {'version': 1, 'objects': {}}
	index = json.loads(path.read_text(encoding='utf-8'))
	if not isinstance(index, dict) or type(index.get('version')) is not int or index['version'] != 1 or not isinstance(index.get('objects'), dict):
		raise ArchiveIntegrityError('Invalid PDF backfill index; preserve and repair index.json before retrying')
	for key, reference in index['objects'].items():
		if not isinstance(reference, dict) or _binary_key(reference) != key:
			raise ArchiveIntegrityError('Invalid PDF backfill index binary identity')
		_object_path(root, reference)
	return index


def _hashes(path: Path) -> dict[str, Any]:
	'''Compute both binary identities and exact byte size in one bounded-memory pass.'''
	sha1, sha256, size = hashlib.sha1(), hashlib.sha256(), 0
	with path.open('rb') as source:
		for chunk in iter(lambda: source.read(1024 * 1024), b''):
			sha1.update(chunk)
			sha256.update(chunk)
			size += len(chunk)
	return {'sha1': sha1.hexdigest(), 'sha256': sha256.hexdigest(), 'size_bytes': size}


def _same_binary(actual: dict, publication: dict) -> bool:
	'''Match the saved publication, including its stronger digest when one was retained.'''
	return (_binary_key(actual) == _binary_key(publication)
		and (not publication.get('sha256') or actual['sha256'] == publication['sha256'].lower()))


def _existing_pdf(root: Path, publication: dict, index: dict) -> dict | None:
	'''Verify a known local original without reading the website or trusting index contents.'''
	reference = index['objects'].get(_binary_key(publication))
	if publication.get('sha256'):
		sha256 = publication['sha256'].lower()
		if reference and reference['sha256'] != sha256:
			raise ArchiveIntegrityError('PDF index and saved publication have different SHA-256 identities')
		reference = {'sha256': sha256, 'pdf_path': publication.get('pdf_path') or f'objects/{sha256}.pdf'}
	if reference is None:
		return None
	path = _object_path(root, reference)
	if not path.exists():
		return None
	actual = _hashes(path)
	if actual['sha256'] != reference['sha256'] or not _same_binary(actual, publication):
		raise ArchiveIntegrityError(f'Archived PDF failed its hash or size check: {path}')
	try:
		validation = validate_pdf(path)
	except InvalidPDFError as error:
		raise ArchiveIntegrityError(f'Archived PDF failed structural validation: {path}') from error
	return {**actual, 'pdf_path': reference['pdf_path'], 'pdf_validation': validation}


def _transfer(client: Any, record_id: str, file_id: str, publication: dict, manifest: dict, staging_dir: Path) -> tuple[str, dict]:
	'''Validate one complete transfer and preserve rejected bytes or local-write recovery evidence.'''
	archive = client.archive
	staging_dir.mkdir(parents=True, exist_ok=True)
	with tempfile.NamedTemporaryFile(dir=staging_dir, prefix='nlb-backfill-', suffix='.pdf', delete=False) as temporary:
		path = Path(temporary.name)
	completed, retained = False, False
	source_url = f'https://elib.nlb.by:8070/viewer?markID={record_id}&fileID={file_id}'
	try:
		client.download_pdf_scan(record_id, file_id, path)
		completed = True
		# A successful retry may have refreshed a previously incorrect manifest.
		manifest = getattr(client, '_scan_manifests', {}).get((record_id, file_id), manifest)
		actual = _hashes(path)
		try:
			validation = validate_pdf(path, manifest)
		except InvalidPDFError as error:
			rejected = archive.preserve_rejected_pdf(record_id, file_id, path, source_url=source_url,
				manifest=manifest, manifest_pages=manifest_page_count(manifest), error=error)
			return 'download_failed', {'error': str(error), 'rejected_pdf': rejected}
		if not _same_binary(actual, publication):
			error = ValueError('Current source PDF differs from the saved published hash or size')
			rejected = archive.preserve_rejected_pdf(record_id, file_id, path, source_url=source_url,
				manifest=manifest, manifest_pages=manifest_page_count(manifest), error=error)
			return 'source_changed', {'error': str(error), 'rejected_pdf': rejected}
		preserved = archive.preserve_pdf(record_id, file_id, path)
		return 'archived', {**actual, **preserved, 'pdf_validation': validation}
	except OSError as error:
		if isinstance(error, ArchiveWriteError) or (completed and not isinstance(error, requests.RequestException)):
			retained = path.exists()
			if retained:
				print(f'[{record_id}:{file_id}] Preservation write failed; staging original retained at {path}', flush=True)
				try:
					atomic_json(path.with_suffix('.recovery.json'), {
						'record_id': record_id, 'file_id': file_id, 'publication': publication,
						'source_url': source_url, 'pdf_path': str(path.resolve()), 'error': str(error),
					})
				except OSError:
					pass  # Keep the original even when its recovery sidecar cannot be written.
		raise
	finally:
		if not retained:
			path.unlink(missing_ok=True)


def backfill_pdfs(
	client: Any, state_path: Path, staging_dir: Path, *, license_template: str,
	explicit_license: bool = False, dry_run: bool = False,
) -> dict[str, Any]:
	'''Backfill saved binary identities without uploads, Commons reads, or state changes.

	The CLI must hold its state/archive writer locks for this complete operation.
	Dry runs inventory local objects only. Reruns verify previously saved originals
	and resume missing ones; current access, metadata scope and rights gate transfers.
	Reports are checkpointed per item, and access/session/outage errors stop the run.
	'''
	from upload import CatalogRecordMissing, DownloadRetriesExhausted, ViewerSessionExpired

	state = json.loads(state_path.read_text(encoding='utf-8'))
	validate_state(state)
	archive = client.archive
	if not archive.keep_pdfs:
		raise ValueError('PDF backfill requires an archive configured to retain PDFs')
	index = _load_index(archive.root)
	report = {'counts': dict.fromkeys(('archived', 'already_archived', 'missing_pdf', 'skipped', 'source_missing',
		'source_changed', 'restricted', 'rights_review', 'metadata_review', 'metadata_failed', 'download_failed', 'archive_failed'), 0),
		'items': [], 'complete': False, 'dry_run': dry_run}
	report_path = archive.root / 'pdf-backfill/report.json'
	groups: dict[str, list[tuple[str, dict, dict]]] = defaultdict(list)
	global_errors = (ViewerAPIUnavailable, SourceAuthorizationError, ViewerSessionExpired, requests.exceptions.SSLError)
	source_errors = (CatalogRetriesExhausted, DownloadRetriesExhausted, requests.RequestException, *global_errors)

	def outcome(key: str, publication: dict, status: str, **details: Any) -> None:
		'''Checkpoint the local result separately from the historical Commons identity.'''
		entry = {'key': key, 'status': status, **details}
		report['counts'][status] += 1
		report['items'].append(entry)
		record_id, file_id = key.split(':', 1)
		if record_id in archive.snapshots:
			result = {**publication, 'status': 'pdf_backfill', 'publication': publication, 'pdf_backfill': entry}
			if status in ('archived', 'already_archived'):
				result.update({name: details[name] for name in ('sha1', 'sha256', 'size_bytes', 'pdf_path', 'pdf_validation')})
				result['publication'] = {**publication, **{name: result[name] for name in ('sha1', 'sha256', 'size_bytes', 'pdf_path', 'pdf_validation')}}
			archive.record_result(record_id, file_id, result)
		atomic_json(report_path, report)
		print(f'[{key}] PDF backfill {status}: {details.get("error", "")}', flush=True)

	def reuse_local(key: str, publication: dict) -> bool:
		'''Reuse only checked content-addressed originals, preserving detected corruption.'''
		try:
			reference = _existing_pdf(archive.root, publication, index)
		except (ArchiveIntegrityError, OSError) as error:
			outcome(key, publication, 'archive_failed', error=str(error))
			return True
		if reference is None:
			return False
		if not dry_run and index['objects'].get(_binary_key(publication)) != reference:
			index['objects'][_binary_key(publication)] = reference
			atomic_json(archive.root / 'pdf-backfill/index.json', index)
		outcome(key, publication, 'already_archived', **reference)
		return True

	def failure(keys: list[tuple[str, dict]], status: str, error: Exception) -> None:
		'''Record failed work before stopping globally for explicit access or session failures.'''
		for key, publication in keys:
			outcome(key, publication, status, error=str(error))
		if isinstance(error, requests.HTTPError) and authorization_failure(error):
			raise SourceAuthorizationError(error.response.status_code) from error
		if isinstance(error, global_errors):
			raise error

	atomic_json(report_path, report)
	for key, previous in state['items'].items():
		publication = publication_evidence(previous)
		titles = [publication.get('commons_title'), *publication.get('duplicates', [])]
		if _binary_key(publication) is None or not any(isinstance(title, str) and title.startswith('File:') for title in titles):
			outcome(key, publication, 'skipped', error='No complete saved publication identity')
		elif not reuse_local(key, publication):
			if dry_run:
				outcome(key, publication, 'missing_pdf')
			else:
				groups[key.split(':', 1)[0]].append((key, previous, publication))
	for record_id, items in groups.items():
		# Earlier records may have supplied the same binary during this run.
		items = [item for item in items if not reuse_local(item[0], item[2])]
		if not items:
			continue
		archive.begin_record(record_id)
		try:
			record_meta, marc_meta = client.fetch_record_metadata(record_id)
			archive.save_record(record_id, record_meta, marc_meta)
			if not client.check_doc_access(record_id).get('publicAccess'):
				for key, _, publication in items:
					outcome(key, publication, 'restricted', error='Source record is not publicly accessible')
				continue
			files = {entry['file_id']: entry for entry in client.resolve_record_files({}, record_meta)}
			decision = evaluate_publication(record_meta, marc_meta, license_template, explicit_license=explicit_license)
		except source_errors as error:
			failure([(key, publication) for key, _, publication in items],
				'source_missing' if isinstance(error, CatalogRecordMissing) else 'metadata_failed', error)
			continue
		for key, previous, publication in items:
			if reuse_local(key, publication):
				continue
			file_id = key.split(':', 1)[1]
			entry = files.get(file_id)
			if entry is None:
				outcome(key, publication, 'source_missing', error='Previously uploaded file ID is absent from the current record')
				continue
			identity = assess_scan_identity(entry, record_meta, marc_meta)
			if not identity['eligible']:
				outcome(key, publication, 'metadata_review', error=identity['reason'], scan_identity=identity)
				continue
			if not decision['eligible']:
				outcome(key, publication, 'rights_review', error=decision['reason'], rights=decision)
				continue
			old_location = previous.get('scan_identity', {}).get('context', {}).get('file', {}).get('file_location')
			if old_location and entry.get('file_location') and old_location != entry['file_location']:
				outcome(key, publication, 'source_changed', error='Source file path changed since publication')
				continue
			stage = 'metadata_failed'
			try:
				manifest = client.fetch_scan_manifest(record_id, file_id)
				stage = 'download_failed'
				status, details = _transfer(client, record_id, file_id, publication, manifest, staging_dir)
			except source_errors as error:
				failure([(key, publication)], stage, error)
				continue
			if status == 'archived':
				index['objects'][_binary_key(publication)] = details
				atomic_json(archive.root / 'pdf-backfill/index.json', index)
			outcome(key, publication, status, **details, rights=decision, scan_identity=identity)
	report.update(complete=True, finished_at=datetime.now(timezone.utc).isoformat())
	atomic_json(report_path, report)
	return report
