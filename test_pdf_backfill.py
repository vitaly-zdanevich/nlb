'''Test local PDF recovery without changing Commons or historical upload state.'''

import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from pdf_backfill import ArchiveIntegrityError, backfill_pdfs
from preservation import ArchiveStore, atomic_json
from test_pdf_validation import pdf_bytes
from test_viewer_api import http_error
from viewer_api import SourceAuthorizationError
import upload


class BackfillCatalog:
	'''Offer a public original through observable, offline catalog and transfer methods.'''

	def __init__(self, root, body):
		'''Keep all requests mocked and all persistent artifacts inside the test archive.'''
		self.archive = ArchiveStore(root, keep_pdfs=True)
		self.body = body
		self.fetch_record_metadata = mock.Mock(return_value=({'title': 'Book', 'file_ids': ['1', '2']}, {'date': '1800'}))
		self.check_doc_access = mock.Mock(return_value={'publicAccess': True})
		self.resolve_record_files = mock.Mock(return_value=[{'file_id': '1'}, {'file_id': '2'}])
		self.fetch_scan_manifest = mock.Mock(return_value={})
		self.download_pdf_scan = mock.Mock(side_effect=lambda record, file_id, path: path.write_bytes(self.body))


class TestPDFBackfill(unittest.TestCase):
	'''Check preserved bytes, reports and restart behavior through the public API.'''

	def setUp(self):
		'''Create a valid one-page PDF and intentionally formatted historical state.'''
		temporary = tempfile.TemporaryDirectory()
		self.addCleanup(temporary.cleanup)
		self.root = Path(temporary.name)
		self.body = pdf_bytes(1)
		self.publication = {'status': 'uploaded', 'sha1': hashlib.sha1(self.body).hexdigest(),
			'size_bytes': len(self.body), 'commons_title': 'File:Published.pdf'}
		self.state = self.root / 'state.json'
		self.save_items({'BY-NLB-one:1': self.publication})
		self.client = BackfillCatalog(self.root / 'archive', self.body)
		self.addCleanup(mock.patch.stopall)
		mock.patch('sys.stdout', io.StringIO()).start()

	def save_items(self, items):
		'''Keep exact input bytes to detect incidental state changes.'''
		self.state.write_text(json.dumps({'version': 1, 'items': items}) + '\n\n')
		self.before = self.state.read_bytes()

	def run_backfill(self, **options):
		'''Run without permitting Commons access and check that state is untouched.'''
		with mock.patch('upload.CommonsUploader') as uploader, mock.patch('upload.check_commons_by_hash') as lookup:
			report = backfill_pdfs(self.client, self.state, self.root / 'staging', license_template=upload.DEFAULT_LICENSE, **options)
		self.assertEqual(self.state.read_bytes(), self.before)
		uploader.assert_not_called()
		lookup.assert_not_called()
		return report

	def test_backfill_restart_and_duplicate_associations_reuse_verified_original(self):
		'''Legacy entries without SHA-256 share one download and reuse its durable index.'''
		self.save_items({'BY-NLB-one:1': self.publication, 'BY-NLB-two:2': self.publication})
		report = self.run_backfill()
		self.assertEqual((report['counts']['archived'], report['counts']['already_archived']), (1, 1))
		self.client.download_pdf_scan.assert_called_once()
		self.client.fetch_record_metadata.assert_called_once_with('BY-NLB-one')
		pdf, = self.client.archive.root.glob('objects/*.pdf')
		self.assertEqual(pdf.read_bytes(), self.body)
		entry = self.client.archive.manifests['BY-NLB-one']['files']['1']
		self.assertEqual(entry['pdf_path'], entry['publication']['pdf_path'])
		self.assertTrue(report['complete'])
		self.client = BackfillCatalog(self.client.archive.root, self.body)
		self.assertEqual(self.run_backfill()['counts']['already_archived'], 2)
		self.client.fetch_record_metadata.assert_not_called()
		self.client.download_pdf_scan.assert_not_called()
		self.assertEqual(list((self.root / 'staging').glob('*.pdf')), [])

	def test_preview_is_local_and_corrupt_objects_are_never_replaced(self):
		'''Preview inventories missing PDFs and both modes preserve corrupt evidence.'''
		self.assertEqual(self.run_backfill(dry_run=True)['counts']['missing_pdf'], 1)
		self.client.fetch_record_metadata.assert_not_called()
		self.assertFalse((self.client.archive.root / 'pdf-backfill/index.json').exists())
		self.run_backfill()
		pdf, = self.client.archive.root.glob('objects/*.pdf')
		pdf.write_bytes(b'corrupt evidence')
		self.client = BackfillCatalog(self.client.archive.root, self.body)
		for dry_run in (True, False):
			self.assertEqual(self.run_backfill(dry_run=dry_run)['counts']['archive_failed'], 1)
		self.assertEqual(pdf.read_bytes(), b'corrupt evidence')
		self.client.fetch_record_metadata.assert_not_called()

	def test_known_sha256_reuses_an_original_without_a_backfill_index(self):
		'''Originals retained by ordinary upload runs need no further source reads.'''
		sha256 = hashlib.sha256(self.body).hexdigest()
		self.save_items({'BY-NLB-one:1': {**self.publication, 'sha256': sha256}})
		path = self.client.archive.root / 'objects' / (sha256 + '.pdf')
		path.parent.mkdir(parents=True)
		path.write_bytes(self.body)
		self.assertEqual(self.run_backfill()['counts']['already_archived'], 1)
		self.client.fetch_record_metadata.assert_not_called()

	def test_changed_original_is_quarantined_and_old_identity_remains(self):
		'''A valid different PDF is evidence, not a replacement for the published original.'''
		self.client.body = pdf_bytes(2)
		self.assertEqual(self.run_backfill()['counts']['source_changed'], 1)
		pdf, = self.client.archive.root.glob('quarantine/*.pdf')
		self.assertEqual(pdf.read_bytes(), self.client.body)
		entry = self.client.archive.manifests['BY-NLB-one']['files']['1']
		self.assertEqual(entry['publication'], self.publication)
		self.assertEqual(entry['sha1'], self.publication['sha1'])
		self.assertNotIn('pdf_path', entry)

	def test_access_rights_scope_and_source_identity_gate_all_downloads(self):
		'''Backfill cannot bypass access, publication rights or catalog-to-scan evidence.'''
		for status in ('restricted', 'rights_review', 'metadata_review', 'source_missing', 'source_changed'):
			with self.subTest(status=status):
				self.client = BackfillCatalog(self.root / status, self.body)
				self.save_items({'BY-NLB-one:1': self.publication})
				if status == 'restricted':
					self.client.check_doc_access.return_value = {'publicAccess': False}
				elif status == 'rights_review':
					self.client.fetch_record_metadata.return_value = ({'title': 'Book'}, {})
				elif status == 'source_missing':
					self.client.resolve_record_files.return_value = []
				elif status == 'source_changed':
					self.save_items({'BY-NLB-one:1': {**self.publication, 'scan_identity': {'context': {'file': {'file_location': 'old'}}}}})
					self.client.resolve_record_files.return_value = [{'file_id': '1', 'file_location': 'new'}]
				with mock.patch('pdf_backfill.assess_scan_identity', return_value={'eligible': status != 'metadata_review', 'reason': 'Volume mismatch'}):
					self.assertEqual(self.run_backfill()['counts'][status], 1)
				self.client.download_pdf_scan.assert_not_called()

	def test_authorization_denials_stop_with_incomplete_checkpoint(self):
		'''A denial at any source stage stops before later records and preserves state.'''
		for stage in ('fetch_record_metadata', 'check_doc_access', 'fetch_scan_manifest', 'download_pdf_scan'):
			for status in (401, 403):
				with self.subTest(stage=stage, status=status):
					self.client = BackfillCatalog(self.root / (stage + str(status)), self.body)
					self.save_items({'BY-NLB-one:1': self.publication, 'BY-NLB-two:2': self.publication})
					getattr(self.client, stage).side_effect = http_error(status)
					with self.assertRaises(SourceAuthorizationError):
						self.run_backfill()
					self.assertEqual(self.state.read_bytes(), self.before)
					self.client.fetch_record_metadata.assert_called_once_with('BY-NLB-one')
					report = json.loads((self.client.archive.root / 'pdf-backfill/report.json').read_text())
					self.assertFalse(report['complete'])
					self.assertEqual(sum(report['counts'].values()), 1)

	def test_archive_write_failure_retains_complete_staging_pdf_and_sidecar(self):
		'''Local persistence failure must leave the completed original recoverable.'''
		self.client.archive.preserve_pdf = mock.Mock(side_effect=OSError('Disk full'))
		with self.assertRaisesRegex(OSError, 'Disk full'):
			self.run_backfill()
		pdf, = (self.root / 'staging').glob('*.pdf')
		self.assertEqual(pdf.read_bytes(), self.body)
		self.assertEqual(json.loads(pdf.with_suffix('.recovery.json').read_text())['publication'], self.publication)
		self.assertEqual(self.state.read_bytes(), self.before)

	def test_malformed_index_and_escaping_paths_fail_before_collection(self):
		'''Malformed archive history is not replaced with an empty index or report.'''
		path = self.client.archive.root / 'pdf-backfill/index.json'
		for document in ({'version': 1, 'objects': []}, {'version': 1, 'objects': {
			self.publication['sha1'] + ':' + str(len(self.body)): {**self.publication, 'sha256': 'a' * 64, 'pdf_path': '../outside.pdf'},
		}}):
			atomic_json(path, document)
			before = path.read_bytes()
			with self.assertRaises(ArchiveIntegrityError):
				self.run_backfill()
			self.assertEqual(path.read_bytes(), before)
			self.client.fetch_record_metadata.assert_not_called()
			self.assertFalse(path.with_name('report.json').exists())

	def test_successful_retry_uses_corrected_manifest(self):
		'''The final check uses the context of the successful download retry.'''
		self.client.fetch_scan_manifest.return_value = {'sequences': [{'canvases': [{}, {}]}]}
		def download(record, file_id, path):
			'''Model a retry that replaces the initial incorrect IIIF page count.'''
			self.client._scan_manifests = {(record, file_id): {'sequences': [{'canvases': [{}]}]}}
			return path.write_bytes(self.body)
		self.client.download_pdf_scan.side_effect = download
		report = self.run_backfill()
		self.assertEqual(report['counts']['archived'], 1)
		self.assertEqual(report['items'][0]['pdf_validation']['manifest_pages'], 1)

	def test_nested_publication_does_not_mix_flat_fields_from_failed_work(self):
		'''Only durable publication evidence can select an original to archive.'''
		self.save_items({'BY-NLB-one:1': {'status': 'metadata_failed', 'publication': self.publication,
			'sha1': 'b' * 40, 'size_bytes': 999, 'commons_title': 'File:Failed.pdf'},
			'BY-NLB-one:2': {'status': 'upload_failed', 'sha1': 'c' * 40, 'size_bytes': 456, 'commons_title': 'File:Pending.pdf'}})
		report = self.run_backfill()
		self.assertEqual((report['counts']['archived'], report['counts']['skipped']), (1, 1))
		self.client.download_pdf_scan.assert_called_once()

	def test_cli_implies_pdf_retention_and_never_initializes_commons(self):
		'''The command writes an alternate report without entering the upload loop.'''
		report_path = self.root / 'summary.json'
		with mock.patch('upload.NlbClient', return_value=self.client), mock.patch('upload.CommonsUploader') as uploader, mock.patch('upload.iterate_and_upload') as ordinary:
			result = upload.main(['--backfill-pdfs', '--state-file', str(self.state), '--archive-dir', str(self.client.archive.root), '--report', str(report_path)])
		self.assertEqual(result, 0)
		self.assertEqual(json.loads(report_path.read_text())['counts']['archived'], 1)
		self.assertTrue(self.client.archive.keep_pdfs)
		self.assertEqual(self.state.read_bytes(), self.before)
		uploader.assert_not_called()
		ordinary.assert_not_called()

	def test_cli_preview_is_local_and_corruption_sets_failure_exit(self):
		'''Local inventory makes no source calls and signals damaged archive evidence.'''
		arguments = ['--backfill-pdfs', '--dry-run', '--state-file', str(self.state), '--archive-dir', str(self.client.archive.root)]
		with mock.patch('upload.NlbClient', return_value=self.client):
			self.assertEqual(upload.main(arguments), 0)
		self.client.fetch_record_metadata.assert_not_called()
		self.client.download_pdf_scan.assert_not_called()
		self.run_backfill()
		pdf, = self.client.archive.root.glob('objects/*.pdf')
		pdf.write_bytes(b'corrupt evidence')
		with mock.patch('upload.NlbClient', return_value=self.client):
			self.assertEqual(upload.main(arguments), 2)

	def test_cli_rejects_unused_flag_invalid_modes_and_index_overwrite(self):
		'''Unsupported arguments cannot silently change scope or replace preservation evidence.'''
		for flags in (['--refresh-metadata'], ['--backfill-pdfs', '--continue'], ['--backfill-pdfs', '--metadata-only'],
			['--backfill-pdfs', '--refresh-uploaded-metadata'], ['--backfill-pdfs', '--to-year', '1800'],
			['--backfill-pdfs', '--report', str(self.client.archive.root / 'pdf-backfill/index.json')]):
			with self.subTest(flags=flags), mock.patch('sys.stderr', io.StringIO()), mock.patch('upload.NlbClient') as client:
				with self.assertRaises(SystemExit) as raised:
					upload.main([*flags, '--state-file', str(self.state), '--archive-dir', str(self.client.archive.root)])
				self.assertEqual(raised.exception.code, 2)
				client.assert_not_called()
