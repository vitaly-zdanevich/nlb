#!/usr/bin/env python3
"""Multi-level unit, component, and end-to-end tests for upload.py."""

import hashlib
import io
import json
from pathlib import Path
import signal
import tempfile
import types
import unittest
from unittest import mock

import upload as upload_nlb


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
	"""Level 1 unit tests for URL/HTML/MARCXML/METS parsing, naming, and state helpers."""

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
		"""Verify HTML record parser and UNIMARC XML parser extract complete metadata."""
		record_meta = upload_nlb.parse_record_html(MOCK_RECORD_HTML_111, 'BY-NLB-rr111')
		self.assertEqual(record_meta['title'], 'Минские губернские ведомости : официальная часть')
		self.assertEqual(record_meta['file_ids'], ['1001'])
		field_names = [f['name'] for f in record_meta['fields']]
		self.assertIn('Дата создания', field_names)
		self.assertIn('Опубликовано', field_names)
		self.assertNotIn('Всего', field_names)
		self.assertNotIn('Шифр', field_names)

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

	def test_parse_marcxml_and_description_with_contents_and_all_notes(self) -> None:
		"""Verify MARC 327 contents note (TOC with section numbers and folio ranges) and 3xx notes are parsed."""
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
		self.assertIn("* '''Содержание (MARC 327):'''", wikitext)
		for entry in marc_meta['contents']:
			self.assertIn(f'** {entry}', wikitext)
		self.assertIn("* '''Примечания (MARC 3xx):'''", wikitext)

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
			"* '''ГРНТИ:'''\n** 11.01.53\n** 03.91\n** 03.17\n** 03.81.37",
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
			state['items']['BY-NLB-rr111:1001'] = {'status': 'uploaded'}
			upload_nlb.save_state(state_path, state)

			reloaded = upload_nlb.load_state(state_path, until_year=1900)
			self.assertEqual(reloaded['current_page'], 3)
			self.assertTrue(upload_nlb.is_item_completed(reloaded, 'BY-NLB-rr111', '1001'))

			changed_year = upload_nlb.load_state(state_path, until_year=1880)
			self.assertEqual(changed_year['until_year'], 1880)
			self.assertEqual(changed_year['current_page'], 1)
			self.assertTrue(upload_nlb.is_item_completed(changed_year, 'BY-NLB-rr111', '1001'))


class TestComponentIntegration(unittest.TestCase):
	"""Level 2 component and integration tests for hash checking, IIIF handshake, and uploader."""

	def test_check_commons_by_hash_via_find_by_hash_script(self) -> None:
		"""Verify check_commons_by_hash executes find_by_hash.sh and parses JSON output."""
		with tempfile.TemporaryDirectory() as tmp_dir:
			tmp_path = Path(tmp_dir)
			pdf_file = tmp_path / 'sample.pdf'
			pdf_bytes = b'%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF\n'
			pdf_file.write_bytes(pdf_bytes)
			expected_sha1 = hashlib.sha1(pdf_bytes).hexdigest()

			mock_script = tmp_path / 'find_by_hash.sh'
			response_json = json.dumps({
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
			})
			mock_script.write_text(
				'#!/usr/bin/env bash\n'
				'echo "* Connected to commons.wikimedia.org" >&2\n'
				f"printf '%s\\n' '{response_json}'\n",
				encoding='utf-8',
			)
			mock_script.chmod(0o755)

			sha1, matches = upload_nlb.check_commons_by_hash(
				pdf_file,
				find_by_hash_script=mock_script,
			)
			self.assertEqual(sha1, expected_sha1)
			self.assertEqual(matches, ['File:Existing NLB Scan.pdf'])

	def test_check_commons_by_hash_http_fallback_and_mismatch_error(self) -> None:
		"""Verify HTTP API fallback when find_by_hash.sh is absent and SHA-1 mismatch detection."""
		with tempfile.TemporaryDirectory() as tmp_dir:
			pdf_file = Path(tmp_dir) / 'sample.pdf'
			pdf_bytes = b'%PDF-1.5\n%test\n%%EOF\n'
			pdf_file.write_bytes(pdf_bytes)
			expected_sha1 = hashlib.sha1(pdf_bytes).hexdigest()

			mock_session = mock.MagicMock()
			mock_response = mock.MagicMock()
			mock_response.json.return_value = {
				'batchcomplete': '',
				'query': {'allimages': []},
			}
			mock_session.get.return_value = mock_response

			sha1, matches = upload_nlb.check_commons_by_hash(
				pdf_file,
				session=mock_session,
				find_by_hash_script=Path(tmp_dir) / 'nonexistent.sh',
			)
			self.assertEqual(sha1, expected_sha1)
			self.assertEqual(matches, [])

			with self.assertRaises(ValueError):
				upload_nlb.parse_commons_allimages_response(
					{'query': {'allimages': [{'title': 'File:Bad.pdf', 'sha1': '0' * 40}]}},
					expected_sha1,
				)

	def test_nlb_client_handshake_and_pdf_download(self) -> None:
		"""Verify NlbClient executes the 5-step port-8070 IIIF handshake and streams the PDF."""
		pdf_payload = b'%PDF-1.6\nFake NLB scan binary payload\n%%EOF\n'
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

	def test_nlb_client_rejects_non_pdf_and_truncated_stream(self) -> None:
		"""Verify NlbClient raises ValueError if downloaded content is not a complete PDF."""
		mock_session = mock.MagicMock()
		mock_session.headers = {}
		mock_session.post.return_value = mock.MagicMock()

		manifest_resp = mock.MagicMock()
		manifest_resp.json.return_value = {'rendering': []}
		ok_resp = mock.MagicMock()

		bad_stream = mock.MagicMock()
		bad_stream.__enter__.return_value = bad_stream
		bad_stream.__exit__.return_value = False
		bad_stream.headers = {'Content-Length': '14'}
		bad_stream.iter_content.return_value = [b'<html>Error</html>']

		mock_session.get.side_effect = [manifest_resp, ok_resp, ok_resp, bad_stream]
		client = upload_nlb.NlbClient(session=mock_session)

		with tempfile.TemporaryDirectory() as tmp_dir:
			target = Path(tmp_dir) / 'bad.pdf'
			with self.assertRaisesRegex(ValueError, 'not a valid PDF'):
				client.download_pdf_scan('BY-NLB-rr111', '1001', target)

	def test_nlb_client_fetch_record_metadata_with_toc_fallback(self) -> None:
		"""Verify NlbClient.fetch_record_metadata falls back to /TOC tab when MARCXML lacks 327."""
		mock_session = mock.MagicMock()
		mock_session.headers = {}

		record_resp = mock.MagicMock()
		record_resp.status_code = 200
		record_resp.text = MOCK_RECORD_HTML_111

		marc_resp = mock.MagicMock()
		marc_resp.status_code = 200
		marc_resp.text = MOCK_MARCXML_111

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

		file_page = mock.MagicMock()
		file_page.exists.return_value = False

		transient_err = fake_UploadError('Stash failed temporarily')
		transient_err.code = 'stashfailed'

		dup_err = fake_UploadError("Uploaded file is a duplicate of ['Old_Scan.pdf'].")
		dup_err.code = 'duplicate'
		dup_err.message = "Uploaded file is a duplicate of ['Old_Scan.pdf']."

		file_page.upload.side_effect = [transient_err, dup_err]
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
	"""In-memory fake CommonsUploader recording uploaded titles, wikitext, and file cleanup."""

	def __init__(self) -> None:
		"""Initialize empty upload history."""
		self.uploads: list[dict[str, str]] = []

	def title_exists(self, commons_filename: str) -> bool:
		"""Return False so all new titles proceed to hash check and upload."""
		return False

	def upload(self, file_path: Path, commons_filename: str, wikitext: str, summary: str) -> dict[str, object]:
		"""Verify the local file exists during upload and record the submission."""
		if not file_path.is_file():
			raise AssertionError('Expected local PDF to exist during upload call')
		self.uploads.append({
			'commons_filename': commons_filename,
			'wikitext': wikitext,
			'summary': summary,
		})
		return {'status': 'uploaded', 'commons_title': f'File:{commons_filename}'}


class TestEndToEndPipeline(unittest.TestCase):
	"""Level 3 end-to-end tests covering full iteration, hash deduplication, cleanup, and resume."""

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
				find_by_hash_script: Path | None = None,
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
		"""Verify progress output prints the item URLs and current actions (downloading, hash check, uploading)."""
		with tempfile.TemporaryDirectory() as tmp_dir:
			tmp_path = Path(tmp_dir)
			state_path = tmp_path / 'state.json'
			staging_dir = tmp_path / 'staging'

			client = FakeNlbClient()
			uploader = FakeCommonsUploader()

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
				'File:Минские губернские ведомости - официальная часть(1890) -- BY-NLB-rr111 -- 1001.pdf',
				output,
			)
			self.assertIn(
				'[BY-NLB-rr111:1001] Downloading scan:\n'
				'https://elib.nlb.by:8070/viewer?markID=BY-NLB-rr111&fileID=1001 '
				'(record: https://elib.nlb.by/elib/Record/BY-NLB-rr111)',
				output,
			)
			self.assertIn(
				'[BY-NLB-rr111:1001] Uploading to Wikimedia Commons:\n'
				'File:Минские губернские ведомости - официальная часть(1890) -- BY-NLB-rr111 -- 1001.pdf',
				output,
			)
			self.assertIn(
				'[BY-NLB-rr111:1001] Uploaded:\nhttps://commons.wikimedia.org/wiki/File:'
				'Минские_губернские_ведомости_-_официальная_часть(1890)_--_BY-NLB-rr111_--_1001.pdf\n\n',
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
		upload_calls: list[dict[str, Any]] = []

		def fake_upload_call(path_str: str, **kwargs: Any) -> bool:
			upload_calls.append({'path': path_str, **kwargs})
			if len(upload_calls) == 1:
				raise bad_path_error
			return True

		fake_file_page = mock.MagicMock()
		fake_file_page.exists.return_value = False
		fake_file_page.upload.side_effect = fake_upload_call

		fake_pywikibot = mock.MagicMock()
		fake_pywikibot.FilePage.return_value = fake_file_page
		fake_pywikibot.exceptions.UploadError = FakeUploadError

		uploader.pywikibot = fake_pywikibot
		uploader.site = fake_site

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
