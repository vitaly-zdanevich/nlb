'''Verify durable source snapshots independently of upload and deduplication.'''

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import requests

from preservation import ArchiveStore, atomic_json, file_integrity


class TestSourceArchive(unittest.TestCase):
	'''Preserve exact response bytes, provenance and every record-to-file association.'''

	def test_source_bytes_and_duplicate_metadata_survive_restart(self):
		'''Identical source bodies share storage while distinct records keep metadata.'''
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			archive = ArchiveStore(root)
			body = b'<record>  original\r\nspacing </record>'
			for record_id in ('BY-NLB-one', 'BY-NLB-two'):
				archive.begin_record(record_id)
				response = requests.Response()
				response.status_code = 200
				response.url = 'https://elib.nlb.by/elib/Record/' + record_id
				response._content = body
				response.headers = {'Content-Type': 'application/xml', 'Set-Cookie': 'private=value'}
				archive.capture_response(response)
				archive.save_record(record_id, {'title': record_id}, {'date': '1800'})
				archive.save_scan(record_id, '1', 'Unique note for ' + record_id, 'File:Shared.pdf')
				archive.record_result(record_id, '1', {'status': 'duplicate_sha1', 'duplicates': ['File:Shared.pdf']})
			manifests = sorted(root.glob('records/*/snapshots/*/manifest.json'))
			self.assertEqual(len(manifests), 2)
			for manifest_path in manifests:
				manifest = json.loads(manifest_path.read_text())
				source = manifest['sources'][0]
				self.assertEqual((root / source['path']).read_bytes(), body)
				self.assertEqual(source['sha256'], hashlib.sha256(body).hexdigest())
				self.assertNotIn('private', manifest_path.read_text())
				self.assertEqual(manifest['files']['1']['status'], 'duplicate_sha1')
				self.assertIn(manifest['record_id'], (root / manifest['files']['1']['description']['path']).read_text())
			self.assertEqual(len(list(root.glob('objects/' + hashlib.sha256(body).hexdigest() + '*'))), 1)

	def test_failed_response_is_captured_and_later_capture_does_not_replace_it(self):
		'''Retain failed HTTP200 bodies and successive source versions for diagnosis.'''
		with tempfile.TemporaryDirectory() as directory:
			archive = ArchiveStore(Path(directory))
			archive.begin_record('BY-NLB-one')
			for body in (b'Fatal error', b'<record>valid</record>'):
				response = requests.Response()
				response.status_code = 200
				response.url = 'https://elib.nlb.by/record'
				response._content = body
				archive.capture_response(response)
			path = archive.save_record('BY-NLB-one', {}, {})
			manifest = json.loads(path.read_text())
			self.assertEqual(len(manifest['sources']), 2)
			self.assertTrue(manifest['tool']['sha256'])
			self.assertTrue(manifest['captured_at'])

	def test_unsafe_record_identifiers_cannot_escape_archive(self):
		'''Remote record IDs must never become arbitrary filesystem paths.'''
		with tempfile.TemporaryDirectory() as directory:
			archive = ArchiveStore(Path(directory))
			with self.assertRaises(ValueError):
				archive.begin_record('../outside')

	def test_existing_corrupt_pdf_object_is_not_claimed_as_preserved(self):
		'''Verify saved bytes before reusing a content-addressed PDF object.'''
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			archive = ArchiveStore(root / 'archive', keep_pdfs=True)
			archive.save_scan('BY-NLB-one', '1', 'Source metadata', 'File:Source.pdf')
			source = root / 'scan.pdf'
			source.write_bytes(b'%PDF- original source bytes')
			digest = hashlib.sha256(source.read_bytes()).hexdigest()
			target = archive.root / 'objects' / (digest + '.pdf')
			target.write_bytes(b'Corrupted previously archived bytes')
			with self.assertRaisesRegex(OSError, 'Archive integrity check failed'):
				archive.preserve_pdf('BY-NLB-one', '1', source)
			manifest = json.loads(archive.snapshots['BY-NLB-one'].read_text())
			self.assertNotIn('pdf_path', manifest['files']['1'])
			self.assertEqual(target.read_bytes(), b'Corrupted previously archived bytes')
			self.assertFalse(list(target.parent.glob('*.new')))

	def test_pdf_capture_uses_unique_temporary_path_and_keeps_source_bytes(self):
		'''A stale or simultaneous writer's temporary file must not be overwritten.'''
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			archive = ArchiveStore(root / 'archive', keep_pdfs=True)
			archive.save_scan('BY-NLB-one', '1', 'Source metadata', 'File:Source.pdf')
			source = root / 'scan.pdf'
			body = b'%PDF-1.4\n' + b'original bytes\r\n' * 100000
			source.write_bytes(body)
			digest = hashlib.sha256(body).hexdigest()
			other_writer = archive.root / 'objects' / (digest + '.pdf.new')
			other_writer.write_bytes(b'Another writer owns this temporary file')
			result = archive.preserve_pdf('BY-NLB-one', '1', source)
			self.assertEqual(result['sha256'], digest)
			self.assertEqual(result['size_bytes'], len(body))
			self.assertEqual((archive.root / result['pdf_path']).read_bytes(), body)
			self.assertEqual(other_writer.read_bytes(), b'Another writer owns this temporary file')
			self.assertEqual(source.read_bytes(), body)
			self.assertEqual(list(other_writer.parent.glob('*.new')), [other_writer])
			self.assertEqual(archive.preserve_pdf('BY-NLB-one', '1', source), result)

	def test_pdf_write_failure_cleans_up_without_publishing_reference(self):
		'''A failed flush leaves metadata intact and no incomplete permanent PDF.'''
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			archive = ArchiveStore(root / 'archive', keep_pdfs=True)
			archive.save_scan('BY-NLB-one', '1', 'Source metadata', 'File:Source.pdf')
			before = archive.snapshots['BY-NLB-one'].read_bytes()
			source = root / 'scan.pdf'
			source.write_bytes(b'%PDF- original bytes')
			with mock.patch('preservation.os.fsync', side_effect=OSError('Disk failure')):
				with self.assertRaisesRegex(OSError, 'Disk failure'):
					archive.preserve_pdf('BY-NLB-one', '1', source)
			self.assertEqual(archive.snapshots['BY-NLB-one'].read_bytes(), before)
			self.assertFalse(list((archive.root / 'objects').glob('*.pdf')))
			self.assertFalse(list((archive.root / 'objects').glob('*.new')))

	def test_atomic_manifest_failure_keeps_previous_snapshot(self):
		'''An interrupted JSON write cannot destroy the last valid on-disk manifest.'''
		with tempfile.TemporaryDirectory() as directory:
			path = Path(directory) / 'manifest.json'
			atomic_json(path, {'status': 'before'})
			with mock.patch('preservation.os.fsync', side_effect=OSError('Disk failure')):
				with self.assertRaisesRegex(OSError, 'Disk failure'):
					atomic_json(path, {'status': 'after'})
			self.assertEqual(json.loads(path.read_text()), {'status': 'before'})
			self.assertEqual(list(path.parent.iterdir()), [path])

	def test_cached_parent_preserves_original_capture_time_for_each_child(self):
		'''Reusing a parent response must not imply a later successful HTTP retrieval.'''
		with tempfile.TemporaryDirectory() as directory:
			archive = ArchiveStore(Path(directory))
			response = requests.Response()
			response.status_code = 200
			response.url = 'https://elib.nlb.by/elib/Record/BY-NLB-parent/Export?style=MARCXML'
			response._content = b'<record>Parent metadata</record>'
			archive.begin_record('BY-NLB-one')
			archive.capture_response(response)
			archive.begin_record('BY-NLB-two')
			archive.capture_response(response)
			first = archive.manifests['BY-NLB-one']['sources'][0]
			second = archive.manifests['BY-NLB-two']['sources'][0]
			self.assertEqual(first['captured_at'], second['captured_at'])
			self.assertEqual(first['path'], second['path'])

	def test_pdf_source_changed_during_copy_cannot_publish_under_old_checksum(self):
		'''Verify copied bytes before assigning their immutable object name and manifest reference.'''
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			archive = ArchiveStore(root / 'archive', keep_pdfs=True)
			archive.save_scan('BY-NLB-one', '1', 'Source metadata', 'File:Source.pdf')
			source = root / 'scan.pdf'
			source.write_bytes(b'Original PDF bytes')

			def source_changes_after_hash(path):
				'''Simulate another process replacing the staging file after its initial hash.'''
				result = file_integrity(path)
				if path == source:
					source.write_bytes(b'Different PDF bytes')
				return result

			with mock.patch('preservation.file_integrity', side_effect=source_changes_after_hash):
				with self.assertRaisesRegex(OSError, 'PDF copy failed integrity check'):
					archive.preserve_pdf('BY-NLB-one', '1', source)
			self.assertFalse(list((archive.root / 'objects').glob('*.pdf')))
			self.assertFalse(list((archive.root / 'objects').glob('*.new')))
			self.assertNotIn('pdf_path', json.loads(archive.snapshots['BY-NLB-one'].read_text())['files']['1'])

	def test_search_sources_are_preserved_without_a_record_context(self):
		'''Keep independently verifiable search provenance before any record is discovered.'''
		with tempfile.TemporaryDirectory() as directory:
			archive = ArchiveStore(Path(directory))
			response = requests.Response()
			response.status_code = 200
			response.url = 'https://elib.nlb.by/elib/Search/Results?page=1'
			response._content = b'<html>Original search response</html>'
			archive.capture_response(response)
			searches = list((archive.root / 'searches').glob('*.json'))
			self.assertEqual(len(searches), 1)
			reference = json.loads(searches[0].read_text())
			self.assertEqual((archive.root / reference['path']).read_bytes(), response.content)
			self.assertEqual(reference['url'], response.url)
			self.assertEqual(reference['sha256'], hashlib.sha256(response.content).hexdigest())

	def test_corrupt_metadata_object_is_detected_before_reuse(self):
		'''Reused source bodies receive the same integrity protection as preserved PDFs.'''
		with tempfile.TemporaryDirectory() as directory:
			archive = ArchiveStore(Path(directory))
			archive.save_record('BY-NLB-one', {'title': 'Source title'}, {})
			original = archive.snapshots['BY-NLB-one'].read_bytes()
			reference = archive.manifests['BY-NLB-one']['parsed']
			(archive.root / reference['path']).write_bytes(b'Corrupt metadata')
			with self.assertRaisesRegex(OSError, 'Archive integrity check failed'):
				archive.save_record('BY-NLB-one', {'title': 'Source title'}, {})
			self.assertEqual(archive.snapshots['BY-NLB-one'].read_bytes(), original)
