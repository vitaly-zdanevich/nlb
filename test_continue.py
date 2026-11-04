'''Protect historical upload identities and state from preservation failures.'''

import copy
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from catalog_requests import CatalogRetriesExhausted
from preservation import ArchiveStore, atomic_json, file_integrity
from test_refresh_uploaded_metadata import RefreshCatalog, association
from test_upload import FakeCommonsUploader, FakeNlbClient
import upload


class TestContinue(unittest.TestCase):
	'''Retain upload identity and reject destructive report destinations.'''

	def setUp(self):
		'''Keep every test's state, archive and output isolated from production.'''
		temporary = tempfile.TemporaryDirectory()
		self.addCleanup(temporary.cleanup)
		self.root = Path(temporary.name)
		self.state = self.root / 'state.json'
		self.addCleanup(mock.patch.stopall)
		mock.patch('sys.stdout', io.StringIO()).start()



	def test_new_access_restriction_keeps_historical_upload_identity(self):
		'''A later library access decision cannot erase an already published file's evidence.'''
		state = upload.default_state()
		previous = association(verification={'sha1': 'a' * 40})
		state['items']['BY-NLB-rr111:1001'] = previous
		upload.save_state(self.state, state)
		client = FakeNlbClient()
		client.archive = ArchiveStore(self.root / 'archive')
		client.check_doc_access = mock.Mock(return_value={'publicAccess': False})
		upload.iterate_and_upload(client, FakeCommonsUploader(), self.state, self.root / 'staging')
		item = json.loads(self.state.read_text())['items']['BY-NLB-rr111:1001']
		self.assertEqual(item['status'], 'restricted')
		self.assertEqual(item['previous_status'], 'uploaded')
		for key in ('commons_title', 'sha1', 'size_bytes', 'verification'):
			self.assertEqual(item[key], previous[key])
		self.assertEqual(client.archive.manifests['BY-NLB-rr111']['files']['1001'], item)
		self.assertEqual(client.downloaded_file_ids, [])







	def test_report_collisions_are_rejected_before_any_work_in_every_mode(self):
		'''Preview and normal modes cannot replace state, source objects or receipts.'''
		for mode in ([], ['--dry-run'], ['--metadata-only'], ['--refresh-uploaded-metadata']):
			for target in ('state.json', 'archive/objects/test.bin'):
				with self.subTest(mode=mode, target=target), mock.patch('upload.NlbClient') as client, mock.patch('sys.stderr', io.StringIO()):
					with self.assertRaises(SystemExit) as error:
						upload.main([*mode, '--state-file', str(self.state), '--report', str(self.root / target)])
					self.assertEqual(error.exception.code, 2)
					client.assert_not_called()

	def test_refresh_outputs_cannot_overwrite_an_unusually_named_state_file(self):
		'''Diagnostics and checkpoints must preserve state even when the archive is damaged.'''
		archive = self.root / 'archive'
		for name in ('archive-issues.json', 'report.json'):
			with self.subTest(name=name):
				state = archive / 'metadata-refresh' / name
				atomic_json(state, {'items': {'BY-NLB-one:1': association()}})
				before = state.read_bytes()
				with mock.patch('upload.NlbClient') as client, mock.patch('metadata_refresh.inspect_refresh_archive', return_value=[{'error': 'Missing archived description'}]) as preflight, mock.patch('sys.stderr', io.StringIO()):
					with self.assertRaises(SystemExit) as error:
						upload.main(['--refresh-uploaded-metadata', '--state-file', str(state), '--archive-dir', str(archive)])
					self.assertEqual(error.exception.code, 2)
					self.assertEqual(state.read_bytes(), before)
					client.assert_not_called()
					preflight.assert_not_called()


if __name__ == '__main__':
	unittest.main()
