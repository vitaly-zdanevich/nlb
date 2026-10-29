'''Keep immutable source bytes and versioned metadata independently of Commons uploads.

Response bodies are content-addressed with SHA-256; manifests associate them with
records, derived descriptions and verified Commons files. Cookies, authorization
headers and viewer session creation responses are never written to this archive.
'''

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any
import uuid


def atomic_json(path: Path, value: Any) -> None:
	'''Replace one JSON document only after its complete contents reach disk.'''
	path.parent.mkdir(parents=True, exist_ok=True)
	temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.new')
	try:
		with temporary.open('w', encoding='utf-8') as stream:
			json.dump(value, stream, ensure_ascii=False, indent=2)
			stream.write('\n')
			stream.flush()
			os.fsync(stream.fileno())
		temporary.replace(path)
	finally:
		temporary.unlink(missing_ok=True)


def file_integrity(path: Path) -> dict[str, Any]:
	'''Hash a file in bounded chunks and count the exact bytes that were read.'''
	digest = hashlib.sha256()
	size = 0
	with path.open('rb') as stream:
		for chunk in iter(lambda: stream.read(1024 * 1024), b''):
			digest.update(chunk)
			size += len(chunk)
	return {'sha256': digest.hexdigest(), 'size_bytes': size}


class ArchiveStore:
	'''Archive successful and failed public reads, with separate snapshots per run.'''

	def __init__(self, root: Path, *, keep_pdfs: bool = False) -> None:
		'''Initialize an archive without accessing credentials or creating network traffic.'''
		self.root = Path(root)
		self.keep_pdfs = keep_pdfs
		self.current_record: str | None = None
		self.snapshots: dict[str, Path] = {}
		self.manifests: dict[str, dict[str, Any]] = {}
		sources = sorted(path for path in Path(__file__).parent.glob('*.py') if not path.name.startswith('test_'))
		digest = hashlib.sha256()
		for path in sources:
			digest.update(path.name.encode() + b'\0' + path.read_bytes())
		self.tool = {'name': 'nlb-commons-uploader', 'sha256': digest.hexdigest(), 'files': [path.name for path in sources]}

	def begin_record(self, record_id: str) -> None:
		'''Start a new capture; permit only a safe catalog identifier as a path component.'''
		if not re.fullmatch(r'[A-Za-z0-9_-]+', record_id):
			raise ValueError('Invalid archive record identifier')
		self.current_record = record_id
		stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ') + '-' + uuid.uuid4().hex[:8]
		self.snapshots[record_id] = self.root / 'records' / record_id / 'snapshots' / stamp / 'manifest.json'
		self.manifests[record_id] = {
			'schema_version': 1, 'record_id': record_id,
			'captured_at': datetime.now(timezone.utc).isoformat(),
			'tool': self.tool, 'sources': [], 'files': {},
		}

	def _object(self, body: bytes, suffix: str = '.bin') -> dict[str, Any]:
		'''Write immutable source bytes once and return their portable integrity reference.'''
		sha256 = hashlib.sha256(body).hexdigest()
		path = self.root / 'objects' / (sha256 + suffix)
		path.parent.mkdir(parents=True, exist_ok=True)
		if not path.exists():
			temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.new')
			try:
				with temporary.open('wb') as stream:
					stream.write(body)
					stream.flush()
					os.fsync(stream.fileno())
				temporary.replace(path)
			finally:
				temporary.unlink(missing_ok=True)
		elif hashlib.sha256(path.read_bytes()).hexdigest() != sha256:
			raise OSError(f'Archive integrity check failed: {path}')
		return {'path': str(path.relative_to(self.root)), 'sha256': sha256, 'size_bytes': len(body)}

	def capture_response(self, response: Any) -> None:
		'''Capture exact decoded HTTP body bytes and a small, non-secret provenance set.

		Requests removes HTTP transfer/content encoding; these are the original
		HTML/XML/JSON payload bytes, not a packet-level HTTP archive.
		'''
		# Cached parent responses retain the original read time for later children.
		captured_at = response.__dict__.setdefault('_nlb_capture_time', datetime.now(timezone.utc).isoformat())
		ref = self._object(response.content)
		ref.update({
			'url': response.url, 'http_status': response.status_code,
			'captured_at': captured_at,
			'headers': {key: response.headers[key] for key in ('Content-Type', 'ETag', 'Last-Modified') if key in response.headers},
		})
		if self.current_record is not None:
			manifest = self.manifests[self.current_record]
			manifest['sources'].append(ref)
			atomic_json(self.snapshots[self.current_record], manifest)
		else:
			# Search responses also need provenance even before a record is selected.
			atomic_json(self.root / 'searches' / (uuid.uuid4().hex + '.json'), ref)

	def save_record(self, record_id: str, record_meta: dict, marc_meta: dict) -> Path:
		'''Save derived metadata without replacing any earlier source snapshot.'''
		if record_id not in self.snapshots:
			self.begin_record(record_id)
		body = json.dumps({'html': record_meta, 'marc': marc_meta}, ensure_ascii=False, indent=2).encode('utf-8')
		self.manifests[record_id]['parsed'] = self._object(body, '.json')
		path = self.snapshots[record_id]
		atomic_json(path, self.manifests[record_id])
		return path

	def save_scan(self, record_id: str, file_id: str, description: str, commons_title: str, **details: Any) -> None:
		'''Preserve readable metadata before a completed or duplicate PDF can be skipped.'''
		if record_id not in self.snapshots:
			self.save_record(record_id, {}, {})
		entry = self.manifests[record_id]['files'].setdefault(file_id, {})
		entry.update(details)
		entry.update({'commons_title': commons_title, 'description': self._object(description.encode('utf-8'), '.wiki')})
		atomic_json(self.snapshots[record_id], self.manifests[record_id])

	def record_result(self, record_id: str, file_id: str, result: dict[str, Any]) -> None:
		'''Associate binary identity, outcome and verification with the archived metadata.'''
		self.manifests[record_id]['files'].setdefault(file_id, {}).update(result)
		atomic_json(self.snapshots[record_id], self.manifests[record_id])

	def preserve_pdf(self, record_id: str, file_id: str, path: Path) -> dict[str, Any]:
		'''Hash original bytes and optionally preserve a verified, atomically published copy.'''
		result = file_integrity(path)
		if self.keep_pdfs:
			# Copy in bounded chunks: scans can be larger than available memory.
			target = self.root / 'objects' / (result['sha256'] + '.pdf')
			target.parent.mkdir(parents=True, exist_ok=True)
			if target.exists():
				if file_integrity(target) != result:
					raise OSError(f'Archive integrity check failed: {target}')
			else:
				temporary = target.with_name(target.name + '.' + uuid.uuid4().hex + '.new')
				try:
					with path.open('rb') as source, temporary.open('wb') as output:
						for chunk in iter(lambda: source.read(1024 * 1024), b''):
							output.write(chunk)
						output.flush()
						os.fsync(output.fileno())
					if file_integrity(temporary) != result:
						raise OSError(f'PDF copy failed integrity check: {path}')
					temporary.replace(target)
				finally:
					temporary.unlink(missing_ok=True)
			result['pdf_path'] = str(target.relative_to(self.root))
		self.record_result(record_id, file_id, result)
		return result
