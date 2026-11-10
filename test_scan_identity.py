'''Reproduce shared-viewer attribution errors with archived NLB structures.'''

import json
from pathlib import Path
import unittest
import tempfile
from unittest import mock

import upload


def node(kind, label, file_id=None, children=None):
	'''Create the proprietary JSON shape returned by the NLB viewer.'''
	return {'data': {'nodeType': kind, 'linkName': label, 'file': {'id': file_id}}, 'children': children or []}


class TestScanIdentity(unittest.TestCase):
	'''Only explicit, consistent volume evidence can scope a shared viewer.'''

	def test_real_standalone_headings_are_retained(self):
		'''The first PDF of Orbis volume three must carry its preceding TEXT heading.'''
		records = json.loads((Path(__file__).parent / 'fixtures/shared_volumes.json').read_text())
		record = next(item for item in records if item['record_id'].endswith('466892'))
		files = upload.extract_files_from_mets_structure(record['mets'])
		entry = next(item for item in files if item['file_id'] == '8391400')
		self.assertIn('Том 3', entry['part_label'])

	def test_archived_volume_associations(self):
		'''Orbis permits its own volume only; Morituri's generic groups remain unresolved.'''
		from scan_identity import assess_scan_identity
		records = json.loads((Path(__file__).parent / 'fixtures/shared_volumes.json').read_text())
		for record in records:
			with self.subTest(record=record['record_id']):
				files = upload.extract_files_from_mets_structure(record['mets'])
				allowed = [item for item in files if assess_scan_identity(item, record['record_meta'], record['marc_meta'])['eligible']]
				self.assertEqual(len(allowed), {'BY-NLB-br0000466892': 6, 'BY-NLB-br0000466893': 4}.get(record['record_id'], 0))

	def test_sibling_scope_resets_and_nested_scope_does_not_leak(self):
		'''Generic sibling headings end the previous volume; nested headings retain ancestors.'''
		from scan_identity import assess_scan_identity
		structure = {'nodes': [node('TEXT', 'Том II'), node('FILE', 'Ч. 1', 1), node('TEXT', 'Other work'), node('FILE', 'Ч. 1', 2), node('TEXT', 'Том II', children=[node('TEXT', 'Contents'), node('FILE', 'Ч. 2', 3)]), node('FILE', 'Ч. 3', 4)]}
		files = upload.extract_files_from_mets_structure(structure)
		self.assertEqual([assess_scan_identity(item, {'title': 'Book T. 2'}, {})['eligible'] for item in files], [True, False, True, False])

	def test_conflicting_occurrences_and_record_identity_defer(self):
		'''Repeated IDs, ranges and conflicting catalog volume evidence cannot be first-wins.'''
		from scan_identity import assess_scan_identity
		for headings in (('Том I', 'Том II'), ('Том I–II',), ('Том IIII',)):
			with self.subTest(headings=headings):
				nodes = []
				for heading in headings:
					nodes += [node('TEXT', heading), node('FILE', 'Ч. 1', 1)]
				entry, = upload.extract_files_from_mets_structure({'nodes': nodes})
				self.assertFalse(assess_scan_identity(entry, {'title': 'Book T. 1'}, {})['eligible'])
		entry, = upload.extract_files_from_mets_structure({'nodes': [node('TEXT', 'Том I'), node('FILE', 'Ч. 1', 1)]})
		marc = {'related_records': [{'tag': '461', 'volume': 'T. 2'}]}
		self.assertFalse(assess_scan_identity(entry, {'title': 'Book T. 1'}, marc)['eligible'])

	def test_containers_inherit_scope_but_hidden_headings_end_it(self):
		'''A container retains preceding scope; hiding the next book cannot relabel its PDFs.'''
		from scan_identity import assess_scan_identity
		hidden = node('TEXT', 'Том II')
		hidden['data']['visible'] = False
		files = upload.extract_files_from_mets_structure({'nodes': [
			node('TEXT', 'Том I'), node('GROUP', 'Contents', children=[node('FILE', 'Ч. 1', 1)]),
			hidden, node('FILE', 'Ч. 1', 2),
		]})
		self.assertEqual([assess_scan_identity(item, {'title': 'Book T. 1'}, {})['eligible'] for item in files], [True, False])

	def test_direct_files_keep_mets_scope_and_location(self):
		'''A direct viewer link cannot bypass a conflicting METS association.'''
		from scan_identity import assess_scan_identity
		client = upload.NlbClient.__new__(upload.NlbClient)
		client.fetch_mets_files = lambda _, **kwargs: upload.extract_files_from_mets_structure({'nodes': [node('TEXT', 'Том II'), node('FILE', 'Ч. 1', 1)]})
		entry, = client.resolve_record_files({'file_ids': ['1'], 'mets_ids': ['1']}, {})
		self.assertIn('mets_contexts', entry)
		self.assertFalse(assess_scan_identity(entry, {'title': 'Book T. 1'}, {})['eligible'])

	def test_ordinary_record_and_single_volume_viewer_remain_usable(self):
		'''Unambiguous record-level viewers do not require invented volume identifiers.'''
		from scan_identity import assess_scan_identity
		self.assertTrue(assess_scan_identity({'file_id': '1'}, {'title': 'Book'}, {})['eligible'])
		entry, = upload.extract_files_from_mets_structure({'nodes': [node('TEXT', 'Том I', children=[node('FILE', 'Ч. 1', 1)])]})
		self.assertTrue(assess_scan_identity(entry, {'title': 'Book [в 3 т.]'}, {})['eligible'])

	def test_archived_records_pass_scope_into_the_full_metadata_pipeline(self):
		'''Metadata refresh cannot generate descriptions for another volume's scans.'''
		from test_preservation_pipeline import Catalog
		from preservation import ArchiveStore
		records = json.loads((Path(__file__).parent / 'fixtures/shared_volumes.json').read_text())
		by_id = {item['record_id']: item for item in records}
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			client = Catalog(ArchiveStore(root / 'archive'))
			client.fetch_search_page = mock.Mock(return_value={'total_results': 4, 'records': [{'marc_id': key} for key in by_id]})
			client.fetch_record_metadata = lambda key: (by_id[key]['record_meta'], by_id[key]['marc_meta'])
			client.resolve_record_files = lambda search, metadata: upload.extract_files_from_mets_structure(by_id[search['marc_id']]['mets'])
			counts = upload.iterate_and_upload(client, None, root / 'state.json', root / 'scans', metadata_only=True)
			self.assertEqual((counts['metadata_ready'], counts['metadata_review']), (10, 40))
			self.assertEqual(client.downloads, [])
			self.assertFalse((root / 'state.json').exists())
			for manifest in client.archive.manifests.values():
				for entry in manifest['files'].values():
					self.assertEqual('description' in entry, entry['scan_identity']['eligible'])
