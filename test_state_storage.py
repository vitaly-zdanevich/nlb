'''Protect saved upload history and test process-level exclusion with local fixtures.'''

import copy
import errno
import json
import multiprocessing
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from state_storage import InvalidStateError, RunLockedError, exclusive_run, validate_state


def _try_lock(state_path, archive_dir, channel):
	'''Try the same entrypoint lock from a fresh process without inherited descriptors.'''
	try:
		with exclusive_run(state_path, archive_dir):
			channel.send('acquired')
	except RunLockedError as error:
		channel.send(error.resource)
	finally:
		channel.close()


def _hold_lock(state_path, archive_dir, channel):
	'''Hold a child writer open until released, including abrupt-exit test coverage.'''
	with exclusive_run(state_path, archive_dir):
		channel.send('acquired')
		channel.recv()
	channel.close()


class TestStateValidation(unittest.TestCase):
	'''Reject malformed known fields without changing supported historical state.'''

	def test_legacy_and_current_history_are_retained_verbatim(self):
		'''Partial legacy identities and new publication evidence survive validation.'''
		documents = [
			{'items': {}},
			{'items': {'BY-NLB-one:1': {'status': 'uploaded'}}},
			{'version': 1, 'current_page': 7, 'scan_identity_version': 0,
				'current_record_id': None, 'current_item_key': None,
				'from_year': None, 'until_year': 1900, 'retry_from_page': None,
				'items': {'BY-NLB-one:1': {'status': 'not_pdf', 'sha1': '', 'size_bytes': 0,
					'commons_title': '', 'verification': {}, 'pdf_path': None}}},
			{'items': {'BY-NLB-one:1': {
				'status': 'download_failed', 'marc_id': 'BY-NLB-one', 'file_id': '1',
				'publication': {'status': 'uploaded', 'sha1': 'a' * 40, 'size_bytes': 123,
					'commons_title': 'File:Book.pdf', 'duplicates': ['File:Book.pdf'],
					'verification': {'page_id': 2, 'revision_id': 3, 'sha256': 'B' * 64}},
				'attempted_binary': {'sha256': 'c' * 64, 'size_bytes': 456, 'pdf_validation': {}},
				'metadata_refresh': {'status': 'metadata_failed', 'checked_at': '2026-11-14'},
				'rights': {}, 'scan_identity': {},
			}}, 'record_failures': {'BY-NLB-two': {'status': 'new_status', 'extra': [1]}},
				'extra': {'preserve': ['all', 'of', 'this']}},
		]
		for document in documents:
			with self.subTest(document=document):
				original = copy.deepcopy(document)
				self.assertIsNone(validate_state(document))
				self.assertEqual(document, original)

	def test_bad_root_and_progress_never_reset_items(self):
		'''A valid JSON value is not necessarily safe resumable state.'''
		invalid_documents = [None, [], {}, {'items': None}, {'items': []}, {'items': 'history'}]
		for field, value in (
			('version', 2), ('version', True), ('version', '1'),
			('scan_identity_version', 2), ('scan_identity_version', True),
			('current_page', 0), ('current_page', True), ('current_page', 1.5),
			('retry_from_page', -1), ('from_year', '1800'), ('until_year', False),
			('search_url', []), ('updated_at', {}), ('current_record_id', '../secret'),
			('current_item_key', 'BY-NLB-one'), ('_read_only', 1),
			('record_failures', []), ('record_failures', {'../bad': {'status': 'failed'}}),
			('record_failures', {'BY-NLB-one': []}), ('record_failures', {'BY-NLB-one': {}}),
		):
			invalid_documents.append({'items': {}, field: value})
		for document in invalid_documents:
			with self.subTest(document=document):
				original = copy.deepcopy(document)
				with self.assertRaises(InvalidStateError):
					validate_state(document)
				self.assertEqual(document, original)

	def test_bad_item_identity_and_nested_evidence_are_rejected(self):
		'''Malformed historical evidence cannot become a different published association.'''
		entries = [None, [], {}, {'status': ''}, {'status': False}]
		for field, value in (
			('marc_id', 'BY-NLB-other'), ('file_id', 1), ('file_id', '2'),
			('sha1', 'invalid'), ('sha256', None), ('size_bytes', -1), ('size_bytes', True),
			('commons_title', []), ('duplicates', 'File:Book.pdf'), ('duplicates', [None]),
			('pdf_path', 10), ('pdf_validation', []), ('verification', None),
			('verification', {'page_id': 0}), ('verification', {'revision_id': True}),
			('verification', {'method': []}), ('verification', {'sha1': 'bad'}),
			('rights', []), ('scan_identity', ''), ('metadata_refresh', None),
			('metadata_refresh', {'status': ''}), ('metadata_refresh', {'checked_at': []}),
			('publication', []), ('publication', {'sha1': 'bad'}),
			('publication', {'verification': {'size_bytes': -1}}),
			('publication', {'status': False}), ('publication', {'duplicates': [3]}),
			('attempted_binary', None), ('attempted_binary', {'size_bytes': '9'}),
			('error', []), ('attempts', 0), ('http_status', 99),
		):
			entries.append({'status': 'uploaded', field: value})
		for entry in entries:
			with self.subTest(entry=entry):
				document = {'items': {'BY-NLB-one:1': entry}}
				original = copy.deepcopy(document)
				with self.assertRaises(InvalidStateError):
					validate_state(document)
				self.assertEqual(document, original)
		for key in (1, 'record', 'record:file:extra', '../record:1', 'record:'):
			with self.subTest(key=key), self.assertRaises(InvalidStateError):
				validate_state({'items': {key: {'status': 'uploaded'}}})

	def test_diagnostics_do_not_echo_saved_values(self):
		'''Malformed untrusted state cannot spill arbitrary data into operator logs.'''
		with self.assertRaises(InvalidStateError) as raised:
			validate_state({'items': {'PRIVATE_VALUE': {'status': 'uploaded'}}})
		self.assertNotIn('PRIVATE_VALUE', str(raised.exception))
		self.assertIn('Preserve the original file', str(raised.exception))


class TestExclusiveRun(unittest.TestCase):
	'''Use independent processes to exercise real advisory locks and crash release.'''

	def try_in_child(self, state_path, archive_dir):
		'''Return a bounded child result and always join the worker after the assertion.'''
		context = multiprocessing.get_context('spawn')
		parent, child = context.Pipe()
		process = context.Process(target=_try_lock, args=(state_path, archive_dir, child))
		process.start()
		child.close()
		try:
			self.assertTrue(parent.poll(10), 'Lock attempt did not return promptly')
			result = parent.recv()
			process.join(10)
			self.assertEqual(process.exitcode, 0)
			return result
		finally:
			parent.close()
			if process.is_alive():
				process.kill()
				process.join(10)
			process.close()

	def test_shared_state_or_archive_rejects_second_process(self):
		'''Neither changing the archive nor changing state bypasses a held writer lock.'''
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			state, archive = root / 'state.json', root / 'archive'
			state.write_text(json.dumps({'items': {'BY-NLB-one:1': {'status': 'uploaded'}}}))
			original = state.read_bytes()
			with exclusive_run(state, archive):
				self.assertEqual(self.try_in_child(state, root / 'another-archive'), 'state file')
				self.assertEqual(self.try_in_child(root / 'another-state.json', archive), 'archive')
				self.assertEqual(self.try_in_child(root / 'isolated.json', root / 'isolated-archive'), 'acquired')
				self.assertEqual(state.read_bytes(), original)
			self.assertEqual(self.try_in_child(state, archive), 'acquired')
			self.assertEqual(state.read_bytes(), original)

	def test_paths_are_resolved_and_lock_inodes_retained_with_private_permissions(self):
		'''Directory aliases use one lock; existing lock files are never unlinked.'''
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			actual = root / 'actual'
			actual.mkdir()
			alias = root / 'alias'
			alias.symlink_to(actual, target_is_directory=True)
			state, archive = actual / 'state.json', actual / 'archive'
			state_lock = actual / 'state.json.lock'
			state_lock.touch(mode=0o666)
			state_lock.chmod(0o666)
			inode = state_lock.stat().st_ino
			with exclusive_run(state, archive):
				self.assertEqual(self.try_in_child(alias / 'state.json', None), 'state file')
				self.assertEqual(self.try_in_child(None, alias / 'archive'), 'archive')
			for path in (state_lock, archive / '.nlb-run.lock'):
				self.assertTrue(path.exists())
				self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
			self.assertEqual(state_lock.stat().st_ino, inode)

	def test_failed_second_lock_and_body_interrupt_release_first_lock(self):
		'''Partial acquisition and exceptions cannot leak a writer lock into the next run.'''
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			state, archive = root / 'a-state.json', root / 'z-archive'
			with exclusive_run(None, archive):
				with self.assertRaises(RunLockedError):
					with exclusive_run(state, archive):
						self.fail('The occupied archive must stop this writer')
				self.assertEqual(self.try_in_child(state, None), 'acquired')
			with self.assertRaises(KeyboardInterrupt):
				with exclusive_run(state, archive):
					raise KeyboardInterrupt
			self.assertEqual(self.try_in_child(state, archive), 'acquired')

	def test_process_death_releases_both_locks(self):
		'''The kernel releases an abruptly terminated writer without deleting lock files.'''
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			state, archive = root / 'state.json', root / 'archive'
			context = multiprocessing.get_context('spawn')
			parent, child = context.Pipe()
			process = context.Process(target=_hold_lock, args=(state, archive, child))
			process.start()
			child.close()
			try:
				self.assertTrue(parent.poll(10))
				self.assertEqual(parent.recv(), 'acquired')
				self.assertEqual(self.try_in_child(state, None), 'state file')
				self.assertEqual(self.try_in_child(None, archive), 'archive')
			finally:
				process.kill()
				process.join(10)
				process.close()
				parent.close()
			self.assertEqual(self.try_in_child(state, archive), 'acquired')

	def test_io_failures_propagate_and_release_open_descriptors(self):
		'''Only lock contention receives the competing-run diagnostic.'''
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			for error in (OSError(errno.EIO, 'disk error'), PermissionError(errno.EPERM, 'denied')):
				with self.subTest(error=error), mock.patch('state_storage.fcntl.flock', side_effect=error):
					with self.assertRaises(OSError) as raised:
						with exclusive_run(root / 'state.json', None):
							self.fail('I/O failure must stop acquisition')
					self.assertIs(raised.exception, error)
			self.assertEqual(self.try_in_child(root / 'state.json', None), 'acquired')

	def test_lock_symlinks_cannot_modify_unrelated_files(self):
		'''Opening a lock refuses a symlink rather than chmodding its destination.'''
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			target = root / 'unrelated.json'
			target.write_text('unchanged')
			target.chmod(0o644)
			(root / 'state.json.lock').symlink_to(target)
			with self.assertRaises(OSError):
				with exclusive_run(root / 'state.json', None):
					self.fail('Symlink lock must not be followed')
			self.assertEqual(target.read_text(), 'unchanged')
			self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)

	def test_inspection_needs_no_resource_lock_and_messages_are_bounded(self):
		'''Read-only users may omit resources; a long lock path remains available as data.'''
		with mock.patch('state_storage.os.open') as opening:
			with exclusive_run(None, None):
				pass
			opening.assert_not_called()
		path = Path('/' + 'a' * 400 + '/state.json.lock')
		error = RunLockedError('state file', path)
		self.assertEqual(error.lock_path, path)
		self.assertLess(len(str(error)), 350)
		self.assertIn('do not delete the lock file', str(error))
