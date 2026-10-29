'''Keep a stale IIIF rendering from associating a different PDF with a scan ID.'''

import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import upload
from test_pdf_validation import pdf_bytes


class TestManifestIdentity(unittest.TestCase):
	'''Exercise the real downloader with valid PDF bytes and mismatched viewer links.'''

	def test_other_file_rendering_is_retried_without_downloading_its_bytes(self):
		'''Fresh and cached manifests must identify the requested scan before any events.'''
		for cached in (False, True):
			with self.subTest(cached=cached), tempfile.TemporaryDirectory() as directory:
				session = mock.MagicMock()
				session.headers = {}
				client = upload.NlbClient(session=session)
				client._viewer_session_initialized = True
				manifest = {'rendering': [{'id': '/iiif/file/2002/bytes?attachmentName=other', 'format': 'application/pdf'}]}
				response = mock.MagicMock()
				response.json.return_value = manifest
				# This is a valid PDF: structural validation alone cannot detect identity.
				response.__enter__.return_value = response
				response.headers = {}
				response.iter_content.return_value = [pdf_bytes()]
				session.get.return_value = response
				if cached:
					client._scan_manifests = {('BY-NLB-one', '1001'): manifest}
				target = Path(directory) / 'scan.pdf'
				with mock.patch('upload.time.sleep'), mock.patch('sys.stdout', io.StringIO()):
					with self.assertRaises(upload.DownloadRetriesExhausted) as raised:
						client.download_pdf_scan('BY-NLB-one', '1001', target)
				self.assertIsInstance(raised.exception.last_error, upload.InvalidPDFError)
				self.assertIn('2002', str(raised.exception.last_error))
				self.assertFalse(target.exists())
				self.assertTrue(all(call.args[0].endswith('/iiif/manifest') for call in session.get.call_args_list))

	def test_matching_relative_and_absolute_renderings_keep_attachment_parameters(self):
		'''The observed NLB bytes routes keep working with their original query string.'''
		for link in ('/iiif/file/1001/bytes?attachmentName=scan_1', upload.VIEWER_BASE_URL + '/iiif/file/1001/bytes?attachmentName=scan_1'):
			with self.subTest(link=link), tempfile.TemporaryDirectory() as directory:
				session = mock.MagicMock()
				session.headers = {}
				client = upload.NlbClient(session=session)
				client._viewer_session_initialized = True
				client._scan_manifests = {('BY-NLB-one', '1001'): {'rendering': [{'@id': link, 'format': 'application/pdf'}]}}
				stream = mock.MagicMock()
				stream.__enter__.return_value = stream
				payload = pdf_bytes()
				stream.headers = {'Content-Length': str(len(payload))}
				stream.iter_content.return_value = [payload]
				session.get.side_effect = [mock.Mock(), mock.Mock(), stream]
				target = Path(directory) / 'scan.pdf'
				with mock.patch('sys.stdout', io.StringIO()):
					self.assertEqual(client.download_pdf_scan('BY-NLB-one', '1001', target), len(payload))
				self.assertEqual(target.read_bytes(), payload)
				self.assertEqual(session.get.call_args.args[0], upload.VIEWER_BASE_URL + '/iiif/file/1001/bytes?attachmentName=scan_1')


if __name__ == '__main__':
	unittest.main()
