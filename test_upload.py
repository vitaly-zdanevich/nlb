#!/usr/bin/env python3
'''Multi-level unit, component, and end-to-end tests for upload.py.'''

import hashlib
import io
import json
from pathlib import Path
import signal
import tempfile
import traceback
import types
import unittest
from unittest import mock
import warnings

import upload as upload_nlb
from test_pdf_validation import pdf_bytes


MOCK_SEARCH_HTML_PAGE_1 = """<!DOCTYPE html>
<html>
<body>
	<div class="search-stats">
		Показано <strong>1 - 2</strong> из <strong>2</strong>
	</div>
	<div id="result0" class="result">
		<input type="hidden" value="BY-NLB-rr111" class="hiddenId" />
		<a href="/elib/Record/BY-NLB-rr111" class="title getFull">
			Минские губернские ведомости, 1890
		</a>
		<div class="result-formats">
			<span class="format">Журнал</span>
		</div>
		<a href="https://elib.nlb.by:8070/viewer?markID=BY-NLB-rr111&amp;fileID=1001" class="fulltext">
			Просмотр
		</a>
	</div>
	<div id="result1" class="result">
		<input type="hidden" value="BY-NLB-rr222" class="hiddenId" />
		<a href="/elib/Record/BY-NLB-rr222" class="title getFull">
			Памятная книжка Виленской губернии на 1895 год
		</a>
		<div class="result-formats">
			<span class="format">Книга</span>
		</div>
		<a href="https://elib.nlb.by:8070/viewer/html?metsID=555&amp;markID=BY-NLB-rr222" class="fulltext">
			Просмотр составного документа
		</a>
	</div>
</body>
</html>
"""

MOCK_RECORD_HTML_111 = """<!DOCTYPE html>
<html>
<body>
	<h1>Минские губернские ведомости : официальная часть</h1>
	<table>
		<tr><th>Дата создания:</th><td>1890</td></tr>
		<tr><th>Опубликовано:</th><td>Минск : Губернская типография, 1890</td></tr>
		<tr><th>Язык:</th><td>русский</td></tr>
		<tr><th>Тематика:</th><td>Беларусь — История — 19 в.</td></tr>
		<tr>
			<th>ГРНТИ:</th>
			<td>
				<span property="grnti"><a href="/elib/Grnti/Home?grnti=11.01.53">11.01.53</a></span><br/>
				<span property="grnti"><a href="/elib/Grnti/Home?grnti=03.91">03.91</a></span><br/>
				<span property="grnti"><a href="/elib/Grnti/Home?grnti=03.17">03.17</a></span><br/>
				<span property="grnti"><a href="/elib/Grnti/Home?grnti=03.81.37">03.81.37</a></span><br/>
			</td>
		</tr>
		<tr>
			<th>Предмет:</th>
			<td>
				<div class="subject-line" property="keywords"><a href="#">История Беларуси</a></div>
				<div class="subject-line" property="keywords"><a href="#">Периодика</a></div>
			</td>
		</tr>
		<tr><th>Всего:</th><td>1 экз.</td></tr>
		<tr><th>Шифр:</th><td>НББ-123</td></tr>
		<tr>
			<th>Е-документ:</th>
			<td><a href="https://elib.nlb.by:8070/viewer?markID=BY-NLB-rr111&amp;fileID=1001">Открыть</a></td>
		</tr>
	</table>
</body>
</html>
"""

MOCK_RECORD_HTML_222 = """<!DOCTYPE html>
<html>
<body>
	<h1>Памятная книжка Виленской губернии на 1895 год</h1>
	<table>
		<tr><th>Авторы:</th><td>Виленский губернский статистический комитет</td></tr>
		<tr><th>Опубликовано:</th><td>Вильна : Типография А. Г. Сыркина, 1894</td></tr>
		<tr><th>Физическое описание:</th><td>320 с.</td></tr>
	</table>
	<a href="https://elib.nlb.by:8070/viewer/html?metsID=555&amp;markID=BY-NLB-rr222">Открыть</a>
</body>
</html>
"""

MOCK_MARCXML_111 = """<?xml version="1.0" encoding="UTF-8"?>
<collection xmlns="http://www.loc.gov/MARC21/slim">
	<record>
		<leader>00881nam a2200253 i 4500</leader>
		<controlfield tag="001">BY-NLB-rr111</controlfield>
		<datafield tag="100" ind1=" " ind2=" ">
			<subfield code="a">20200101d1890    k  y0rusy0103    ba</subfield>
		</datafield>
		<datafield tag="101" ind1="0" ind2=" ">
			<subfield code="a">rus</subfield>
		</datafield>
		<datafield tag="102" ind1=" " ind2=" ">
			<subfield code="a">BY</subfield>
		</datafield>
		<datafield tag="200" ind1="1" ind2=" ">
			<subfield code="a">Минские губернские ведомости</subfield>
			<subfield code="e">официальная часть</subfield>
			<subfield code="f">Минское губернское правление</subfield>
		</datafield>
		<datafield tag="205" ind1=" " ind2=" ">
			<subfield code="a">Изд. неофиц. и офиц.</subfield>
		</datafield>
		<datafield tag="210" ind1=" " ind2=" ">
			<subfield code="a">Минск</subfield>
			<subfield code="c">Губернская типография</subfield>
			<subfield code="d">1890</subfield>
		</datafield>
		<datafield tag="215" ind1=" " ind2=" ">
			<subfield code="a">48 с.</subfield>
			<subfield code="d">35 см</subfield>
		</datafield>
		<datafield tag="225" ind1="2" ind2=" ">
			<subfield code="a">Губернская печать</subfield>
			<subfield code="v">Вып. 4</subfield>
		</datafield>
		<datafield tag="300" ind1=" " ind2=" ">
			<subfield code="a">Приложения к номерам</subfield>
		</datafield>
		<datafield tag="606" ind1=" " ind2=" ">
			<subfield code="a">Периодическая печать</subfield>
			<subfield code="z">Минская губерния</subfield>
		</datafield>
		<datafield tag="675" ind1=" " ind2=" ">
			<subfield code="a">94(476)</subfield>
		</datafield>
		<datafield tag="700" ind1=" " ind2="1">
			<subfield code="a">Иванов</subfield>
			<subfield code="g">Иван Иванович</subfield>
			<subfield code="f">1840—1912</subfield>
		</datafield>
	</record>
</collection>
"""

MOCK_METS_STRUCTURE_555 = {
	'id': 555,
	'nodes': [
		{
			'data': {
				'nodeType': 'SECTION',
				'linkName': 'Том 1',
				'description': 'Адрес-календарь',
				'visible': True,
			},
			'children': [
				{
					'data': {
						'nodeType': 'FILE',
						'linkName': 'Часть 1',
						'description': 'Отдел общий',
						'visible': True,
						'elibLocal': False,
						'file': {
							'id': 2001,
							'fileLocation': 'v1_p1.pdf',
						},
					},
					'children': [],
				},
				{
					'data': {
						'nodeType': 'FILE',
						'linkName': 'Часть 2',
						'description': 'Справочный отдел',
						'visible': True,
						'elibLocal': False,
						'file': {
							'id': 2002,
							'fileLocation': 'v1_p2.pdf',
						},
					},
					'children': [],
				},
				{
					'data': {
						'nodeType': 'FILE',
						'linkName': 'Скрытый файл',
						'visible': False,
						'elibLocal': False,
						'file': {'id': 2003},
					},
					'children': [],
				},
				{
					'data': {
						'nodeType': 'FILE',
						'linkName': 'Только локальный доступ',
						'visible': True,
						'elibLocal': True,
						'file': {'id': 2004},
					},
					'children': [],
				},
			],
		},
	],
}


class TestUnitParsingAndFormatting(unittest.TestCase):
	'''Level 1 unit tests for URL/HTML/MARCXML/METS parsing, naming, and state helpers.'''

	def test_parse_viewer_link_variants(self) -> None:
		"""Verify viewer link parser extracts markID, fileID, and metsID across formats."""
		direct = upload_nlb.parse_viewer_link(
			'https://elib.nlb.by:8070/viewer?markID=BY-NLB-rr111&fileID=1001'
		)
		self.assertEqual(
			direct,
			{'mark_id': 'BY-NLB-rr111', 'file_id': '1001', 'mets_id': None},
		)

		mets = upload_nlb.parse_viewer_link(
			'https://elib.nlb.by:8070/viewer/html?metsID=555&markID=BY-NLB-rr222'
		)
		self.assertEqual(
			mets,
			{'mark_id': 'BY-NLB-rr222', 'file_id': None, 'mets_id': '555'},
		)

		self.assertIsNone(upload_nlb.parse_viewer_link('/elib/Record/BY-NLB-rr111'))
		self.assertIsNone(upload_nlb.parse_viewer_link('https://elib.nlb.by:8070/viewer'))

	def test_parse_search_results_html(self) -> None:
		"""Verify search results HTML parser extracts total count, fileIDs, and metsIDs."""
		parsed = upload_nlb.parse_search_results_html(MOCK_SEARCH_HTML_PAGE_1)
		self.assertEqual(parsed['total_results'], 2)
		self.assertEqual(len(parsed['records']), 2)

		first = parsed['records'][0]
		self.assertEqual(first['marc_id'], 'BY-NLB-rr111')
		self.assertEqual(first['file_ids'], ['1001'])
		self.assertEqual(first['mets_ids'], [])
		self.assertEqual(first['formats'], ['Журнал'])

		second = parsed['records'][1]
		self.assertEqual(second['marc_id'], 'BY-NLB-rr222')
		self.assertEqual(second['file_ids'], [])
		self.assertEqual(second['mets_ids'], ['555'])

	def test_extract_files_from_mets_structure(self) -> None:
		"""Verify METS structure flattening keeps visible open files and builds labels."""
		files = upload_nlb.extract_files_from_mets_structure(MOCK_METS_STRUCTURE_555)
		self.assertEqual(
			files,
			[
				{
					'file_id': '2001',
					'part_label': 'Том 1 — Адрес-календарь / Часть 1 — Отдел общий',
					'file_location': 'v1_p1.pdf',
				},
				{
					'file_id': '2002',
					'part_label': 'Том 1 — Адрес-календарь / Часть 2 — Справочный отдел',
					'file_location': 'v1_p2.pdf',
				},
			],
		)

	def test_parse_record_html_and_marcxml(self) -> None:
		'''Verify HTML record parser and UNIMARC XML parser extract complete metadata.'''
		record_meta = upload_nlb.parse_record_html(MOCK_RECORD_HTML_111, 'BY-NLB-rr111')
		self.assertEqual(record_meta['title'], 'Минские губернские ведомости : официальная часть')
		self.assertEqual(record_meta['file_ids'], ['1001'])
		field_names = [f['name'] for f in record_meta['fields']]
		self.assertIn('Дата создания', field_names)
		self.assertIn('Опубликовано', field_names)
		self.assertIn('Всего', field_names)
		self.assertIn('Шифр', field_names)

		marc_meta = upload_nlb.parse_marcxml(MOCK_MARCXML_111)
		self.assertEqual(marc_meta['control_number'], 'BY-NLB-rr111')
		self.assertEqual(marc_meta['year'], '1890')
		self.assertEqual(marc_meta['languages'], ['rus'])
		self.assertEqual(marc_meta['country'], 'BY')
		self.assertEqual(marc_meta['title'], 'Минские губернские ведомости')
		self.assertEqual(marc_meta['subtitle'], 'официальная часть')
		self.assertEqual(marc_meta['responsibility'], 'Минское губернское правление')
		self.assertEqual(marc_meta['edition'], 'Изд. неофиц. и офиц.')
		self.assertEqual(marc_meta['places'], ['Минск'])
		self.assertEqual(marc_meta['publishers'], ['Губернская типография'])
		self.assertEqual(marc_meta['physical_description'], '48 с. : 35 см')
		self.assertEqual(marc_meta['series'], ['Губернская печать ; Вып. 4'])
		self.assertEqual(marc_meta['notes'], ['Приложения к номерам'])
		self.assertEqual(marc_meta['subjects'], ['Периодическая печать — Минская губерния'])
		self.assertEqual(marc_meta['udc'], ['94(476)'])
		self.assertEqual(marc_meta['authors'], ['Иванов, Иван Иванович (1840—1912)'])
		self.assertEqual(upload_nlb.resolve_publication_year(record_meta, marc_meta), 1890)

	def test_real_manufacturing_printers_reach_description_without_becoming_publishers(self) -> None:
		"""Preserve both reported 210$g losses even when HTML has no printer or publisher."""
		cases = (
			('BY-NLB-br0000060196', 'Типография Императорской академии наук', '1891'),
			('BY-NLB-br0000060109', 'Типография С. В. Кульженко', '1881'),
		)
		for record_id, printer, year in cases:
			with self.subTest(record_id=record_id):
				fixture = Path(__file__).parent / 'tests' / 'fixtures' / f'{record_id}-manufacturing.xml'
				metadata = upload_nlb.parse_marcxml(fixture.read_text(encoding='utf-8'))
				wikitext = upload_nlb.build_commons_description({'title': 'Library record'}, metadata, record_id, '123')
				self.assertIn(printer, wikitext)
				self.assertIn(f"* '''Изготовление (MARC 210):''' Типография / изготовитель ($g): {printer}", wikitext)
				self.assertEqual(metadata['publishers'], [])
				self.assertEqual(metadata['date'], year)
				self.assertIn(f'|date={year}\n', wikitext)
				self.assertNotIn('Издатель (MARC 210$c)', wikitext)
				self.assertNotIn('|author=', wikitext)

	def test_real_publication_only_volume_does_not_invent_manufacturing_metadata(self) -> None:
		"""The reported Pamiętniki polskie volume supplies only 210$d, with no printer."""
		fixture = Path(__file__).parent / 'tests' / 'fixtures' / 'BY-NLB-br0000067302-manufacturing.xml'
		metadata = upload_nlb.parse_marcxml(fixture.read_text(encoding='utf-8'))
		record = {'title': 'Pamiętniki polskie / zebrane przez Ksawerego Bronikowskiego. T. 2'}
		wikitext = upload_nlb.build_commons_description(record, metadata, 'BY-NLB-br0000067302', '3466525')
		self.assertEqual(metadata['manufacturing_statements'], [])
		self.assertEqual(metadata['publishers'], [])
		self.assertIn('|date=1884\n', wikitext)
		self.assertNotIn('Изготовление (MARC 210)', wikitext)

	def test_manufacturing_statements_preserve_order_repetitions_and_field_boundaries(self) -> None:
		'''Keep manufacture places, addresses, names and dates together in source order.'''
		first_field = '''<datafield tag="210">
			<subfield code="a">Publication city</subfield>
			<subfield code="c">Publisher</subfield>
			<subfield code="d">1890</subfield>
			<subfield code="e">Print city</subfield>
			<subfield code="f">Street | 1</subfield>
			<subfield code="g">Printer | A</subfield>
			<subfield code="h">[1892?]</subfield>
			<subfield code="e">Other city</subfield>
			<subfield code="g">Printer B</subfield>
			<subfield code="h">1893</subfield>
		</datafield>'''
		marcxml = f'''<collection><record>
			{first_field}
			{first_field}
			<datafield tag="210">
				<subfield code="g">Printer | A</subfield>
				<subfield code="h">1894</subfield>
			</datafield>
		</record></collection>'''
		metadata = upload_nlb.parse_marcxml(marcxml)
		expected = [
			'Место изготовления ($e): Print city; Адрес изготовителя ($f): Street | 1; '
			'Типография / изготовитель ($g): Printer | A; Дата изготовления ($h): [1892?]; '
			'Место изготовления ($e): Other city; Типография / изготовитель ($g): Printer B; '
			'Дата изготовления ($h): 1893',
			'Типография / изготовитель ($g): Printer | A; Дата изготовления ($h): 1894',
		]
		self.assertEqual(metadata['manufacturing_statements'], expected)
		self.assertEqual(metadata['places'], ['Publication city'])
		self.assertEqual(metadata['publishers'], ['Publisher'])
		self.assertEqual(metadata['date'], '1890')
		self.assertEqual(metadata['year'], '1890')
		wikitext = upload_nlb.build_commons_description({'title': 'Test'}, metadata, 'record', 'file')
		for statement in expected:
			self.assertIn('** ' + upload_nlb.escape_wikitext(statement), wikitext)
		self.assertEqual(wikitext.count(upload_nlb.escape_wikitext('Дата изготовления ($h): [1892?]')), 1)
		self.assertNotIn('Printer | A', wikitext)
		self.assertNotIn('Street | 1', wikitext)
		self.assertIn('|date=1890\n', wikitext)

	def test_partial_manufacturing_statements_do_not_supply_publication_dates(self) -> None:
		'''Preserve lone manufacture subfields without requiring a printer or inferring publication.'''
		cases = (
			('e', 'Print city', 'Место изготовления ($e)'),
			('f', 'Street 1', 'Адрес изготовителя ($f)'),
			('g', 'Printer', 'Типография / изготовитель ($g)'),
			('h', '[1891?]', 'Дата изготовления ($h)'),
		)
		for code, value, label in cases:
			with self.subTest(code=code):
				metadata = upload_nlb.parse_marcxml(f'''<collection><record>
					<datafield tag="210"><subfield code="{code}">{value}</subfield></datafield>
				</record></collection>''')
				wikitext = upload_nlb.build_commons_description({'title': 'Test'}, metadata, 'record', 'file')
				self.assertIn(upload_nlb.escape_wikitext(f'{label}: {value}'), wikitext)
				self.assertEqual(metadata['date'], '')
				self.assertEqual(metadata['year'], '')
				self.assertIsNone(upload_nlb.resolve_publication_year({}, metadata))
				self.assertIn('|date=\n', wikitext)

	def test_absent_manufacturing_metadata_has_no_empty_description_section(self) -> None:
		"""Handle empty XML, publication-only records, blank subfields and older metadata dicts."""
		for xml in ('', '<invalid', MOCK_MARCXML_111, '''<collection><record>
			<datafield tag="210"><subfield code="g"> </subfield><subfield code="9">internal</subfield></datafield>
		</record></collection>'''):
			with self.subTest(xml=xml):
				metadata = upload_nlb.parse_marcxml(xml)
				self.assertEqual(metadata['manufacturing_statements'], [])
				wikitext = upload_nlb.build_commons_description({'title': 'Test'}, metadata, 'record', 'file')
				self.assertNotIn('Изготовление (MARC 210)', wikitext)
		wikitext = upload_nlb.build_commons_description({'title': 'Test'}, {}, 'record', 'file')
		self.assertNotIn('Изготовление (MARC 210)', wikitext)

	def test_real_contributor_roles_preserve_names_without_misattributing_authorship(self) -> None:
		"""Keep former owners and issuing bodies separate, preserving a translator's two roles."""
		cases = {
			'BY-NLB-br0000060196': {
				'authors': ['Миллер, В. Ф. (Всеволод Федорович), фольклорист, языковед (1848—1913)', 'Кнауэр, Ф. И.'],
				'roles': [['070'], ['070'], ['390'], ['390'], ['475'], ['475']],
				'labels': ['Автор (070)', 'Предыдущий владелец (390)', 'Выпускающая организация (475)'],
			},
			'BY-NLB-br0000058446': {
				'authors': [],
				'roles': [['390']],
				'labels': ['Сапунов, А. П. (Алексей Парфенович), историк (1851—1924) — Предыдущий владелец (390)'],
			},
			'BY-NLB-br0000060109': {
				'authors': ['Гоголь, М. В. (Микола Васильович), пісьменнік (1809—1852)'],
				'roles': [[], ['730', '080']],
				'labels': ['Переводчик (730)', 'Автор вступления, примечаний и т. п. (080)'],
			},
		}
		for record_id, expected in cases.items():
			with self.subTest(record_id=record_id):
				fixture = Path(__file__).parent / 'tests' / 'fixtures' / f'{record_id}-contributors.xml'
				metadata = upload_nlb.parse_marcxml(fixture.read_text(encoding='utf-8'))
				self.assertEqual(metadata['authors'], expected['authors'])
				self.assertEqual([entry['roles'] for entry in metadata['contributors']], expected['roles'])
				wikitext = upload_nlb.build_commons_description({'title': 'Library record'}, metadata, record_id, '123')
				for label in expected['labels']:
					self.assertIn(label, wikitext)
				for contributor in metadata['contributors']:
					self.assertIn(contributor['name'], wikitext)
				author_lines = [line for line in wikitext.splitlines() if line.startswith('|author=')]
				self.assertEqual(author_lines, ['|author=' + '<br>'.join(expected['authors'])] if expected['authors'] else [])

	def test_owner_roles_do_not_claim_verified_ownership_of_the_scan(self) -> None:
		"""Keep catalog ownership separate from the scan even when a role has a copy reference."""
		qualification = 'Сведения о прежних владельцах приведены по каталогу НББ; их связь с отсканированным экземпляром не установлена.'
		for role, copy_id in (('390', ''), ('390', 'BY-HM0000:13//7647K(026)'), ('070', '')):
			with self.subTest(role=role, copy_id=copy_id):
				copy_xml = f'<subfield code="5">{copy_id}</subfield>' if copy_id else ''
				metadata = upload_nlb.parse_marcxml(f'''<collection><record>
					<datafield tag="702"><subfield code="a">Сапунов</subfield>
						<subfield code="4">{role}</subfield>{copy_xml}
					</datafield>
				</record></collection>''')
				wikitext = upload_nlb.build_commons_description({
					'title': 'Test', 'fields': [{
						'name': 'Примечания',
						'value': 'Владельческая запись: А. Сапунов BY-HM0000:13//7647K(026)',
					}],
				}, metadata, 'record', '7396778')
				self.assertIn('Участники и роли по каталогу НББ (MARC 7xx$4)', wikitext)
				self.assertIn('Владельческая запись: А. Сапунов BY-HM0000:13//7647K(026)', wikitext)
				if role == '390':
					self.assertIn(qualification, wikitext)
					self.assertNotIn('|author=', wikitext)
				else:
					self.assertNotIn(qualification, wikitext)
					self.assertIn('|author=Сапунов\n', wikitext)
				if copy_id:
					self.assertIn(f'экземпляр в каталоге: {copy_id}', wikitext)

	def test_ownership_notes_keep_copy_references_without_assigning_them_to_people(self) -> None:
		"""Preserve distinct copy notes without guessing which copy an unscoped role describes."""
		metadata = upload_nlb.parse_marcxml('''<collection><record>
			<datafield tag="702"><subfield code="a">Сапунов</subfield><subfield code="4">390</subfield></datafield>
			<datafield tag="317"><subfield code="a">Владельческая запись: А. Сапунов</subfield>
				<subfield code="5">BY-HM0000:13//7647K(026)</subfield></datafield>
			<datafield tag="317"><subfield code="a">Владельческая запись: А. Сапунов</subfield>
				<subfield code="5">Another|copy</subfield><subfield code="5">Third copy</subfield></datafield>
		</record></collection>''')
		self.assertEqual(metadata['contributors'][0]['copies'], [])
		self.assertEqual(metadata['notes'], [
			'Владельческая запись: А. Сапунов; экземпляр в каталоге: BY-HM0000:13//7647K(026)',
			'Владельческая запись: А. Сапунов; экземпляр в каталоге: Another|copy; Third copy',
		])
		wikitext = upload_nlb.build_commons_description({'title': 'Test'}, metadata, 'record', 'file')
		self.assertIn('экземпляр в каталоге: BY-HM0000:13//7647K(026)', wikitext)
		self.assertIn('экземпляр в каталоге: Another{{!}}copy; Third copy', wikitext)

	def test_non_author_roles_block_untyped_author_fallbacks(self) -> None:
		"""Do not restore a former owner through responsibility statements or HTML author lists."""
		for role, label in (
			('390', 'Предыдущий владелец (390)'),
			('340', 'Редактор (340)'),
			('220', 'Составитель (220)'),
			('650', 'Издатель (650)'),
			('999', 'Роль UNIMARC 999'),
			('000', 'Функция не установлена (000)'),
		):
			with self.subTest(role=role):
				metadata = upload_nlb.parse_marcxml(f'''<collection><record>
					<datafield tag="200"><subfield code="f">Из коллекции Владельца</subfield></datafield>
					<datafield tag="702"><subfield code="a">Владелец</subfield><subfield code="4">{role}</subfield></datafield>
				</record></collection>''')
				wikitext = upload_nlb.build_commons_description({
					'title': 'Test', 'fields': [{'name': 'Автор', 'value': 'Владелец'}, {'name': 'Другие авторы', 'value': 'Владелец'}],
				}, metadata, 'record', 'file')
				self.assertNotIn('|author=', wikitext)
				self.assertIn(label, wikitext)
				self.assertIn('Из коллекции Владельца', wikitext)
				self.assertIn('Владелец', wikitext)

	def test_contributor_roles_override_tags_and_keep_uncoded_secondary_names(self) -> None:
		'''Respect explicit roles in every supported tag; infer only uncoded primary responsibility.'''
		for tag in ('700', '701', '702', '710', '711', '712'):
			for role in ('', '070', '390', '999'):
				with self.subTest(tag=tag, role=role):
					role_xml = f'<subfield code="4">{role}</subfield>' if role else ''
					metadata = upload_nlb.parse_marcxml(f'''<collection><record>
						<datafield tag="{tag}"><subfield code="a">Name</subfield>{role_xml}</datafield>
					</record></collection>''')
					is_author = role == '070' or (not role and tag in {'700', '701', '710', '711'})
					self.assertEqual(metadata['authors'], ['Name'] if is_author else [])
					self.assertEqual(metadata['contributors'], [{
						'name': 'Name', 'tag': tag, 'roles': [role] if role else [], 'copies': [],
						'dates': [], 'kind': 'person' if tag in {'700', '701', '702'} else 'organization',
					}])
					wikitext = upload_nlb.build_commons_description({'title': 'Test'}, metadata, 'record', 'file')
					if not role:
						self.assertIn(f'Роль не указана (MARC {tag})', wikitext)
					elif role == '999':
						self.assertIn('Роль UNIMARC 999', wikitext)

	def test_contributor_roles_keep_copy_associations_and_unknown_codes(self) -> None:
		"""Deduplicate exact entries while retaining distinct roles, copies, and escaped values."""
		owner = '''<datafield tag="702"><subfield code="a">Owner|name</subfield>
			<subfield code="4">390</subfield><subfield code="4">390</subfield>
			<subfield code="5">BY-HM0000:copy|A</subfield></datafield>'''
		metadata = upload_nlb.parse_marcxml(f'''<collection><record>
			{owner}{owner}{owner.replace('copy|A', 'copy|B')}
			<datafield tag="702"><subfield code="a">Writer</subfield>
				<subfield code="4">070</subfield><subfield code="4">730</subfield>
				<subfield code="4">999|custom</subfield><subfield code="4">070</subfield>
			</datafield>
			<datafield tag="702"><subfield code="a">Writer</subfield><subfield code="4">070</subfield></datafield>
			<datafield tag="702"><subfield code="4">390</subfield></datafield>
			<datafield tag="702"><subfield code="f">1800—1900</subfield><subfield code="4">390</subfield></datafield>
		</record></collection>''')
		self.assertEqual(metadata['authors'], ['Writer'])
		self.assertEqual(len(metadata['contributors']), 4)
		self.assertEqual(metadata['contributors'][0]['roles'], ['390'])
		self.assertEqual(metadata['contributors'][0]['copies'], ['BY-HM0000:copy|A'])
		self.assertEqual(metadata['contributors'][1]['copies'], ['BY-HM0000:copy|B'])
		self.assertEqual(metadata['contributors'][2]['roles'], ['070', '730', '999|custom'])
		wikitext = upload_nlb.build_commons_description({'title': 'Test'}, metadata, 'record', 'file')
		self.assertIn('Owner{{!}}name — Предыдущий владелец (390)', wikitext)
		self.assertIn('экземпляр в каталоге: BY-HM0000:copy{{!}}A', wikitext)
		self.assertIn('экземпляр в каталоге: BY-HM0000:copy{{!}}B', wikitext)
		self.assertIn('Роль UNIMARC 999{{!}}custom', wikitext)
		self.assertNotIn('Owner|name', wikitext)
		self.assertIn('|author=Writer\n', wikitext)

	def test_parse_marcxml_and_description_with_contents_and_all_notes(self) -> None:
		'''Verify MARC 327 contents note (TOC with section numbers and folio ranges) and 3xx notes are parsed.'''
		marcxml = """<?xml version="1.0" encoding="UTF-8"?>
		<collection xmlns="http://www.loc.gov/MARC21/slim">
			<record>
				<controlfield tag="001">BY-NLB-br0000015701</controlfield>
				<datafield tag="100" ind1=" " ind2=" ">
					<subfield code="a">20070206f18021805|||y0rusy50  ||||ca</subfield>
				</datafield>
				<datafield tag="200" ind1="1" ind2=" ">
					<subfield code="a">Псалтирь</subfield>
				</datafield>
				<datafield tag="210" ind1=" " ind2=" ">
					<subfield code="a">[Гродно [т. е. Махновка]</subfield>
					<subfield code="d">1788 [после 1802]]</subfield>
				</datafield>
				<datafield tag="306" ind1=" " ind2=" ">
					<subfield code="a">Старообрядческое издание с московского издания 1651 г.</subfield>
				</datafield>
				<datafield tag="307" ind1=" " ind2=" ">
					<subfield code="a">Фолиация в нижнем правом углу: 1-367 л.</subfield>
				</datafield>
				<datafield tag="316" ind1=" " ind2=" ">
					<subfield code="a">Дефектный экземпляр: утрачены л. 1-5</subfield>
				</datafield>
				<datafield tag="321" ind1=" " ind2=" ">
					<subfield code="a">Библиография: Вознесенский № 152</subfield>
				</datafield>
				<datafield tag="327" ind1="1" ind2=" ">
					<subfield code="a">Содержание: 1) 1-143, кафизмы, 1-20; 2) 8-12, 6-7, 13-14, о крестном знамении; 3) 14 об.-20 об., о вере Анастасия Антиохийского, Кирилла Александрийского, Максима Грека; 4) 21-24 об., указ о поклонах; 5) 25-33 об., устав о Псалтири; 6) 152-197, кафизмы; 7) 197 об.-218, библейские песни; 8) 218 об.-285, многомилостивое; 9) 285-319, устав всем, хотящим пети Псалтирь; 10) 319 об.-329, канон за единоумершего; 11) 329 об.-340, за умерших; 12) 340 об.-349, за творящих милостыню; 13) 349-355, на исход души</subfield>
				</datafield>
			</record>
		</collection>
		"""
		marc_meta = upload_nlb.parse_marcxml(marcxml)
		self.assertIn('contents', marc_meta)
		self.assertEqual(len(marc_meta['contents']), 13)
		self.assertTrue(marc_meta['contents'][0].startswith('1) 1-143, кафизмы'))
		self.assertTrue(marc_meta['contents'][-1].startswith('13) 349-355, на исход души'))
		self.assertIn('Старообрядческое издание с московского издания 1651 г.', marc_meta['notes'])
		self.assertIn('Фолиация в нижнем правом углу: 1-367 л.', marc_meta['notes'])
		self.assertIn('Дефектный экземпляр: утрачены л. 1-5', marc_meta['notes'])
		self.assertIn('Библиография: Вознесенский № 152', marc_meta['notes'])

		wikitext = upload_nlb.build_commons_description(
			record_meta={'title': 'Псалтирь', 'fields': []},
			marc_meta=marc_meta,
			marc_id='BY-NLB-br0000015701',
			file_id='7386109',
			part_label='Ч. 3',
		)
		self.assertIn("* '''Содержание (MARC 327 / оглавление каталога):'''", wikitext)
		for entry in marc_meta['contents']:
			self.assertIn(f'** {entry}', wikitext)
		self.assertIn("* '''Примечания (MARC 3xx):'''", wikitext)

	def test_parse_marcxml_and_description_with_geo_places_genres_shelfmarks_and_deduplication(self) -> None:
		"""Verify MARC 620 geographic places, 608 genres, 690 collections, and 899 shelfmarks are parsed and deduplicated."""
		marcxml = """<?xml version="1.0" encoding="UTF-8"?>
		<collection xmlns="http://www.loc.gov/MARC21/slim">
			<record>
				<controlfield tag="001">BY-NLB-br0000015701</controlfield>
				<datafield tag="200" ind1="1" ind2=" ">
					<subfield code="a">Псалтирь</subfield>
				</datafield>
				<datafield tag="210" ind1=" " ind2=" ">
					<subfield code="a">[Гродно [т. е. Махновка]</subfield>
					<subfield code="d">1788 [после 1802]]</subfield>
				</datafield>
				<datafield tag="605" ind1=" " ind2=" ">
					<subfield code="a">Псалтирь (богослужебное издание)</subfield>
				</datafield>
				<datafield tag="608" ind1=" " ind2=" ">
					<subfield code="a">КАНОН (религия)</subfield>
				</datafield>
				<datafield tag="608" ind1=" " ind2=" ">
					<subfield code="a">Пометы</subfield>
					<subfield code="z">19 ВЕК</subfield>
					<subfield code="z">20 ВЕК</subfield>
				</datafield>
				<datafield tag="620" ind1=" " ind2=" ">
					<subfield code="d">Гродно</subfield>
					<subfield code="c">Гродненский район</subfield>
					<subfield code="b">Гродненская область</subfield>
				</datafield>
				<datafield tag="620" ind1=" " ind2=" ">
					<subfield code="d">Комсомольское</subfield>
				</datafield>
				<datafield tag="690" ind1=" " ind2=" ">
					<subfield code="a">Кірыліца ― беларускія выданні</subfield>
				</datafield>
				<datafield tag="899" ind1=" " ind2=" ">
					<subfield code="b">076</subfield>
					<subfield code="j">08:1</subfield>
					<subfield code="p">12//4(026),</subfield>
				</datafield>
			</record>
		</collection>
		"""
		marc_meta = upload_nlb.parse_marcxml(marcxml)
		self.assertEqual(
			marc_meta['geo_places'],
			['Гродно — Гродненский район — Гродненская область', 'Комсомольское'],
		)
		self.assertEqual(
			marc_meta['genres'],
			['КАНОН (религия)', 'Пометы — 19 ВЕК — 20 ВЕК'],
		)
		self.assertEqual(
			marc_meta['shelfmarks'],
			['12//4(026)'],
		)
		self.assertIn('Псалтирь (богослужебное издание)', marc_meta['subjects'])
		self.assertIn('Кірыліца ― беларускія выданні', marc_meta['subjects'])

		# Case 1: When HTML fields are empty, all fields are rendered
		wikitext_plain = upload_nlb.build_commons_description(
			record_meta={'title': 'Псалтирь', 'fields': []},
			marc_meta=marc_meta,
			marc_id='BY-NLB-br0000015701',
			file_id='7386109',
		)
		self.assertIn("* '''Место издания (MARC 620):'''", wikitext_plain)
		self.assertIn('** Гродно — Гродненский район — Гродненская область', wikitext_plain)
		self.assertIn('** Комсомольское', wikitext_plain)
		self.assertIn("* '''Форма / жанр (MARC 608):'''", wikitext_plain)
		self.assertIn('** КАНОН (религия)', wikitext_plain)
		self.assertIn('** Пометы — 19 ВЕК — 20 ВЕК', wikitext_plain)
		self.assertIn("* '''Шифр хранения (MARC 899):''' 12//4(026)", wikitext_plain)
		self.assertIn("* '''Тематика (MARC 6xx):'''", wikitext_plain)
		self.assertIn('** Кірыліца ― беларускія выданні', wikitext_plain)

		# Case 2: When HTML already contains identical shelfmark, genre, and subject, avoid duplicate entries
		wikitext_dedup = upload_nlb.build_commons_description(
			record_meta={
				'title': 'Псалтирь',
				'fields': [
					{'name': 'Шифр документа', 'value': '12//4(026),', 'values': ['12//4(026),']},
					{'name': 'Предмет', 'value': 'Псалтирь (богослужебное издание)', 'values': ['Псалтирь (богослужебное издание)']},
					{'name': 'Жанр', 'value': 'КАНОН (религия)\nПометы — 19 ВЕК — 20 ВЕК', 'values': ['КАНОН (религия)', 'Пометы — 19 ВЕК — 20 ВЕК']},
				],
			},
			marc_meta=marc_meta,
			marc_id='BY-NLB-br0000015701',
			file_id='7386109',
		)
		self.assertIn("* '''Шифр документа:''' 12//4(026),", wikitext_dedup)
		self.assertNotIn("* '''Шифр хранения (MARC 899):'''", wikitext_dedup)
		self.assertNotIn("* '''Форма / жанр (MARC 608):'''", wikitext_dedup)
		self.assertIn("* '''Место издания (MARC 620):'''", wikitext_dedup)
		self.assertIn("* '''Тематика (MARC 6xx):''' Кірыліца ― беларускія выданні", wikitext_dedup)

	def test_description_preserves_library_alternate_terms_and_place_names(self) -> None:
		'''Retain actual NLB aliases with their main headings, including qualified places.'''
		cases = {
			'BY-NLB-br0000015701': {
				'alternate_genres': [
					'КАНОН (религия): КАНОН (рэлігія)',
					'БОГОСЛУЖЕБНАЯ КНИГА: литургическая книга; БОГАСЛУЖЭБНАЯ КНІГА; богаслужбовая кніга; літургічная кніга',
					'ПСАЛОМ: псалм; хвалебная песня',
				],
				'alternate_geo_places': [
					'Гродно — Гродненский район — Гродненская область: Grodno; Городень; Городно; Гродна; Гродна — Гродзенскі раён — Гродзенская вобласць; Grodna; Гародна; Гародня; Горадзен',
					'Комсомольское: Махновка; Камсамольскае',
				],
			},
			'BY-NLB-br0000059041': {
				'alternate_genres': ["ПЬЕСА: П'ЕСА", 'КОМЕДИЯ: КАМЕДЫЯ'],
				'alternate_geo_places': [],
			},
		}
		for record_id, expected_fields in cases.items():
			with self.subTest(record_id=record_id):
				fixture = Path(__file__).parent / 'tests' / 'fixtures' / f'{record_id}-alternates.xml'
				marc_meta = upload_nlb.parse_marcxml(fixture.read_text(encoding='utf-8'))
				wikitext = upload_nlb.build_commons_description(
					{'title': 'Library record', 'fields': []}, marc_meta, record_id, '123',
				)
				for key, expected in expected_fields.items():
					self.assertEqual(marc_meta[key], expected)
					for entry in expected:
						self.assertIn(upload_nlb.escape_wikitext(entry), wikitext)
				self.assertIn('Варианты названий формы / жанра (MARC 908)', wikitext)
				self.assertEqual('Варианты географических названий (MARC 926)' in wikitext, bool(expected_fields['alternate_geo_places']))
				for code in ('BY-NLB-ar', 'BYGNR', 'DVNLB', 'BY-auth'):
					self.assertNotIn(code, wikitext.split('Дополнительные сведения каталога')[0])

	def test_alternate_headings_handle_order_duplicates_and_missing_authorities(self) -> None:
		'''Keep unlinked aliases and subdivisions without conflating distinct authorities.'''
		marcxml = '''<collection><record>
			<datafield tag="926">
				<subfield code="3">place1</subfield><subfield code="9">560</subfield>
				<subfield code="a">Country</subfield><subfield code="b">Region</subfield>
				<subfield code="d">Town</subfield><subfield code="d">Town alias</subfield>
				<subfield code="9">460</subfield><subfield code="d">Town alias</subfield>
			</datafield>
			<datafield tag="908">
				<subfield code="3">genre1</subfield><subfield code="a">Alias|one</subfield>
				<subfield code="x">Subdivision</subfield><subfield code="a">Alias two</subfield>
				<subfield code="9">480</subfield><subfield code="a">Alias two</subfield>
				<subfield code="9">580</subfield><subfield code="a">Main|genre</subfield>
			</datafield>
			<datafield tag="908"><subfield code="3">genre2</subfield><subfield code="a">Alias two</subfield></datafield>
			<datafield tag="908"><subfield code="3">unknown</subfield><subfield code="a">Unlinked genre</subfield></datafield>
			<datafield tag="908"><subfield code="a">No authority</subfield></datafield>
			<datafield tag="926"><subfield code="3">unknown</subfield><subfield code="d">Unlinked place</subfield></datafield>
			<datafield tag="926"><subfield code="d">No place authority</subfield></datafield>
			<datafield tag="908"><subfield code="9">480</subfield><subfield code="5">w</subfield><subfield code="2">vocabulary</subfield></datafield>
			<datafield tag="926"><subfield code="3">place1</subfield><subfield code="9">460</subfield></datafield>
			<datafield tag="608"><subfield code="3">genre1</subfield><subfield code="a">Main|genre</subfield></datafield>
			<datafield tag="608"><subfield code="3">genre2</subfield><subfield code="a">Other genre</subfield></datafield>
			<datafield tag="620"><subfield code="3">place1</subfield><subfield code="d">Main town</subfield></datafield>
		</record></collection>'''
		marc_meta = upload_nlb.parse_marcxml(marcxml)
		self.assertEqual(marc_meta['alternate_genres'], [
			'Main|genre: Alias|one — Subdivision; Alias two',
			'Other genre: Alias two', 'Unlinked genre', 'No authority',
		])
		self.assertEqual(marc_meta['alternate_geo_places'], [
			'Main town: Town — Region — Country; Town alias', 'Unlinked place', 'No place authority',
		])
		wikitext = upload_nlb.build_commons_description(
			{'title': 'Test'}, marc_meta, 'record', 'file',
		)
		self.assertIn('Main{{!}}genre: Alias{{!}}one — Subdivision; Alias two', wikitext)
		self.assertNotIn('vocabulary', wikitext.split('Дополнительные сведения каталога')[0])
		self.assertIn('Система / словарь ($2): vocabulary', wikitext)
		self.assertNotIn('Main|genre', wikitext)

	def test_alternate_headings_omit_empty_and_canonical_only_variants(self) -> None:
		'''Omit variant sections when fields are absent, blank, or repeat the main heading.'''
		marcxml = '''<collection><record>
			<datafield tag="608"><subfield code="3">genre</subfield><subfield code="a">Main genre</subfield></datafield>
			<datafield tag="908"><subfield code="3">genre</subfield><subfield code="a">Main genre</subfield></datafield>
			<datafield tag="926"><subfield code="d"> </subfield></datafield>
		</record></collection>'''
		for xml in ('', marcxml):
			with self.subTest(xml=xml):
				marc_meta = upload_nlb.parse_marcxml(xml)
				self.assertEqual(marc_meta['alternate_genres'], [])
				self.assertEqual(marc_meta['alternate_geo_places'], [])
				wikitext = upload_nlb.build_commons_description({'title': 'Test'}, marc_meta, 'record', 'file')
				self.assertNotIn('Варианты названий формы / жанра (MARC 908)', wikitext)
				self.assertNotIn('Варианты географических названий (MARC 926)', wikitext)

	def test_parse_marcxml_and_description_with_secondary_responsibility_and_accompanying_material(self) -> None:
		"""Verify UNIMARC 200$g secondary responsibility and 215$e accompanying material are parsed and rendered."""
		marcxml = """<?xml version="1.0" encoding="UTF-8"?>
		<collection xmlns="http://www.loc.gov/MARC21/slim">
			<record>
				<controlfield tag="001">BY-NLB-rr333</controlfield>
				<datafield tag="200" ind1="1" ind2=" ">
					<subfield code="a">Сборник материалов</subfield>
					<subfield code="f">составил И. И. Мещерский</subfield>
					<subfield code="g">под редакцией А. А. Сидорова</subfield>
					<subfield code="g">художник В. В. Васильев</subfield>
				</datafield>
				<datafield tag="215" ind1=" " ind2=" ">
					<subfield code="a">48 с.</subfield>
					<subfield code="d">35 см</subfield>
					<subfield code="e">1 прил. (16 с.)</subfield>
					<subfield code="e">1 карта</subfield>
				</datafield>
			</record>
		</collection>
		"""
		marc_meta = upload_nlb.parse_marcxml(marcxml)
		self.assertEqual(
			marc_meta['responsibility'],
			'составил И. И. Мещерский ; под редакцией А. А. Сидорова ; художник В. В. Васильев',
		)
		self.assertEqual(
			marc_meta['physical_description'],
			'48 с. : 35 см + 1 прил. (16 с.) + 1 карта',
		)

		wikitext = upload_nlb.build_commons_description(
			record_meta={'title': 'Сборник материалов', 'fields': []},
			marc_meta=marc_meta,
			marc_id='BY-NLB-rr333',
			file_id='3001',
		)
		self.assertIn(
			"* '''Ответственность / Responsibility:''' составил И. И. Мещерский ; под редакцией А. А. Сидорова ; художник В. В. Васильев",
			wikitext,
		)
		self.assertIn(
			"* '''Физическое описание (MARC 215):''' 48 с. : 35 см + 1 прил. (16 с.) + 1 карта",
			wikitext,
		)

	def test_parse_marcxml_and_description_with_language_roles(self) -> None:
		"""Verify UNIMARC 101 language roles (text 101$a, original 101$c, summary 101$d) are parsed and rendered."""
		marcxml = """<?xml version="1.0" encoding="UTF-8"?>
		<collection xmlns="http://www.loc.gov/MARC21/slim">
			<record>
				<controlfield tag="001">BY-NLB-rr444</controlfield>
				<datafield tag="200" ind1="1" ind2=" ">
					<subfield code="a">Книга</subfield>
				</datafield>
				<datafield tag="101" ind1="1" ind2=" ">
					<subfield code="a">chu</subfield>
					<subfield code="c">heb</subfield>
					<subfield code="d">rus</subfield>
				</datafield>
			</record>
		</collection>
		"""
		marc_meta = upload_nlb.parse_marcxml(marcxml)
		self.assertEqual(marc_meta['languages'], ['chu'])
		self.assertEqual(marc_meta['original_languages'], ['heb'])
		self.assertEqual(marc_meta['summary_languages'], ['rus'])

		wikitext = upload_nlb.build_commons_description(
			record_meta={'title': 'Книга', 'fields': []},
			marc_meta=marc_meta,
			marc_id='BY-NLB-rr444',
			file_id='4001',
		)
		self.assertIn("* '''Код языка (MARC 101$a):''' chu", wikitext)
		self.assertIn("* '''Код языка оригинала (MARC 101$c):''' heb", wikitext)
		self.assertIn("* '''Код языка резюме (MARC 101$d):''' rus", wikitext)

	def test_parse_marcxml_repeated_physical_descriptions_with_substring_extents(self) -> None:
		"""Verify repeated 215 fields are preserved when one entry is a substring of another."""
		marcxml = """<?xml version="1.0" encoding="UTF-8"?>
		<collection xmlns="http://www.loc.gov/MARC21/slim">
			<record>
				<controlfield tag="001">BY-NLB-rr555</controlfield>
				<datafield tag="200" ind1="1" ind2=" ">
					<subfield code="a">Многотомник</subfield>
				</datafield>
				<datafield tag="215" ind1=" " ind2=" ">
					<subfield code="a">148 с.</subfield>
					<subfield code="d">20 см</subfield>
				</datafield>
				<datafield tag="215" ind1=" " ind2=" ">
					<subfield code="a">48 с.</subfield>
					<subfield code="d">20 см</subfield>
				</datafield>
			</record>
		</collection>
		"""
		marc_meta = upload_nlb.parse_marcxml(marcxml)
		self.assertEqual(
			marc_meta['physical_descriptions'],
			['148 с. : 20 см', '48 с. : 20 см'],
		)
		self.assertEqual(
			marc_meta['physical_description'],
			'148 с. : 20 см ; 48 с. : 20 см',
		)

		wikitext = upload_nlb.build_commons_description(
			record_meta={'title': 'Многотомник', 'fields': []},
			marc_meta=marc_meta,
			marc_id='BY-NLB-rr555',
			file_id='5001',
		)
		self.assertIn("* '''Физическое описание (MARC 215):'''", wikitext)
		self.assertIn('** 148 с. : 20 см', wikitext)
		self.assertIn('** 48 с. : 20 см', wikitext)

	def test_build_commons_filename_truncates_within_utf8_byte_limit(self) -> None:
		"""Verify Commons filename sanitization and 240-byte UTF-8 length bound."""
		record_meta = {'title': 'Сборник ' * 40, 'fields': []}
		marc_meta = {'title': 'Сборник ' * 40, 'subtitle': 'Материалы [том 1|2]', 'year': '1888'}
		filename = upload_nlb.build_commons_filename(
			record_meta=record_meta,
			marc_meta=marc_meta,
			marc_id='BY-NLB-rr111',
			file_id='1001',
			part_label='Выпуск 1/2',
		)
		self.assertTrue(filename.endswith('-- BY-NLB-rr111 -- 1001.pdf'))
		self.assertLessEqual(len(filename.encode('utf-8')), 240)
		self.assertNotIn('[', filename)
		self.assertNotIn('|', filename)
		self.assertNotIn('/', filename)
		self.assertEqual(
			upload_nlb.sanitize_commons_filename(
				'Сборник постановлений по Министерству народного просвещения : [в 17 т.]. Алфавитный указатель'
			),
			'Сборник постановлений по Министерству народного просвещения - (в 17 т.). Алфавитный указатель',
		)

	def test_filename_sanitization_handles_query_like_title_blacklist(self) -> None:
		"""Normalize blacklisted punctuation combinations while preserving other titles."""
		for original, expected in (
			('Piano & Violon = Ноты.pdf', 'Piano & Violon - Ноты.pdf'),
			('Pourquoi? Titre = Traduction.pdf', 'Pourquoi? Titre - Traduction.pdf'),
			('A && B = C = D.pdf', 'A && B - C - D.pdf'),
			('A & B = C & D = E.pdf', 'A & B - C & D - E.pdf'),
			('Piano & Violon.pdf', 'Piano & Violon.pdf'),
			('Titre = Traduction.pdf', 'Titre = Traduction.pdf'),
			('Pourquoi?.pdf', 'Pourquoi?.pdf'),
			('Titre = Piano & Violon.pdf', 'Titre = Piano & Violon.pdf'),
		):
			with self.subTest(filename=original):
				filename = upload_nlb.sanitize_commons_filename(original)
				self.assertEqual(filename, expected)
				self.assertNotRegex(filename, r'.*[?&]+[^=]+=[^&]+.*')
				self.assertEqual(upload_nlb.sanitize_commons_filename(filename), filename)

	def test_reported_potpourris_filename_avoids_title_blacklist(self) -> None:
		"""Keep the rejected scan's title and identifiers with only its separator corrected."""
		original = (
			'Potpourris (Ноты) - pour Piano & Violon - № 1―10 par H. Cramer, '
			'№ 11 et les suivans par G. Wichtl. № 38 - Flotow. Rübezahl - par G. Wichtl '
			'(5. Balfe «Zigeunerin = Bohemian(1850) -- BY-NLB-br0000354027 -- 6517951.pdf'
		)
		self.assertRegex(original, r'.*[?&]+[^=]+=[^&]+.*')
		self.assertEqual(upload_nlb.sanitize_commons_filename(original), original.replace(' = ', ' - '))
		filename = upload_nlb.build_commons_filename(
			record_meta={'title': original.split('(1850)')[0] + ' Girl») ' + 'Ноты ' * 20},
			marc_meta={'year': '1850'},
			marc_id='BY-NLB-br0000354027', file_id='6517951',
		)
		self.assertIn('Piano & Violon', filename)
		self.assertIn('Zigeunerin - Bohemian', filename)
		self.assertTrue(filename.endswith('(1850) -- BY-NLB-br0000354027 -- 6517951.pdf'))
		self.assertLessEqual(len(filename.encode('utf-8')), 240)
		self.assertNotRegex(filename, r'.*[?&]+[^=]+=[^&]+.*')

	def test_filename_blacklist_sanitization_includes_combined_metadata(self) -> None:
		"""Handle matches across title, subtitle, part label and the appended ID suffix."""
		for title, subtitle, part_label in (
			('Piano & Violon', 'Zigeunerin = Bohemian Girl', ''),
			('Piano & Violon', '', 'Zigeunerin = Bohemian Girl'),
			('Piano & Violon =', '', ''),
		):
			with self.subTest(title=title, subtitle=subtitle, part_label=part_label):
				filename = upload_nlb.build_commons_filename(
					record_meta={'title': title}, marc_meta={'subtitle': subtitle},
					marc_id='BY-NLB-rr111', file_id='1001', part_label=part_label,
				)
				self.assertNotRegex(filename, r'.*[?&]+[^=]+=[^&]+.*')
				self.assertIn('Piano & Violon', filename)
				self.assertTrue(filename.endswith('-- BY-NLB-rr111 -- 1001.pdf'))

	def test_volume_record_uses_full_original_title_instead_of_volume_number(self) -> None:
		"""Verify records like BY-NLB-br0000002570 use the full original title instead of bare 'T. 2'."""
		html = """<!DOCTYPE html>
		<html><body>
			<!-- <h1 property="name"></h1> -->
			<h3 property="name">Księga pamiątkowa na uczczenie setnej rocznicy urodzin Adama Mickiewicza (1798―1898) / [autorzy: Maryan Dubiecki [et al.]. T. 2</h3>
		</body></html>
		"""
		marcxml = """<?xml version="1.0" encoding="UTF-8"?>
		<collection xmlns="http://www.loc.gov/MARC21/slim">
			<record>
				<controlfield tag="001">BY-NLB-br0000002570</controlfield>
				<datafield tag="100" ind1=" " ind2=" ">
					<subfield code="a">20120329d1899    m  y0bely50      ba</subfield>
				</datafield>
				<datafield tag="200" ind1="0" ind2=" ">
					<subfield code="a">T. 2</subfield>
				</datafield>
				<datafield tag="461" ind1=" " ind2="0">
					<subfield code="1">001BY-NLB-br0000782850</subfield>
					<subfield code="v">T. 2</subfield>
				</datafield>
				<datafield tag="961" ind1=" " ind2="0">
					<subfield code="a">Księga pamiątkowa na uczczenie setnej rocznicy urodzin Adama Mickiewicza (1798―1898) / [autorzy: Maryan Dubiecki [et al.]</subfield>
				</datafield>
			</record>
		</collection>
		"""
		record_meta = upload_nlb.parse_record_html(html, 'BY-NLB-br0000002570')
		marc_meta = upload_nlb.parse_marcxml(marcxml)

		self.assertEqual(
			record_meta['title'],
			'Księga pamiątkowa na uczczenie setnej rocznicy urodzin Adama Mickiewicza (1798―1898) / [autorzy: Maryan Dubiecki [et al.]. T. 2',
		)
		self.assertIn('Księga pamiątkowa', marc_meta['title'])
		self.assertEqual(marc_meta['related_records'][0]['volume'], 'T. 2')

		filename = upload_nlb.build_commons_filename(
			record_meta=record_meta,
			marc_meta=marc_meta,
			marc_id='BY-NLB-br0000002570',
			file_id='3424475',
		)
		self.assertTrue(filename.startswith('Księga pamiątkowa na uczczenie setnej rocznicy urodzin Adama Mickiewicza'))
		self.assertFalse(filename.startswith('T. 2'))
		self.assertTrue(filename.endswith('(1899) -- BY-NLB-br0000002570 -- 3424475.pdf'))

	def test_build_commons_description_includes_all_metadata_and_single_category(self) -> None:
		"""Verify Commons wikitext includes all NLB fields and only [[Category:elib.nlb.by]]."""
		record_meta = upload_nlb.parse_record_html(MOCK_RECORD_HTML_111, 'BY-NLB-rr111')
		marc_meta = upload_nlb.parse_marcxml(MOCK_MARCXML_111)
		wikitext = upload_nlb.build_commons_description(
			record_meta=record_meta,
			marc_meta=marc_meta,
			marc_id='BY-NLB-rr111',
			file_id='1001',
			part_label='Том 1',
		)
		self.assertIn('Минские губернские ведомости', wikitext)
		self.assertIn('Минское губернское правление', wikitext)
		self.assertIn('Губернская типография', wikitext)
		self.assertIn('Периодическая печать — Минская губерния', wikitext)
		self.assertIn('94(476)', wikitext)
		self.assertIn('Иванов, Иван Иванович (1840—1912)', wikitext)
		self.assertIn('BY-NLB-rr111', wikitext)
		self.assertIn('1001', wikitext)
		self.assertEqual(upload_nlb.DEFAULT_UNTIL_YEAR, 1900)
		self.assertEqual(upload_nlb.DEFAULT_LICENSE, 'PD-scan|PD-old-assumed-expired')
		self.assertIn('{{PD-scan|PD-old-assumed-expired}}', wikitext)
		self.assertEqual(wikitext.count('{{PD-scan|PD-old-assumed-expired}}'), 1)
		self.assertNotIn('|permission=', wikitext)
		self.assertNotIn('Russia', wikitext)
		self.assertIn('[[Category:elib.nlb.by]]', wikitext)
		self.assertEqual(wikitext.count('[[Category:'), 1)

	def test_description_preserves_marc_subtitle_missing_from_html_title(self) -> None:
		"""Keep the parsed MARC subtitle in the description before the scan part label."""
		record_meta = upload_nlb.parse_record_html(
			'<h1>Минские губернские ведомости</h1>',
			'BY-NLB-rr111',
		)
		marc_meta = upload_nlb.parse_marcxml(MOCK_MARCXML_111)
		wikitext = upload_nlb.build_commons_description(
			record_meta=record_meta,
			marc_meta=marc_meta,
			marc_id='BY-NLB-rr111',
			file_id='1001',
			part_label='Том 1',
		)
		self.assertIn(
			"|description={{ru|1='''Минские губернские ведомости: официальная часть (Том 1)'''\n",
			wikitext,
		)

	def test_description_does_not_duplicate_subtitle_already_in_title(self) -> None:
		"""Retain a complete HTML title even when its subtitle uses different capitalization."""
		marc_meta = upload_nlb.parse_marcxml(MOCK_MARCXML_111)
		for subtitle in ('официальная часть', 'Официальная часть'):
			with self.subTest(subtitle=subtitle):
				title = f'Минские губернские ведомости : {subtitle}'
				wikitext = upload_nlb.build_commons_description(
					record_meta={'title': title, 'fields': []},
					marc_meta=marc_meta,
					marc_id='BY-NLB-rr111',
					file_id='1001',
				)
				self.assertIn("|description={{ru|1='''" + title + "'''\n", wikitext)
				self.assertEqual(wikitext.casefold().count('официальная часть'), 1)

	def test_description_subtitle_fallback_empty_values_and_escaping(self) -> None:
		"""Handle missing HTML titles and empty subtitles while escaping subtitle pipes."""
		cases = [
			('', '  Очерки и документы  ', 'История Беларуси: Очерки и документы'),
			('История Беларуси', None, 'История Беларуси'),
			('История Беларуси', '', 'История Беларуси'),
			('История Беларуси', '   ', 'История Беларуси'),
			('История Беларуси', 'Очерки | документы', 'История Беларуси: Очерки {{!}} документы'),
		]
		for html_title, subtitle, expected_title in cases:
			with self.subTest(html_title=html_title, subtitle=subtitle):
				wikitext = upload_nlb.build_commons_description(
					record_meta={'title': html_title, 'fields': []},
					marc_meta={'title': 'История Беларуси', 'subtitle': subtitle},
					marc_id='BY-NLB-rr111',
					file_id='1001',
				)
				self.assertIn("|description={{ru|1='''" + expected_title + "'''\n", wikitext)

	def test_multiline_metadata_fields_are_formatted_on_separate_lines(self) -> None:
		"""Verify multi-value HTML and MARC fields are rendered on separate lines, not collapsed."""
		record_meta = upload_nlb.parse_record_html(MOCK_RECORD_HTML_111, 'BY-NLB-rr111')
		marc_meta = upload_nlb.parse_marcxml(MOCK_MARCXML_111)
		marc_meta['udc'] = ['94(476)', '070(476)']

		grnti_field = next(f for f in record_meta['fields'] if f['name'] == 'ГРНТИ')
		self.assertEqual(grnti_field['values'], ['11.01.53', '03.91', '03.17', '03.81.37'])

		wikitext = upload_nlb.build_commons_description(
			record_meta=record_meta,
			marc_meta=marc_meta,
			marc_id='BY-NLB-rr111',
			file_id='1001',
		)
		self.assertNotIn('11.01.53 03.91 03.17 03.81.37', wikitext)
		self.assertIn(
			"* '''ГРНТИ:'''\n" + '\n'.join(
				f'** [https://elib.nlb.by/elib/Grnti/Home?grnti={code} {code}]'
				for code in ('11.01.53', '03.91', '03.17', '03.81.37')
			),
			wikitext,
		)
		self.assertIn(
			"* '''Предмет:'''\n** История Беларуси\n** Периодика",
			wikitext,
		)
		self.assertIn(
			"* '''УДК (MARC 675):'''\n** 94(476)\n** 070(476)",
			wikitext,
		)

	def test_build_commons_description_omits_author_when_unknown_and_uses_html_author(self) -> None:
		"""Verify |author= is omitted when unknown and populated from HTML table when MARC is empty."""
		no_author_meta = {'title': 'Сборник документов', 'fields': [{'name': 'Язык', 'value': 'русский'}]}
		empty_marc = upload_nlb.parse_marcxml('')
		wikitext_no_author = upload_nlb.build_commons_description(
			record_meta=no_author_meta,
			marc_meta=empty_marc,
			marc_id='BY-NLB-rr999',
			file_id='9999',
		)
		self.assertNotIn('|author=', wikitext_no_author)
		self.assertNotIn('{{unknown|author}}', wikitext_no_author)

		html_author_meta = upload_nlb.parse_record_html(MOCK_RECORD_HTML_222, 'BY-NLB-rr222')
		wikitext_html_author = upload_nlb.build_commons_description(
			record_meta=html_author_meta,
			marc_meta=empty_marc,
			marc_id='BY-NLB-rr222',
			file_id='2001',
		)
		self.assertIn('|author=Виленский губернский статистический комитет\n', wikitext_html_author)

	def test_build_search_page_url_and_extract_year_bounds(self) -> None:
		"""Verify custom search URLs and --from-year / --to-year parameters build valid NLB URLs."""
		custom_url = (
			'https://elib.nlb.by/elib/Search/Results?type=AllFields&isElib=true'
			'&daterange%5B%5D=publishDate&publishDatefrom=1812&publishDateto=1917'
		)
		from_yr, to_yr = upload_nlb.extract_year_bounds_from_search_url(custom_url)
		self.assertEqual(from_yr, 1812)
		self.assertEqual(to_yr, 1917)

		page2_url = upload_nlb.build_search_page_url(
			page=2,
			from_year=from_yr,
			to_year=to_yr,
			search_url=custom_url,
		)
		self.assertIn('publishDatefrom=1812', page2_url)
		self.assertIn('publishDateto=1917', page2_url)
		self.assertIn('page=2', page2_url)
		self.assertIn('limit=100', page2_url)

		default_url = upload_nlb.build_search_page_url(page=1, from_year=1850, to_year=1900)
		self.assertIn('publishDatefrom=1850', default_url)
		self.assertIn('publishDateto=1900', default_url)

	def test_state_load_and_atomic_save(self) -> None:
		"""Verify state.json is atomically saved and reloaded accurately."""
		with tempfile.TemporaryDirectory() as tmp_dir:
			state_path = Path(tmp_dir) / 'state.json'
			state = upload_nlb.load_state(state_path, until_year=1900)
			self.assertEqual(state['current_page'], 1)
			self.assertEqual(state['until_year'], 1900)
			self.assertFalse(upload_nlb.is_item_completed(state, 'BY-NLB-rr111', '1001'))

			state['current_page'] = 3
			state['retry_from_page'] = 2
			state['items']['BY-NLB-rr111:1001'] = {'status': 'uploaded'}
			upload_nlb.save_state(state_path, state)

			reloaded = upload_nlb.load_state(state_path, until_year=1900)
			self.assertEqual(reloaded['current_page'], 3)
			self.assertEqual(reloaded['retry_from_page'], 2)
			self.assertTrue(upload_nlb.is_item_completed(reloaded, 'BY-NLB-rr111', '1001'))

			changed_year = upload_nlb.load_state(state_path, until_year=1880)
			self.assertEqual(changed_year['until_year'], 1880)
			self.assertEqual(changed_year['current_page'], 1)
			self.assertIsNone(changed_year.get('retry_from_page'))
			self.assertTrue(upload_nlb.is_item_completed(changed_year, 'BY-NLB-rr111', '1001'))


class TestComponentIntegration(unittest.TestCase):
	'''Level 2 component and integration tests for hash checking, IIIF handshake, and uploader.'''

	def test_check_commons_by_hash_uses_api_without_shell(self) -> None:
		"""Ignore a legacy helper's hash-only output and query Commons in Python."""
		with tempfile.TemporaryDirectory() as tmp_dir:
			pdf_file = Path(tmp_dir) / 'sample.pdf'
			pdf_bytes = b'%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF\n'
			pdf_file.write_bytes(pdf_bytes)
			expected_sha1 = hashlib.sha1(pdf_bytes).hexdigest()

			session = mock.MagicMock()
			session.get.return_value.__enter__.return_value = session.get.return_value
			session.get.return_value.json.return_value = {
				'batchcomplete': '',
				'query': {
					'allimages': [
						{
							'name': 'Existing_NLB_Scan.pdf',
							'title': 'File:Existing NLB Scan.pdf',
							'sha1': expected_sha1,
						}
					]
				},
			}
			with mock.patch.object(Path, 'is_file', return_value=True), mock.patch(
				'subprocess.run',
				return_value=types.SimpleNamespace(stdout=f'sha1:\n{expected_sha1}\n\n'),
			) as shell:
				sha1, matches = upload_nlb.check_commons_by_hash(pdf_file, session=session)
				shell.assert_not_called()
			self.assertEqual(sha1, expected_sha1)
			self.assertEqual(matches, ['File:Existing NLB Scan.pdf'])
			session.get.assert_called_once()
			request = session.get.call_args
			self.assertEqual(request.args, (upload_nlb.COMMONS_API_URL,))
			self.assertEqual(request.kwargs['params']['aisha1'], expected_sha1)
			self.assertEqual(request.kwargs['params']['list'], 'allimages')
			self.assertEqual(request.kwargs['params']['aiprop'], 'sha1')
			self.assertEqual(request.kwargs['timeout'], 30)
			session.get.return_value.raise_for_status.assert_called_once()

	def test_check_commons_by_hash_empty_result_and_mismatch_error(self) -> None:
		"""Accept an empty Commons result and reject a different file's digest."""
		with tempfile.TemporaryDirectory() as tmp_dir:
			pdf_file = Path(tmp_dir) / 'sample.pdf'
			pdf_bytes = b'%PDF-1.5\n%test\n%%EOF\n'
			pdf_file.write_bytes(pdf_bytes)
			expected_sha1 = hashlib.sha1(pdf_bytes).hexdigest()

			mock_session = mock.MagicMock()
			mock_response = mock.MagicMock()
			mock_response.__enter__.return_value = mock_response
			mock_response.json.return_value = {
				'batchcomplete': '',
				'query': {'allimages': []},
			}
			mock_session.get.return_value = mock_response

			sha1, matches = upload_nlb.check_commons_by_hash(
				pdf_file,
				session=mock_session,
			)
			self.assertEqual(sha1, expected_sha1)
			self.assertEqual(matches, [])

			with self.assertRaises(ValueError):
				upload_nlb.parse_commons_allimages_response(
					{'query': {'allimages': [{'title': 'File:Bad.pdf', 'sha1': '0' * 40}]}},
					expected_sha1,
				)

	def test_commons_hash_response_rejects_incomplete_results(self) -> None:
		"""Do not interpret malformed or incomplete API results as no duplicates."""
		for payload in (
			[],
			{},
			{'error': {'code': 'badvalue', 'info': 'Invalid hash'}},
			{'errors': [{'code': 'badvalue'}], 'query': {'allimages': []}},
			{'query': None},
			{'query': {'allimages': None}},
			{'query': {'allimages': {}}},
			{'query': {'allimages': [{}]}},
			{'query': {'allimages': ['File:Invalid.pdf']}},
			{'query': {'allimages': [{'title': 'File:Missing hash.pdf'}]}},
			{'query': {'allimages': [{'title': '', 'sha1': '0' * 40}]}},
			{'query': {'allimages': [{'title': 123, 'sha1': '0' * 40}]}},
			{'query': {'allimages': []}, 'continue': {'aicontinue': 'Next.pdf'}},
		):
			with self.subTest(payload=payload), self.assertRaises(ValueError):
				upload_nlb.parse_commons_allimages_response(payload, '0' * 40)

	def test_nlb_client_handshake_and_pdf_download(self) -> None:
		'''Verify NlbClient executes the 5-step port-8070 IIIF handshake and streams the PDF.'''
		pdf_payload = pdf_bytes()
		mock_session = mock.MagicMock()
		mock_session.headers = {}

		post_resp = mock.MagicMock()
		post_resp.status_code = 200
		mock_session.post.return_value = post_resp

		manifest_resp = mock.MagicMock()
		manifest_resp.json.return_value = {
			'rendering': [
				{
					'id': '/iiif/file/1001/bytes?attachmentName=scan1001',
					'type': None,
					'format': 'application/pdf',
				}
			]
		}
		page_views_resp = mock.MagicMock()
		download_attempt_resp = mock.MagicMock()

		stream_resp = mock.MagicMock()
		stream_resp.__enter__.return_value = stream_resp
		stream_resp.__exit__.return_value = False
		stream_resp.headers = {'Content-Length': str(len(pdf_payload))}
		stream_resp.iter_content.return_value = [pdf_payload[:12], pdf_payload[12:]]

		mock_session.get.side_effect = [
			manifest_resp,
			page_views_resp,
			download_attempt_resp,
			stream_resp,
		]

		client = upload_nlb.NlbClient(session=mock_session)
		with tempfile.TemporaryDirectory() as tmp_dir:
			target_pdf = Path(tmp_dir) / 'downloaded.pdf'
			written = client.download_pdf_scan('BY-NLB-rr111', '1001', target_pdf)
			self.assertEqual(written, len(pdf_payload))
			self.assertEqual(target_pdf.read_bytes(), pdf_payload)

		mock_session.post.assert_called_once()
		self.assertEqual(mock_session.get.call_count, 4)
		download_call = mock_session.get.call_args_list[3]
		self.assertEqual(
			download_call.args[0],
			'https://elib.nlb.by:8070/iiif/file/1001/bytes?attachmentName=scan1001',
		)
		self.assertEqual(
			download_call.kwargs['headers']['Authorization'],
			upload_nlb.VIEWER_BASIC_AUTH,
		)

	def test_nlb_client_rejects_non_pdf(self) -> None:
		'''Retry an unexpected HTML response and leave the scan pending after exhaustion.'''
		mock_session = mock.MagicMock()
		mock_session.headers = {}
		mock_session.post.return_value = mock.MagicMock()

		manifest_resp = mock.MagicMock()
		manifest_resp.json.return_value = {'rendering': []}
		ok_resp = mock.MagicMock()

		bad_stream = mock.MagicMock()
		bad_stream.__enter__.return_value = bad_stream
		bad_stream.__exit__.return_value = False
		bad_stream.headers = {'Content-Length': str(len(b'<html>Error</html>'))}
		bad_stream.iter_content.return_value = [b'<html>Error</html>']

		mock_session.get.side_effect = [manifest_resp, ok_resp, ok_resp, bad_stream] * 5
		client = upload_nlb.NlbClient(session=mock_session)

		with tempfile.TemporaryDirectory() as tmp_dir, mock.patch('upload.time.sleep'):
			target = Path(tmp_dir) / 'bad.pdf'
			with self.assertRaisesRegex(upload_nlb.DownloadRetriesExhausted, 'not a valid PDF'):
				client.download_pdf_scan('BY-NLB-rr111', '1001', target)
			self.assertFalse(target.exists())
			self.assertEqual(mock_session.get.call_count, 20)

	def test_nlb_client_fetch_record_metadata_with_toc_fallback(self) -> None:
		'''Verify NlbClient.fetch_record_metadata falls back to /TOC tab when MARCXML lacks 327.'''
		mock_session = mock.MagicMock()
		mock_session.headers = {}

		record_resp = mock.MagicMock()
		record_resp.status_code = 200
		record_resp.text = MOCK_RECORD_HTML_111

		marc_resp = mock.MagicMock()
		marc_resp.status_code = 200
		marc_resp.text = MOCK_MARCXML_111
		marc_resp.content = MOCK_MARCXML_111.encode()

		toc_resp = mock.MagicMock()
		toc_resp.status_code = 200
		toc_resp.text = '<div class="tab-pane toc-tab"><ul class="toc"><li>Содержание: 1) Глава 1; 2) Глава 2</li></ul></div>'

		mock_session.get.side_effect = [record_resp, marc_resp, toc_resp]
		client = upload_nlb.NlbClient(session=mock_session)

		record_meta, marc_meta = client.fetch_record_metadata('BY-NLB-rr111')
		self.assertEqual(record_meta['contents'], ['1) Глава 1', '2) Глава 2'])

	def test_commons_uploader_retry_and_duplicate_handling(self) -> None:
		"""Verify CommonsUploader retries transient errors and parses duplicate warnings."""
		fake_UploadError = type('UploadError', (Exception,), {})
		fake_pwb = types.SimpleNamespace(
			exceptions=types.SimpleNamespace(UploadError=fake_UploadError),
		)

		uploader = upload_nlb.CommonsUploader.__new__(upload_nlb.CommonsUploader)
		uploader.pywikibot = fake_pwb
		uploader.site = mock.MagicMock()
		uploader.site.is_uploaddisabled.return_value = False

		file_page = mock.MagicMock()
		file_page.exists.return_value = False

		transient_err = fake_UploadError('Stash failed temporarily')
		transient_err.code = 'stashfailed'

		dup_err = fake_UploadError("Uploaded file is a duplicate of ['Old_Scan.pdf'].")
		dup_err.code = 'duplicate'
		dup_err.message = "Uploaded file is a duplicate of ['Old_Scan.pdf']."

		driver = mock.Mock()
		driver.upload.side_effect = [transient_err, dup_err]
		uploader._upload_driver_class = mock.Mock(return_value=driver)
		fake_pwb.FilePage = mock.MagicMock(return_value=file_page)

		with mock.patch('time.sleep') as sleep_mock:
			result = uploader.upload(
				Path('/tmp/fake.pdf'),
				'New_Scan.pdf',
				'=={{int:filedesc}}==',
				'summary',
			)
			sleep_mock.assert_called_once_with(60)

		self.assertEqual(result['status'], 'duplicate_sha1')
		self.assertEqual(result['duplicates'], ['Old_Scan.pdf'])


class TestCommonsHashLookup(unittest.TestCase):
	'''Exercise direct Commons lookups with real files and mocked HTTP responses.'''

	def setUp(self) -> None:
		"""Create a PDF fixture and an isolated HTTP session for each test."""
		tmp_dir = tempfile.TemporaryDirectory()
		self.addCleanup(tmp_dir.cleanup)
		self.tmp_path = Path(tmp_dir.name)
		self.pdf_file = self.tmp_path / 'scan.pdf'
		self.pdf_file.write_bytes(b'%PDF-1.4\nMock scan\n%%EOF\n')
		self.digest = upload_nlb.calculate_sha1(self.pdf_file)
		self.session = mock.MagicMock()

	def response(self, payload: object, status: int = 200) -> upload_nlb.requests.Response:
		"""Create a real Requests response to exercise status and JSON handling."""
		response = upload_nlb.requests.Response()
		response.status_code = status
		response._content = json.dumps(payload).encode('utf-8')
		response._content_consumed = True
		return response

	def test_timeout_and_server_error_retry_without_rehashing(self) -> None:
		"""Recover from transient failures while computing the file hash once."""
		self.session.get.side_effect = [
			upload_nlb.requests.exceptions.ReadTimeout('Commons timed out'),
			self.response({}, status=503),
			self.response({'query': {'allimages': []}}),
		]
		with mock.patch('upload.time.sleep') as sleep, mock.patch(
			'upload.calculate_sha1', wraps=upload_nlb.calculate_sha1,
		) as hash_file:
			self.assertEqual(upload_nlb.check_commons_by_hash(self.pdf_file, self.session), (self.digest, []))
			hash_file.assert_called_once_with(self.pdf_file)
			self.assertEqual(sleep.call_args_list, [mock.call(5), mock.call(10)])
		self.assertEqual(self.session.get.call_count, 3)

	def test_empty_response_is_retried(self) -> None:
		"""Retry an empty HTTP body instead of interpreting it as no duplicates."""
		empty = self.response(None)
		empty._content = b''
		self.session.get.side_effect = [empty, self.response({'query': {'allimages': []}})]
		with mock.patch('upload.time.sleep') as sleep:
			self.assertEqual(upload_nlb.check_commons_by_hash(self.pdf_file, self.session), (self.digest, []))
			sleep.assert_called_once_with(5)

	def test_permanent_errors_are_not_retried(self) -> None:
		"""Reject permanent HTTP/API errors and bad certificates immediately."""
		for response, error_type in (
			(self.response({}, status=403), upload_nlb.requests.exceptions.HTTPError),
			(self.response({'error': {'code': 'badvalue'}}), ValueError),
			(self.response({'query': {'allimages': [{}]}}), ValueError),
			(upload_nlb.requests.exceptions.SSLError('invalid certificate'), upload_nlb.requests.exceptions.SSLError),
		):
			with self.subTest(error=error_type.__name__), mock.patch('upload.time.sleep') as sleep:
				self.session.get.reset_mock()
				self.session.get.side_effect = [response]
				with self.assertRaises(error_type):
					upload_nlb.check_commons_by_hash(self.pdf_file, self.session)
				sleep.assert_not_called()
				self.session.get.assert_called_once()

	def test_exhausted_lookup_does_not_upload_and_can_resume(self) -> None:
		'''Leave the item pending after lookup failure and skip it on a later match.'''
		error = upload_nlb.requests.exceptions.ReadTimeout('Commons timed out')
		self.session.get.side_effect = error
		client = FakeNlbClient()
		client.session = self.session
		uploader = FakeCommonsUploader()
		kwargs = {
			'client': client,
			'uploader': uploader,
			'state': upload_nlb.default_state(),
			'state_path': self.tmp_path / 'state.json',
			'staging_dir': self.tmp_path / 'staging',
			'marc_id': 'BY-NLB-rr111',
			'file_id': '1001',
			'part_label': '',
			'record_meta': upload_nlb.parse_record_html(MOCK_RECORD_HTML_111, 'BY-NLB-rr111'),
			'marc_meta': upload_nlb.parse_marcxml(MOCK_MARCXML_111),
		}
		with mock.patch('upload.time.sleep') as sleep:
			self.assertEqual(upload_nlb.process_single_scan(**kwargs), 'verification_failed')
			self.assertEqual(self.session.get.call_count, 5)
			self.assertEqual(sleep.call_args_list, [mock.call(5), mock.call(10), mock.call(20), mock.call(40)])
		self.assertEqual(uploader.uploads, [])
		self.assertEqual(list(kwargs['staging_dir'].iterdir()), [])
		state = upload_nlb.load_state(kwargs['state_path'])
		self.assertEqual(state['current_item_key'], 'BY-NLB-rr111:1001')
		self.assertFalse(upload_nlb.is_item_completed(state, 'BY-NLB-rr111', '1001'))

		downloaded_payload = b'%PDF-1.4\nRecord=BY-NLB-rr111 File=1001\n%%EOF\n'
		self.session.get.side_effect = [self.response({'query': {'allimages': [{
			'title': 'File:Existing scan.pdf',
			'sha1': hashlib.sha1(downloaded_payload).hexdigest(),
		}]}})]
		kwargs['state'] = state
		self.assertEqual(upload_nlb.process_single_scan(**kwargs), 'duplicate_sha1')
		self.assertEqual(uploader.uploads, [])
		self.assertEqual(list(kwargs['staging_dir'].iterdir()), [])
		self.assertTrue(upload_nlb.is_item_completed(upload_nlb.load_state(kwargs['state_path']), 'BY-NLB-rr111', '1001'))


class TestViewerCookies(unittest.TestCase):
	'''Reuse browser cookies without replacing the authenticated viewer session.'''

	def setUp(self) -> None:
		"""Create a local cookie export and a real session with no network requests."""
		self.directory = tempfile.TemporaryDirectory()
		self.addCleanup(self.directory.cleanup)
		self.cookie_path = Path(self.directory.name) / 'viewer-cookies.txt'
		self.session = upload_nlb.requests.Session()
		self.addCleanup(self.session.close)

	def write_cookies(self, rows: list[str]) -> None:
		"""Write Netscape-format mock browser cookies, preserving tab-delimited fields."""
		self.cookie_path.write_text('# Netscape HTTP Cookie File\n' + '\n'.join(rows) + '\n', encoding='utf-8')

	def cookie_header(self, url: str) -> str:
		"""Prepare a real request to inspect domain, path and secure cookie filtering."""
		request = upload_nlb.requests.Request('GET', url)
		return self.session.prepare_request(request).headers.get('Cookie', '')

	def test_import_scopes_browser_cookies_and_preserves_authenticated_session(self) -> None:
		"""Keep HttpOnly and session cookies, honoring scope without posting a guest session."""
		self.write_cookies([
			'#HttpOnly_.nlb.by\tTRUE\t/\tTRUE\t4102444800\telib_session_id\tviewer-secret',
			'#HttpOnly_elib.nlb.by\tFALSE\t/\tTRUE\t\tJSESSIONID\tauth-secret',
			'elib.nlb.by\tFALSE\t/\tTRUE\t0\tzero_expiry\tsession-secret',
			'.nlb.by\tTRUE\t/iiif\tTRUE\t4102444800\tpath_cookie\tpath-secret',
			'www.nlb.by\tFALSE\t/\tTRUE\t4102444800\tportal_cookie\tportal-secret',
			'nlb.by\tFALSE\t/\tTRUE\t4102444800\tJSESSIONID\troot-host-only-secret',
			'.nlb.by\tTRUE\t/\tTRUE\t1\texpired_cookie\texpired-secret',
			'.wikimedia.org\tTRUE\t/\tTRUE\t4102444800\tforeign_cookie\tforeign-secret',
			'.notnlb.by\tTRUE\t/\tTRUE\t4102444800\tlookalike_cookie\tlookalike-secret',
		])
		client = upload_nlb.NlbClient(session=self.session, viewer_cookies=self.cookie_path)
		with mock.patch.object(self.session, 'post') as post:
			client.ensure_viewer_session()
			post.assert_not_called()
		cookies = {cookie.name: cookie for cookie in self.session.cookies}
		self.assertEqual(set(cookies), {'elib_session_id', 'JSESSIONID', 'zero_expiry', 'path_cookie'})
		self.assertEqual(cookies['elib_session_id'].domain, '.nlb.by')
		self.assertEqual(cookies['elib_session_id'].expires, 4102444800)
		self.assertTrue(cookies['elib_session_id'].secure)
		self.assertTrue(cookies['elib_session_id'].has_nonstandard_attr('HTTPOnly'))
		self.assertIsNone(cookies['JSESSIONID'].expires)
		self.assertIsNone(cookies['zero_expiry'].expires)
		self.assertEqual(cookies['path_cookie'].path, '/iiif')
		self.assertFalse(cookies['JSESSIONID'].domain_specified)
		viewer_header = self.cookie_header('https://elib.nlb.by:8070/iiif/file/123/bytes')
		for cookie in ('elib_session_id=viewer-secret', 'JSESSIONID=auth-secret', 'zero_expiry=session-secret', 'path_cookie=path-secret'):
			self.assertIn(cookie, viewer_header)
		self.assertNotIn('portal_cookie', viewer_header)
		self.assertNotIn('root-host-only-secret', viewer_header)
		self.assertNotIn('path_cookie', self.cookie_header('https://elib.nlb.by/elib/Record/example'))
		self.assertNotIn('JSESSIONID', self.cookie_header('https://www.nlb.by/'))
		self.assertEqual(self.cookie_header('http://elib.nlb.by:8070/iiif/file/123/bytes'), '')
		self.assertEqual(self.cookie_header('https://commons.wikimedia.org/w/api.php'), '')

	def test_rejects_missing_expired_or_inapplicable_viewer_session(self) -> None:
		"""Explain unusable exports instead of silently replacing them with guest cookies."""
		for viewer_cookie in (
			'',
			'.nlb.by\tTRUE\t/\tTRUE\t1\telib_session_id\texpired-secret',
			'www.nlb.by\tFALSE\t/\tTRUE\t4102444800\telib_session_id\twrong-domain-secret',
			'nlb.by\tFALSE\t/\tTRUE\t4102444800\telib_session_id\troot-host-only-secret',
			'.nlb.by\tTRUE\t/account\tTRUE\t4102444800\telib_session_id\twrong-path-secret',
		):
			with self.subTest(cookie=viewer_cookie):
				self.write_cookies([
					'elib.nlb.by\tFALSE\t/\tTRUE\t4102444800\tJSESSIONID\tauth-secret',
					viewer_cookie,
				])
				with upload_nlb.requests.Session() as session, mock.patch.object(session, 'post') as post:
					with self.assertRaises(ValueError) as raised:
						upload_nlb.NlbClient(session=session, viewer_cookies=self.cookie_path)
					self.assertIn('elib_session_id', str(raised.exception))
					self.assertIn('export', str(raised.exception).lower())
					self.assertNotIn('secret', str(raised.exception))
					post.assert_not_called()

	def test_invalid_cookie_export_does_not_disclose_cookie_contents(self) -> None:
		"""Sanitize both invalid headers and malformed cookie rows before reporting them."""
		for contents in (
			'not-a-cookie-file secret-auth-value\n',
			'# Netscape HTTP Cookie File\n.nlb.by\tTRUE\t/\tTRUE\tbroken-expiry\telib_session_id\tsecret-auth-value\n',
		):
			with self.subTest(contents=contents):
				self.cookie_path.write_text(contents, encoding='utf-8')
				with warnings.catch_warnings(record=True) as emitted_warnings:
					warnings.simplefilter('always')
					with self.assertRaises(ValueError) as raised:
						upload_nlb.NlbClient(session=self.session, viewer_cookies=self.cookie_path)
					self.assertIn('Netscape', str(raised.exception))
					self.assertNotIn('secret-auth-value', ''.join(traceback.format_exception(raised.exception)))
					self.assertNotIn('secret-auth-value', '\n'.join(str(item.message) for item in emitted_warnings))

	def test_download_sends_browser_cookies_only_to_library(self) -> None:
		'''Exercise authenticated download and Commons lookup through prepared requests.'''
		self.write_cookies([
			'#HttpOnly_.nlb.by\tTRUE\t/\tTRUE\t4102444800\telib_session_id\tviewer-secret',
			'elib.nlb.by\tFALSE\t/\tTRUE\t0\tJSESSIONID\tauth-secret',
		])
		client = upload_nlb.NlbClient(session=self.session, viewer_cookies=self.cookie_path)
		payload = pdf_bytes()
		requests_seen = []

		def send(request, **_) -> upload_nlb.requests.Response:
			"""Return local HTTP responses after Requests has applied the cookie policy."""
			requests_seen.append(request)
			self.assertEqual(request.method, 'GET')
			response = upload_nlb.requests.Response()
			response.status_code = 200
			response.request = request
			response.url = request.url
			response._content_consumed = True
			if '/iiif/file/' in request.url:
				response._content = payload
				response.headers['Content-Length'] = str(len(payload))
			elif 'commons.wikimedia.org' in request.url:
				response._content = b'{"query":{"allimages":[]}}'
			else:
				response._content = b'{"rendering":[]}'
			return response

		with mock.patch.object(self.session, 'send', side_effect=send):
			target = Path(self.directory.name) / 'scan.pdf'
			self.assertEqual(client.download_pdf_scan('BY-NLB-br0000059074', '12541585', target), len(payload))
			self.assertEqual(target.read_bytes(), payload)
			self.assertEqual(upload_nlb.check_commons_by_hash(target, session=self.session), (hashlib.sha1(payload).hexdigest(), []))
		self.assertEqual(len(requests_seen), 5)
		for request in requests_seen[:-1]:
			self.assertIn('elib_session_id=viewer-secret', request.headers.get('Cookie', ''))
			self.assertIn('JSESSIONID=auth-secret', request.headers.get('Cookie', ''))
		self.assertNotIn('Cookie', requests_seen[-1].headers)

	def test_cli_passes_viewer_cookie_export_to_client(self) -> None:
		'''Wire the optional cookie path into the client while retaining dry-run behavior.'''
		with (
			mock.patch('upload.NlbClient') as client_type,
			mock.patch('upload.CommonsUploader') as uploader_type,
			mock.patch('upload.iterate_and_upload', return_value={'dry_run_ready': 1}) as iterate,
			mock.patch('sys.stdout', new_callable=io.StringIO),
		):
			self.assertEqual(upload_nlb.main(['--viewer-cookies', str(self.cookie_path), '--dry-run']), 0)
			client_type.assert_called_once_with(viewer_cookies=self.cookie_path, archive=mock.ANY)
			uploader_type.assert_not_called()
			self.assertIs(iterate.call_args.kwargs['client'], client_type.return_value)
			self.assertTrue(iterate.call_args.kwargs['dry_run'])


class TestNlbDownloadRetries(unittest.TestCase):
	'''Exercise download retries with mocked responses and real temporary files.'''

	def setUp(self) -> None:
		'''Prepare a client and successful IIIF responses without network access.'''
		self.session = mock.MagicMock()
		self.session.headers = {}
		self.client = upload_nlb.NlbClient(session=self.session)
		self.manifest = mock.MagicMock()
		self.manifest.json.return_value = {'rendering': []}
		self.ok_response = mock.MagicMock()
		self.payload = pdf_bytes()

	def stream_response(self, payload: bytes) -> mock.MagicMock:
		"""Return a context-managed PDF response split into two chunks."""
		response = mock.MagicMock()
		response.__enter__.return_value = response
		response.headers = {'Content-Length': str(len(payload))}
		response.iter_content.return_value = [payload[:12], payload[12:]]
		return response

	def download_responses(self, stream: mock.MagicMock) -> list[mock.MagicMock]:
		"""Return the manifest, statistics and stream responses for one attempt."""
		return [self.manifest, self.ok_response, self.ok_response, stream]

	def http_error_response(self, status: int) -> upload_nlb.requests.Response:
		"""Build a real HTTP failure with a consumed body and observable cleanup."""
		response = upload_nlb.requests.Response()
		response.status_code = status
		response.url = f'{upload_nlb.VIEWER_BASE_URL}/iiif/file/7837809/bytes?attachmentName=ch_2'
		response._content = b'Temporary library error'
		response._content_consumed = True
		response.close = mock.Mock(wraps=response.close)
		return response

	def test_transient_http_errors_retry_at_each_download_stage(self) -> None:
		"""Retry temporary manifest, event and PDF errors through the complete download path."""
		self.manifest.json.return_value = {'rendering': [{'id': '/iiif/file/7837809/bytes?attachmentName=ch_2'}]}
		for status in (500, 502, 503, 504):
			for stage in range(4):
				with self.subTest(status=status, stage=stage), tempfile.TemporaryDirectory() as tmp_dir:
					self.session.reset_mock()
					self.client._viewer_session_initialized = False
					failed = self.http_error_response(status)
					success = self.download_responses(self.stream_response(self.payload))
					self.session.get.side_effect = [*success[:stage], failed, *success]
					target = Path(tmp_dir) / 'scan.pdf'
					target.write_bytes(b'Partial previous scan')
					with mock.patch('upload.time.sleep') as sleep, mock.patch('sys.stdout', io.StringIO()) as output:
						sleep.side_effect = lambda _: self.assertFalse(target.exists())
						written = self.client.download_pdf_scan('BY-NLB-br0000370146', '7837809', target)
					self.assertEqual(written, len(self.payload))
					self.assertEqual(target.read_bytes(), self.payload)
					self.assertEqual(self.session.get.call_count, stage + 5)
					self.assertEqual(self.session.get.call_args.args[0], failed.url)
					self.session.post.assert_called_once()
					failed.close.assert_called()
					sleep.assert_called_once_with(5)
					self.assertIn('Download attempt 1/5 failed (HTTPError:', output.getvalue())
					self.assertIn(str(status), output.getvalue())

	def test_http_and_network_errors_share_one_download_retry_budget(self) -> None:
		"""Switching between network and HTTP failures must not reset the five-attempt limit."""
		http_errors = []
		for status in (500, 503, 504):
			try:
				self.http_error_response(status).raise_for_status()
			except upload_nlb.requests.HTTPError as error:
				http_errors.append(error)
		errors = [
			upload_nlb.requests.ReadTimeout('manifest timeout'), http_errors[0],
			upload_nlb.requests.exceptions.ChunkedEncodingError('incomplete transfer'), *http_errors[1:],
		]
		with tempfile.TemporaryDirectory() as tmp_dir, mock.patch.object(
			self.client, '_download_pdf_scan_once', side_effect=errors,
		) as attempt, mock.patch('upload.time.sleep') as sleep, mock.patch('sys.stdout', io.StringIO()):
			with self.assertRaises(upload_nlb.DownloadRetriesExhausted) as raised:
				self.client.download_pdf_scan('record', 'file', Path(tmp_dir) / 'scan.pdf')
		self.assertEqual(attempt.call_count, 5)
		self.assertEqual(raised.exception.attempts, 5)
		self.assertIs(raised.exception.last_error, errors[-1])
		self.assertIs(raised.exception.__cause__, errors[-1])
		self.assertEqual(sleep.call_args_list, [mock.call(delay) for delay in (5, 10, 20, 40)])
		for error in http_errors:
			error.response.close.assert_called_once_with()

	def test_http_500_exhaustion_continues_batch_and_retries_only_pending_scan(self) -> None:
		"""Use the real downloader to defer a persistent HTTP 500 and preserve completed PDFs."""
		client = FakeNlbClient()
		client.download_pdf_scan = mock.Mock(wraps=self.client.download_pdf_scan)
		uploader = FakeCommonsUploader()
		responses = self.download_responses(self.stream_response(self.payload))
		failures = [self.http_error_response(500) for _ in range(5)]
		for failure in failures:
			responses.extend(self.download_responses(failure))
		responses.extend(self.download_responses(self.stream_response(self.payload)))
		self.session.get.side_effect = responses
		with tempfile.TemporaryDirectory() as tmp_dir, mock.patch('upload.time.sleep') as sleep, mock.patch(
			'upload.check_commons_by_hash', side_effect=lambda path, **_: (upload_nlb.calculate_sha1(path), []),
		) as hash_check, mock.patch('sys.stdout', io.StringIO()):
			root = Path(tmp_dir)
			kwargs = {'client': client, 'uploader': uploader, 'state_path': root / 'state.json', 'staging_dir': root / 'staging'}
			counts = upload_nlb.iterate_and_upload(**kwargs)
			self.assertEqual(counts['uploaded'], 2)
			self.assertEqual(counts['download_failed'], 1)
			self.assertEqual(hash_check.call_count, 2)
			self.assertEqual(self.session.get.call_count, 28)
			self.assertEqual(sleep.call_args_list, [mock.call(delay) for delay in (5, 10, 20, 40)])
			state = upload_nlb.load_state(kwargs['state_path'])
			entry = state['items']['BY-NLB-rr222:2001']
			self.assertEqual(entry['status'], 'download_failed')
			self.assertEqual(entry['attempts'], 5)
			self.assertEqual(entry['error_type'], 'HTTPError')
			self.assertIn('500', entry['error'])
			self.assertIn('Temporary library error', entry['error'])
			self.assertEqual(state['retry_from_page'], 1)
			self.assertFalse(upload_nlb.is_item_completed(state, 'BY-NLB-rr222', '2001'))
			self.assertEqual(list(kwargs['staging_dir'].iterdir()), [])
			for failure in failures:
				failure.close.assert_called()

			self.session.get.reset_mock()
			client.download_pdf_scan.reset_mock()
			self.session.get.side_effect = self.download_responses(self.stream_response(self.payload))
			counts = upload_nlb.iterate_and_upload(**kwargs)
			self.assertEqual(counts['uploaded'], 1)
			self.assertEqual(counts['download_failed'], 0)
			client.download_pdf_scan.assert_called_once()
			self.assertEqual(client.download_pdf_scan.call_args.args[:2], ('BY-NLB-rr222', '2001'))
			self.assertEqual(self.session.get.call_count, 4)
			self.assertEqual(len(uploader.uploads), 3)
			state = upload_nlb.load_state(kwargs['state_path'])
			self.assertTrue(upload_nlb.is_item_completed(state, 'BY-NLB-rr222', '2001'))
			self.assertNotIn('retry_from_page', state)
			self.assertEqual(list(kwargs['staging_dir'].iterdir()), [])

	def test_permanent_http_download_errors_are_not_retried(self) -> None:
		"""Keep rejections and unclassified server errors outside the transient retry policy."""
		for status in (400, 401, 403, 404, 429, 501, 505):
			with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp_dir:
				self.session.get.reset_mock()
				response = self.http_error_response(status)
				self.session.get.side_effect = self.download_responses(response)
				with mock.patch('upload.time.sleep') as sleep:
					with self.assertRaises(upload_nlb.requests.HTTPError) as raised:
						self.client.download_pdf_scan('record', 'file', Path(tmp_dir) / 'scan.pdf')
				self.assertIs(raised.exception.response, response)
				self.assertEqual(self.session.get.call_count, 4)
				sleep.assert_not_called()

	def test_session_setup_http_error_is_not_retried(self) -> None:
		"""A session-creation POST failure remains outside the per-scan GET retry budget."""
		response = self.http_error_response(500)
		self.session.post.return_value = response
		with tempfile.TemporaryDirectory() as tmp_dir, mock.patch('upload.time.sleep') as sleep:
			with self.assertRaises(upload_nlb.requests.HTTPError) as raised:
				self.client.download_pdf_scan('record', 'file', Path(tmp_dir) / 'scan.pdf')
		self.assertIs(raised.exception.response, response)
		self.session.post.assert_called_once()
		self.session.get.assert_not_called()
		sleep.assert_not_called()

	def test_manifest_timeout_then_success(self) -> None:
		"""Retry the reported manifest ReadTimeout and write the complete PDF."""
		self.session.get.side_effect = [
			upload_nlb.requests.exceptions.ReadTimeout('manifest timed out'),
			*self.download_responses(self.stream_response(self.payload)),
		]
		with tempfile.TemporaryDirectory() as tmp_dir, mock.patch('upload.time.sleep') as sleep:
			target = Path(tmp_dir) / 'scan.pdf'
			written = self.client.download_pdf_scan('BY-NLB-br0000059041', '12541593', target)
			self.assertEqual(written, len(self.payload))
			self.assertEqual(target.read_bytes(), self.payload)
			sleep.assert_called_once_with(5)
		self.assertEqual(self.session.get.call_count, 5)
		self.assertEqual(self.session.get.call_args_list[0], self.session.get.call_args_list[1])
		self.session.post.assert_called_once()

	def test_download_progress_omits_unknown_size(self) -> None:
		"""Stream normally without inventing a size when Content-Length is unusable."""
		for header in (None, '', 'unknown', '-1'):
			with self.subTest(content_length=header), tempfile.TemporaryDirectory() as tmp_dir, mock.patch('upload.time.sleep'):
				response = self.stream_response(self.payload)
				response.headers = {} if header is None else {'Content-Length': header}
				self.session.get.side_effect = self.download_responses(response)
				with mock.patch('sys.stdout', new_callable=io.StringIO) as output:
					written = self.client.download_pdf_scan('BY-NLB-rr111', '1001', Path(tmp_dir) / 'scan.pdf')
				self.assertEqual(written, len(self.payload))
				self.assertIn('[BY-NLB-rr111:1001] Downloading scan:\n', output.getvalue())
				self.assertNotIn(' MB', output.getvalue())
		self.session.head.assert_not_called()

	def test_page_views_timeout_is_deferred_after_five_attempts(self) -> None:
		'''The reported statistics timeout leaves a pending item without hashing or uploading.'''
		error = upload_nlb.requests.exceptions.ReadTimeout('PAGE_VIEWS timed out')
		self.session.get.side_effect = [self.manifest, error] * 5
		uploader = FakeCommonsUploader()
		with tempfile.TemporaryDirectory() as tmp_dir, mock.patch('upload.time.sleep') as sleep, mock.patch(
			'upload.check_commons_by_hash',
		) as hash_check, mock.patch('sys.stdout', new_callable=io.StringIO) as output:
			root = Path(tmp_dir)
			state = upload_nlb.default_state()
			state['current_page'] = 4
			status = upload_nlb.process_single_scan(
				client=self.client, uploader=uploader, state=state,
				state_path=root / 'state.json', staging_dir=root / 'staging',
				marc_id='BY-NLB-br0000078549', file_id='10284033', part_label='',
				record_meta={'title': 'Reported volume'}, marc_meta={'date': '1800'},
			)
			self.assertEqual(status, 'download_failed')
			self.assertEqual(sleep.call_args_list, [mock.call(5), mock.call(10), mock.call(20), mock.call(40)])
			self.assertEqual(self.session.get.call_count, 10)
			self.assertEqual(
				[call.args[0] for call in self.session.get.call_args_list],
				[
					f'{upload_nlb.VIEWER_BASE_URL}/iiif/manifest',
					f'{upload_nlb.VIEWER_BASE_URL}/stats/event/PAGE_VIEWS',
				] * 5,
			)
			self.session.post.assert_called_once()
			hash_check.assert_not_called()
			self.assertEqual(uploader.uploads, [])
			self.assertEqual(list((root / 'staging').iterdir()), [])
			saved = upload_nlb.load_state(root / 'state.json')
			entry = saved['items']['BY-NLB-br0000078549:10284033']
			self.assertEqual(entry['status'], 'download_failed')
			self.assertEqual(entry['attempts'], 5)
			self.assertEqual(entry['error_type'], 'ReadTimeout')
			self.assertIn('PAGE_VIEWS timed out', entry['error'])
			self.assertEqual(saved['retry_from_page'], 4)
			self.assertFalse(upload_nlb.is_item_completed(saved, 'BY-NLB-br0000078549', '10284033'))
			self.assertIn('Deferring download after 5 attempts', output.getvalue())

	def test_exhaustion_preserves_original_error_and_attempt_count(self) -> None:
		"""Only exhausted transient attempts produce the typed error handled by the batch."""
		for error_type in (
			upload_nlb.requests.exceptions.ReadTimeout,
			upload_nlb.requests.exceptions.ConnectionError,
			upload_nlb.requests.exceptions.ChunkedEncodingError,
		):
			with self.subTest(error=error_type.__name__), tempfile.TemporaryDirectory() as tmp_dir:
				error = error_type('download interrupted')
				with mock.patch.object(self.client, '_download_pdf_scan_once', side_effect=error) as attempt, mock.patch(
					'upload.time.sleep',
				):
					with self.assertRaises(upload_nlb.DownloadRetriesExhausted) as raised:
						self.client.download_pdf_scan('record', 'file', Path(tmp_dir) / 'scan.pdf')
				self.assertEqual(attempt.call_count, 5)
				self.assertEqual(raised.exception.attempts, 5)
				self.assertIs(raised.exception.last_error, error)
				self.assertIs(raised.exception.__cause__, error)

	def test_session_setup_timeout_is_not_misreported_as_exhausted_download(self) -> None:
		'''A session POST fails before per-scan retry handling, so it remains visible.'''
		error = upload_nlb.requests.exceptions.ReadTimeout('session POST timed out')
		self.session.post.side_effect = error
		with tempfile.TemporaryDirectory() as tmp_dir, mock.patch('upload.time.sleep') as sleep:
			root = Path(tmp_dir)
			with self.assertRaises(upload_nlb.requests.exceptions.ReadTimeout) as raised:
				upload_nlb.process_single_scan(
					client=self.client, uploader=FakeCommonsUploader(), state=upload_nlb.default_state(),
					state_path=root / 'state.json', staging_dir=root / 'staging',
					marc_id='record', file_id='file', part_label='', record_meta={}, marc_meta={},
				)
			self.assertIs(raised.exception, error)
			self.session.get.assert_not_called()
			self.session.post.assert_called_once()
			sleep.assert_not_called()
			self.assertFalse((root / 'staging').exists())
			self.assertNotIn('record:file', upload_nlb.load_state(root / 'state.json')['items'])

	def test_download_rejection_reports_library_reason_and_leaves_scan_pending(self) -> None:
		'''Skip the reported HTTP 400 without retries, uploading or completing the scan.'''
		response = upload_nlb.requests.Response()
		response.status_code = 400
		response.url = 'https://elib.nlb.by:8070/iiif/file/12541585/bytes?attachmentName=r18108_4_1'
		response._content = b'Security alert! Some conditions are missed!'
		response._content_consumed = True
		self.session.get.side_effect = self.download_responses(response)
		uploader = FakeCommonsUploader()
		with tempfile.TemporaryDirectory() as tmp_dir, mock.patch('upload.time.sleep') as sleep, mock.patch(
			'upload.check_commons_by_hash',
		) as hash_check:
			tmp_path = Path(tmp_dir)
			state_path = tmp_path / 'state.json'
			staging_dir = tmp_path / 'staging'
			status = upload_nlb.process_single_scan(
				client=self.client, uploader=uploader, state=upload_nlb.default_state(),
				state_path=state_path, staging_dir=staging_dir,
				marc_id='BY-NLB-br0000059074', file_id='12541585', part_label='',
				record_meta={'title': 'Test record'}, marc_meta={'date': '1800'},
			)
			self.assertEqual(status, 'download_rejected')
			self.assertEqual(uploader.uploads, [])
			self.assertEqual(list(staging_dir.iterdir()), [])
			hash_check.assert_not_called()
			sleep.assert_not_called()
			state = upload_nlb.load_state(state_path)
			self.assertEqual(state['current_item_key'], 'BY-NLB-br0000059074:12541585')
			self.assertFalse(upload_nlb.is_item_completed(state, 'BY-NLB-br0000059074', '12541585'))
			entry = state['items']['BY-NLB-br0000059074:12541585']
			self.assertEqual(entry['status'], 'download_rejected')
			self.assertEqual(entry['http_status'], 400)
			self.assertIn('Security alert! Some conditions are missed!', entry['error'])
			self.assertEqual(state['retry_from_page'], 1)
		self.assertEqual(self.session.get.call_count, 4)

	def test_download_error_body_is_bounded_and_read_failure_preserves_http_error(self) -> None:
		"""Limit diagnostic text and keep an HTTP rejection if its body cannot be read."""
		for unreadable in (False, True):
			with self.subTest(unreadable=unreadable):
				response = upload_nlb.requests.Response()
				response.status_code = 400
				response._content = b'Denied\n' + b'x' * 2000
				response._content_consumed = True
				self.session.get.side_effect = self.download_responses(response)
				if unreadable:
					response.iter_content = mock.Mock(side_effect=upload_nlb.requests.exceptions.ReadTimeout('error body timed out'))
				with tempfile.TemporaryDirectory() as tmp_dir, mock.patch('upload.time.sleep') as sleep:
					with self.assertRaises(upload_nlb.requests.exceptions.HTTPError) as raised:
						self.client.download_pdf_scan('record', 'file', Path(tmp_dir) / 'scan.pdf')
					self.assertIs(raised.exception.response, response)
					if unreadable:
						self.assertNotIn('NLB response:', str(raised.exception))
					else:
						self.assertIn('NLB response: Denied ', str(raised.exception))
						self.assertLess(len(str(raised.exception)), 650)
					sleep.assert_not_called()

	def test_interrupted_stream_restarts_without_partial_bytes(self) -> None:
		"""Discard partial output before retrying timeouts and broken transfers."""
		for error_type in (
			upload_nlb.requests.exceptions.ConnectionError,
			upload_nlb.requests.exceptions.ChunkedEncodingError,
		):
			with self.subTest(error=error_type.__name__):
				partial = b'%PDF-1.4\n' + b'partial scan data' * 10
				broken_stream = self.stream_response(partial)

				def interrupted_chunks(**_):
					"""Emit a partial PDF before simulating a lost connection."""
					yield partial
					raise error_type('connection lost while reading PDF')

				broken_stream.iter_content.side_effect = interrupted_chunks
				self.session.get.side_effect = [
					*self.download_responses(broken_stream),
					*self.download_responses(self.stream_response(self.payload)),
				]
				with tempfile.TemporaryDirectory() as tmp_dir, mock.patch('upload.time.sleep') as sleep:
					target = Path(tmp_dir) / 'scan.pdf'
					sleep.side_effect = lambda _: self.assertFalse(target.exists())
					self.assertEqual(
						self.client.download_pdf_scan('BY-NLB-rr111', '1001', target),
						len(self.payload),
					)
					self.assertEqual(target.read_bytes(), self.payload)
					sleep.assert_called_once_with(5)
				broken_stream.__exit__.assert_called_once()

	def test_truncated_pdf_is_downloaded_again(self) -> None:
		"""Retry a transfer that ends before its advertised Content-Length."""
		for partial in (b'', b'%P', self.payload[:12]):
			with self.subTest(partial=partial):
				truncated = self.stream_response(partial)
				truncated.headers['Content-Length'] = str(len(self.payload))
				self.session.get.side_effect = [
					*self.download_responses(truncated),
					*self.download_responses(self.stream_response(self.payload)),
				]
				with tempfile.TemporaryDirectory() as tmp_dir, mock.patch('upload.time.sleep') as sleep:
					target = Path(tmp_dir) / 'scan.pdf'
					self.client.download_pdf_scan('BY-NLB-rr111', '1001', target)
					self.assertEqual(target.read_bytes(), self.payload)
					sleep.assert_called_once_with(5)

	def test_exhausted_retries_leave_scan_resumable(self) -> None:
		"""Bound retries, remove partial files, and resume without a false upload."""
		error = upload_nlb.requests.exceptions.ConnectionError('stream timed out')
		broken_stream = self.stream_response(self.payload)

		def interrupted_chunks(**_):
			"""Leave partial bytes on disk before failing each attempt."""
			yield self.payload[:12]
			raise error

		broken_stream.iter_content.side_effect = interrupted_chunks
		self.session.get.side_effect = self.download_responses(broken_stream) * 5
		uploader = FakeCommonsUploader()
		with tempfile.TemporaryDirectory() as tmp_dir, mock.patch('upload.time.sleep') as sleep, mock.patch(
			'upload.check_commons_by_hash',
			side_effect=lambda path, **_: (upload_nlb.calculate_sha1(path), []),
		) as hash_check:
			tmp_path = Path(tmp_dir)
			kwargs = {
				'client': self.client,
				'uploader': uploader,
				'state': upload_nlb.default_state(),
				'state_path': tmp_path / 'state.json',
				'staging_dir': tmp_path / 'staging',
				'marc_id': 'BY-NLB-rr111',
				'file_id': '1001',
				'part_label': '',
				'record_meta': upload_nlb.parse_record_html(MOCK_RECORD_HTML_111, 'BY-NLB-rr111'),
				'marc_meta': upload_nlb.parse_marcxml(MOCK_MARCXML_111),
			}
			self.assertEqual(upload_nlb.process_single_scan(**kwargs), 'download_failed')
			self.assertEqual(self.session.get.call_count, 20)
			self.assertEqual(sleep.call_args_list, [mock.call(5), mock.call(10), mock.call(20), mock.call(40)])
			self.assertEqual(list(kwargs['staging_dir'].iterdir()), [])
			self.assertEqual(uploader.uploads, [])
			hash_check.assert_not_called()
			state = upload_nlb.load_state(kwargs['state_path'])
			self.assertEqual(state['current_item_key'], 'BY-NLB-rr111:1001')
			self.assertFalse(upload_nlb.is_item_completed(state, 'BY-NLB-rr111', '1001'))
			self.assertEqual(state['retry_from_page'], 1)
			entry = state['items']['BY-NLB-rr111:1001']
			self.assertEqual(entry['status'], 'download_failed')
			self.assertEqual(entry['attempts'], 5)
			self.assertEqual(entry['error_type'], 'ConnectionError')
			self.assertIn(str(error), entry['error'])

			kwargs['state'] = state
			self.session.get.side_effect = self.download_responses(self.stream_response(self.payload))
			self.assertEqual(upload_nlb.process_single_scan(**kwargs), 'uploaded')
			self.assertEqual(len(uploader.uploads), 1)
			self.assertEqual(list(kwargs['staging_dir'].iterdir()), [])
			self.assertTrue(upload_nlb.is_item_completed(upload_nlb.load_state(kwargs['state_path']), 'BY-NLB-rr111', '1001'))

	def test_permanent_errors_and_interrupts_are_not_retried(self) -> None:
		"""Propagate HTTP, certificate, filesystem and interrupt errors immediately."""
		for error in (
			upload_nlb.requests.exceptions.HTTPError('403 Forbidden'),
			upload_nlb.requests.exceptions.SSLError('certificate verify failed'),
			OSError('disk full'),
			KeyboardInterrupt(),
		):
			with self.subTest(error=type(error).__name__):
				self.session.get.reset_mock()
				self.session.get.side_effect = error
				with tempfile.TemporaryDirectory() as tmp_dir, mock.patch('upload.time.sleep') as sleep:
					with self.assertRaises(type(error)) as raised:
						self.client.download_pdf_scan('BY-NLB-rr111', '1001', Path(tmp_dir) / 'scan.pdf')
					self.assertIs(raised.exception, error)
					sleep.assert_not_called()
				self.session.get.assert_called_once()


class FakeNlbClient:
	"""Controllable fake NlbClient for end-to-end pipeline and interrupt/resume tests."""

	def __init__(self, interrupt_on_file_id: str | None = None) -> None:
		"""Initialize fake responses and optional KeyboardInterrupt trigger."""
		self.session = mock.MagicMock()
		self.interrupt_on_file_id = interrupt_on_file_id
		self.downloaded_file_ids: list[str] = []

	def fetch_search_page(
		self,
		page: int,
		from_year: int | None = None,
		to_year: int | None = 1900,
		search_url: str | None = None,
	) -> dict[str, object]:
		"""Return synthetic page 1 with two records and empty page 2."""
		if page == 1:
			return upload_nlb.parse_search_results_html(MOCK_SEARCH_HTML_PAGE_1)
		return {'total_results': 2, 'records': []}

	def check_doc_access(self, marc_id: str) -> dict[str, object]:
		"""Allow open access to both synthetic records."""
		return {'publicAccess': True}

	def fetch_record_metadata(self, marc_id: str) -> tuple[dict[str, object], dict[str, object]]:
		"""Return parsed HTML and MARCXML metadata for each synthetic record."""
		if marc_id == 'BY-NLB-rr111':
			return (
				upload_nlb.parse_record_html(MOCK_RECORD_HTML_111, marc_id),
				upload_nlb.parse_marcxml(MOCK_MARCXML_111),
			)
		return (
			upload_nlb.parse_record_html(MOCK_RECORD_HTML_222, marc_id),
			upload_nlb.parse_marcxml(''),
		)

	def resolve_record_files(
		self,
		search_entry: dict[str, object],
		record_meta: dict[str, object],
	) -> list[dict[str, str]]:
		"""Return direct file_ids or flattened METS files."""
		if search_entry['marc_id'] == 'BY-NLB-rr111':
			return [{'file_id': '1001', 'part_label': ''}]
		return upload_nlb.extract_files_from_mets_structure(MOCK_METS_STRUCTURE_555)

	def download_pdf_scan(self, marc_id: str, file_id: str, target_path: Path) -> int:
		"""Write a deterministic PDF file and optionally raise KeyboardInterrupt."""
		self.downloaded_file_ids.append(file_id)
		payload = f'%PDF-1.4\nRecord={marc_id} File={file_id}\n%%EOF\n'.encode('utf-8')
		target_path.write_bytes(payload)
		if self.interrupt_on_file_id == file_id:
			self.interrupt_on_file_id = None
			raise KeyboardInterrupt('Simulated user interrupt during scan processing')
		return len(payload)


class FakeCommonsUploader:
	'''In-memory fake CommonsUploader recording uploaded titles, wikitext, and file cleanup.'''

	def __init__(self) -> None:
		'''Initialize empty upload history.'''
		self.uploads: list[dict[str, str]] = []
		self.files: dict[str, dict] = {}

	def title_exists(self, commons_filename: str) -> bool:
		"""Return False so all new titles proceed to hash check and upload."""
		return False

	def upload(self, file_path: Path, commons_filename: str, wikitext: str, summary: str) -> dict[str, object]:
		'''Verify the local file exists during upload and record the submission.'''
		if not file_path.is_file():
			raise AssertionError('Expected local PDF to exist during upload call')
		self.uploads.append({
			'commons_filename': commons_filename,
			'wikitext': wikitext,
			'summary': summary,
		})
		self.files[f'File:{commons_filename}'] = {'sha1': upload_nlb.calculate_sha1(file_path), 'size_bytes': file_path.stat().st_size, 'revision_id': 1, 'page_id': 1}
		return {'status': 'uploaded', 'commons_title': f'File:{commons_filename}'}

	def file_info(self, title: str) -> dict:
		'''Return the binary identity actually recorded by the offline upload service.'''
		return self.files[title]


class TestPywikibotUploadTokens(unittest.TestCase):
	"""Exercise Pywikibot's real request and chunk-upload loops with an offline API."""

	@classmethod
	def setUpClass(cls) -> None:
		"""Import the installed library without reading the user's configuration or cookies."""
		config_dir = tempfile.TemporaryDirectory(prefix='nlb-pywikibot-test-')
		cls.addClassCleanup(config_dir.cleanup)
		with mock.patch.dict('os.environ', {
			'PYWIKIBOT_DIR': config_dir.name, 'PYWIKIBOT_NO_USER_CONFIG': '2',
		}):
			import pywikibot
			import pywikibot.data.api
			from pywikibot.login import LoginStatus
			from pywikibot.site import APISite
			from pywikibot.site._upload import Uploader
		cls.pwb = pywikibot
		cls.api = pywikibot.data.api
		cls.site_type = APISite
		cls.login_status = LoginStatus.AS_USER
		cls.driver_type = Uploader

	def setUp(self) -> None:
		"""Connect the actual request factory to fake identity, tokens and HTTP responses."""
		self.site = mock.MagicMock()
		self.site.userinfo = {'name': 'Test uploader'}
		self.site.user.return_value = 'Test uploader'
		self.site.username.return_value = 'Test uploader'
		self.site._loginstatus = self.login_status
		self.site.is_uploaddisabled.return_value = False
		self.site.has_right.return_value = True
		self.site._request_class = self.site_type._request_class
		self.site._request = types.MethodType(self.site_type._request, self.site)
		self.site.simple_request = types.MethodType(self.site_type.simple_request, self.site)
		self.token = 'expired-test-token'
		self.site.tokens.__getitem__.side_effect = lambda key: self.token
		self.site.tokens.update_tokens.side_effect = self.refresh_token
		self.sent = []
		self.payload_handler = lambda request: {'upload': {'result': 'Success'}}
		self.warning = mock.Mock()
		patches = [
			mock.patch.object(self.api.Request, '_add_defaults'),
			mock.patch.object(self.api.Request, '_use_get', return_value=False),
			mock.patch.object(self.api.Request, '_http_param_string', return_value=''),
			mock.patch.object(self.api.Request, '_simulate', return_value=None),
			mock.patch.object(self.api.Request, '_get_request_params', return_value=(False, '/w/api.php', None, {})),
			mock.patch.object(self.api.Request, '_http_request', autospec=True, side_effect=self.http_request),
			mock.patch.object(self.api.Request, '_userinfo_query', return_value=False),
			mock.patch.object(self.api.Request, '_handle_warnings', return_value=False),
			mock.patch('pywikibot.data.api._invalidate_superior_cookies'),
			mock.patch('pywikibot.comms.http.request', side_effect=AssertionError('Unexpected network request')),
			mock.patch.object(self.pwb, 'warning', self.warning),
			mock.patch.object(self.pwb, 'log'),
			mock.patch.object(self.pwb, 'debug'),
		]
		for patcher in patches:
			patcher.start()
			self.addCleanup(patcher.stop)
		with mock.patch.object(self.pwb, 'Site', return_value=self.site):
			self.uploader = upload_nlb.CommonsUploader()

	def refresh_token(self, old_tokens: list[str]) -> list[str]:
		"""Simulate the server replacing an expired CSRF token."""
		self.token = 'fresh-test-token'
		return [self.token]

	def http_request(self, request: object, *args: object) -> tuple[object, bool]:
		"""Record requests and feed JSON into the real Pywikibot response handling."""
		self.sent.append({key: list(value) for key, value in request.items()})
		response = upload_nlb.requests.Response()
		response.status_code = 200
		response._content = json.dumps(self.payload_handler(request)).encode('utf-8')
		return response, False

	def test_refreshed_token_reaches_later_chunks_polls_and_precreated_final_request(self) -> None:
		"""One expired token must not be reused throughout a successful chunked upload."""
		publishing = False

		def server(request: object) -> dict:
			"""Reject stale tokens, then simulate chunk assembly and publication polling."""
			nonlocal publishing
			if request['token'] != ['fresh-test-token']:
				return {'error': {'code': 'badtoken', 'info': 'Invalid CSRF token.'}}
			if request.get('stash'):
				offset = request['offset'][0]
				if offset < 8:
					return {'upload': {'result': 'Continue', 'offset': offset + 4, 'filekey': 'test-stash'}}
				return {'upload': {'result': 'Poll', 'filekey': 'test-stash'}}
			if request.get('checkstatus'):
				return {'upload': {'result': 'Success', 'filekey': 'test-stash', 'imageinfo': {}}}
			self.assertFalse(publishing)
			publishing = True
			return {'upload': {'result': 'Poll', 'filekey': 'test-stash'}}

		self.payload_handler = server
		with tempfile.TemporaryDirectory() as tmp_dir:
			file_path = Path(tmp_dir) / 'scan.pdf'
			file_path.write_bytes(b'%PDF-1.4\n!')
			file_page = mock.MagicMock()
			file_page.title.return_value = 'Scan.pdf'
			driver = self.driver_type(
				self.site, file_page, source_filename=str(file_path), comment='Test', text='Test',
				chunk_size=4, asynchronous=True, report_success=False,
			)
			self.assertTrue(driver.upload())
		self.site.tokens.update_tokens.assert_called_once()
		self.assertEqual(len(self.sent), 7)
		self.assertEqual(self.sent[0]['token'], ['expired-test-token'])
		self.assertTrue(all(request['token'] == ['fresh-test-token'] for request in self.sent[1:]))
		self.assertEqual(self.warning.call_count, 1)
		self.assertTrue(publishing)
		file_page._load_file_revisions.assert_called_once_with([{}])

	def test_persistent_badtoken_is_bounded_even_when_error_details_change(self) -> None:
		"""Stop after one refresh instead of repeatedly retrying an unrecoverable request."""
		def server(request: object) -> dict:
			"""Vary error details so Pywikibot's identical-error guard cannot stop the loop."""
			if len(self.sent) > 4:
				raise AssertionError('Unbounded token recovery')
			return {'error': {'code': 'badtoken', 'info': f'Invalid CSRF token, attempt {len(self.sent)}'}}

		self.payload_handler = server
		request = self.site.simple_request(action='upload', token='expired-test-token', filename='Scan.pdf')
		with self.assertRaises(self.pwb.exceptions.APIError) as raised:
			request.submit()
		self.assertEqual(raised.exception.code, 'badtoken')
		self.assertFalse(upload_nlb.is_retryable_upload_error(raised.exception))
		self.assertEqual(len(self.sent), 2)
		self.site.tokens.update_tokens.assert_called_once()

	def run_stash_lock_upload(self, *, polling: bool = False, persistent: bool = False,
		published_on_failure: bool = False, max_attempts: int = 3,
		first_chunk_failure: bool = False, final_warning: str = '') -> tuple[dict, mock.Mock, mock.Mock]:
		"""Run real chunk/publication requests against a server with a controllable stash lock."""
		file_page = mock.MagicMock()
		file_page.title.return_value = 'Scan.pdf'
		file_page._pageid = 0
		file_page.exists.side_effect = lambda: file_page._pageid > 0
		published = False
		publications = 0
		stash_number = 0
		chunk_failed = False
		self.site.loadpageinfo.side_effect = lambda page: setattr(page, '_pageid', int(published))

		def server(request: object) -> dict:
			"""Make completed chunks reusable, and return the reported failure at publication or polling."""
			nonlocal published, publications, stash_number, chunk_failed
			if request.get('stash'):
				if first_chunk_failure and not chunk_failed:
					chunk_failed = True
					return {'error': {'code': 'stashfailed', 'info': 'Temporary chunk storage failure.'}}
				offset = request['offset'][0]
				if offset == 0:
					stash_number += 1
				return {'upload': {
					'result': 'Continue' if offset < 8 else 'Success',
					'offset': min(offset + 4, 10), 'filekey': f'stash-{stash_number}',
				}}
			if not request.get('checkstatus'):
				publications += 1
				if polling and (persistent or publications == 1):
					return {'upload': {'result': 'Poll', 'filekey': f'stash-{stash_number}'}}
			if request.get('checkstatus') or persistent or publications == 1:
				published = published_on_failure
				return {'error': {
					'code': 'stashfailed',
					'info': 'Could not acquire lock. Somebody else is doing something to this file.',
				}}
			if final_warning:
				return {'upload': {
					'result': 'Warning', 'filekey': f'stash-{stash_number}',
					'warnings': {final_warning: ['Existing.pdf'] if final_warning == 'duplicate' else 'Scan.pdf'},
				}}
			return {'upload': {'result': 'Success', 'imageinfo': {}}}

		self.payload_handler = server
		with tempfile.TemporaryDirectory() as tmp_dir, mock.patch.object(
			self.pwb, 'FilePage', return_value=file_page,
		), mock.patch('upload.UPLOAD_CHUNK_SIZE', 4), mock.patch(
			'upload.UPLOAD_MAX_RETRIES', max_attempts,
		), mock.patch('upload.time.sleep') as sleep:
			file_path = Path(tmp_dir) / 'scan.pdf'
			file_path.write_bytes(b'%PDF-1.4\n!')
			result = self.uploader.upload(file_path, 'Scan.pdf', 'Preserved description', 'Test')
		return result, file_page, sleep

	def test_stash_lock_reuses_assembled_file_after_publication_or_poll_failure(self) -> None:
		"""Recover using the original publication request without sending the chunks again."""
		for polling in (False, True):
			with self.subTest(polling=polling):
				self.sent.clear()
				result, _, sleep = self.run_stash_lock_upload(polling=polling)
				self.assertEqual(result['status'], 'uploaded')
				chunks = [request for request in self.sent if request.get('stash')]
				publications = [request for request in self.sent if not request.get('stash') and not request.get('checkstatus')]
				self.assertEqual(len(chunks), 3)
				self.assertEqual(len(publications), 2)
				self.assertTrue(all(request['filekey'] == ['stash-1'] for request in publications))
				self.assertTrue(all(request['text'] == ['Preserved description'] for request in publications))
				self.assertTrue(all(request['ignorewarnings'] == [False] for request in publications))
				sleep.assert_called_once_with(60)

	def test_persistent_stash_lock_has_one_bounded_publication_budget(self) -> None:
		"""Exhaustion stops publication retries without multiplying full-upload attempts."""
		with self.assertRaises(upload_nlb.UploadRetriesExhausted) as raised:
			self.run_stash_lock_upload(polling=True, persistent=True)
		self.assertEqual(raised.exception.attempts, 3)
		self.assertEqual(raised.exception.last_error.code, 'stashfailed')
		self.assertEqual(len([request for request in self.sent if request.get('stash')]), 3)
		self.assertEqual(len([request for request in self.sent if request.get('checkstatus')]), 3)

	def test_publication_failure_refreshes_missing_title_even_on_last_attempt(self) -> None:
		"""A newly published title prevents retries and is not falsely counted as our upload."""
		result, file_page, sleep = self.run_stash_lock_upload(
			persistent=True, published_on_failure=True, max_attempts=1,
		)
		self.assertEqual(result['status'], 'existing_title')
		self.site.loadpageinfo.assert_called_once_with(file_page)
		sleep.assert_not_called()

	def test_chunk_and_publication_failures_share_one_budget(self) -> None:
		"""A failed chunk consumes one attempt before publication starts using the same budget."""
		with self.assertRaises(upload_nlb.UploadRetriesExhausted) as raised:
			self.run_stash_lock_upload(first_chunk_failure=True, persistent=True, max_attempts=3)
		self.assertEqual(raised.exception.attempts, 3)
		self.assertEqual(len([request for request in self.sent if request.get('stash')]), 4)
		self.assertEqual(len([request for request in self.sent if not request.get('stash')]), 2)
		self.assertEqual(self.site.loadpageinfo.call_count, 3)

	def test_publication_retry_still_honors_exists_and_duplicate_warnings(self) -> None:
		"""A lock retry must not overwrite a title or bypass Commons duplicate detection."""
		for warning, status in (('exists', 'existing_title'), ('duplicate', 'duplicate_sha1')):
			with self.subTest(warning=warning):
				self.sent.clear()
				result, _, _ = self.run_stash_lock_upload(final_warning=warning)
				self.assertEqual(result['status'], status)
				if warning == 'duplicate':
					self.assertEqual(result['duplicates'], ['Existing.pdf'])
				self.assertEqual(len([request for request in self.sent if request.get('stash')]), 3)
				self.assertTrue(all(request['ignorewarnings'] == [False] for request in self.sent))

	def test_upload_preconditions_and_incomplete_driver_result_remain_fatal(self) -> None:
		"""Direct driver use must retain permissions checks and never claim a false success."""
		for scenario in ('permission', 'disabled', 'incomplete'):
			with self.subTest(scenario=scenario):
				self.site.has_right.return_value = scenario != 'permission'
				self.site.is_uploaddisabled.return_value = scenario == 'disabled'
				file_page = mock.Mock()
				file_page.exists.return_value = False
				with mock.patch.object(self.pwb, 'FilePage', return_value=file_page), mock.patch.object(
					self.uploader, '_upload_driver_class',
				) as driver_class, mock.patch('upload.time.sleep') as sleep:
					driver_class.return_value.upload.return_value = False
					error_type = RuntimeError if scenario == 'incomplete' else self.pwb.exceptions.Error
					with self.assertRaises(error_type):
						self.uploader.upload(Path('/unused.pdf'), 'Scan.pdf', 'Description', 'Test')
					if scenario != 'incomplete':
						driver_class.assert_not_called()
					sleep.assert_not_called()

	def test_non_upload_requests_keep_the_original_request_classes(self) -> None:
		"""Keep queries unchanged and avoid wrapping a reused Pywikibot site twice."""
		self.assertIs(self.site._request_class({'parameters': {'action': 'query'}}), self.api.Request)
		self.assertIs(self.site._request_class({'parameters': {'action': 'query'}, 'expiry': 1}), self.api.CachedRequest)
		installed_factory = self.site._request_class
		upload_nlb.configure_upload_token_recovery(self.site, self.api.Request, self.pwb.exceptions.APIError)
		self.assertIs(self.site._request_class, installed_factory)

	def test_badtoken_failure_leaves_scan_pending_and_removes_temporary_pdf(self) -> None:
		"""Let persistent authentication errors escape the upload retry loop without completing the item."""
		self.payload_handler = lambda request: {'error': {'code': 'badtoken', 'info': 'Invalid CSRF token.'}}
		file_page = mock.MagicMock()
		file_page.exists.return_value = False
		file_page.title.return_value = 'Scan.pdf'
		with tempfile.TemporaryDirectory() as tmp_dir, mock.patch.object(
			self.pwb, 'FilePage', return_value=file_page,
		), mock.patch(
			'upload.check_commons_by_hash', side_effect=lambda path, **_: (upload_nlb.calculate_sha1(path), []),
		), mock.patch('upload.time.sleep') as sleep, mock.patch('sys.stdout', io.StringIO()):
			state_path = Path(tmp_dir) / 'state.json'
			staging_dir = Path(tmp_dir) / 'staging'
			with self.assertRaises(self.pwb.exceptions.APIError) as raised:
				upload_nlb.iterate_and_upload(
					client=FakeNlbClient(), uploader=self.uploader,
					state_path=state_path, staging_dir=staging_dir,
				)
			self.assertEqual(raised.exception.code, 'badtoken')
			self.assertEqual(len(self.sent), 2)
			sleep.assert_not_called()
			self.assertEqual(list(staging_dir.iterdir()), [])
			state = upload_nlb.load_state(state_path)
			self.assertEqual(state['current_item_key'], 'BY-NLB-rr111:1001')
			self.assertFalse(upload_nlb.is_item_completed(state, 'BY-NLB-rr111', '1001'))


class TestEndToEndPipeline(unittest.TestCase):
	'''Level 3 end-to-end tests covering full iteration, hash deduplication, cleanup, and resume.'''

	def test_blacklisted_filename_is_corrected_before_lookup_and_upload(self) -> None:
		'''Use the corrected filename throughout the pipeline and retain the original metadata.'''
		original_title = 'Potpourris - Piano & Violon = Ноты'
		expected_filename = 'Potpourris - Piano & Violon - Ноты (1850) -- BY-NLB-rr111 -- 1001.pdf'
		client = FakeNlbClient()
		uploader = FakeCommonsUploader()
		with tempfile.TemporaryDirectory() as tmp_dir, mock.patch.object(
			client, 'fetch_record_metadata', return_value=({'title': original_title}, {'year': '1850'}),
		), mock.patch.object(uploader, 'title_exists', return_value=False) as title_exists, mock.patch(
			'upload.check_commons_by_hash', side_effect=lambda path, **_: (upload_nlb.calculate_sha1(path), []),
		), mock.patch('sys.stdout', io.StringIO()):
			root = Path(tmp_dir)
			counts = upload_nlb.iterate_and_upload(
				client=client, uploader=uploader,
				state_path=root / 'state.json', staging_dir=root / 'staging', max_uploads=1,
			)
			self.assertEqual(counts['uploaded'], 1)
			title_exists.assert_called_once_with(expected_filename)
			self.assertEqual(uploader.uploads[0]['commons_filename'], expected_filename)
			self.assertIn(upload_nlb.escape_wikitext(original_title), uploader.uploads[0]['wikitext'])
			state = upload_nlb.load_state(root / 'state.json')
			self.assertEqual(state['items']['BY-NLB-rr111:1001']['commons_title'], f'File:{expected_filename}')
			self.assertEqual(list((root / 'staging').iterdir()), [])

	def test_upload_preserves_manufacturing_printer(self) -> None:
		"""Carry the library's printer through iteration into the submitted Commons description."""
		fixture = Path(__file__).parent / 'tests' / 'fixtures' / 'BY-NLB-br0000060196-manufacturing.xml'
		marc_meta = upload_nlb.parse_marcxml(fixture.read_text(encoding='utf-8'))
		client = FakeNlbClient()
		uploader = FakeCommonsUploader()
		with tempfile.TemporaryDirectory() as tmp_dir, mock.patch.object(
			client, 'fetch_record_metadata', return_value=({'title': 'Руководство к изучению санскрита'}, marc_meta),
		), mock.patch(
			'upload.check_commons_by_hash', side_effect=lambda path, **_: (upload_nlb.calculate_sha1(path), []),
		), mock.patch('sys.stdout', io.StringIO()):
			counts = upload_nlb.iterate_and_upload(
				client=client, uploader=uploader,
				state_path=Path(tmp_dir) / 'state.json', staging_dir=Path(tmp_dir) / 'staging',
				max_uploads=1,
			)
		self.assertEqual(counts['uploaded'], 1)
		self.assertIn('Типография Императорской академии наук', uploader.uploads[0]['wikitext'])
		self.assertIn('|date=1891\n', uploader.uploads[0]['wikitext'])

	def test_upload_preserves_contributor_roles_and_only_attributes_authors(self) -> None:
		'''Carry real library roles through iteration into the submitted Commons description.'''
		fixture = Path(__file__).parent / 'tests' / 'fixtures' / 'BY-NLB-br0000060196-contributors.xml'
		marc_meta = upload_nlb.parse_marcxml(fixture.read_text(encoding='utf-8'))
		record_meta = {
			'title': marc_meta['title'],
			'fields': [{'name': 'Авторы', 'values': [entry['name'] for entry in marc_meta['contributors']]}],
		}
		client = FakeNlbClient()
		uploader = FakeCommonsUploader()
		with tempfile.TemporaryDirectory() as tmp_dir, mock.patch.object(
			client, 'fetch_record_metadata', return_value=(record_meta, marc_meta),
		), mock.patch(
			'upload.check_commons_by_hash', side_effect=lambda path, **_: (upload_nlb.calculate_sha1(path), []),
		), mock.patch('sys.stdout', io.StringIO()):
			counts = upload_nlb.iterate_and_upload(
				client=client, uploader=uploader,
				state_path=Path(tmp_dir) / 'state.json', staging_dir=Path(tmp_dir) / 'staging',
				max_uploads=1, explicit_license=True,
			)
		self.assertEqual(counts['uploaded'], 1)
		wikitext = uploader.uploads[0]['wikitext']
		self.assertIn('|author=Миллер, В. Ф. (Всеволод Федорович), фольклорист, языковед (1848—1913)<br>Кнауэр, Ф. И.\n', wikitext)
		self.assertIn('Карский, Е. Ф. (Евфимий Федорович), славист (1861—1931) — Предыдущий владелец (390)', wikitext)
		self.assertIn('Императорский университет святого Владимира, Киев — Выпускающая организация (475)', wikitext)

	def test_deferred_downloads_continue_across_pages_and_retry_on_restart(self) -> None:
		"""Revisit rejected and exhausted downloads across upload limits, dry runs and restarts."""
		for first_run_limit, failure_status in (
			(0, 'download_rejected'), (1, 'download_rejected'),
			(0, 'download_failed'), (1, 'download_failed'),
		):
			with self.subTest(max_uploads=first_run_limit, status=failure_status), tempfile.TemporaryDirectory() as tmp_dir:
				tmp_path = Path(tmp_dir)
				client = FakeNlbClient()
				uploader = FakeCommonsUploader()
				records = client.fetch_search_page(1)['records']
				client.fetch_search_page = mock.Mock(side_effect=lambda page, **_: {
					'total_results': 2, 'records': records[page - 1:page],
				})
				original_download = client.download_pdf_scan
				reject_first = True

				def download_with_rejection(marc_id: str, file_id: str, target_path: Path) -> int:
					"""Write partial bytes before rejecting the first scan until it recovers."""
					size = original_download(marc_id, file_id, target_path)
					if file_id == '1001' and reject_first:
						if failure_status == 'download_failed':
							raise upload_nlb.DownloadRetriesExhausted(
								upload_nlb.requests.exceptions.ReadTimeout('PAGE_VIEWS timed out'),
								upload_nlb.DOWNLOAD_MAX_ATTEMPTS,
							)
						response = upload_nlb.requests.Response()
						response.status_code = 400
						raise upload_nlb.requests.exceptions.HTTPError(
							'400; NLB response: Security alert! Some conditions are missed!', response=response,
						)
					return size

				client.download_pdf_scan = download_with_rejection
				kwargs = {
					'client': client, 'uploader': uploader,
					'state_path': tmp_path / 'state.json', 'staging_dir': tmp_path / 'staging',
				}
				output = io.StringIO()
				with mock.patch('upload.SEARCH_PAGE_SIZE', 1), mock.patch(
					'upload.check_commons_by_hash', side_effect=lambda path, **_: (upload_nlb.calculate_sha1(path), []),
				) as hash_check, mock.patch('sys.stdout', output):
					counts = upload_nlb.iterate_and_upload(**kwargs, max_uploads=first_run_limit)
					self.assertEqual(counts[failure_status], 1)
					self.assertEqual(counts['uploaded'], first_run_limit or 2)
					self.assertEqual(client.downloaded_file_ids, ['1001', '2001'] if first_run_limit else ['1001', '2001', '2002'])
					self.assertEqual(hash_check.call_count, first_run_limit or 2)
					self.assertEqual(list(kwargs['staging_dir'].iterdir()), [])
					state = upload_nlb.load_state(kwargs['state_path'])
					self.assertEqual(state['current_page'], 2)
					self.assertEqual(state['retry_from_page'], 1)
					self.assertFalse(upload_nlb.is_item_completed(state, 'BY-NLB-rr111', '1001'))

					# Each later run defers a still-failing download again.
					client.downloaded_file_ids.clear()
					counts = upload_nlb.iterate_and_upload(**kwargs)
					self.assertEqual(counts[failure_status], 1)
					self.assertEqual(counts['uploaded'], 1 if first_run_limit else 0)
					self.assertEqual(client.downloaded_file_ids, ['1001', '2002'] if first_run_limit else ['1001'])

					# A successful dry run must not lose the pending upload on an earlier page.
					reject_first = False
					client.downloaded_file_ids.clear()
					counts = upload_nlb.iterate_and_upload(**kwargs, dry_run=True)
					self.assertEqual(counts['dry_run_ready'], 1)
					self.assertEqual(client.downloaded_file_ids, ['1001'])
					self.assertEqual(len(uploader.uploads), 2)
					self.assertEqual(upload_nlb.load_state(kwargs['state_path'])['retry_from_page'], 1)

					# Once the library recovers, the rejected scan is uploaded and saved.
					client.downloaded_file_ids.clear()
					counts = upload_nlb.iterate_and_upload(**kwargs)
					self.assertEqual(counts[failure_status], 0)
					self.assertEqual(counts['uploaded'], 1)
					self.assertEqual(client.downloaded_file_ids, ['1001'])
					self.assertEqual(len(uploader.uploads), 3)
					state = upload_nlb.load_state(kwargs['state_path'])
					self.assertTrue(upload_nlb.is_item_completed(state, 'BY-NLB-rr111', '1001'))
					self.assertIsNone(state.get('retry_from_page'))
					self.assertEqual(list(kwargs['staging_dir'].iterdir()), [])
				if failure_status == 'download_rejected':
					self.assertIn('Security alert! Some conditions are missed!', output.getvalue())
					self.assertIn('Skipping download (HTTP 400)', output.getvalue())
				else:
					self.assertIn('PAGE_VIEWS timed out', output.getvalue())
					self.assertIn('Deferring download after 5 attempts', output.getvalue())

	def test_rejection_retry_page_survives_an_immediate_interrupt(self) -> None:
		"""Persist the rewind before a search request can fail or be interrupted."""
		with tempfile.TemporaryDirectory() as tmp_dir:
			tmp_path = Path(tmp_dir)
			state_path = tmp_path / 'state.json'
			state = upload_nlb.default_state()
			state['current_page'] = 5
			state['retry_from_page'] = 2
			upload_nlb.save_state(state_path, state)
			client = FakeNlbClient()
			client.fetch_search_page = mock.Mock(side_effect=KeyboardInterrupt())
			kwargs = {
				'client': client, 'uploader': FakeCommonsUploader(),
				'state_path': state_path, 'staging_dir': tmp_path / 'staging',
			}
			with self.assertRaises(KeyboardInterrupt):
				upload_nlb.iterate_and_upload(**kwargs)
			self.assertEqual(upload_nlb.load_state(state_path)['current_page'], 2)
			client.fetch_search_page = mock.Mock(return_value={'total_results': 0, 'records': []})
			upload_nlb.iterate_and_upload(**kwargs)
			self.assertEqual(client.fetch_search_page.call_args.kwargs['page'], 2)

	def test_unrelated_http_errors_are_not_skipped(self) -> None:
		'''Keep other download failures and Commons HTTP 400 errors visible to callers.'''
		for stage, http_status in (
			('download_pdf_scan', 401), ('download_pdf_scan', 403),
			('download_pdf_scan', 429), ('download_pdf_scan', 500),
			('check_commons_by_hash', 400), ('upload', 400),
		):
			with self.subTest(stage=stage, status=http_status), tempfile.TemporaryDirectory() as tmp_dir:
				tmp_path = Path(tmp_dir)
				client = FakeNlbClient()
				uploader = FakeCommonsUploader()
				response = upload_nlb.requests.Response()
				response.status_code = http_status
				error = upload_nlb.requests.exceptions.HTTPError(f'{http_status} error', response=response)
				if stage == 'download_pdf_scan':
					client.download_pdf_scan = mock.Mock(side_effect=error)
				elif stage == 'upload':
					uploader.upload = mock.Mock(side_effect=error)
				with mock.patch('upload.check_commons_by_hash') as hash_check:
					hash_check.return_value = ('0' * 40, [])
					if stage == 'check_commons_by_hash':
						hash_check.side_effect = error
					with self.assertRaises(upload_nlb.requests.exceptions.HTTPError) as raised:
						upload_nlb.process_single_scan(
							client=client, uploader=uploader, state=upload_nlb.default_state(),
							state_path=tmp_path / 'state.json', staging_dir=tmp_path / 'staging',
							marc_id='BY-NLB-rr111', file_id='1001', part_label='',
							record_meta={'title': 'Test record'}, marc_meta={'date': '1800'},
						)
					self.assertIs(raised.exception, error)
				state = upload_nlb.load_state(tmp_path / 'state.json')
				self.assertEqual(state['items'], {})
				self.assertIsNone(state.get('retry_from_page'))
				self.assertEqual(list((tmp_path / 'staging').iterdir()), [])

	def test_download_deferral_does_not_hide_other_failures(self) -> None:
		'''Keep certificate, disk, interrupt, hash and upload errors outside download deferral.'''
		for stage, error in (
			('download', upload_nlb.requests.exceptions.SSLError('invalid certificate')),
			('download', OSError('disk full')),
			('download', KeyboardInterrupt()),
			('upload', upload_nlb.requests.exceptions.ConnectionError('Commons upload failed')),
		):
			with self.subTest(stage=stage, error=type(error).__name__), tempfile.TemporaryDirectory() as tmp_dir:
				root = Path(tmp_dir)
				client = FakeNlbClient()
				uploader = FakeCommonsUploader()
				if stage == 'download':
					client.download_pdf_scan = mock.Mock(side_effect=error)
				elif stage == 'upload':
					uploader.upload = mock.Mock(side_effect=error)
				with mock.patch('upload.check_commons_by_hash', return_value=('0' * 40, [])) as hash_check:
					if stage == 'hash':
						hash_check.side_effect = error
					with self.assertRaises(type(error)) as raised:
						upload_nlb.process_single_scan(
							client=client, uploader=uploader, state=upload_nlb.default_state(),
							state_path=root / 'state.json', staging_dir=root / 'staging',
							marc_id='record', file_id='file', part_label='', record_meta={}, marc_meta={'date': '1800'},
						)
				self.assertIs(raised.exception, error)
				self.assertEqual(list((root / 'staging').iterdir()), [])
				self.assertNotIn('record:file', upload_nlb.load_state(root / 'state.json')['items'])

	def test_end_to_end_pipeline_deletes_local_scans_and_skips_sha1_duplicates(self) -> None:
		"""Verify full pipeline uploads new scans, skips SHA-1 matches, and removes local PDFs."""
		with tempfile.TemporaryDirectory() as tmp_dir:
			tmp_path = Path(tmp_dir)
			state_path = tmp_path / 'state.json'
			staging_dir = tmp_path / 'staging'

			dup_payload = b'%PDF-1.4\nRecord=BY-NLB-rr111 File=1001\n%%EOF\n'
			dup_sha1 = hashlib.sha1(dup_payload).hexdigest()

			def fake_hash_check(
				file_path: Path,
				session: object = None,
			) -> tuple[str, list[str]]:
				digest = upload_nlb.calculate_sha1(file_path)
				if digest == dup_sha1:
					return digest, ['File:Already_On_Commons.pdf']
				return digest, []

			client = FakeNlbClient()
			uploader = FakeCommonsUploader()

			with mock.patch('upload.check_commons_by_hash', side_effect=fake_hash_check):
				counts = upload_nlb.iterate_and_upload(
					client=client,
					uploader=uploader,
					state_path=state_path,
					staging_dir=staging_dir,
				)

			self.assertEqual(counts['duplicate_sha1'], 1)
			self.assertEqual(counts['uploaded'], 2)
			self.assertEqual(len(uploader.uploads), 2)
			self.assertEqual(
				uploader.uploads[0]['summary'],
				'From https://elib.nlb.by/elib/Record/BY-NLB-rr222 (fileID 2001)',
			)
			for uploaded in uploader.uploads:
				self.assertIn('[[Category:elib.nlb.by]]', uploaded['wikitext'])
				self.assertEqual(uploaded['wikitext'].count('[[Category:'), 1)

			remaining_files = list(staging_dir.glob('*'))
			self.assertEqual(remaining_files, [])

	def test_interrupt_and_resume_continues_from_exact_item(self) -> None:
		"""Verify an interrupted run cleans up its temp file and resumes without repeating work."""
		with tempfile.TemporaryDirectory() as tmp_dir:
			tmp_path = Path(tmp_dir)
			state_path = tmp_path / 'state.json'
			staging_dir = tmp_path / 'staging'

			client = FakeNlbClient(interrupt_on_file_id='2001')
			uploader = FakeCommonsUploader()

			with mock.patch(
				'upload.check_commons_by_hash',
				side_effect=lambda path, **_: (upload_nlb.calculate_sha1(path), []),
			):
				with self.assertRaises(KeyboardInterrupt):
					upload_nlb.iterate_and_upload(
						client=client,
						uploader=uploader,
						state_path=state_path,
						staging_dir=staging_dir,
					)

				self.assertEqual(list(staging_dir.glob('*')), [])
				self.assertEqual(len(uploader.uploads), 1)
				self.assertEqual(client.downloaded_file_ids, ['1001', '2001'])

				saved_state = upload_nlb.load_state(state_path)
				self.assertTrue(upload_nlb.is_item_completed(saved_state, 'BY-NLB-rr111', '1001'))
				self.assertFalse(upload_nlb.is_item_completed(saved_state, 'BY-NLB-rr222', '2001'))
				self.assertEqual(saved_state['current_item_key'], 'BY-NLB-rr222:2001')

				counts = upload_nlb.iterate_and_upload(
					client=client,
					uploader=uploader,
					state_path=state_path,
					staging_dir=staging_dir,
				)

			self.assertEqual(counts['uploaded'], 2)
			self.assertEqual(len(uploader.uploads), 3)
			self.assertEqual(client.downloaded_file_ids, ['1001', '2001', '2001', '2002'])
			self.assertEqual(list(staging_dir.glob('*')), [])

	def test_progress_logging_prints_urls_and_current_actions(self) -> None:
		'''Show decimal MB before streaming and keep the later hash-check line size-free.'''
		with tempfile.TemporaryDirectory() as tmp_dir:
			tmp_path = Path(tmp_dir)
			state_path = tmp_path / 'state.json'
			staging_dir = tmp_path / 'staging'

			client = FakeNlbClient()
			uploader = FakeCommonsUploader()
			payload = pdf_bytes(padding=1_600_000)
			stream = mock.MagicMock()
			stream.__enter__.return_value = stream
			stream.headers = {'Content-Length': str(len(payload))}
			manifest = mock.MagicMock()
			manifest.json.return_value = {'rendering': []}
			client.session.get.side_effect = [manifest, mock.MagicMock(), mock.MagicMock(), stream]
			client.download_pdf_scan = upload_nlb.NlbClient(session=client.session).download_pdf_scan

			def chunks_after_progress(**_):
				"""Require the size message before the first response bytes are consumed."""
				self.assertIn('[BY-NLB-rr111:1001] Downloading scan (1.6 MB):\n', stdout_buf.getvalue())
				self.assertNotIn('Checking SHA-1', stdout_buf.getvalue())
				yield payload

			stream.iter_content.side_effect = chunks_after_progress

			with (
				mock.patch(
					'upload.check_commons_by_hash',
					side_effect=lambda path, **_: (upload_nlb.calculate_sha1(path), []),
				),
				mock.patch('sys.stdout', new_callable=io.StringIO) as stdout_buf,
			):
				upload_nlb.iterate_and_upload(
					client=client,
					uploader=uploader,
					state_path=state_path,
					staging_dir=staging_dir,
					max_uploads=1,
				)
				output = stdout_buf.getvalue()

			self.assertIn('https://elib.nlb.by/elib/Record/BY-NLB-rr111', output)
			self.assertIn('https://elib.nlb.by:8070/viewer?markID=BY-NLB-rr111&fileID=1001', output)
			self.assertIn('Downloading', output)
			self.assertIn('Checking SHA-1', output)
			self.assertIn('Uploading', output)
			self.assertIn(
				'[BY-NLB-rr111:1001] Checking title on Commons:\n'
				'File:Минские губернские ведомости - официальная часть (1890) -- BY-NLB-rr111 -- 1001.pdf',
				output,
			)
			self.assertIn(
				'[BY-NLB-rr111:1001] Downloading scan (1.6 MB):\n'
				'https://elib.nlb.by:8070/viewer?markID=BY-NLB-rr111&fileID=1001 '
				'(record: https://elib.nlb.by/elib/Record/BY-NLB-rr111)',
				output,
			)
			self.assertEqual(output.count('Downloading scan'), 1)
			self.assertIn(
				'[BY-NLB-rr111:1001] Checking SHA-1 hash on Commons: '
				'https://elib.nlb.by:8070/viewer?markID=BY-NLB-rr111&fileID=1001',
				output,
			)
			self.assertNotIn('Checking SHA-1 hash on Commons (', output)
			self.assertEqual(client.session.get.call_count, 4)
			client.session.head.assert_not_called()
			self.assertIn(
				'[BY-NLB-rr111:1001] Uploading to Wikimedia Commons:\n'
				'File:Минские губернские ведомости - официальная часть (1890) -- BY-NLB-rr111 -- 1001.pdf',
				output,
			)
			self.assertIn(
				'[BY-NLB-rr111:1001] Uploaded and verified:\nhttps://commons.wikimedia.org/wiki/',
				output,
			)
			self.assertNotIn('Uploaded (uploaded)', output)

	def test_year_range_filtering_and_custom_search_url(self) -> None:
		"""Verify --from-year / --to-year filtering and custom search URL CLI execution."""
		with tempfile.TemporaryDirectory() as tmp_dir:
			tmp_path = Path(tmp_dir)
			state_path = tmp_path / 'state.json'
			staging_dir = tmp_path / 'staging'

			fake_client = FakeNlbClient()
			with (
				mock.patch('upload.NlbClient', return_value=fake_client),
				mock.patch(
					'upload.check_commons_by_hash',
					side_effect=lambda path, **_: (upload_nlb.calculate_sha1(path), []),
				),
				mock.patch('sys.stdout', new_callable=io.StringIO) as stdout_buf,
			):
				exit_code = upload_nlb.main([
					'https://elib.nlb.by/elib/Search/Results?type=AllFields&isElib=true&daterange%5B%5D=publishDate&publishDatefrom=1892&publishDateto=1895',
					'--dry-run',
					'--state-file',
					str(state_path),
					'--staging-dir',
					str(staging_dir),
				])
				self.assertEqual(exit_code, 0)
				output = stdout_buf.getvalue()
				self.assertIn('Skipping (year 1890 < 1892)', output)
				summary = json.loads(output.strip().splitlines()[-1])
				self.assertEqual(summary['dry_run_ready'], 2)

	def test_cli_main_dry_run_and_keyboard_interrupt_exit_code(self) -> None:
		"""Verify CLI main() supports --dry-run and returns 130 on KeyboardInterrupt."""
		with tempfile.TemporaryDirectory() as tmp_dir:
			tmp_path = Path(tmp_dir)
			state_path = tmp_path / 'state.json'
			staging_dir = tmp_path / 'staging'

			fake_client = FakeNlbClient()
			with (
				mock.patch('upload.NlbClient', return_value=fake_client),
				mock.patch(
					'upload.check_commons_by_hash',
					side_effect=lambda path, **_: (upload_nlb.calculate_sha1(path), []),
				),
				mock.patch('sys.stdout', new_callable=io.StringIO) as stdout_buf,
			):
				exit_code = upload_nlb.main([
					'--dry-run',
					'--max-uploads',
					'2',
					'--state-file',
					str(state_path),
					'--staging-dir',
					str(staging_dir),
				])
				self.assertEqual(exit_code, 0)
				last_line = stdout_buf.getvalue().strip().splitlines()[-1]
				summary = json.loads(last_line)
				self.assertEqual(summary['dry_run_ready'], 2)

			interrupt_client = FakeNlbClient(interrupt_on_file_id='1001')
			with (
				mock.patch('upload.NlbClient', return_value=interrupt_client),
				mock.patch('sys.stdout', new_callable=io.StringIO),
			):
				exit_code = upload_nlb.main([
					'--dry-run',
					'--state-file',
					str(tmp_path / 'state2.json'),
					'--staging-dir',
					str(staging_dir),
				])
				self.assertEqual(exit_code, 130)

	def test_graceful_sigint_finishes_current_item_and_prints_uploaded_count_and_mb(self) -> None:
		"""Verify SIGINT (Ctrl-C) finishes the current item, stops before the next item, and logs uploaded count and MBs."""
		with tempfile.TemporaryDirectory() as tmp_dir:
			tmp_path = Path(tmp_dir)
			state_path = tmp_path / 'state.json'
			staging_dir = tmp_path / 'staging'

			client = FakeNlbClient()
			uploader = FakeCommonsUploader()
			payload_size = int(1.5 * 1024 * 1024)

			def download_and_send_sigint(marc_id: str, file_id: str, target_path: Path) -> int:
				client.downloaded_file_ids.append(file_id)
				header = f'%PDF-1.4\nRecord={marc_id} File={file_id}\n'.encode('utf-8')
				footer = b'\n%%EOF\n'
				padding = b'0' * (payload_size - len(header) - len(footer))
				target_path.write_bytes(header + padding + footer)
				if file_id == '1001':
					signal.raise_signal(signal.SIGINT)
				return payload_size

			client.download_pdf_scan = download_and_send_sigint  # type: ignore[method-assign]

			with (
				mock.patch(
					'upload.check_commons_by_hash',
					side_effect=lambda path, **_: (upload_nlb.calculate_sha1(path), []),
				),
				mock.patch('sys.stdout', new_callable=io.StringIO) as stdout_buf,
			):
				counts = upload_nlb.iterate_and_upload(
					client=client,
					uploader=uploader,
					state_path=state_path,
					staging_dir=staging_dir,
				)
				output = stdout_buf.getvalue()

			self.assertEqual(counts['uploaded'], 1)
			self.assertEqual(len(uploader.uploads), 1)
			self.assertEqual(client.downloaded_file_ids, ['1001'])
			self.assertEqual(list(staging_dir.glob('*')), [])
			self.assertIn('1.50 MB', output)
			self.assertIn('Uploaded: 1', output)
			self.assertIn('Spent ', output)
			self.assertEqual(
				upload_nlb.format_elapsed_time(2 * 86400 + 23 * 3600 + 13 * 60 + 5),
				'Spent 2 days 23 hours 13 minutes 5 seconds',
			)
			self.assertEqual(
				upload_nlb.format_elapsed_time(1 * 86400 + 1 * 3600 + 1 * 60 + 1),
				'Spent 1 day 1 hour 1 minute 1 second',
			)
			self.assertEqual(upload_nlb.format_elapsed_time(0), 'Spent 0 seconds')

	def test_second_sigint_aborts_upload_and_leaves_it_pending_for_restart(self) -> None:
		"""Escape a stuck upload on a second Ctrl-C, clean up, and resume the pending item."""
		with tempfile.TemporaryDirectory() as tmp_dir:
			tmp_path = Path(tmp_dir)
			client = FakeNlbClient()
			uploader = FakeCommonsUploader()
			original_upload = uploader.upload
			original_handler = signal.getsignal(signal.SIGINT)
			state_path = tmp_path / 'state.json'
			staging_dir = tmp_path / 'staging'
			args = ['--state-file', str(state_path), '--staging-dir', str(staging_dir), '--max-uploads', '1']

			def stuck_upload(*args: object, **kwargs: object) -> dict:
				"""Send both interrupts while the scan is still pending."""
				signal.raise_signal(signal.SIGINT)
				signal.raise_signal(signal.SIGINT)
				return original_upload(*args, **kwargs)

			uploader.upload = stuck_upload
			with mock.patch('upload.NlbClient', return_value=client), mock.patch(
				'upload.CommonsUploader', return_value=uploader,
			), mock.patch(
				'upload.check_commons_by_hash', side_effect=lambda path, **_: (upload_nlb.calculate_sha1(path), []),
			), mock.patch('sys.stdout', io.StringIO()) as output:
				self.assertEqual(upload_nlb.main(args), 130)
				self.assertIs(signal.getsignal(signal.SIGINT), original_handler)
				self.assertEqual(uploader.uploads, [])
				self.assertEqual(list(staging_dir.iterdir()), [])
				state = upload_nlb.load_state(state_path)
				self.assertFalse(upload_nlb.is_item_completed(state, 'BY-NLB-rr111', '1001'))
				self.assertEqual(state['current_item_key'], 'BY-NLB-rr111:1001')
				self.assertIn('aborting current item', output.getvalue())
				uploader.upload = original_upload
				self.assertEqual(upload_nlb.main(args), 0)
				self.assertEqual(len(uploader.uploads), 1)
				self.assertTrue(upload_nlb.is_item_completed(upload_nlb.load_state(state_path), 'BY-NLB-rr111', '1001'))

	def test_commons_uploader_retries_on_uploadstash_bad_path_and_uses_async_chunked_upload(self) -> None:
		"""Verify uploadstash-bad-path is treated as retryable and CommonsUploader passes asynchronous=True."""
		class FakeAPIError(Exception):
			def __init__(self, code: str, info: str = '') -> None:
				super().__init__(f'{code}: {info}')
				self.code = code
				self.info = info

		class FakeUploadError(Exception):
			def __init__(self, code: str, message: str = '') -> None:
				super().__init__(message)
				self.code = code
				self.message = message

		bad_path_error = FakeAPIError('uploadstash-bad-path', "Path doesn't exist.")
		self.assertTrue(upload_nlb.is_retryable_upload_error(bad_path_error))

		uploader = upload_nlb.CommonsUploader.__new__(upload_nlb.CommonsUploader)
		fake_site = mock.MagicMock()
		fake_site.is_uploaddisabled.return_value = False
		upload_calls: list[dict[str, Any]] = []

		def fake_upload_driver(site: Any, file_page: Any, **kwargs: Any) -> Any:
			"""Capture transfer options and fail the first upload before recovering."""
			upload_calls.append(kwargs)
			driver = mock.Mock(already_exists=False)
			driver.upload.side_effect = [bad_path_error if len(upload_calls) == 1 else True]
			return driver

		fake_file_page = mock.MagicMock()
		fake_file_page.exists.return_value = False

		fake_pywikibot = mock.MagicMock()
		fake_pywikibot.FilePage.return_value = fake_file_page
		fake_pywikibot.exceptions.UploadError = FakeUploadError

		uploader.pywikibot = fake_pywikibot
		uploader.site = fake_site
		uploader._upload_driver_class = fake_upload_driver

		with mock.patch('upload.time.sleep') as sleep_mock:
			result = uploader.upload(
				Path('/tmp/scan.pdf'),
				'Book (1888) -- BY-NLB-br0000010197 -- 9884583.pdf',
				'== {{int:filedesc}} ==',
				'From https://elib.nlb.by/elib/Record/BY-NLB-br0000010197 (fileID 9884583)',
			)

		self.assertEqual(result['status'], 'uploaded')
		self.assertEqual(len(upload_calls), 2)
		self.assertTrue(upload_calls[0].get('asynchronous'))
		sleep_mock.assert_called_once_with(60)

	def test_disable_pywikibot_mime_from_mangling(self) -> None:
		"""Verify multipart MIME serialization does not prepend '>' to summaries starting with 'From '."""
		from email.generator import BytesGenerator
		from email.mime.multipart import MIMEMultipart
		from email.mime.nonmultipart import MIMENonMultipart
		from io import BytesIO

		class FakeCTEBinaryMIMEMultipart(MIMEMultipart):
			def as_bytes(self, unixfrom: bool = False, policy: Any = None) -> bytes:
				active_policy = self.policy if policy is None else policy
				fp = BytesIO()
				gen = BytesGenerator(fp, policy=active_policy)
				gen.flatten(self, unixfrom=unixfrom)
				return fp.getvalue()

		upload_nlb.patch_mime_multipart_from_mangling(FakeCTEBinaryMIMEMultipart)

		container = FakeCTEBinaryMIMEMultipart(_subtype='form-data')
		part = MIMENonMultipart('text', 'plain')
		part.add_header('Content-disposition', 'form-data', name='comment')
		part.set_payload('From https://elib.nlb.by/elib/Record/BY-NLB-br0000017173 (fileID 3973676)')
		container.attach(part)

		serialized = container.as_bytes().decode('utf-8')
		self.assertIn('\n\nFrom https://elib.nlb.by/elib/Record/BY-NLB-br0000017173 (fileID 3973676)', serialized)
		self.assertNotIn('>From ', serialized)


if __name__ == '__main__':
	unittest.main()
