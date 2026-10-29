#!/usr/bin/env python3
'''Preserve useful catalog details and render source text literally on Commons.'''

from html import unescape
from pathlib import Path
import unittest
from xml.sax.saxutils import escape

import upload


FIXTURES = Path(__file__).parent / 'tests' / 'fixtures'


def field(tag: str, *pairs: tuple[str, str], indicators: tuple[str, str] = (' ', ' ')) -> str:
	'''Build escaped synthetic UNIMARC fields with repeated subfields in source order.'''
	return f'<datafield tag="{tag}" ind1="{indicators[0]}" ind2="{indicators[1]}">' + ''.join(
		f'<subfield code="{code}">{escape(value)}</subfield>' for code, value in pairs
	) + '</datafield>'


def record(*fields: str) -> str:
	'''Wrap fields in an NLB-compatible MARCXML collection.'''
	return '<collection><record><controlfield tag="001">BY-NLB-test</controlfield>' + ''.join(fields) + '</record></collection>'


def render(xml: str, html: dict | None = None) -> tuple[dict, str]:
	'''Exercise parsing and Commons formatting together without network access.'''
	metadata = upload.parse_marcxml(xml)
	return metadata, upload.build_commons_description(html or {}, metadata, 'BY-NLB-test', '123')


class TestMetadataPreservation(unittest.TestCase):
	'''Regression coverage for audit omissions, source markup and date precision.'''

	def test_exact_marc_structure_retains_leader_indicators_and_whitespace(self) -> None:
		'''Machine-readable fields preserve XML-decoded values before display normalization.'''
		xml = '<record xmlns="http://www.loc.gov/MARC21/slim"><leader>01234nam  2200000   4500</leader><controlfield tag="001">BY-NLB-test</controlfield>' + field('999', ('a', '  Original\n spacing  '), ('a', 'Second'), indicators=('1', '2')) + '</record>'
		metadata = upload.parse_marcxml(xml)
		self.assertEqual(metadata['control_number'], 'BY-NLB-test')
		self.assertEqual(metadata['leader'], '01234nam  2200000   4500')
		self.assertEqual(metadata['raw_marc_fields'][-1], {
			'kind': 'datafield', 'tag': '999', 'ind1': '1', 'ind2': '2',
			'subfields': [{'code': 'a', 'value': '  Original\n spacing  '}, {'code': 'a', 'value': 'Second'}],
		})

	def test_linked_work_contributors_and_family_references_are_readable(self) -> None:
		'''Real 464/488 contributors and 602/902 families survive with their own context.'''
		cases = {
			'BY-NLB-br0000354027': ('Фридрих фон', 'композитор', '1812—1883'),
			'BY-NLB-br0000160345': ('Генрих Христофорович', '1846—1907'),
			'BY-NLB-br0000370146': ('Radziwiłłowie (род)', 'Дамінік Геранім', '08:2:Radz.'),
		}
		for identifier, values in cases.items():
			with self.subTest(record=identifier):
				metadata, wikitext = render((FIXTURES / f'{identifier}-preservation.xml').read_text())
				for value in values:
					self.assertIn(value, unescape(wikitext))
				self.assertEqual(metadata['authors'], [])
		metadata, _ = render((FIXTURES / 'BY-NLB-br0000370146-preservation.xml').read_text())
		variant = next(item for item in metadata['personal_name_variants'] if item['name'].startswith('Radvilos'))
		self.assertEqual(variant['main_names'], ['Радзівілы (род)'])

	def test_organization_components_and_creator_dates_remain_separate(self) -> None:
		'''Corporate qualifiers never replace subdivisions, and dates retain their type.'''
		metadata, wikitext = render(record(
			field('710', ('a', 'Society'), ('b', 'Branch'), ('g', 'Expanded name'), ('c', 'Qualification'), ('d', '9'), ('e', 'Vilnius'), ('f', '1893'), ('4', '070')),
			field('700', ('a', 'Writer'), ('f', '1800—1870'), ('4', '070')),
		))
		for value in ('Branch', 'Expanded name', 'Qualification', '9', 'Vilnius', '1893'):
			self.assertIn(value, metadata['contributors'][0]['name'])
		self.assertEqual(metadata['contributors'][0]['kind'], 'organization')
		self.assertEqual(metadata['contributors'][1]['dates'], ['1800—1870'])
		self.assertEqual(metadata['contributors'][1]['kind'], 'person')
		self.assertIn('Qualification', wikitext)

	def test_unhandled_fields_subfields_and_repeated_statements_remain_readable(self) -> None:
		'''Unknown fields get labeled fallbacks; recognized fields cannot discard extensions.'''
		_, wikitext = render(record(
			field('010', ('a', '978-0-00-000000-0')),
			field('200', ('a', 'First title'), ('h', 'Volume heading')),
			field('200', ('a', 'Second title')),
			field('615', ('a', 'National document'), ('n', 'Local classification')),
			field('899', ('a', 'Library'), ('b', 'Collection'), ('j', 'Shelf location'), ('p', 'Shelfmark'), ('z', 'Copy note')),
			field('955', ('a', 'Unknown useful term'), ('q', 'Other detail'), ('q', 'Repeated detail')),
		))
		for value in ('978-0-00-000000-0', 'Volume heading', 'Second title', 'National document', 'Local classification', 'Library', 'Collection', 'Shelf location', 'Shelfmark', 'Copy note', 'Unknown useful term', 'Other detail', 'Repeated detail'):
			self.assertIn(value, wikitext)
		self.assertIn('MARC 955', wikitext)
		self.assertIn('$q', wikitext)
		self.assertIn('ISBN', wikitext)

	def test_repeated_country_codes_cannot_match_unrelated_language_codes(self) -> None:
		'''Country RU must not disappear just because another field contains language rus.'''
		_, wikitext = render(record(field('101', ('a', 'rus')), field('102', ('a', 'PL'), ('a', 'RU'))))
		self.assertIn('PL', wikitext)
		self.assertIn('RU', wikitext)

	def test_linked_work_groups_keep_shared_contributors_with_each_work(self) -> None:
		'''Identical embedded author lines still belong to each distinct linked work.'''
		metadata = upload.parse_marcxml(record(
			field('464', ('1', '2001 '), ('a', 'First linked work'), ('1', '700 1'), ('a', 'Shared contributor')),
			field('464', ('1', '2001 '), ('a', 'Second linked work'), ('1', '700 1'), ('a', 'Shared contributor')),
		))
		entries = upload.format_marc_supplement(metadata, '')
		for title in ('First linked work', 'Second linked work'):
			self.assertTrue(any(title in entry and 'Shared contributor' in entry for entry in entries), title)

	def test_source_wiki_markup_cannot_invoke_templates_links_or_categories(self) -> None:
		'''All source text is literal, while deliberately generated catalog links still work.'''
		literal = "[[Изоматериал]] {{Injected}} }} | <ref>text</ref> & ''italic''"
		_, wikitext = render(record(
			field('200', ('a', literal), ('f', literal)),
			field('300', ('a', literal)),
			field('481', ('0', 'BY-NLB-related'), ('t', literal)),
			field('955', ('a', '[[Category:Injected]]')),
		), {'title': literal, 'fields': [{'name': literal, 'values': [literal]}]})
		for unsafe in ('[[Изоматериал]]', '{{Injected}}', '[[Category:Injected]]', '<ref>text</ref>', "''italic''"):
			self.assertNotIn(unsafe, wikitext)
		self.assertIn('&#91;&#91;Изоматериал&#93;&#93;', wikitext)
		self.assertNotIn('&amp;#91;', wikitext)
		self.assertIn('[https://elib.nlb.by/elib/Record/BY-NLB-related BY-NLB-related]', wikitext)

	def test_uncertain_dates_ranges_and_distinct_contents_are_preserved(self) -> None:
		'''The date field retains the source statement and TOC supplements MARC contents.'''
		for date in ('[1850?]', '[да 1850?]', '1890—1910'):
			with self.subTest(date=date):
				_, wikitext = render(record(field('210', ('d', date)), field('327', ('a', 'First chapter'))), {'contents': ['First chapter', 'Second chapter']})
				self.assertIn(f'|date={date}\n', unescape(wikitext))
				self.assertIn('Second chapter', wikitext)

	def test_html_holdings_and_non_catalog_links_survive(self) -> None:
		'''Storage rows and source document URLs remain visible with safe external links.'''
		html = '<h1>Title</h1><table><tr><th>Шифр</th><td>08:2:Radz.</td></tr><tr><th>Справочник</th><td><a href="https://example.org/reference?a=1&amp;b=2">Reference</a></td></tr></table>'
		parsed = upload.parse_record_html(html, 'BY-NLB-test')
		wikitext = upload.build_commons_description(parsed, {}, 'BY-NLB-test', '123')
		self.assertIn('08:2:Radz.', wikitext)
		self.assertIn('[https://example.org/reference?a=1&b=2 Reference]', wikitext)
		self.assertEqual(wikitext.count('Reference'), 1)
		self.assertNotIn('Справочник — ссылки в каталоге', wikitext)

	def test_holdings_columns_preserve_each_copy_without_order_controls(self) -> None:
		'''Column-based holdings retain copy association but omit transient ordering links.'''
		html = '''<h1>Title</h1><table><tr><th>Всего: 2</th><td>Доступно <a class="placehold btn" href="/Hold?hashKey=session">Заказать</a></td></tr>
		<tr><td colspan="2"><table><tr><th>Шифр</th><th>Место нахождения</th><th>Читальный зал</th></tr>
		<tr><td>Copy A</td><td>Shelf A</td><td>Reading room A</td></tr>
		<tr><td>Copy B</td><td>Shelf B</td><td>Reading room B</td></tr></table></td></tr></table>'''
		metadata = upload.parse_record_html(html, 'BY-NLB-test')
		copies = [item for item in metadata['fields'] if item['name'] == 'Хранение / экземпляр']
		self.assertEqual(len(copies), 2)
		self.assertEqual(copies[0]['values'], ['Шифр: Copy A', 'Место нахождения: Shelf A', 'Читальный зал: Reading room A'])
		wikitext = upload.build_commons_description(metadata, {}, 'BY-NLB-test', '123')
		self.assertIn('Reading room B', wikitext)
		self.assertNotIn('session', wikitext)
		self.assertNotIn('Заказать', wikitext)

	def test_publication_field_214_keeps_date_uncertainty(self) -> None:
		'''Only publication dates from 214 are used in the structured date display.'''
		_, wikitext = render(record(field('214', ('d', '[1890?]'), indicators=(' ', '1'))))
		self.assertIn('|date=[1890?]\n', unescape(wikitext))

	def test_filename_separates_date_and_record_suffix(self) -> None:
		'''The date gets a space while the stable NLB/file IDs keep names unique.'''
		self.assertEqual(upload.build_commons_filename({}, {'title': 'Example', 'date': '1850'}, 'BY-NLB-test', '123'), 'Example (1850) -- BY-NLB-test -- 123.pdf')


if __name__ == '__main__':
	unittest.main()
