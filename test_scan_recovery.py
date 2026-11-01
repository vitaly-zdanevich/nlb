'''Exercise association review and recovery without another binary transfer.'''

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import upload
from preservation import ArchiveStore
from test_preservation_pipeline import Catalog


class TestScanRecovery(unittest.TestCase):
	'''Keep historical identity while checking newly discovered source associations.'''

	def run_scan(self, root, state, uploader, **kwargs):
		'''Run one archived fixture with no real catalog or Commons traffic.'''
		client = Catalog(ArchiveStore(root / 'archive'))
		status = upload.process_single_scan(
			client, uploader, state, root / 'state.json', root / 'scans',
			'BY-NLB-one', '1', '', {'title': 'Book'}, {'date': '1800'}, **kwargs,
		)
		return status, client

	def test_review_precedes_terminal_and_metadata_only_shortcuts(self):
		'''An older upload is evidence for repair, not proof of correct volume metadata.'''
		for metadata_only in (False, True):
			with self.subTest(metadata_only=metadata_only), tempfile.TemporaryDirectory() as directory:
				root = Path(directory)
				state = upload.default_state()
				previous = {'status': 'uploaded', 'sha1': 'a' * 40, 'commons_title': 'File:Wrong volume.pdf', 'verification': {'page_id': 1}}
				state['items']['BY-NLB-one:1'] = previous.copy()
				with mock.patch('upload.assess_scan_identity', create=True, return_value={'eligible': False, 'reason': 'Viewer volume differs', 'context': {'volume': 3}}):
					status, client = self.run_scan(root, state, None, metadata_only=metadata_only)
				self.assertEqual(status, 'metadata_review')
				entry = client.archive.manifests['BY-NLB-one']['files']['1']
				self.assertEqual(entry['sha1'], previous['sha1'])
				self.assertEqual(entry['commons_title'], previous['commons_title'])
				self.assertFalse(entry['scan_identity']['eligible'])
				self.assertNotIn('description', entry)
				self.assertEqual(client.downloads, [])
				if metadata_only:
					self.assertEqual(state['items']['BY-NLB-one:1'], previous)
				else:
					self.assertEqual(state['retry_from_page'], 1)


	def test_legacy_state_rewinds_once_for_scan_identity_audit(self):
		'''An upgrade rechecks records before the old cursor without erasing prior results.'''
		with tempfile.TemporaryDirectory() as directory:
			path = Path(directory) / 'state.json'
			legacy = {'current_page': 35, 'items': {'BY-NLB-one:1': {'status': 'uploaded'}}}
			path.write_text(json.dumps(legacy))
			state = upload.load_state(path)
			self.assertEqual(state['retry_from_page'], 1)
			self.assertEqual(state['items'], legacy['items'])
			state.pop('retry_from_page')
			upload.save_state(path, state)
			self.assertNotIn('retry_from_page', upload.load_state(path))
