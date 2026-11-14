'''Keep historical Commons identity across retries without mixing new binary evidence.'''

import copy
import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import upload
from preservation import ArchiveStore
from publication_history import publication_evidence, retain_publication
from test_preservation_pipeline import Catalog


class TestPublicationHistory(unittest.TestCase):
	'''Exercise historical state through actual scan recovery and deferred attempts.'''

	def test_legacy_and_restricted_pending_uploads_reverify_before_any_transfer(self):
		'''A later access or rights change cannot erase a known uploaded hash and title.'''
		for status, prior in (('uploaded', None), ('restricted', 'verification_failed'), ('metadata_failed', 'uploaded')):
			with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
				root = Path(directory)
				old = {'status': status, 'sha1': 'a' * 40, 'size_bytes': 123, 'commons_title': 'File:Historical.pdf'}
				if prior:
					old['previous_status'] = prior
				state = upload.default_state()
				state['items']['BY-NLB-one:1'] = old
				client = Catalog(ArchiveStore(root / 'archive'))
				uploader = mock.Mock()
				uploader.file_info.side_effect = TimeoutError('Commons unavailable')
				with mock.patch('sys.stdout', io.StringIO()):
					for _ in range(2):
						result = upload.process_single_scan(client, uploader, state, root / 'state.json', root / 'staging',
							'BY-NLB-one', '1', '', {'title': 'Book'}, {})
						self.assertEqual(result, 'verification_failed')
						entry = state['items']['BY-NLB-one:1']
						for field in ('sha1', 'size_bytes', 'commons_title'):
							self.assertEqual(entry[field], old[field])
						self.assertEqual(publication_evidence(entry)['status'], prior or status)
					self.assertEqual(client.downloads, [])
					self.assertFalse(uploader.upload.called)
					uploader.file_info.side_effect = None
					uploader.file_info.return_value = {'sha1': 'a' * 40, 'size_bytes': 123, 'page_id': 1}
					self.assertEqual(upload.process_single_scan(client, uploader, state, root / 'state.json', root / 'staging',
						'BY-NLB-one', '1', '', {'title': 'Book'}, {}), 'existing_title')
				self.assertNotIn('error', state['items']['BY-NLB-one:1'])

	def test_incomplete_legacy_publication_survives_rights_and_download_failures(self):
		'''Partial legacy history remains useful even when it cannot verify a binary.'''
		with tempfile.TemporaryDirectory() as directory, mock.patch('sys.stdout', io.StringIO()):
			root = Path(directory)
			old = {'status': 'uploaded', 'sha1': 'a' * 40, 'commons_title': 'File:Historical.pdf'}
			state = upload.default_state()
			state['items']['BY-NLB-one:1'] = old.copy()
			client = Catalog(ArchiveStore(root / 'archive'))
			uploader = mock.Mock()
			uploader.title_exists.return_value = False
			for date, expected in (({}, 'rights_review'), ({'date': '1800'}, 'download_failed')):
				client.download_pdf_scan = mock.Mock(side_effect=upload.DownloadRetriesExhausted(TimeoutError('offline'), 5))
				self.assertEqual(upload.process_single_scan(client, uploader, state, root / 'state.json', root / 'staging',
					'BY-NLB-one', '1', '', {'title': 'Book'}, date), expected)
				self.assertEqual(publication_evidence(state['items']['BY-NLB-one:1']), old)
			uploader.upload.assert_not_called()

	def test_new_failed_binary_never_replaces_old_published_identity(self):
		'''New hashes remain separate from the old Commons title, verification and PDF.'''
		old = {'status': 'uploaded', 'sha1': 'a' * 40, 'sha256': 'b' * 64, 'size_bytes': 123,
			'commons_title': 'File:Historical.pdf', 'verification': {'sha1': 'a' * 40}, 'pdf_path': 'objects/old.pdf'}
		before = copy.deepcopy(old)
		result = retain_publication(old, {'status': 'upload_failed', 'sha1': 'c' * 40, 'sha256': 'd' * 64,
			'size_bytes': 456, 'commons_title': 'File:Proposed.pdf', 'pdf_path': 'objects/new.pdf'})
		self.assertEqual(publication_evidence(result), old)
		self.assertEqual(result['sha1'], old['sha1'])
		self.assertEqual(result['pdf_path'], old['pdf_path'])
		self.assertEqual(result['attempted_binary']['size_bytes'], 456)
		self.assertEqual(old, before)
		pending = retain_publication(result, {'status': 'restricted'})
		self.assertEqual(pending['attempted_binary'], result['attempted_binary'])
		self.assertEqual(publication_evidence(pending), old)

	def test_failed_new_download_cannot_fill_gaps_in_archived_old_identity(self):
		'''Archive and state describe the same old binary even after capture of new bytes.'''
		with tempfile.TemporaryDirectory() as directory, mock.patch('sys.stdout', io.StringIO()):
			root = Path(directory)
			old = {'status': 'uploaded', 'sha1': 'a' * 40, 'commons_title': 'File:Historical.pdf'}
			state = upload.default_state()
			state['items']['BY-NLB-one:1'] = old.copy()
			client = Catalog(ArchiveStore(root / 'archive', keep_pdfs=True))
			uploader = mock.Mock()
			uploader.title_exists.return_value = False
			with mock.patch('upload.check_commons_by_hash', side_effect=upload.requests.Timeout('offline')):
				self.assertEqual(upload.process_single_scan(client, uploader, state, root / 'state.json', root / 'staging',
					'BY-NLB-one', '1', '', {'title': 'Book'}, {'date': '1800'}), 'verification_failed')
			for result in (state['items']['BY-NLB-one:1'], client.archive.manifests['BY-NLB-one']['files']['1']):
				self.assertEqual(result['sha1'], old['sha1'])
				for field in ('sha256', 'size_bytes', 'pdf_path'):
					self.assertNotIn(field, result)
					self.assertIn(field, result['attempted_binary'])
			uploader.upload.assert_not_called()

	def test_nested_publication_overrides_conflicting_flat_attempt_fields(self):
		'''Canonical saved evidence permits recovery without downloading another original.'''
		for verified in (True, False):
			with self.subTest(verified=verified), tempfile.TemporaryDirectory() as directory, mock.patch('sys.stdout', io.StringIO()):
				root = Path(directory)
				old = {'status': 'uploaded', 'sha1': 'a' * 40, 'size_bytes': 123, 'commons_title': 'File:Historical.pdf'}
				if verified:
					old['verification'] = {'sha1': 'a' * 40}
				state = upload.default_state()
				state['items']['BY-NLB-one:1'] = {'status': 'download_failed', 'publication': old,
					'sha1': 'b' * 40, 'size_bytes': 456, 'commons_title': 'File:Attempt.pdf'}
				client = Catalog(ArchiveStore(root / 'archive'))
				uploader = mock.Mock()
				uploader.file_info.return_value = {'sha1': 'a' * 40, 'size_bytes': 123}
				result = upload.process_single_scan(client, uploader, state, root / 'state.json', root / 'staging',
					'BY-NLB-one', '1', '', {'title': 'Book'}, {'date': '1800'})
				self.assertEqual(result, 'completed' if verified else 'existing_title')
				self.assertEqual(client.downloads, [])
				uploader.upload.assert_not_called()
				self.assertEqual(state['items']['BY-NLB-one:1']['commons_title'], old['commons_title'])
				if not verified:
					uploader.file_info.assert_called_once_with(old['commons_title'])
