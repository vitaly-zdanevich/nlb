'''Refresh only historical Commons associations, preserving their state and binary identities.'''

import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from catalog_requests import CatalogRetriesExhausted, InvalidCatalogResponse
from preservation import ArchiveStore
import upload


class RefreshCatalog(upload.NlbClient):
	'''Use the real file resolver with mocked catalog, METS and per-file IIIF reads.'''

	def __init__(self, root, records, mets=None):
		'''Capture source request histories while forbidding search, PDF and Commons reads.'''
		self.archive = ArchiveStore(root)
		self.fetch_record_metadata = mock.Mock(side_effect=lambda key: copy.deepcopy(records[key]))
		self.check_doc_access = mock.Mock(return_value={'publicAccess': True})
		self.fetch_mets_files = mock.Mock(side_effect=lambda key: copy.deepcopy((mets or {})[key]))
		self.fetch_scan_manifest = mock.Mock(side_effect=lambda record, file_id: {
			'metadata': [{'label': 'Scan note', 'value': f'Fresh viewer note {file_id}'}],
		})
		self.fetch_search_page = mock.Mock(side_effect=AssertionError('Unexpected full catalog search'))
		self.download_pdf_scan = mock.Mock(side_effect=AssertionError('Unexpected PDF download'))


def association(status='uploaded', **overrides):
	'''Return a previously recorded binary and its actual, possibly renamed Commons target.'''
	return {'status': status, 'sha1': 'a' * 40, 'size_bytes': 123,
		'commons_title': 'File:Actual old target.pdf', **overrides}


class TestRefreshUploadedMetadata(unittest.TestCase):
	'''Exercise the source refresh through archived results and exact unchanged state bytes.'''

	def setUp(self):
		'''Isolate archives and state while suppressing routine progress output.'''
		temporary = tempfile.TemporaryDirectory()
		self.addCleanup(temporary.cleanup)
		self.root = Path(temporary.name)
		self.state_path = self.root / 'state.json'
		self.addCleanup(mock.patch.stopall)
		mock.patch('sys.stdout', io.StringIO()).start()

	def write_state(self, items):
		'''Keep unusual formatting and old progress to detect even incidental state rewrites.'''
		state = {'version': 1, 'current_page': 35, 'until_year': 1880,
			'search_url': 'https://elib.nlb.by/elib/Search/Results?lookfor=old',
			'retry_from_page': 21, 'items': items}
		self.state_path.write_text(json.dumps(state, ensure_ascii=False) + '\n\n')
		return self.state_path.read_bytes()

	def collect(self, client):
		'''Call the collector without creating an upload service.'''
		with mock.patch('upload.CommonsUploader') as uploader, mock.patch('upload.check_commons_by_hash') as lookup:
			counts = upload.refresh_uploaded_metadata(client, self.state_path, self.root / 'staging')
		uploader.assert_not_called()
		lookup.assert_not_called()
		client.fetch_search_page.assert_not_called()
		client.download_pdf_scan.assert_not_called()
		self.assertFalse((self.root / 'staging').exists())
		return counts

	def test_only_known_hash_identified_targets_are_refreshed(self):
		'''All supported historical outcomes retain actual targets while new files are ignored.'''
		items = {
			'BY-NLB-book:1': association(),
			'BY-NLB-book:2': association('existing_title'),
			'BY-NLB-book:3': association('duplicate_sha1', duplicates=['File:Duplicate.pdf']),
			'BY-NLB-book:4': association('verification_failed', error="'pageid'"),
			'BY-NLB-unfinished:5': association('download_failed'),
			'BY-NLB-nohash:6': association(sha1=''),
			'BY-NLB-notarget:7': association(commons_title=''),
			'BY-NLB-book:8': association('metadata_review', previous_status='uploaded'),
		}
		before = self.write_state(items)
		client = RefreshCatalog(self.root / 'archive', {'BY-NLB-book': (
			{'title': 'Fresh catalog title', 'file_ids': ['1', '2', '3', '4', '8', '99']}, {'date': '1800'},
		)})
		counts = self.collect(client)
		self.assertEqual((counts['metadata_ready'], counts['skipped']), (5, 3))
		client.fetch_record_metadata.assert_called_once_with('BY-NLB-book')
		client.check_doc_access.assert_called_once_with('BY-NLB-book')
		self.assertEqual(client.fetch_scan_manifest.call_args_list,
			[mock.call('BY-NLB-book', str(number)) for number in (1, 2, 3, 4, 8)])
		files = client.archive.manifests['BY-NLB-book']['files']
		self.assertEqual(set(files), {'1', '2', '3', '4', '8'})
		for file_id, saved in files.items():
			previous = items[f'BY-NLB-book:{file_id}']
			for field in ('status', 'sha1', 'size_bytes', 'commons_title', 'duplicates'):
				self.assertEqual(saved.get(field), previous.get(field))
			self.assertTrue(saved['scan_identity']['eligible'])
			text = (client.archive.root / saved['description']['path']).read_text()
			self.assertIn('Fresh catalog title', text)
			self.assertIn(f'Fresh viewer note {file_id}', text)
		self.assertEqual(self.state_path.read_bytes(), before)

	def test_fresh_mets_scope_blocks_the_wrong_volume(self):
		'''A historical upload does not override new evidence that its viewer volume differs.'''
		before = self.write_state({'BY-NLB-book:1': association(), 'BY-NLB-book:2': association()})
		mets = upload.extract_files_from_mets_structure({'nodes': [
			{'data': {'nodeType': 'TEXT', 'linkName': 'Том I'}},
			{'data': {'nodeType': 'FILE', 'linkName': 'Ч. 1', 'file': {'id': '1'}}},
			{'data': {'nodeType': 'TEXT', 'linkName': 'Том II'}},
			{'data': {'nodeType': 'FILE', 'linkName': 'Ч. 1', 'file': {'id': '2'}}},
		]})
		client = RefreshCatalog(self.root / 'archive', {'BY-NLB-book': (
			{'title': 'Book T. 1', 'mets_ids': ['viewer']}, {'date': '1800'},
		)}, {'viewer': mets})
		counts = self.collect(client)
		self.assertEqual((counts['metadata_ready'], counts['metadata_review']), (1, 1))
		client.fetch_mets_files.assert_called_once_with('viewer')
		client.fetch_scan_manifest.assert_called_once_with('BY-NLB-book', '1')
		blocked = client.archive.manifests['BY-NLB-book']['files']['2']
		self.assertEqual(blocked['status'], 'metadata_review')
		self.assertEqual(blocked['sha1'], 'a' * 40)
		self.assertEqual(blocked['commons_title'], 'File:Actual old target.pdf')
		self.assertFalse(blocked['scan_identity']['eligible'])
		self.assertNotIn('description', blocked)
		self.assertEqual(self.state_path.read_bytes(), before)

	def test_absent_source_ids_are_not_reintroduced_from_history(self):
		'''Fresh discovery must confirm the old source association before producing a description.'''
		before = self.write_state({'BY-NLB-book:1': association()})
		client = RefreshCatalog(self.root / 'archive', {'BY-NLB-book': (
			{'title': 'Book', 'file_ids': ['99']}, {'date': '1800'},
		)})
		counts = self.collect(client)
		self.assertEqual(counts['source_missing'], 1)
		client.fetch_scan_manifest.assert_not_called()
		entry = client.archive.manifests['BY-NLB-book']['files']['1']
		self.assertEqual(entry['sha1'], 'a' * 40)
		self.assertEqual(entry['commons_title'], 'File:Actual old target.pdf')
		self.assertIn('99', entry['metadata_refresh']['current_file_ids'])
		self.assertFalse(entry['scan_identity']['eligible'])
		self.assertNotIn('description', entry)
		self.assertEqual(self.state_path.read_bytes(), before)

	def test_changed_source_location_requires_review(self):
		'''A reused file ID with a changed known path cannot authenticate current source metadata.'''
		before = self.write_state({'BY-NLB-book:1': association(scan_identity={
			'eligible': True, 'context': {'file': {'file_id': '1', 'file_location': '/old.pdf'}},
		})})
		client = RefreshCatalog(self.root / 'archive', {'BY-NLB-book': (
			{'title': 'Book', 'mets_ids': ['viewer']}, {'date': '1800'},
		)}, {'viewer': [{'file_id': '1', 'file_location': '/new.pdf'}]})
		counts = self.collect(client)
		self.assertEqual(counts['source_changed'], 1)
		client.fetch_scan_manifest.assert_not_called()
		entry = client.archive.manifests['BY-NLB-book']['files']['1']
		self.assertFalse(entry['scan_identity']['eligible'])
		self.assertIn('/old.pdf', entry['error'])
		self.assertIn('/new.pdf', entry['error'])
		self.assertEqual(self.state_path.read_bytes(), before)

	def test_manifest_failure_retains_original_binary_identity(self):
		'''An incomplete refresh must not replace the actual target with a newly generated filename.'''
		before = self.write_state({'BY-NLB-book:1': association()})
		client = RefreshCatalog(self.root / 'archive', {'BY-NLB-book': (
			{'title': 'Changed catalog title', 'file_ids': ['1']}, {'date': '1800'},
		)})
		client.fetch_scan_manifest.side_effect = CatalogRetriesExhausted(InvalidCatalogResponse('Bad manifest'), 5)
		counts = self.collect(client)
		self.assertEqual(counts['metadata_failed'], 1)
		entry = client.archive.manifests['BY-NLB-book']['files']['1']
		self.assertEqual(entry['status'], 'metadata_failed')
		self.assertEqual(entry['sha1'], 'a' * 40)
		self.assertEqual(entry['commons_title'], 'File:Actual old target.pdf')
		self.assertEqual(self.state_path.read_bytes(), before)

	def test_record_failure_does_not_block_later_associations(self):
		'''A missing record remains documented while the remaining known records refresh.'''
		before = self.write_state({'BY-NLB-bad:1': association(), 'BY-NLB-good:2': association()})
		client = RefreshCatalog(self.root / 'archive', {})
		client.fetch_record_metadata.side_effect = [
			CatalogRetriesExhausted(InvalidCatalogResponse('Record unavailable'), 5),
			({'title': 'Good book', 'file_ids': ['2']}, {'date': '1800'}),
		]
		counts = self.collect(client)
		self.assertEqual((counts['metadata_failed'], counts['metadata_ready']), (1, 1))
		entry = client.archive.manifests['BY-NLB-bad']['files']['1']
		self.assertEqual(entry['sha1'], 'a' * 40)
		self.assertFalse(entry['scan_identity']['eligible'])
		self.assertEqual(self.state_path.read_bytes(), before)


	def test_permanent_record_and_manifest_failures_remain_pending(self):
		'''A removed record or manifest does not discard historical identity or stop the batch.'''
		import requests
		for stage in ('record', 'manifest'):
			with self.subTest(stage=stage):
				before = self.write_state({'BY-NLB-book:1': association()})
				client = RefreshCatalog(self.root / 'archive', {'BY-NLB-book': ({'title': 'Book', 'file_ids': ['1']}, {'date': '1800'})})
				response = requests.Response()
				response.status_code = 404
				error = requests.HTTPError('Not found', response=response)
				if stage == 'record':
					client.fetch_record_metadata.side_effect = error
				else:
					client.fetch_scan_manifest.side_effect = error
				counts = self.collect(client)
				self.assertEqual(counts['source_missing' if stage == 'record' else 'metadata_failed'], 1)
				self.assertEqual(self.state_path.read_bytes(), before)

	def test_missing_state_is_an_error_without_creating_progress(self):
		'''A misspelled state path must not silently create an empty refresh run.'''
		client = RefreshCatalog(self.root / 'archive', {})
		with self.assertRaises(FileNotFoundError):
			self.collect(client)
		self.assertFalse(self.state_path.exists())
		client.fetch_record_metadata.assert_not_called()


class TestRefreshMetadataCLI(unittest.TestCase):
	'''The new flag applies automatically and never enters the binary upload loop.'''

	def test_refresh_flag_applies_by_default_and_optional_dry_run_only_previews(self):
		'''Explicit automatic behavior needs no separate confirmation or apply command.'''
		for dry_run in (False, True):
			with self.subTest(dry_run=dry_run), tempfile.TemporaryDirectory() as directory:
				root = Path(directory)
				with mock.patch('upload.NlbClient'), mock.patch('upload.CommonsUploader') as uploader, mock.patch('upload.iterate_and_upload') as upload_loop, mock.patch('upload.refresh_uploaded_metadata', return_value={'metadata_ready': 1}), mock.patch('metadata_refresh.apply_uploaded_metadata', return_value={'counts': {'metadata_updated': 1}, 'items': []}) as apply, mock.patch('sys.stdout', io.StringIO()):
					args = ['--refresh-uploaded-metadata', '--state-file', str(root / 'state.json')]
					if dry_run:
						args.append('--dry-run')
					self.assertEqual(upload.main(args), 0)
					self.assertEqual(apply.call_args.kwargs['dry_run'], dry_run)
					uploader.assert_not_called()
					upload_loop.assert_not_called()
					self.assertFalse((root / 'state.json').exists())

	def test_incompatible_filters_and_state_overwriting_report_are_rejected(self):
		'''A whole-upload refresh cannot silently discard a user-supplied selection.'''
		for extras in (['--to-year', '1800'], ['--metadata-only'], ['--max-uploads', '1'], ['--state-file', 'same.json', '--report', 'same.json']):
			with self.subTest(extras=extras), mock.patch('sys.stderr', io.StringIO()), self.assertRaises(SystemExit) as error:
				upload.main(['--refresh-uploaded-metadata', *extras])
			self.assertEqual(error.exception.code, 2)


class TestMetadataReadSession(unittest.TestCase):
	'''Public metadata reads do not depend on the viewer statistics session endpoint.'''

	def test_metadata_reads_never_create_a_statistics_session(self):
		'''METS, access and manifest reads work even while POST /stats/session fails.'''
		session = mock.Mock()
		session.headers = {}
		client = upload.NlbClient(session=session, metadata_reads_only=True)
		client._get_catalog = mock.Mock()
		client._get_catalog.return_value.json.side_effect = [
			{'publicAccess': True}, {'nodes': []}, {'metadata': []},
		]
		self.assertTrue(client.check_doc_access('BY-NLB-one')['publicAccess'])
		self.assertEqual(client.fetch_mets_files('1'), [])
		self.assertEqual(client.fetch_scan_manifest('BY-NLB-one', '1'), {'metadata': []})
		session.post.assert_not_called()
		with self.assertRaisesRegex(ValueError, 'metadata'):
			client.download_pdf_scan('BY-NLB-one', '1', Path('unused.pdf'))


if __name__ == '__main__':
	unittest.main()
