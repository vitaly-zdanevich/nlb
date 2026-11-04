'''Resume interrupted metadata collection from verified, recent source snapshots.

The checkpoint is separate from upload state. It binds snapshots to the selected
associations, renderer configuration and code fingerprint; expired or changed
inputs start a fresh collection. Commons identity/revision checks still happen
when applying metadata. Only a fully completed application closes the run.
'''

from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import Callable
import uuid

from preservation import ArchiveStore, atomic_json, file_integrity


MAX_COLLECTION_AGE = timedelta(hours=24)
STATUSES = frozenset({'metadata_ready', 'metadata_review', 'metadata_failed', 'source_missing', 'source_changed', 'restricted', 'skipped'})


class RefreshCheckpoint:
	'''Persist complete record boundaries without exposing partial captures as ready.'''

	def __init__(self, archive: ArchiveStore, selection: dict, settings: dict, *, resume: bool = False, log: Callable = print) -> None:
		'''Reuse only an unfinished run with identical inputs and a recent start time.'''
		self.archive = archive
		self.selection = selection
		self.log = log
		self.path = archive.root / 'metadata-refresh' / 'collection.json'
		self.verified: set[tuple[str, str, int]] = set()
		fingerprint = hashlib.sha256(json.dumps({
			'selection': selection, 'settings': settings, 'tool': archive.tool,
		}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
		now = datetime.now(timezone.utc)
		previous = None
		if self.path.exists():
			try:
				previous = json.loads(self.path.read_text())
				started = datetime.fromisoformat(previous['started_at'])
				valid = (previous['schema_version'] == 1 and previous['fingerprint'] == fingerprint
					and not previous['complete'] and isinstance(previous['records'], dict)
					and timedelta(0) <= now - started < MAX_COLLECTION_AGE)
			except (ValueError, KeyError, TypeError):
				valid = False
				self.log('Unreadable metadata collection checkpoint; preserving it and collecting fresh sources.')
			if not valid or not resume:
				# Preserve the old run index, including malformed bytes, for diagnosis.
				self.path.rename(self.path.with_name('collection-' + uuid.uuid4().hex + '.json'))
				previous = None
		self.data = previous or {'schema_version': 1, 'fingerprint': fingerprint,
			'started_at': now.isoformat(), 'complete': False, 'records': {}}
		atomic_json(self.path, self.data)

	def restore(self, record_id: str) -> dict[str, int] | None:
		'''Verify recorded metadata objects before reusing a completed source collection.'''
		entry = self.data['records'].get(record_id)
		if entry is None:
			return None
		try:
			counts = entry['counts']
			if (not isinstance(counts, dict) or set(counts) != STATUSES
				or any(type(value) is not int or value < 0 for value in counts.values())):
				raise ValueError('Invalid checkpoint counters')
			if counts.get('metadata_failed'):
				return None
			path = (self.archive.root / entry['path']).resolve()
			expected_dir = (self.archive.root / 'records' / record_id / 'snapshots').resolve()
			if not path.is_relative_to(expected_dir) or file_integrity(path)['sha256'] != entry['sha256']:
				raise ValueError('Snapshot path or checksum mismatch')
			manifest = json.loads(path.read_text())
			if (not isinstance(manifest, dict) or manifest.get('record_id') != record_id
				or not isinstance(manifest.get('files'), dict)
				or any(not isinstance(item, dict) for item in manifest['files'].values())):
				raise ValueError('Snapshot record identity mismatch')
			if set(manifest['files']) != set(self.selection[record_id]):
				raise ValueError('Snapshot does not cover the selected scans')
			actual = dict.fromkeys(STATUSES, 0)
			for item in manifest['files'].values():
				refresh = item.get('metadata_refresh')
				if not isinstance(refresh, dict) or refresh.get('status') not in STATUSES:
					raise ValueError('Snapshot has an incomplete scan outcome')
				actual[refresh['status']] += 1
			if actual != counts:
				raise ValueError('Checkpoint counters disagree with snapshot outcomes')
			refs = [*manifest.get('sources', []), *([manifest['parsed']] if manifest.get('parsed') else [])]
			refs.extend(item['description'] for item in manifest.get('files', {}).values() if item.get('description'))
			for reference in refs:
				identity = (reference['path'], reference['sha256'], reference['size_bytes'])
				if identity in self.verified:
					continue
				obj = (self.archive.root / reference['path']).resolve()
				if not obj.is_relative_to(self.archive.root.resolve()) or file_integrity(obj) != {
					'sha256': reference['sha256'], 'size_bytes': reference['size_bytes'],
				}:
					raise ValueError('Source object path, checksum or length mismatch')
				self.verified.add(identity)
			self.archive.snapshots[record_id] = path
			self.archive.manifests[record_id] = manifest
			return entry['counts']
		except (OSError, ValueError, KeyError, TypeError) as error:
			self.log(f'[{record_id}] Cannot reuse collected metadata ({error}); collecting fresh sources.')
			return None

	def save_record(self, record_id: str, counts: dict[str, int]) -> None:
		'''Checkpoint only after all selected scans in a record have a durable outcome.'''
		path = self.archive.snapshots[record_id]
		self.data['records'][record_id] = {
			'path': str(path.resolve().relative_to(self.archive.root.resolve())),
			'sha256': file_integrity(path)['sha256'], 'counts': counts,
		}
		atomic_json(self.path, self.data)

	def complete(self) -> None:
		'''Close an applied run; a later invocation will fetch new source metadata.'''
		self.data['complete'] = True
		atomic_json(self.path, self.data)
