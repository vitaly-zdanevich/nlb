#!/usr/bin/env python3
"""Preserve geographic and organization references and publication terms."""

from pathlib import Path
import unittest

import upload
from test_personal_names import field, record


FIXTURES = Path(__file__).parent / 'tests' / 'fixtures'


def render(xml: str) -> tuple[dict, str]:
	"""Render catalog metadata with no HTML fallback to expose omissions."""
	metadata = upload.parse_marcxml(xml)
	return metadata, upload.build_commons_description({'title': 'Book'}, metadata, 'BY-NLB-test', '1')


class TestMoreMetadata(unittest.TestCase):
	"""Keep variants attached to the correct subject or organization, with source roles."""

	def test_real_vilnius_variants_survive(self) -> None:
		"""The congress record retains historical and multilingual names of its subject city."""
		metadata, text = render((FIXTURES / 'BY-NLB-br0000160345-catalog-variants.xml').read_text())
		for term in ('Вильна, город', 'Вильно, город', 'Вильня, город', 'Vilnius, город'):
			entry = next(item for item in metadata['geographic_subject_variants'] if item['term'] == term)
			self.assertEqual(entry['authority_id'], 'BY-NLB-ar5418')
			self.assertTrue(any('Вильнюс' in main for main in entry['main_headings']))
			self.assertIn(term, text)
			self.assertNotIn(term, metadata['geo_places'])
			self.assertNotIn(term, metadata['subjects'])
		self.assertIn('MARC 907', text)

	def test_real_printer_variants_survive_without_new_contributors(self) -> None:
		"""Printer aliases retain their authority identity and do not create more authors."""
		metadata, text = render((FIXTURES / 'BY-NLB-br0000160923-catalog-variants.xml').read_text())
		for term in ('Друкарня базыльян', 'Типография базилиан'):
			entry = next(item for item in metadata['organization_variants'] if item['term'].startswith(term))
			self.assertIn('$4=475', entry['term'])
			self.assertTrue(any('Drukarnia' in name for name in entry['main_headings']))
			self.assertIn(term, text)
			self.assertNotIn(term, metadata['authors'])
			self.assertFalse(any(term == item['name'] for item in metadata['contributors']))

	def test_reference_fields_match_their_own_source_tags(self) -> None:
		"""The same ID on a place, author and secondary contributor cannot cross-link them."""
		metadata, _ = render(record(
			field('907', ('3', 'shared'), ('9', '415'), ('a', 'City alias'), ('x', 'Districts')),
			field('910', ('3', 'shared'), ('9', '410'), ('a', 'Primary alias')),
			field('911', ('3', 'shared'), ('9', '410'), ('a', 'Equal alias')),
			field('912', ('3', 'shared'), ('9', '410'), ('a', 'Secondary alias')),
			field('607', ('3', 'shared'), ('a', 'City')),
			field('710', ('3', 'shared'), ('a', 'Primary')),
			field('711', ('3', 'shared'), ('a', 'Equal')),
			field('712', ('3', 'shared'), ('a', 'Secondary'), ('4', '610')),
		))
		self.assertEqual(metadata['geographic_subject_variants'][0]['main_headings'], ['City'])
		self.assertEqual([item['main_headings'] for item in metadata['organization_variants']], [['Primary'], ['Equal'], ['Secondary']])
		self.assertEqual(metadata['authors'], ['Primary', 'Equal'])

	def test_historical_relationships_and_meeting_qualifiers_keep_context(self) -> None:
		"""Historical related bodies and their names must not be flattened into synonyms."""
		metadata, text = render(record(
			field('712', ('3', 'org'), ('a', 'Library'), ('4', '390')),
			field('912', ('3', 'org'), ('9', '510'), ('5', 'a'), ('a', 'Earlier library'),
				('b', 'Department'), ('c', 'Minsk'), ('d', '2'), ('e', 'Vilnius'), ('f', '1893'),
				('9', '410'), ('a', 'Earlier alias'), ('2', 'DVNLB')),
			field('907', ('3', 'place'), ('9', '515'), ('5', 'b'), ('a', 'Later region'),
				('9', '415'), ('a', 'Later alias')),
		))
		org = metadata['organization_variants']
		for qualifier in ('Department', 'Minsk', '2', 'Vilnius', '1893'):
			self.assertIn(qualifier, org[0]['term'])
		self.assertEqual(org[1]['context'], org[0]['term'])
		self.assertEqual(org[0]['codes'], ['a'])
		self.assertEqual(metadata['geographic_subject_variants'][1]['context'], 'Later region')
		self.assertIn('$5=a', text)
		self.assertIn('$5=b', text)
		self.assertEqual(len(metadata['contributors']), 1)
		self.assertEqual(metadata['authors'], [])

	def test_publication_terms_remain_grouped_with_their_edition_identifier(self) -> None:
		"""Preserve repeated binding and price qualifications without inventing an ISBN."""
		terms = field('010', ('b', 'У пераплёце'), ('d', '35 к.'))
		metadata, text = render(record(terms, terms,
			field('010', ('a', 'test-isbn'), ('b', 'Paperback'), ('d', '40 копеек'), ('d', 'Скидка'))))
		self.assertEqual(len(metadata['publication_terms']), 2)
		self.assertIn('У пераплёце', metadata['publication_terms'][0])
		self.assertIn('35 к.', metadata['publication_terms'][0])
		self.assertNotIn('ISBN', metadata['publication_terms'][0])
		for term in ('test-isbn', 'Paperback', '40 копеек', 'Скидка'):
			self.assertIn(term, metadata['publication_terms'][1])
		self.assertIn('MARC 010', text)
		self.assertEqual(metadata['physical_descriptions'], [])

	def test_new_reference_content_is_literal_and_unmatched_names_survive(self) -> None:
		"""Unknown references remain visible without executing catalog text as wikitext."""
		metadata, text = render(record(
			field('907', ('9', '999'), ('a', '{{Place}}')),
			field('912', ('3', 'missing'), ('9', '410'), ('a', '[[Category:Company]]')),
			field('010', ('d', '{{price}} | amount')),
		))
		self.assertEqual(metadata['organization_variants'][0]['main_headings'], [])
		self.assertNotIn('[[Category:Company]]', text)
		self.assertIn('&#123;&#123;Place&#125;&#125;', text)
		self.assertIn('&#123;&#123;price&#125;&#125;', text)
		self.assertIn('999', text)


if __name__ == '__main__':
	unittest.main()
