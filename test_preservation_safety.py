'''Keep source identity and original bytes across stale metadata and disk failures.'''

import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import requests

from catalog_requests import CatalogRetriesExhausted, InvalidCatalogResponse
from catalog_validation import validate_manifest
from preservation import ArchiveStore, atomic_json
from test_pdf_validation import pdf_bytes
from test_preservation_pipeline import Catalog
from test_upload import FakeCommonsUploader
import upload


def manifest_response(payload: dict) -> requests.Response:
	'''Provide exact source bytes while keeping requests and identity validation real.'''
	response = requests.Response()
	response.status_code = 200
	response.url = upload.VIEWER_BASE_URL + '/iiif/manifest?marcID=BY-NLB-one&fileId=1'
	response.encoding = 'utf-8'
	response._content = json.dumps(payload).encode('utf-8')
	response._content_consumed = True
	return response


class TestSharedManifestIdentity(unittest.TestCase):
	'''Do not let metadata-only refresh bypass scan identity checks in the downloader.'''

	def test_explicit_record_and_file_ids_must_agree_in_every_identity_location(self):
		'''Reject self links, direct identities and both NLB PDF rendering routes.'''
		invalid = (
			{'id': 'https://[invalid/iiif/manifest?fileId=1'},
			{'id': '/iiif/manifest?marcID=BY-NLB-other&fileId=1'},
			{'@id': '/iiif/manifest?markID=BY-NLB-one&fileID=2'},
			{'id': '/iiif/manifest?marcID=BY-NLB-one&fileId=1&fileId=2'},
			{'marcID': 'BY-NLB-other'}, {'markID': 'BY-NLB-other'},
			{'fileId': 2}, {'fileID': '2'},
			{'rendering': [{'id': '/iiif/file/2/bytes?attachmentName=other', 'format': 'application/pdf'}]},
			{'rendering': [{'id': '/iiif/file/2/bytes', 'format': 'application/octet-stream', 'type': None}]},
			{'rendering': [{'@id': '/iiif/file/2/pages', 'type': 'Range', 'format': 'application/pdf'}]},
		)
		for fields in invalid:
			with self.subTest(fields=fields), self.assertRaises(InvalidCatalogResponse):
				validate_manifest(manifest_response({'rendering': [], **fields}), 'BY-NLB-one', '1')

	def test_real_nlb_shape_and_related_links_do_not_require_a_self_identifier(self):
		'''Empty manifest IDs and generic canvases are valid; linked records are not self IDs.'''
		payload = {
			'id': '', 'items': [{'id': 'canvas1', 'label': {'ru': ['1']}}],
			'rendering': [
				{'id': '/iiif/file/1/bytes?attachmentName=scan', 'format': 'application/pdf', 'type': None},
				{'id': '/iiif/file/1/pages', 'format': 'application/pdf', 'type': 'Range'},
			],
			'partOf': [{'id': '/iiif/manifest?marcID=BY-NLB-parent&fileId=999'}],
		}
		validate_manifest(manifest_response(payload), 'BY-NLB-one', '1')
		validate_manifest(manifest_response({'rendering': [], 'marcID': 'BY-NLB-one', 'fileId': 1}), 'BY-NLB-one', '1')

	def test_wrong_file_metadata_never_becomes_eligible_for_commons_refresh(self):
		'''Use the real HTTP validation path, then verify no Commons page is even read.'''
		from metadata_refresh import apply_uploaded_metadata
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			state_path = root / 'state.json'
			previous = {'status': 'uploaded', 'sha1': 'a' * 40, 'size_bytes': 123, 'commons_title': 'File:Actual.pdf'}
			state_path.write_text(json.dumps({'items': {'BY-NLB-one:1': previous}}))
			original_state = state_path.read_bytes()
			session = mock.Mock(headers={})
			session.get.side_effect = lambda *args, **kwargs: manifest_response({
				'rendering': [{'id': '/iiif/file/2/bytes', 'format': 'application/pdf'}],
				'metadata': [{'label': 'Scan note', 'value': 'WRONG FILE DESCRIPTION'}],
			})
			archive = ArchiveStore(root / 'archive')
			client = upload.NlbClient(session=session, archive=archive, metadata_reads_only=True)
			client.fetch_record_metadata = mock.Mock(return_value=({'title': 'Book', 'file_ids': ['1']}, {'date': '1800'}))
			client.check_doc_access = mock.Mock(return_value={'publicAccess': True})
			reader, writer = mock.Mock(), mock.Mock()
			with mock.patch('catalog_requests.time.sleep'), mock.patch('sys.stdout', io.StringIO()):
				counts = upload.refresh_uploaded_metadata(client, state_path, root / 'staging')
				report = apply_uploaded_metadata(archive.root, archive.snapshots.values(), fetch_page=reader, edit_page=writer)
			self.assertEqual((counts['metadata_ready'], counts['metadata_failed']), (0, 1))
			self.assertEqual(report['counts']['metadata_updated'], 0)
			reader.assert_not_called()
			writer.assert_not_called()
			self.assertEqual(session.get.call_count, 5)
			self.assertEqual(state_path.read_bytes(), original_state)
			item = archive.manifests['BY-NLB-one']['files']['1']
			self.assertEqual(item['commons_title'], previous['commons_title'])
			self.assertIn('WRONG FILE DESCRIPTION', (archive.root / archive.manifests['BY-NLB-one']['sources'][0]['path']).read_text())
			self.assertNotIn('WRONG FILE DESCRIPTION', (archive.root / item['description']['path']).read_text())


class TestPreservationFailureRecovery(unittest.TestCase):
	'''Keep earlier publication evidence and completed downloads recoverable after failures.'''

	def test_manifest_outage_keeps_historical_identity_and_recovers_without_redownload(self):
		'''Repeated outages keep their original publication status until a fresh read succeeds.'''
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			archive = ArchiveStore(root / 'archive')
			client = Catalog(archive)
			client.fetch_scan_manifest = mock.Mock(side_effect=CatalogRetriesExhausted(InvalidCatalogResponse('Unavailable'), 5))
			previous = {
				'status': 'uploaded', 'commons_title': 'File:Original title.pdf',
				'sha1': 'a' * 40, 'sha256': 'b' * 64, 'size_bytes': 123,
				'verification': {'sha1': 'a' * 40, 'page_id': 10}, 'duplicates': ['File:Original title.pdf'],
			}
			state = upload.default_state()
			state['items']['BY-NLB-one:1'] = copy.deepcopy(previous)
			uploader = mock.Mock()
			arguments = (client, uploader, state, root / 'state.json', root / 'scans', 'BY-NLB-one', '1', '', {'title': 'Fresh title'}, {'date': '1800'})
			with mock.patch('sys.stdout', io.StringIO()):
				for _ in range(2):
					self.assertEqual(upload.process_single_scan(*arguments), 'metadata_failed')
					item = json.loads((root / 'state.json').read_text())['items']['BY-NLB-one:1']
					self.assertEqual(item['previous_status'], 'uploaded')
					for field in ('commons_title', 'sha1', 'sha256', 'size_bytes', 'verification', 'duplicates'):
						self.assertEqual(item[field], previous[field])
				client.fetch_scan_manifest.side_effect = None
				client.fetch_scan_manifest.return_value = {'rendering': []}
				self.assertEqual(upload.process_single_scan(*arguments), 'completed')
			item = state['items']['BY-NLB-one:1']
			self.assertEqual(item['status'], 'uploaded')
			self.assertNotIn('error', item)
			self.assertNotIn('previous_status', item)
			self.assertEqual(client.downloads, [])
			uploader.upload.assert_not_called()
			uploader.title_exists.assert_not_called()

	def test_manifest_outage_keeps_unfinished_publication_verifiable_without_redownload(self):
		'''A metadata failure cannot hide the pending verification of a previous upload.'''
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			client = Catalog(ArchiveStore(root / 'archive'))
			client.fetch_scan_manifest = mock.Mock(side_effect=CatalogRetriesExhausted(InvalidCatalogResponse('Unavailable'), 5))
			state = upload.default_state()
			state['items']['BY-NLB-one:1'] = {
				'status': 'verification_failed', 'commons_title': 'File:Published.pdf',
				'sha1': 'a' * 40, 'sha256': 'b' * 64, 'size_bytes': 123,
			}
			uploader = mock.Mock()
			uploader.file_info.return_value = {'commons_title': 'File:Published.pdf', 'sha1': 'a' * 40, 'size_bytes': 123, 'page_id': 10}
			arguments = (client, uploader, state, root / 'state.json', root / 'scans', 'BY-NLB-one', '1', '', {'title': 'Book'}, {'date': '1800'})
			with mock.patch('sys.stdout', io.StringIO()):
				self.assertEqual(upload.process_single_scan(*arguments), 'metadata_failed')
				client.fetch_scan_manifest.side_effect = None
				client.fetch_scan_manifest.return_value = {'rendering': []}
				self.assertEqual(upload.process_single_scan(*arguments), 'existing_title')
			self.assertEqual(client.downloads, [])
			uploader.file_info.assert_called_once_with('File:Published.pdf')
			uploader.upload.assert_not_called()
			item = state['items']['BY-NLB-one:1']
			self.assertEqual(item['verification']['page_id'], 10)
			self.assertNotIn('previous_status', item)

	def test_uncertain_rights_cannot_hide_pending_verification_or_allow_new_upload(self):
		'''Keep historical identity while checking existing bytes without changing Commons.'''
		for remote in ('matching', 'missing', 'different'):
			with self.subTest(remote=remote), tempfile.TemporaryDirectory() as directory:
				root = Path(directory)
				client = Catalog(ArchiveStore(root / 'archive'))
				state = upload.default_state()
				previous = {
					'status': 'verification_failed', 'commons_title': 'File:Published.pdf',
					'sha1': 'a' * 40, 'sha256': 'b' * 64, 'size_bytes': 123,
				}
				state['items']['BY-NLB-one:1'] = previous.copy()
				uploader = mock.Mock()
				if remote == 'missing':
					uploader.file_info.side_effect = ValueError('File is missing')
				else:
					uploader.file_info.return_value = {
						'commons_title': 'File:Published.pdf', 'sha1': ('c' if remote == 'different' else 'a') * 40,
						'size_bytes': 123, 'page_id': 10,
					}
				with mock.patch('sys.stdout', io.StringIO()):
					status = upload.process_single_scan(client, uploader, state, root / 'state.json', root / 'scans',
						'BY-NLB-one', '1', '', {'title': 'Unknown publication date'}, {})
				self.assertEqual(status, 'existing_title' if remote == 'matching' else 'verification_failed')
				item = state['items']['BY-NLB-one:1']
				self.assertFalse(item['rights']['eligible'])
				for field in ('commons_title', 'sha1', 'sha256', 'size_bytes'):
					self.assertEqual(item[field], previous[field])
				self.assertEqual(client.downloads, [])
				uploader.file_info.assert_called_once_with('File:Published.pdf')
				uploader.upload.assert_not_called()
				uploader.title_exists.assert_not_called()
				self.assertFalse((root / 'scans').exists())

	def test_completed_pdf_survives_archive_and_final_state_write_failures(self):
		'''Each local-write boundary retains original bytes and identifies them for recovery.'''
		for phase in ('pdf_copy', 'result_manifest', 'state'):
			with self.subTest(phase=phase), tempfile.TemporaryDirectory() as directory:
				root = Path(directory)
				archive = ArchiveStore(root / 'archive', keep_pdfs=True)
				client = Catalog(archive)
				uploader = FakeCommonsUploader()
				state = upload.default_state()
				actual_save = upload.save_state
				actual_record = archive.record_result
				def save(path, document):
					'''Fail only the outcome write, after the completed transfer.'''
					if phase == 'state' and document['items']:
						raise OSError('state disk full')
					return actual_save(path, document)
				def record(record_id, file_id, result):
					'''Allow description and PDF preservation, then fail the final manifest write.'''
					if phase == 'result_manifest' and result.get('status'):
						raise OSError('manifest disk full')
					return actual_record(record_id, file_id, result)
				archive.record_result = record
				if phase == 'pdf_copy':
					archive.preserve_pdf = mock.Mock(side_effect=OSError('archive disk full'))
				with mock.patch('upload.save_state', side_effect=save), mock.patch('upload.check_commons_by_hash', return_value=('a' * 40, ['File:Existing.pdf'])), mock.patch('sys.stdout', io.StringIO()):
					with self.assertRaisesRegex(OSError, 'disk full'):
						upload.process_single_scan(client, uploader, state, root / 'state.json', root / 'scans', 'BY-NLB-one', '1', '', {'title': 'Book'}, {'date': '1800'})
				path, = (root / 'scans').glob('*.pdf')
				self.assertEqual(path.read_bytes(), pdf_bytes())
				recovery = json.loads(path.with_suffix('.recovery.json').read_text())
				self.assertEqual((recovery['marc_id'], recovery['file_id']), ('BY-NLB-one', '1'))
				self.assertEqual(recovery['pdf_path'], str(path.resolve()))
				self.assertIn('sha256', recovery)
				self.assertEqual(uploader.uploads, [])

	def test_recovery_sidecar_failure_cannot_delete_completed_original(self):
		'''The retained bytes survive even when both archive and recovery metadata storage fail.'''
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			archive = ArchiveStore(root / 'archive', keep_pdfs=True)
			client = Catalog(archive)
			archive.preserve_pdf = mock.Mock(side_effect=OSError('archive disk full'))
			def write_json(path, document):
				'''Allow the source archive, but simulate no room for the recovery sidecar.'''
				if str(path).endswith('.recovery.json'):
					raise OSError('recovery disk full')
				return atomic_json(path, document)
			with mock.patch('preservation.atomic_json', side_effect=write_json), mock.patch('sys.stdout', io.StringIO()):
				with self.assertRaisesRegex(OSError, 'archive disk full'):
					upload.process_single_scan(client, FakeCommonsUploader(), upload.default_state(), root / 'state.json', root / 'scans', 'BY-NLB-one', '1', '', {'title': 'Book'}, {'date': '1800'})
			path, = (root / 'scans').iterdir()
			self.assertEqual(path.read_bytes(), pdf_bytes())
