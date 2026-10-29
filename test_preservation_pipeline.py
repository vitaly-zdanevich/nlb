'''Check preservation, dry-run isolation and verification across complete mocked runs.'''

import io
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import upload
from preservation import ArchiveStore
from test_upload import FakeCommonsUploader
from test_pdf_validation import pdf_bytes


class Catalog:
	'''Provide two pages without network traffic, including distinct source metadata.'''

	def __init__(self, archive=None):
		'''Keep request history and an optional durable metadata archive.'''
		self.session = mock.Mock()
		self.archive = archive
		self.pages, self.downloads = [], []

	def fetch_search_page(self, page, **kwargs):
		'''Use a catalog total that exercises advancement to a second page.'''
		self.pages.append(page)
		return {'total_results': 101, 'records': [{'marc_id': f'BY-NLB-{page}', 'file_ids': [str(page)]}] if page < 3 else []}

	def check_doc_access(self, record_id):
		'''Allow both synthetic records.'''
		return {'publicAccess': True}

	def fetch_record_metadata(self, record_id):
		'''Return useful source notes that must survive duplicate detection.'''
		return {'title': record_id, 'fields': [{'name': 'Notes', 'values': ['Unique note ' + record_id]}]}, {'date': '1800'}

	def resolve_record_files(self, entry, metadata):
		'''Expose a single scan on each record.'''
		return [{'file_id': entry['file_ids'][0], 'part_label': ''}]

	def download_pdf_scan(self, record_id, file_id, target):
		'''Write a valid local PDF and record download attempts.'''
		self.downloads.append(file_id)
		return target.write_bytes(pdf_bytes(int(file_id)))


class TestPreservationPipeline(unittest.TestCase):
	'''Observe state and archived artifacts instead of implementation call order.'''

	def test_metadata_only_refreshes_completed_records_without_changing_progress(self):
		'''A backfill archives descriptions and prior file identity without downloading.'''
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			state_path = root / 'state.json'
			state = upload.default_state()
			state['current_page'] = 2
			state['items']['BY-NLB-1:1'] = {
				'status': 'uploaded', 'sha1': 'a' * 40, 'commons_title': 'File:Old.pdf',
				'verification': {'sha1': 'a' * 40},
			}
			upload.save_state(state_path, state)
			before = state_path.read_bytes()
			client = Catalog(ArchiveStore(root / 'archive'))
			with mock.patch('sys.stdout', io.StringIO()), mock.patch('upload.check_commons_by_hash') as lookup:
				counts = upload.iterate_and_upload(client, None, state_path, root / 'scans', metadata_only=True)
			self.assertEqual(counts['metadata_ready'], 2)
			self.assertEqual(client.pages, [1, 2])
			self.assertEqual(client.downloads, [])
			lookup.assert_not_called()
			self.assertEqual(state_path.read_bytes(), before)
			manifest = json.loads(client.archive.snapshots['BY-NLB-1'].read_text())
			self.assertEqual(manifest['files']['1']['sha1'], 'a' * 40)
			self.assertEqual(manifest['files']['1']['commons_title'], 'File:Old.pdf')
			self.assertIn('Unique note BY-NLB-1', (client.archive.root / manifest['files']['1']['description']['path']).read_text())

	def test_legacy_non_pdf_result_rewinds_once_and_remains_pending(self):
		'''HTTP200 error pages marked terminal by older versions are retried after upgrade.'''
		with tempfile.TemporaryDirectory() as directory:
			path = Path(directory) / 'state.json'
			state = upload.default_state()
			state['current_page'] = 21
			state['items']['BY-NLB-old:1'] = {'status': 'not_pdf'}
			upload.save_state(path, state)
			state = upload.load_state(path)
			self.assertEqual(state['retry_from_page'], 1)
			self.assertFalse(upload.is_item_completed(state, 'BY-NLB-old', '1'))
			self.assertEqual(state['items']['BY-NLB-old:1']['status'], 'download_failed')
			state.pop('retry_from_page')
			upload.save_state(path, state)
			self.assertNotIn('retry_from_page', upload.load_state(path))

	def test_cli_reports_pending_work_with_nonzero_exit(self):
		'''Automation receives a failure status and JSON counts for unresolved scans.'''
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			for status in ('rights_review', 'verification_failed', 'metadata_failed', 'download_failed', 'title_conflict'):
				with self.subTest(status=status), mock.patch('upload.NlbClient', return_value=Catalog()), mock.patch('upload.iterate_and_upload', return_value={status: 1}), mock.patch('sys.stdout', io.StringIO()):
					result = upload.main(['--metadata-only', '--state-file', str(root / 'state.json'), '--report', str(root / 'report.json')])
					self.assertEqual(result, 2)
					self.assertEqual(json.loads((root / 'report.json').read_text())['counts'], {status: 1})

	def test_manifest_metadata_keeps_links_and_literal_markup(self):
		'''IIIF statements remain readable with useful URLs and safe wiki characters.'''
		original = {'title': 'Book'}
		metadata = upload.with_viewer_metadata(original, {
			'metadata': [{'label': {'en': ['Credit']}, 'value': '<a href="https://example.org/source">Library</a> [[literal]]'}],
			'rights': 'https://example.org/rights',
		})
		text = upload.build_commons_description(metadata, {'date': '1800'}, 'BY-NLB-one', '1', '')
		self.assertIn('https://example.org/source', text)
		self.assertIn('https://example.org/rights', text)
		self.assertIn(upload.escape_wikitext('[[literal]]'), text)
		self.assertEqual(original, {'title': 'Book'})

	def test_file_info_validates_server_binary_and_records_revision(self):
		'''Verification reads authoritative imageinfo, rejecting absent or malformed files.'''
		uploader = upload.CommonsUploader.__new__(upload.CommonsUploader)
		uploader.site = mock.Mock()
		page = {'title': 'File:Book.pdf', 'pageid': 1, 'lastrevid': 2, 'imageinfo': [{'sha1': 'a' * 40, 'size': 123}]}
		uploader.site.simple_request.return_value.submit.return_value = {'query': {'pages': [page]}}
		info = uploader.file_info('File:Book.pdf')
		self.assertEqual((info['sha1'], info['size_bytes'], info['revision_id']), ('a' * 40, 123, 2))
		for pages in ([], [{}], [{'imageinfo': [{'sha1': 'bad', 'size': 123}]}]):
			with self.subTest(pages=pages):
				uploader.site.simple_request.return_value.submit.return_value = {'query': {'pages': pages}}
				with self.assertRaises(ValueError):
					uploader.file_info('File:Book.pdf')

	def test_dry_run_preserves_existing_state_and_real_run_starts_at_page_one(self):
		'''Previewing two pages cannot make the following real run omit page one.'''
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			state_path = root / 'state.json'
			upload.save_state(state_path, upload.default_state())
			original = state_path.read_bytes()
			client, uploader = Catalog(), FakeCommonsUploader()
			kwargs = dict(client=client, uploader=uploader, state_path=state_path, staging_dir=root / 'scans')
			with mock.patch('upload.check_commons_by_hash', side_effect=lambda path, **kw: (upload.calculate_sha1(path), [])), mock.patch('sys.stdout', io.StringIO()):
				self.assertEqual(upload.iterate_and_upload(**kwargs, dry_run=True)['dry_run_ready'], 2)
				self.assertEqual(state_path.read_bytes(), original)
				client.pages.clear()
				self.assertEqual(upload.iterate_and_upload(**kwargs)['uploaded'], 2)
				self.assertEqual(client.pages, [1, 2])

	def test_duplicates_keep_readable_metadata_and_actual_target(self):
		'''Metadata is archived for both source records even when their scans exist.'''
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			archive = ArchiveStore(root / 'archive')
			client = Catalog(archive)
			with mock.patch('upload.check_commons_by_hash', side_effect=lambda path, **kw: (upload.calculate_sha1(path), ['File:Existing.pdf'])), mock.patch('sys.stdout', io.StringIO()):
				counts = upload.iterate_and_upload(client, FakeCommonsUploader(), root / 'state.json', root / 'scans')
			self.assertEqual(counts['duplicate_sha1'], 2)
			for record_id, path in archive.snapshots.items():
				manifest = json.loads(path.read_text())
				entry, = manifest['files'].values()
				self.assertIn('Unique note ' + record_id, (archive.root / entry['description']['path']).read_text())
				self.assertEqual(entry['commons_title'], 'File:Existing.pdf')
				self.assertEqual(len(entry['sha256']), 64)

	def test_unknown_rights_and_title_conflicts_stay_pending(self):
		'''Neither a historical title year nor an occupied destination permits upload.'''
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			for metadata, exists, expected in (({}, False, 'rights_review'), ({'date': '1800'}, True, 'title_conflict')):
				with self.subTest(expected=expected):
					state, client, uploader = upload.default_state(), Catalog(), FakeCommonsUploader()
					uploader.title_exists = mock.Mock(return_value=exists)
					with mock.patch('upload.check_commons_by_hash', side_effect=lambda path, **kw: (upload.calculate_sha1(path), [])), mock.patch('sys.stdout', io.StringIO()):
						status = upload.process_single_scan(client, uploader, state, root / 'state.json', root / 'scans', 'BY-NLB-one', '1', '', {'title': 'History of 1812'}, metadata)
					self.assertEqual(status, expected)
					self.assertEqual(state['retry_from_page'], 1)
					self.assertEqual(uploader.uploads, [])
					self.assertFalse(upload.is_item_completed(state, 'BY-NLB-one', '1'))

	def test_mismatched_published_bytes_remain_pending(self):
		'''A successful API upload alone cannot mark a different remote binary complete.'''
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			state, client, uploader = upload.default_state(), Catalog(), FakeCommonsUploader()
			uploader.file_info = mock.Mock(return_value={'sha1': '0' * 40, 'size_bytes': 1})
			with mock.patch('upload.check_commons_by_hash', side_effect=lambda path, **kw: (upload.calculate_sha1(path), [])), mock.patch('sys.stdout', io.StringIO()):
				status = upload.process_single_scan(client, uploader, state, root / 'state.json', root / 'scans', 'BY-NLB-one', '1', '', {'title': 'Book'}, {'date': '1800'})
			self.assertEqual(status, 'verification_failed')
			self.assertFalse(upload.is_item_completed(state, 'BY-NLB-one', '1'))
			self.assertEqual(list((root / 'scans').iterdir()), [])

	def test_upload_duplicate_warning_uses_file_namespace_and_canonical_title(self):
		'''A concurrent duplicate warning uses bare filenames, unlike allimages results.'''
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			state, client, uploader = upload.default_state(), Catalog(), FakeCommonsUploader()
			payload = pdf_bytes()
			uploader.upload = mock.Mock(return_value={'status': 'duplicate_sha1', 'duplicates': ['Old_scan.pdf']})
			uploader.file_info = mock.Mock(return_value={
				'sha1': hashlib.sha1(payload).hexdigest(), 'size_bytes': len(payload),
				'commons_title': 'File:Old scan.pdf', 'page_id': 123, 'revision_id': 456,
			})
			with mock.patch('upload.check_commons_by_hash', side_effect=lambda path, **kw: (upload.calculate_sha1(path), [])), mock.patch('sys.stdout', io.StringIO()):
				status = upload.process_single_scan(client, uploader, state, root / 'state.json', root / 'scans', 'BY-NLB-one', '1', '', {'title': 'Book'}, {'date': '1800'})
			self.assertEqual(status, 'duplicate_sha1')
			uploader.file_info.assert_called_once_with('File:Old_scan.pdf')
			entry = state['items']['BY-NLB-one:1']
			self.assertEqual(entry['commons_title'], 'File:Old scan.pdf')
			self.assertEqual(entry['duplicates'], ['File:Old scan.pdf'])
			self.assertEqual(entry['verification']['revision_id'], 456)
