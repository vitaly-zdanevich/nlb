'''Validate resumable upload history and serialize writers sharing state or archives.'''

from contextlib import contextmanager
import errno
import fcntl
import os
from pathlib import Path
import re
from typing import Any, Iterator


class InvalidStateError(ValueError):
	'''Report unsupported or malformed saved history without resetting its contents.'''


class RunLockedError(RuntimeError):
	'''Report another run that currently owns a required state or archive lock.'''

	def __init__(self, resource: str, lock_path: Path):
		'''Keep the complete lock location available while bounding the user message.'''
		self.lock_path = lock_path
		self.resource = resource
		location = str(lock_path)
		if len(location) > 180:
			location = '…' + location[-179:]
		super().__init__(f'Another NLB run is using this {resource} (lock: {location}). '
			'Wait for that run to finish before retrying; do not delete the lock file.')


def _invalid(location: str, requirement: str) -> None:
	'''Describe a structural error without including arbitrary saved values.'''
	raise InvalidStateError(f'Invalid saved state: {location} {requirement}. '
		'Preserve the original file and correct or restore it before retrying.')


def _identifier(value: Any) -> bool:
	'''Accept the catalog identifiers supported by the archive directory layout.'''
	return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]+', value) is not None


def _item_key(value: Any) -> bool:
	'''Require the unambiguous record:file identity used by every saved scan.'''
	return isinstance(value, str) and len(parts := value.split(':')) == 2 and all(_identifier(part) for part in parts)


def _optional_strings(data: dict[str, Any], fields: tuple[str, ...], location: str) -> None:
	'''Check optional text while allowing legacy null values and empty sentinels.'''
	for field in fields:
		if field in data and data[field] is not None and not isinstance(data[field], str):
			_invalid(f'{location}.{field}', 'must be text or null')


def _integer(data: dict[str, Any], field: str, location: str, minimum: int, *, nullable: bool = False) -> None:
	'''Reject booleans, fractions and negative values in arithmetic state fields.'''
	if field not in data or (nullable and data[field] is None):
		return
	if type(data[field]) is not int or data[field] < minimum:
		_invalid(f'{location}.{field}', f'must be an integer of at least {minimum}' + (' or null' if nullable else ''))


def _binary_identity(data: dict[str, Any], location: str) -> None:
	'''Validate available binary evidence without requiring it in older histories.'''
	for field, length in (('sha1', 40), ('sha256', 64)):
		if field in data and (not isinstance(data[field], str)
			or (data[field] and re.fullmatch(rf'[0-9a-fA-F]{{{length}}}', data[field]) is None)):
			_invalid(f'{location}.{field}', f'must be an empty string or a {length}-digit hexadecimal hash')
	_integer(data, 'size_bytes', location, 0)
	_optional_strings(data, ('commons_title',), location)


def _publication_fields(data: dict[str, Any], location: str) -> None:
	'''Check known identity fields in current, historical and attempted binaries.'''
	_binary_identity(data, location)
	_optional_strings(data, ('pdf_path', 'updated_at'), location)
	if 'duplicates' in data and (not isinstance(data['duplicates'], list)
		or not all(isinstance(title, str) and title for title in data['duplicates'])):
		_invalid(f'{location}.duplicates', 'must be a list of nonempty titles')
	for field in ('verification', 'pdf_validation'):
		if field in data and not isinstance(data[field], dict):
			_invalid(f'{location}.{field}', 'must be an object')
	if 'verification' in data:
		verification = data['verification']
		_binary_identity(verification, f'{location}.verification')
		for field in ('page_id', 'revision_id'):
			_integer(verification, field, f'{location}.verification', 1)
		_optional_strings(verification, ('method', 'timestamp', 'url'), f'{location}.verification')


def _outcome(data: dict[str, Any], location: str) -> None:
	'''Validate result fields while keeping unfamiliar status names forward compatible.'''
	if not isinstance(data.get('status'), str) or not data['status'].strip():
		_invalid(f'{location}.status', 'must be nonempty text')
	_optional_strings(data, ('previous_status', 'error', 'error_type', 'error_code', 'updated_at'), location)
	_integer(data, 'attempts', location, 1)
	_integer(data, 'http_status', location, 100, nullable=True)


def validate_state(data: Any) -> None:
	'''Validate saved state before migration, leaving all supplied data untouched.

	Version 1 histories may omit newer fields, including verification and the
	scan-identity migration marker. An explicit items object is always required;
	there is no supported legacy format in which missing items means empty history.
	Unknown fields and textual status names remain available for later readers.
	'''
	if not isinstance(data, dict):
		_invalid('root', 'must be an object')
	if not isinstance(data.get('items'), dict):
		_invalid('items', 'must be an explicitly saved object')
	if 'version' in data and (type(data['version']) is not int or data['version'] != 1):
		_invalid('version', 'must be the supported version 1')
	if 'scan_identity_version' in data and (type(data['scan_identity_version']) is not int
		or data['scan_identity_version'] not in (0, 1)):
		_invalid('scan_identity_version', 'must be the supported version 0 or 1')
	_integer(data, 'current_page', 'root', 1)
	_integer(data, 'retry_from_page', 'root', 1, nullable=True)
	for field in ('from_year', 'until_year'):
		_integer(data, field, 'root', 0, nullable=True)
	_optional_strings(data, ('search_url', 'updated_at'), 'root')
	for field, validator in (('current_record_id', _identifier), ('current_item_key', _item_key)):
		if field in data and data[field] is not None and not validator(data[field]):
			_invalid(field, 'must contain a valid catalog identity or null')
	if '_read_only' in data and type(data['_read_only']) is not bool:
		_invalid('_read_only', 'must be a boolean')
	for key, entry in data['items'].items():
		if not _item_key(key):
			_invalid('an items key', 'must have the form record_id:file_id using catalog identifiers')
		location = 'an items entry'
		if not isinstance(entry, dict):
			_invalid(location, 'must be an object')
		_outcome(entry, location)
		for field, expected in zip(('marc_id', 'file_id'), key.split(':')):
			if field in entry and entry[field] != expected:
				_invalid(f'{location}.{field}', 'must match its saved item key')
		_publication_fields(entry, location)
		for field in ('rights', 'scan_identity', 'metadata_refresh', 'publication', 'attempted_binary'):
			if field in entry and not isinstance(entry[field], dict):
				_invalid(f'{location}.{field}', 'must be an object')
		for field in ('publication', 'attempted_binary'):
			if field in entry:
				evidence = entry[field]
				_publication_fields(evidence, f'{location}.{field}')
				if 'status' in evidence:
					_outcome(evidence, f'{location}.{field}')
		if 'metadata_refresh' in entry:
			refresh = entry['metadata_refresh']
			if 'status' in refresh:
				_outcome(refresh, f'{location}.metadata_refresh')
			_optional_strings(refresh, ('checked_at',), f'{location}.metadata_refresh')
	if 'record_failures' in data:
		if not isinstance(data['record_failures'], dict):
			_invalid('record_failures', 'must be an object')
		for record_id, failure in data['record_failures'].items():
			if not _identifier(record_id):
				_invalid('a record_failures key', 'must contain a valid catalog identifier')
			if not isinstance(failure, dict):
				_invalid('a record_failures entry', 'must be an object')
			_outcome(failure, 'a record_failures entry')


@contextmanager
def exclusive_run(state_path: Path | None, archive_dir: Path | None) -> Iterator[None]:
	'''Hold nonblocking process locks for every resource a run may write.

	Call before loading mutable state or writing an archive, and retain the context
	for the complete operation. Pass None for resources used only for inspection.
	Resolved paths cover aliases; separately locking state and archive protects runs
	that share either one. Lock files remain in place to avoid inode replacement
	races. Kernel locks end when descriptors close, including after process death.
	This context is deliberately not reentrant: one entrypoint owns the run locks.
	'''
	locks: dict[Path, str] = {}
	if state_path is not None:
		state_path = Path(state_path).resolve()
		locks[state_path.with_name(state_path.name + '.lock')] = 'state file'
	if archive_dir is not None:
		locks[Path(archive_dir).resolve() / '.nlb-run.lock'] = 'archive'
	opened: list[int] = []
	try:
		for lock_path, resource in sorted(locks.items(), key=lambda item: str(item[0])):
			lock_path.parent.mkdir(parents=True, exist_ok=True)
			flags = os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW
			descriptor = os.open(lock_path, flags, 0o600)
			opened.append(descriptor)
			try:
				fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
			except OSError as error:
				if error.errno in (errno.EACCES, errno.EAGAIN):
					raise RunLockedError(resource, lock_path) from None
				raise
			os.fchmod(descriptor, 0o600)
		yield
	finally:
		for descriptor in reversed(opened):
			os.close(descriptor)
