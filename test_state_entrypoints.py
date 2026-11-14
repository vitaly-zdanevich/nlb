'''Verify command-line operations validate history and hold locks before external work.'''

import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import reconcile_metadata
from state_storage import InvalidStateError, RunLockedError, exclusive_run
import upload


class TestStateEntrypoints(unittest.TestCase):
	'''Exercise real CLI validation and lock acquisition around mocked network operations.'''

	def setUp(self):
		'''Keep every state file, archive and lock inside an isolated temporary directory.'''
		self.temporary = tempfile.TemporaryDirectory()
		self.addCleanup(self.temporary.cleanup)
		self.root = Path(self.temporary.name)
		self.state = self.root / 'state.json'
		self.archive = self.root / 'archive'
		self.arguments = ['--state-file', str(self.state), '--archive-dir', str(self.archive)]

	def assert_resources_locked(self):
		'''Check each shared resource independently while an entrypoint is doing work.'''
		for state, archive in ((self.state, None), (None, self.archive)):
			with self.assertRaises(RunLockedError):
				with exclusive_run(state, archive):
					self.fail('An active operation released a resource lock too early')

	def test_malformed_history_stops_all_modes_before_clients_and_preserves_bytes(self):
		'''Schema and JSON failures cannot clear history or begin any remote operation.'''
		for mode in ([], ['--dry-run'], ['--metadata-only'], ['--refresh-uploaded-metadata']):
			for body in ('{"items":[]}', '{"items":{"BY-NLB-one:1":null}}', '{"items":'):
				with self.subTest(mode=mode, body=body):
					self.state.write_text(body)
					with mock.patch('upload.NlbClient') as client, mock.patch('upload.CommonsUploader') as uploader, mock.patch('upload.run_locked') as run, mock.patch('sys.stdout', io.StringIO()) as output:
						self.assertEqual(upload.main([*self.arguments, *mode]), 2)
					self.assertEqual(self.state.read_text(), body)
					client.assert_not_called()
					uploader.assert_not_called()
					run.assert_not_called()
					self.assertIn('Stopped:', output.getvalue())
					self.assertFalse((self.archive / 'records').exists())
					with exclusive_run(self.state, self.archive):
						pass

	def test_direct_state_load_does_not_migrate_malformed_history(self):
		'''Schema failure precedes any legacy-state migration or progress reset.'''
		body = '{"items":[],"until_year":1700,"current_page":21}'
		self.state.write_text(body)
		with self.assertRaises(InvalidStateError):
			upload.load_state(self.state, until_year=1900)
		self.assertEqual(self.state.read_text(), body)

	def test_existing_writer_prevents_client_initialization_in_all_modes(self):
		'''Competing CLI runs stop before they authenticate, collect sources or edit Commons.'''
		self.state.write_text('{"items":{}}')
		before = self.state.read_bytes()
		for mode in ([], ['--dry-run'], ['--metadata-only'], ['--refresh-uploaded-metadata']):
			for state, archive in ((self.state, None), (None, self.archive)):
				with self.subTest(mode=mode, resource='state' if state else 'archive'), exclusive_run(state, archive):
					with mock.patch('upload.NlbClient') as client, mock.patch('upload.CommonsUploader') as uploader, mock.patch('upload.run_locked') as run, mock.patch('sys.stdout', io.StringIO()) as output:
						self.assertEqual(upload.main([*self.arguments, *mode]), 2)
					client.assert_not_called()
					uploader.assert_not_called()
					run.assert_not_called()
					self.assertIn('Another NLB run', output.getvalue())
					self.assertEqual(self.state.read_bytes(), before)

	def test_output_paths_cannot_replace_state_or_lock_inodes(self):
		'''Path validation runs before acquiring locks or writing a report.'''
		self.state.write_text('{"items":{}}')
		state_lock, archive_lock = self.state.with_name('state.json.lock'), self.archive / '.nlb-run.lock'
		with exclusive_run(self.state, self.archive):
			before = {path: (path.stat().st_ino, path.read_bytes()) for path in (self.state, state_lock, archive_lock)}
			for output in (self.state, state_lock, archive_lock):
				with self.subTest(output=output), mock.patch('upload.NlbClient') as client, mock.patch('sys.stderr', io.StringIO()):
					with self.assertRaises(SystemExit) as raised:
						upload.main([*self.arguments, '--report', str(output)])
					self.assertEqual(raised.exception.code, 2)
					client.assert_not_called()
			for path, (inode, contents) in before.items():
				self.assertEqual(path.stat().st_ino, inode)
				self.assertEqual(path.read_bytes(), contents)
		with mock.patch('upload.NlbClient') as client, mock.patch('sys.stderr', io.StringIO()):
			with self.assertRaises(SystemExit) as raised:
				upload.main(['--state-file', str(archive_lock), '--archive-dir', str(self.archive)])
			self.assertEqual(raised.exception.code, 2)
			client.assert_not_called()

	def test_ordinary_work_holds_both_locks_until_it_finishes(self):
		'''The entrypoint protects the whole operation, not just individual state saves.'''
		def iterate(**kwargs):
			'''Simulate work while probing real locks around the operation.'''
			self.assert_resources_locked()
			return {'uploaded': 0}
		with mock.patch('upload.NlbClient'), mock.patch('upload.CommonsUploader'), mock.patch('upload.iterate_and_upload', side_effect=iterate) as run, mock.patch('sys.stdout', io.StringIO()):
			self.assertEqual(upload.main(self.arguments), 0)
		run.assert_called_once()
		with exclusive_run(self.state, self.archive):
			pass

	def test_metadata_collection_and_commons_apply_share_one_lock_lifetime(self):
		'''Source collection cannot release the archive before the corresponding edit phase.'''
		self.state.write_text('{"items":{}}')
		before = self.state.read_bytes()
		def collect(*args, **kwargs):
			'''Return an empty successful collection while checking ownership.'''
			self.assert_resources_locked()
			return {'metadata_ready': 0}
		def apply(*args, **kwargs):
			'''Return an empty edit report while the same resources remain held.'''
			self.assert_resources_locked()
			return {'counts': {'metadata_updated': 0}}
		with mock.patch('upload.NlbClient'), mock.patch('upload.CommonsUploader') as uploader, mock.patch('metadata_refresh.inspect_refresh_archive', return_value=[]), mock.patch('upload.refresh_uploaded_metadata', side_effect=collect) as collector, mock.patch('metadata_refresh.apply_uploaded_metadata', side_effect=apply) as applier, mock.patch('sys.stdout', io.StringIO()):
			self.assertEqual(upload.main([*self.arguments, '--refresh-uploaded-metadata']), 0)
		collector.assert_called_once()
		applier.assert_called_once()
		uploader.assert_not_called()
		self.assertEqual(self.state.read_bytes(), before)
		with exclusive_run(self.state, self.archive):
			pass

	def test_reconciliation_apply_defers_to_an_existing_archive_writer(self):
		'''Manual plan application must honor the uploader's archive lock before Commons reads.'''
		plan_path = self.root / 'plan.json'
		plan_path.write_text(json.dumps({'schema_version': 1, 'entries': []}))
		arguments = ['--archive-dir', str(self.archive), '--apply', str(plan_path)]
		with exclusive_run(None, self.archive), mock.patch('reconcile_metadata.apply_plan') as apply, mock.patch('sys.stdout', io.StringIO()) as output:
			self.assertEqual(reconcile_metadata.main(arguments), 2)
		apply.assert_not_called()
		self.assertIn('Another NLB run', output.getvalue())
		def apply_with_lock(plan):
			'''Check that a successful manual application retains the archive lock.'''
			with self.assertRaises(RunLockedError):
				with exclusive_run(None, self.archive):
					self.fail('Manual apply released its archive lock')
			return 0
		with mock.patch('reconcile_metadata.apply_plan', side_effect=apply_with_lock) as apply, mock.patch('sys.stdout', io.StringIO()):
			self.assertEqual(reconcile_metadata.main(arguments), 0)
		apply.assert_called_once()

	def test_reconciliation_plan_cannot_replace_locks_or_archive_evidence(self):
		'''Planning remains read-only even when the operator chooses a protected output path.'''
		with exclusive_run(self.state, self.archive):
			lock = self.archive / '.nlb-run.lock'
			inode = lock.stat().st_ino
			for path in (lock, self.state.with_name('state.json.lock'),
				self.archive / 'objects' / 'source.json',
				self.archive / 'records' / 'manifest.json',
				self.archive / 'quarantine' / 'source.pdf',
				self.archive / 'metadata-refresh' / 'report.json',
				self.archive / 'pdf-backfill' / 'index.json'):
				with self.subTest(path=path), mock.patch('reconcile_metadata.generate_plan') as generate, mock.patch('sys.stderr', io.StringIO()):
					with self.assertRaises(SystemExit) as raised:
						reconcile_metadata.main(['--archive-dir', str(self.archive), '--plan', str(path)])
					self.assertEqual(raised.exception.code, 2)
					generate.assert_not_called()
			self.assertEqual(lock.stat().st_ino, inode)
			self.assertEqual(lock.read_bytes(), b'')
			with self.assertRaises(RunLockedError):
				with exclusive_run(None, self.archive):
					self.fail('Rejected plan replaced the live archive lock')

	def test_generated_plan_records_resolved_archive_and_apply_uses_its_lock(self):
		'''Applying a saved plan from another working directory still protects its source archive.'''
		self.archive.mkdir()
		alias = self.root / 'archive-alias'
		alias.symlink_to(self.archive, target_is_directory=True)
		plan_path = self.root / 'plan.json'
		with mock.patch('sys.stdout', io.StringIO()):
			self.assertEqual(reconcile_metadata.main(['--archive-dir', str(alias), '--plan', str(plan_path)]), 0)
		plan = json.loads(plan_path.read_text())
		self.assertEqual(plan['archive_dir'], str(self.archive.resolve()))
		for archive_arguments in ([], ['--archive-dir', str(alias)]):
			with self.subTest(archive_arguments=archive_arguments), exclusive_run(None, self.archive), mock.patch('reconcile_metadata.apply_plan') as apply, mock.patch('sys.stdout', io.StringIO()):
				self.assertEqual(reconcile_metadata.main([*archive_arguments, '--apply', str(plan_path)]), 2)
				apply.assert_not_called()
		with mock.patch('reconcile_metadata.apply_plan', return_value=0) as apply, mock.patch('sys.stdout', io.StringIO()):
			self.assertEqual(reconcile_metadata.main(['--apply', str(plan_path)]), 0)
		apply.assert_called_once_with(plan)

	def test_reconciliation_rejects_explicit_archive_different_from_the_plan(self):
		'''An archive override cannot redirect the lock away from the plan's source archive.'''
		plan_path = self.root / 'plan.json'
		plan_path.write_text(json.dumps({'schema_version': 1, 'archive_dir': str(self.archive), 'entries': []}))
		other = self.root / 'different-archive'
		with mock.patch('reconcile_metadata.apply_plan') as apply, mock.patch('sys.stderr', io.StringIO()) as error:
			with self.assertRaises(SystemExit) as raised:
				reconcile_metadata.main(['--archive-dir', str(other), '--apply', str(plan_path)])
			self.assertEqual(raised.exception.code, 2)
		apply.assert_not_called()
		self.assertIn('differs from the archive recorded in the plan', error.getvalue())
		self.assertFalse(other.exists())

	def test_state_symlink_keeps_one_lock_identity_after_atomic_save(self):
		'''A write through an alias updates the canonical target without replacing the alias.'''
		self.state.write_text('{"items":{}}')
		alias = self.root / 'state-alias.json'
		alias.symlink_to(self.state)
		def run(**options):
			'''Save once while asserting both aliases remain protected by the held lock.'''
			self.assertEqual(options['state_path'], self.state.resolve())
			state = upload.load_state(options['state_path'])
			state['current_page'] = 9
			upload.save_state(options['state_path'], state)
			self.assertTrue(alias.is_symlink())
			for path in (alias, self.state):
				with self.assertRaises(RunLockedError), exclusive_run(path, None):
					self.fail('Atomic save changed the state lock identity')
			return {}
		with mock.patch('upload.NlbClient'), mock.patch('upload.iterate_and_upload', side_effect=run), mock.patch('sys.stdout', io.StringIO()):
			self.assertEqual(upload.main(['--metadata-only', '--state-file', str(alias), '--archive-dir', str(self.archive)]), 0)
		self.assertTrue(alias.is_symlink())
		self.assertEqual(json.loads(self.state.read_text())['current_page'], 9)
