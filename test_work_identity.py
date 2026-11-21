'''Verify conservative bound-work attribution against real and adversarial METS.'''

from copy import deepcopy
import json
import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scan_identity import assess_scan_identity, flatten_mets
import upload


def file_node(file_id: str, label: str, description: str = '') -> dict:
	'''Build a viewer FILE with separate bibliographic and descriptive labels.'''
	return {'data': {
		'nodeType': 'FILE', 'linkName': label, 'description': description,
		'file': {'id': file_id, 'fileLocation': f'/public/{file_id}.pdf'},
	}, 'children': []}


def bound_record(title: str = 'Collected historical essays') -> tuple[dict, dict]:
	'''Supply a bound-with catalog record that requires positive work evidence.'''
	return (
		{'marc_id': 'BY-NLB-test-work', 'title': title},
		{'title': title, 'related_records': [{'tag': '482', 'record_id': 'BY-NLB-owner'}]},
	)


class TestWorkIdentity(unittest.TestCase):
	'''A FILE must identify this work without borrowing a neighbor's metadata.'''

	@classmethod
	def setUpClass(cls):
		'''Load captured catalog records and their original viewer nodes once.'''
		path = Path(__file__).parent / 'fixtures/matching_work_examples.json'
		cls.records = json.loads(path.read_text())['records']

	def test_archived_work_associations(self):
		'''Eight exact works match; author collisions and unproven volumes defer.'''
		for record in self.records:
			with self.subTest(record=record['record_id']):
				files = flatten_mets(record['mets'])
				allowed = [entry['file_id'] for entry in files if assess_scan_identity(
					entry, record['record_meta'], record['marc_meta'],
				)['eligible']]
				self.assertEqual(allowed, record['expected_file_ids'], record['case_note'])

	def test_archived_works_pass_through_metadata_preparation(self):
		'''Only the proven works receive Commons descriptions, with raw labels retained.'''
		from preservation import ArchiveStore
		from test_preservation_pipeline import Catalog
		by_id = {item['record_id']: item for item in self.records}
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			client = Catalog(ArchiveStore(root / 'archive'))
			client.fetch_search_page = mock.Mock(return_value={'total_results': len(by_id), 'records': [{'marc_id': key} for key in by_id]})
			client.fetch_record_metadata = lambda key: (by_id[key]['record_meta'], by_id[key]['marc_meta'])
			client.resolve_record_files = lambda search, metadata: flatten_mets(by_id[search['marc_id']]['mets'])
			with mock.patch('sys.stdout', io.StringIO()):
				counts = upload.iterate_and_upload(client, None, root / 'state.json', root / 'scans', metadata_only=True)
			self.assertEqual(counts['metadata_ready'], 8)
			self.assertEqual(counts['metadata_review'], sum(len(flatten_mets(item['mets'])) for item in self.records) - 8)
			self.assertEqual(client.downloads, [])
			self.assertFalse((root / 'state.json').exists())
			for key, manifest in client.archive.manifests.items():
				for file_id, entry in manifest['files'].items():
					self.assertEqual('description' in entry, file_id in by_id[key]['expected_file_ids'])
					if 'description' in entry:
						text = (client.archive.root / entry['description']['path']).read_text()
						self.assertIn(key, text)

	def test_same_title_different_authors_remains_ambiguous(self):
		'''A matching creator cannot authorize a generic title repeated in this viewer.'''
		record, marc = bound_record()
		marc['authors'] = ['Novak, Adam']
		files = flatten_mets({'nodes': [
			file_node('1', 'Novak, Adam «Collected historical essays»'),
			file_node('2', 'Kowalski, Jan «Collected historical essays»'),
		]})
		self.assertEqual([assess_scan_identity(entry, record, marc)['eligible'] for entry in files], [False, False])

	def test_separate_mets_cannot_hide_a_title_collision(self):
		'''Record resolution checks all viewers together, including directly linked files.'''
		record, marc = bound_record()
		record.update(file_ids=['1'], mets_ids=['first', 'second'])
		structures = {
			'first': {'nodes': [file_node('1', 'Collected historical essays')]},
			'second': {'nodes': [file_node('2', 'Collected historical essays')]},
		}
		for structure in structures.values():
			entry, = flatten_mets(structure)
			self.assertTrue(assess_scan_identity(entry, record, marc)['eligible'])
		client = upload.NlbClient.__new__(upload.NlbClient)
		client.verify_shared_viewers = mock.Mock(return_value={})
		client.fetch_mets_files = mock.Mock(side_effect=lambda mets_id, **kwargs: flatten_mets(structures[mets_id]))
		files = client.resolve_record_files({}, record)
		self.assertEqual({entry['file_id'] for entry in files}, {'1', '2'})
		self.assertFalse(any(assess_scan_identity(entry, record, marc)['eligible'] for entry in files))
		self.assertEqual(client.fetch_mets_files.call_count, 2)

	def test_repeated_same_file_is_not_a_second_work(self):
		'''Identical evidence in two viewers remains usable for the same PDF identity.'''
		record, marc = bound_record()
		record['mets_ids'] = ['first', 'second']
		client = upload.NlbClient.__new__(upload.NlbClient)
		client.verify_shared_viewers = mock.Mock(return_value={})
		client.fetch_mets_files = mock.Mock(side_effect=lambda *args, **kwargs: flatten_mets({
			'nodes': [file_node('1', 'Collected historical essays')],
		}))
		entry, = client.resolve_record_files({}, record)
		self.assertEqual(len(entry['mets_contexts']), 2)
		self.assertTrue(assess_scan_identity(entry, record, marc)['eligible'])

	def test_conflicting_file_occurrences_never_choose_the_first(self):
		'''Contradictory labels for one file remain visible and prevent attribution.'''
		record, marc = bound_record()
		for labels in (
			('Collected historical essays', 'Collected geographical essays'),
			('Collected historical essays: first edition', 'Collected historical essays: second edition'),
		):
			with self.subTest(labels=labels):
				entry, = flatten_mets({'nodes': [file_node('1', label) for label in labels]})
				self.assertFalse(assess_scan_identity(entry, record, marc)['eligible'])
				self.assertEqual([context['file_link_name'] for context in entry['mets_contexts']], list(labels))

	def test_volume_part_and_series_requirements_cannot_be_bypassed(self):
		'''A shared main title does not establish which numbered component was scanned.'''
		for requirement in ('T. 2', 'Cz. 2', 'Ser. 4. T. 2', 'T. 4. Cz. 2'):
			with self.subTest(requirement=requirement):
				record, marc = bound_record()
				marc['related_records'].append({'tag': '461', 'volume': requirement})
				entry, = flatten_mets({'nodes': [file_node('1', 'Collected historical essays')]})
				self.assertFalse(assess_scan_identity(entry, record, marc)['eligible'])

	def test_explicit_file_title_overrides_a_stale_heading(self):
		'''A following work's FILE cannot inherit the preceding work's title.'''
		first, first_marc = bound_record('Collected historical essays')
		second, second_marc = bound_record('Collected geographical essays')
		files = flatten_mets({'nodes': [
			{'data': {'nodeType': 'TEXT', 'linkName': '«Collected historical essays»'}, 'children': []},
			file_node('1', 'Collected historical essays'),
			file_node('2', 'Collected geographical essays'),
		]})
		self.assertEqual([assess_scan_identity(entry, first, first_marc)['eligible'] for entry in files], [True, False])
		self.assertEqual([assess_scan_identity(entry, second, second_marc)['eligible'] for entry in files], [False, True])
		self.assertEqual(files[1]['mets_contexts'][0]['headings'], ['«Collected historical essays»'])

	def test_wrong_explicit_author_subtitle_and_year_defer(self):
		'''Separate title, creator, subtitle and publication claims must all agree.'''
		record, marc = bound_record()
		marc.update(authors=['Novak, Adam'], subtitle='a history of ancient towns', date='1890', year='1890')
		cases = (
			('Novak, Adam «Collected historical essays»', '', True),
			('Kowalski, Jan «Collected historical essays»', '', False),
			('Collected historical essays: a history of ancient towns', '', True),
			('Collected historical essays: a history of modern towns', '', False),
			('Collected historical essays', '1890 rok', True),
			('Collected historical essays', '1891 rok', False),
			('«Collected historical essays» (1891 rok)', '', False),
		)
		for label, description, expected in cases:
			with self.subTest(label=label, description=description):
				entry, = flatten_mets({'nodes': [file_node('1', label, description)]})
				self.assertEqual(assess_scan_identity(entry, record, marc)['eligible'], expected)

	def test_later_quotes_and_title_substrings_do_not_match(self):
		'''Quoted incipits, mentioned books and longer titles cannot identify this work.'''
		record, marc = bound_record()
		for label in (
			'«Collected geographical essays» — «Collected historical essays»',
			'Collected historical essays and geographical studies',
			'Introduction to Collected historical essays',
		):
			with self.subTest(label=label):
				entry, = flatten_mets({'nodes': [file_node('1', label)]})
				self.assertFalse(assess_scan_identity(entry, record, marc)['eligible'])

	def test_catalog_related_work_title_collision_defers(self):
		'''A catalog sibling with the same title prevents selecting by title alone.'''
		record, marc = bound_record()
		record['record_links'] = [{
			'record_id': 'BY-NLB-other-work',
			'title': 'Collected historical essays / another author. - 1890.',
		}]
		entry, = flatten_mets({'nodes': [file_node('1', 'Collected historical essays')]})
		self.assertFalse(assess_scan_identity(entry, record, marc)['eligible'])

	def test_composer_aliases_exclude_related_families_and_other_roles(self):
		'''Authority aliases can name a composer; family relationships are not aliases.'''
		record, marc = bound_record()
		name = 'Массне, Ж. (Жюль) (1842—1912)'
		marc.update(authors=[], contributors=[
			{'name': name, 'kind': 'person', 'tag': '700', 'roles': ['230']},
			{'name': 'Editor, Edward', 'kind': 'person', 'tag': '702', 'roles': ['340']},
		], personal_name_variants=[
			{'name': 'Massenet, J. (Jules) (1842—1912)', 'type': '400', 'main_names': [name]},
			{'name': 'Massenets (family)', 'type': '500', 'main_names': [name], 'codes': []},
			{'name': 'Unrelated, Adam', 'type': '400', 'main_names': [name], 'context': 'Massenets (family)'},
		])
		for author, expected in (('Массне, Жюль', True), ('Massenet, Jules', True), ('Massenets (family)', False), ('Editor, Edward', False), ('Unrelated, Adam', False)):
			with self.subTest(author=author):
				entry, = flatten_mets({'nodes': [file_node('1', f'{author} «Collected historical essays»')]})
				self.assertEqual(assess_scan_identity(entry, record, marc)['eligible'], expected)

	def test_typographic_normalization_preserves_diacritics(self):
		'''Case and punctuation differences are harmless; changed letters stay distinct.'''
		record, marc = bound_record('Études historiques anciennes')
		for label, expected in (('«ÉTUDES HISTORIQUES ANCIENNES!»', True), ('«Études historiques anciennes»', True), ('«Etudes historiques anciennes»', False)):
			with self.subTest(label=label):
				entry, = flatten_mets({'nodes': [file_node('1', label)]})
				self.assertEqual(assess_scan_identity(entry, record, marc)['eligible'], expected)

	def test_damaged_title_is_not_positive_identity_evidence(self):
		'''Replacement characters cannot disappear during word normalization.'''
		record, marc = bound_record()
		entry, = flatten_mets({'nodes': [file_node('1', 'Collected historical essays \ufffd')]})
		self.assertFalse(assess_scan_identity(entry, record, marc)['eligible'])

	def test_original_missing_pages_note_survives_matching(self):
		'''Approving a quoted title cannot discard a preserved incompleteness warning.'''
		record = next(item for item in self.records if item['record_id'] == 'BY-NLB-br0000023767')
		structure = deepcopy(record['mets'])
		entry = next(item for item in flatten_mets(structure) if item['file_id'] == '7389652')
		before = deepcopy(entry)
		self.assertTrue(assess_scan_identity(entry, record['record_meta'], record['marc_meta'])['eligible'])
		self.assertEqual(structure, record['mets'])
		self.assertEqual(entry, before)
		self.assertIn('адсутничае пачатак выдання', entry['part_label'])
		self.assertIn('адсутничае пачатак выдання', entry['mets_contexts'][0]['file_link_name'])


if __name__ == '__main__':
	unittest.main()
