#!/usr/bin/env python3
"""Preserve NLB subject references with their types, context and authority IDs."""

from pathlib import Path
import unittest

import upload
from test_personal_names import field, record


FIXTURES = Path(__file__).parent / 'tests' / 'fixtures'


def render(*fields: str, namespace: bool = True) -> tuple[dict, str]:
	"""Render synthetic MARC subject references without any HTML fallback."""
	metadata = upload.parse_marcxml(record(*fields, namespace=namespace))
	text = upload.build_commons_description({'title': 'Book'}, metadata, 'BY-NLB-test', '1')
	return metadata, text


class TestSubjectVariants(unittest.TestCase):
	"""Keep synonyms separate from related subjects and their own synonyms."""

	def test_real_uprising_variants_keep_related_heading_context(self) -> None:
		"""Repeated Russian aliases belong to the Russian heading in the public export."""
		metadata = upload.parse_marcxml((FIXTURES / 'BY-NLB-br0000159462-subject-variants.xml').read_text())
		variants = metadata['subject_variants']
		by_term = {entry['term']: entry for entry in variants}
		main = 'ПОЛЬСКАЕ ПАЎСТАННЕ 1830—1831'
		self.assertEqual(by_term['Лістападаўскае паўстанне 1830—1831']['main_subjects'], [main])
		self.assertEqual(by_term['Лістападаўскае паўстанне 1830—1831']['context'], '')
		for term in ('Ноябрьское восстание 1830—1831', 'Русско-польская война 1830—1831'):
			self.assertEqual(by_term[term]['context'], 'ПОЛЬСКОЕ ВОССТАНИЕ 1830—1831')
			self.assertEqual(by_term[term]['type'], '450')
			self.assertEqual(by_term[term]['schemes'], ['DVNLB'])
			self.assertEqual(by_term[term]['codes'], [])
			self.assertNotIn(term, metadata['subjects'])
		self.assertEqual(by_term['ПОЛЬСКОЕ ВОССТАНИЕ 1830—1831']['codes'], ['w'])
		self.assertEqual(by_term['гістарычная навука']['context'], 'ГІСТОРЫЯ (навука)')

	def test_matching_is_order_independent_and_excludes_other_heading_types(self) -> None:
		"""Only 606 with the same nonempty authority ID can identify a 906 source."""
		headings = [field('606', ('3', 'id'), ('a', 'Main')), field('607', ('3', 'id'), ('a', 'Place'))]
		aliases = [
			field('906', ('3', 'id'), ('9', '450'), ('a', 'Alias')),
			field('906', ('3', 'unknown'), ('9', '777'), ('a', 'Unknown')),
			field('906', ('9', '450'), ('a', 'No ID')),
		]
		for namespace in (True, False):
			for fields in (headings + aliases, aliases + headings):
				with self.subTest(namespace=namespace, aliases_first=fields[0] in aliases):
					metadata, text = render(*fields, namespace=namespace)
					self.assertEqual(metadata['subject_variants'][0]['main_subjects'], ['Main'])
					self.assertEqual(metadata['subject_variants'][1]['main_subjects'], [])
					self.assertEqual(metadata['subject_variants'][2]['main_subjects'], [])
					self.assertIn('777', text)
					self.assertIn('No ID', text)

	def test_subdivisions_and_relationship_codes_stay_with_each_heading(self) -> None:
		"""Geography, chronology and form distinguish otherwise identical terms."""
		metadata, text = render(
			field('606', ('3', 'id'), ('a', 'History'), ('j', 'Handbooks'), ('y', 'Poland')),
			field('906', ('3', 'id'), ('9', '550'), ('5', 'c'), ('a', 'Science'),
				('x', 'Teaching'), ('y', 'Poland'), ('z', '1800'), ('j', 'Handbooks'),
				('9', '450'), ('a', 'Study'), ('x', 'Education'), ('a', 'Learning'), ('2', 'DVNLB')),
		)
		entries = metadata['subject_variants']
		self.assertEqual(entries[0]['main_subjects'], ['History — Handbooks — Poland'])
		self.assertEqual(entries[0]['term'], 'Science — Handbooks — Teaching — Poland — 1800')
		self.assertEqual(entries[1]['context'], entries[0]['term'])
		self.assertEqual(entries[1]['term'], 'Study — Education')
		self.assertEqual(entries[2]['term'], 'Learning')
		self.assertEqual(entries[2]['codes'], [])
		self.assertIn('$5=c', text)

	def test_same_term_with_different_context_survives_and_exact_duplicates_collapse(self) -> None:
		"""Deduplication must not erase a synonym's distinct association or authority."""
		alias = field('906', ('3', 'id'), ('9', '450'), ('a', 'Shared'))
		metadata, _ = render(alias, alias,
			field('906', ('3', 'id'), ('9', '550'), ('a', 'Related'), ('9', '450'), ('a', 'Shared')),
			field('906', ('3', 'other'), ('9', '450'), ('a', 'Shared')),
		)
		self.assertEqual(len(metadata['subject_variants']), 4)
		self.assertEqual([entry['context'] for entry in metadata['subject_variants'] if entry['term'] == 'Shared'], ['', 'Related', ''])

	def test_authority_change_resets_context_and_trailing_id_identifies_whole_field(self) -> None:
		"""Do not leak reference type or parent context into another authority segment."""
		metadata, _ = render(
			field('906', ('3', 'first'), ('9', '550'), ('a', 'Related'),
				('3', 'second'), ('a', 'Untyped'), ('9', '450'), ('a', 'Variant')),
			field('906', ('9', '450'), ('a', 'Trailing'), ('3', 'last')),
		)
		entries = metadata['subject_variants']
		self.assertEqual(entries[1]['type'], '')
		self.assertEqual(entries[1]['authority_id'], 'second')
		self.assertEqual(entries[2]['context'], '')
		self.assertEqual(entries[3]['authority_id'], 'last')

	def test_related_title_qualifications_are_not_lost(self) -> None:
		"""NLB also embeds 530/430 references; their $k values distinguish statutes."""
		metadata, text = render(
			field('606', ('3', 'statutes'), ('a', 'Statutes')),
			field('906', ('3', 'statutes'), ('9', '530'), ('a', 'Statute'), ('k', '1529'),
				('9', '430'), ('a', 'First statute')),
			field('906', ('3', 'statutes'), ('9', '530'), ('a', 'Statute'), ('k', '1566'),
				('9', '430'), ('a', 'Second statute')),
		)
		self.assertIn('1529', metadata['subject_variants'][1]['context'])
		self.assertIn('1566', metadata['subject_variants'][3]['context'])
		self.assertIn('$k=1529', text)
		self.assertIn('530', text)

	def test_markup_is_literal_and_unknown_subfields_remain_visible(self) -> None:
		"""Catalog data cannot insert Commons templates, categories or formatting."""
		_, text = render(field('906', ('3', 'id'), ('9', '450'),
			('a', "{{Term}} [[Category:X]] <ref>x</ref> 'name' |"), ('0', 'See also'), ('2', '{{scheme}}')))
		self.assertIn('&#123;&#123;Term&#125;&#125;', text)
		self.assertNotIn('[[Category:X]]', text)
		self.assertNotIn('<ref>x</ref>', text)
		self.assertIn('$0=See also', text)
		self.assertIn('&#123;&#123;scheme&#125;&#125;', text)


if __name__ == '__main__':
	unittest.main()
