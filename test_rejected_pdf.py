'''Keep rejected original scans for diagnosis and bound identical invalid downloads.'''

import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import requests

from preservation import ArchiveStore, ArchiveWriteError
from test_pdf_validation import pdf_bytes
import upload


class TestRejectedPDF(unittest.TestCase):
	'''Exercise completed invalid PDFs separately from incomplete or non-PDF responses.'''

	def setUp(self):
		'''Create real archive files and mocked HTTP responses without network traffic.'''
		self.directory = tempfile.TemporaryDirectory()
		self.addCleanup(self.directory.cleanup)
		self.root = Path(self.directory.name)
		self.archive = ArchiveStore(self.root / 'archive')
		self.archive.save_scan('BY-NLB-one', '1', 'Bibliographic description', 'File:Scan.pdf')
		self.session = mock.MagicMock()
		self.session.headers = {}
		self.client = upload.NlbClient(session=self.session, archive=self.archive)
		self.target = self.root / 'scan.pdf'
		self.payload = pdf_bytes()
		self.invalid = self.payload[:-10]
		self.sleep = self.enterContext(mock.patch('upload.time.sleep'))
		self.enterContext(mock.patch('sys.stdout', io.StringIO()))

	def responses(self, payload, manifest=None, *, advertised_length=None):
		'''Build one complete attempt, with independently controllable manifest and length.'''
		response = requests.Response()
		response.status_code = 200
		response.url = upload.VIEWER_BASE_URL + '/iiif/manifest?marcID=BY-NLB-one&fileId=1'
		response._content = json.dumps(manifest or {'rendering': []}).encode()
		stream = mock.MagicMock()
		stream.__enter__.return_value = stream
		stream.headers = {'Content-Length': str(len(payload) if advertised_length is None else advertised_length)}
		stream.iter_content.return_value = [payload[:12], payload[12:]]
		return [response, mock.MagicMock(), mock.MagicMock(), stream]

	def download_failure(self):
		'''Return the resumable failure without uploading or retaining staging files.'''
		with self.assertRaises(upload.DownloadRetriesExhausted) as raised:
			self.client.download_pdf_scan('BY-NLB-one', '1', self.target)
		self.assertFalse(self.target.exists())
		return raised.exception

	def test_identical_complete_invalid_pdf_stops_after_second_copy_and_preserves_evidence(self):
		'''Keep exact originals and provenance even without --keep-pdfs or valid structure.'''
		manifest = {'rendering': [], 'label': 'Source bibliographic title'}
		self.session.get.side_effect = self.responses(self.invalid, manifest) * 5
		error = self.download_failure()
		self.assertEqual(error.attempts, 2)
		self.assertEqual(self.session.get.call_count, 8)
		self.sleep.assert_called_once_with(5)
		entry = json.loads(self.archive.snapshots['BY-NLB-one'].read_text())['files']['1']
		self.assertNotIn('pdf_path', entry)
		self.assertNotIn('sha256', entry)
		self.assertEqual(len(entry['rejected_downloads']), 2)
		for rejected in entry['rejected_downloads']:
			self.assertEqual(rejected['sha256'], hashlib.sha256(self.invalid).hexdigest())
			self.assertEqual(rejected['size_bytes'], len(self.invalid))
			self.assertTrue(rejected['pdf_path'].startswith('quarantine/'))
			self.assertEqual((self.archive.root / rejected['pdf_path']).read_bytes(), self.invalid)
			self.assertEqual(rejected['record_id'], 'BY-NLB-one')
			self.assertEqual(rejected['file_id'], '1')
			self.assertEqual(rejected['source_url'], upload.VIEWER_BASE_URL + '/iiif/file/1/bytes')
			self.assertEqual(json.loads((self.archive.root / rejected['manifest']['path']).read_text()), manifest)
			self.assertEqual(rejected['error_type'], 'InvalidPDFError')
			self.assertIn('EOF', rejected['error'])
			self.assertTrue(rejected['captured_at'])
		self.assertEqual(len(list((self.archive.root / 'quarantine').glob('*.pdf'))), 1)
		self.assertEqual(error.last_error.rejected_pdf, entry['rejected_downloads'][-1])

	def test_changed_validation_context_or_pdf_receives_remaining_attempts(self):
		'''A new page count or new PDF body may recover, so it keeps the five-attempt budget.'''
		for changed_context in (False, True):
			with self.subTest(changed_context=changed_context):
				attempts = []
				for number in range(5):
					manifest = {'rendering': [], 'items': [{'type': 'Canvas'}] * (number + 2)}
					payload = self.payload if changed_context else self.invalid + str(number).encode()
					attempts.extend(self.responses(payload, manifest if changed_context else None))
				self.session.get.side_effect = attempts
				self.assertEqual(self.download_failure().attempts, 5)

	def test_descriptive_manifest_changes_do_not_hide_repeated_invalid_bytes(self):
		'''Only the canvas count used by validation affects the repeat comparison.'''
		attempts = []
		for number in range(5):
			attempts.extend(self.responses(self.payload, {'rendering': [], 'label': str(number), 'sequences': [{'canvases': [{}, {}]}]}))
		self.session.get.side_effect = attempts
		error = self.download_failure()
		self.assertEqual(error.attempts, 2)
		self.assertEqual(error.last_error.rejected_pdf['manifest_pages'], 2)

	def test_corrected_manifest_allows_identical_pdf_to_succeed(self):
		'''Re-fetch the manifest rather than treating a previous page mismatch as permanent.'''
		self.session.get.side_effect = [
			*self.responses(self.payload, {'items': [{'type': 'Canvas'}] * 2}),
			*self.responses(self.payload, {'items': [{'type': 'Canvas'}]}),
		]
		self.assertEqual(self.client.download_pdf_scan('BY-NLB-one', '1', self.target), len(self.payload))
		self.assertEqual(self.target.read_bytes(), self.payload)
		self.assertEqual(self.client.pdf_validation[('BY-NLB-one', '1')]['manifest_pages'], 1)

	def test_partial_and_html_responses_never_become_stable_pdf_evidence(self):
		'''Do not hash, quarantine or stop retries early for incomplete and login responses.'''
		for payload, advertised_length in ((self.invalid, len(self.payload)), (b'<html>Log in</html>', None)):
			with self.subTest(payload=payload):
				self.session.get.side_effect = self.responses(payload, advertised_length=advertised_length) * 5
				error = self.download_failure()
				self.assertEqual(error.attempts, 5)
				self.assertFalse(hasattr(error.last_error, 'rejected_pdf'))
				self.assertFalse((self.archive.root / 'quarantine').exists())

	def test_quarantine_failure_does_not_discard_original_or_claim_evidence(self):
		'''An archive write failure leaves the staging original available for diagnosis.'''
		self.session.get.side_effect = self.responses(self.invalid)
		with mock.patch('preservation.os.fsync', side_effect=OSError('disk full')):
			# The prefetched manifest avoids injecting the failure into source capture.
			self.client._scan_manifests = {('BY-NLB-one', '1'): {'rendering': []}}
			self.session.get.side_effect = self.responses(self.invalid)[1:]
			with self.assertRaisesRegex(OSError, 'disk full'):
				self.client.download_pdf_scan('BY-NLB-one', '1', self.target)
		self.assertEqual(self.target.read_bytes(), self.invalid)
		self.assertFalse(list(self.archive.root.rglob('*.new')))
		entry = json.loads(self.archive.snapshots['BY-NLB-one'].read_text())['files']['1']
		self.assertNotIn('rejected_downloads', entry)


class TestQuarantinePipeline(unittest.TestCase):
	'''Do not delete the only original when quarantine storage fails during a run.'''

	def test_failed_archive_write_retains_staging_pdf(self):
		'''The pipeline propagates disk failure and leaves exact bytes for recovery.'''
		from test_preservation_pipeline import Catalog
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			client = Catalog()
			payload = b'%PDF-original requiring diagnosis'
			def fail_archive(record, file_id, path):
				'''Simulate a complete stream followed by an unavailable quarantine disk.'''
				path.write_bytes(payload)
				raise ArchiveWriteError('disk full')
			client.download_pdf_scan = fail_archive
			with self.assertRaisesRegex(OSError, 'disk full'):
				upload.process_single_scan(client, mock.Mock(), upload.default_state(), root / 'state.json', root / 'scans', 'BY-NLB-one', '1', '', {'title': 'Book'}, {'date': '1800'})
			path, = (root / 'scans').glob('*.pdf')
			self.assertEqual(path.read_bytes(), payload)
			recovery = json.loads(path.with_suffix('.recovery.json').read_text())
			self.assertEqual((recovery['marc_id'], recovery['file_id']), ('BY-NLB-one', '1'))


if __name__ == '__main__':
	unittest.main()
