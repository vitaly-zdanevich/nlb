#!/usr/bin/env python3
'''Preserve personal-name headings without assigning aliases extra book roles.'''

from pathlib import Path
import tempfile
import unittest
from unittest import mock
import xml.etree.ElementTree as ET

import upload as upload_nlb
from test_upload import FakeCommonsUploader, FakeNlbClient


FIXTURES = Path(__file__).parent / 'tests' / 'fixtures'
NAMES_LABEL = 'Имена и варианты по каталогу НББ (MARC 900/902/990–992)'


def fixture(record_id: str) -> str:
	"""Read personal-name fields extracted from the public NLB MARCXML export."""
	return (FIXTURES / f'{record_id}-personal-names.xml').read_text()


def field(tag: str, *subfields: tuple[str, str]) -> str:
	"""Build a synthetic field while escaping names as XML character data."""
	element = ET.Element('datafield', tag=tag)
	for code, value in subfields:
		ET.SubElement(element, 'subfield', code=code).text = value
	return ET.tostring(element, encoding='unicode')


def record(*fields: str, namespace: bool = True) -> str:
	"""Wrap synthetic fields with or without the namespace used by NLB."""
	xmlns = ' xmlns="http://www.loc.gov/MARC21/slim"' if namespace else ''
	return (
		f'<collection{xmlns}><record><controlfield tag="001">BY-NLB-test</controlfield>'
		+ ''.join(fields) + '</record></collection>'
	)


def render(xml: str) -> tuple[dict, str]:
	"""Parse the source and render the same description submitted to Commons."""
	metadata = upload_nlb.parse_marcxml(xml)
	wikitext = upload_nlb.build_commons_description(
		{'title': 'Personal-name regression', 'fields': []},
		metadata, metadata['control_number'], '123',
	)
	return metadata, wikitext


def names_lines(wikitext: str) -> list[str]:
	"""Read only the new names section, avoiding author/contributor repetitions."""
	lines = wikitext.splitlines()
	start = next((index for index, line in enumerate(lines) if NAMES_LABEL in line), None)
	if start is None:
		return []
	result = [lines[start]]
	for line in lines[start + 1:]:
		if line.startswith('* ') or line.startswith('|'):
			break
		result.append(line)
	return result


class TestPersonalNames(unittest.TestCase):
	'''Exercise real omissions and ambiguous local authority-field associations.'''

	def test_real_pseudonyms_and_full_names_survive_without_extra_authors(self) -> None:
		"""Dunin-Marcinkiewicz's pseudonyms remain metadata, not extra creators."""
		metadata, wikitext = render(fixture('BY-NLB-br0000116590'))
		self.assertEqual(metadata['authors'], [
			'Дунін-Марцінкевіч, В. (Вінцэнт), пісьменнік (1808—1884)',
		])
		self.assertEqual(len(metadata['contributors']), 2)
		for name in (
			'Навум Прыгаворка (1808—1884)',
			'Войт Навум (1808—1884)',
			'Dunin-Marcinkiewicz, W. (Wincuk) (1807—1884)',
			'Dunin-Marcinkiewicz, W. (Wincenty) (1807—1884)',
			'Дунін-Марцінкевіч, В. Я. (Вінцэнт Якуб) (1808—1884)',
			'Марцінкевічы (род)',
			'Марцинкевичи (род)',
			'Наум Приговорка (1807—1884)',
		):
			with self.subTest(name=name):
				self.assertIn(name, '\n'.join(names_lines(wikitext)))
				self.assertNotIn(name, metadata['authors'])

	def test_real_name_qualifiers_survive_in_author_and_contributor(self) -> None:
		"""Repeated 700$c qualifiers distinguish the 1669 book's named author."""
		metadata, wikitext = render(fixture('BY-NLB-br0000118156'))
		expected = 'Иннокентий, Гизель, архимандрит Киево-Печерской лавры (1600—1683)'
		self.assertEqual(metadata['authors'], [expected])
		self.assertEqual(metadata['contributors'][0]['name'], expected)
		self.assertIn(f'|author={expected}', wikitext)
		self.assertIn('Гизель, И. (Иннокентий) (1600—1683)', '\n'.join(names_lines(wikitext)))

	def test_complete_names_numerals_and_corporate_names(self) -> None:
		'''Personal name parts survive together; corporate formatting stays intact.'''
		xml = record(
			field('700', ('a', 'Миллер'), ('b', 'В. Ф.'), ('g', 'Всеволод Федорович'),
				('c', 'фольклорист'), ('c', 'языковед'), ('f', '1848—1913')),
			field('701', ('a', 'Иоанн'), ('d', 'II'), ('c', 'король'), ('f', '1450—1500')),
			field('702', ('a', 'Smith'), ('g', 'John'), ('4', '340')),
			field('710', ('a', 'Academy'), ('b', 'Division'), ('c', 'Minsk'), ('f', '1900')),
		)
		metadata, _ = render(xml)
		self.assertEqual([item['name'] for item in metadata['contributors']], [
			'Миллер, В. Ф. (Всеволод Федорович), фольклорист, языковед (1848—1913)',
			'Иоанн II, король (1450—1500)',
			'Smith, John',
			'Academy, Division, Minsk (1900)',
		])

	def test_real_991_and_992_variants_remain_with_the_right_contributor(self) -> None:
		"""Translator and former-owner aliases retain their source heading context."""
		cases = (
			('BY-NLB-br0000060109', 'Косач-Драгоманова, О. П. (Ольга Петровна)',
				'Пчилка, О. (Олена), писательница', 'Гоголь, М. В.'),
			('BY-NLB-br0000060196', 'Карський, Ю. Ф. (Юхим Федорович)',
				'Карский, Е. Ф. (Евфимий Федорович)', 'Миллер, В. Ф.'),
		)
		for record_id, alias, heading, unrelated in cases:
			with self.subTest(record_id=record_id):
				metadata, wikitext = render(fixture(record_id))
				matching = [line for line in names_lines(wikitext) if alias in line]
				self.assertTrue(matching)
				self.assertTrue(all(heading in line for line in matching))
				self.assertTrue(all(unrelated not in line for line in matching))
				self.assertFalse(any(alias in name for name in metadata['authors']))
				self.assertFalse(any(alias in item['name'] for item in metadata['contributors']))

	def test_real_subject_name_and_family_context_are_preserved(self) -> None:
		"""900 aliases belong to the subject, including a related family's variants."""
		metadata, wikitext = render(fixture('BY-NLB-br0000116057'))
		subject = 'Муравьев, М. Н. (Михаил Николаевич), государственный деятель (1796—1866)'
		self.assertIn(subject, metadata['subjects'])
		lines = names_lines(wikitext)
		for name in ('Муравьев-Виленский', 'Муравьевы-Амурские (род)', 'Муравьевы-Апостолы (род)'):
			matching = [line for line in lines if name in line]
			self.assertTrue(matching, name)
			self.assertTrue(all(subject in line for line in matching), name)
			self.assertFalse(any(name in author for author in metadata['authors']))
		family_variant = next(line for line in lines if 'Муравьевы-Амурские (род)' in line)
		self.assertIn('Муравьевы (род)', family_variant)
		self.assertIn('420', family_variant)
		self.assertIn('520', '\n'.join(lines))
		self.assertNotIn('Муравьев', '\n'.join(metadata['authors']))

	def test_authority_matching_uses_both_id_and_tag_and_is_order_independent(self) -> None:
		"""Identical IDs in subject and contributor fields must not cross-link aliases."""
		headings = [
			field('700', ('3', 'authority-shared'), ('a', 'Creator')),
			field('701', ('3', 'authority-shared'), ('a', 'Translator'), ('4', '730')),
			field('600', ('3', 'authority-shared'), ('a', 'Subject')),
			field('700', ('a', 'Missing-ID creator')),
		]
		aliases = [
			field('990', ('3', 'authority-shared'), ('9', '400'), ('a', 'Creator alias')),
			field('991', ('3', 'authority-shared'), ('9', '400'), ('a', 'Translator alias')),
			field('900', ('3', 'authority-shared'), ('9', '400'), ('a', 'Subject alias')),
			field('992', ('3', 'authority-shared'), ('9', '400'), ('a', 'Unlinked alias')),
			field('990', ('9', '400'), ('a', 'Missing-ID alias')),
			field('990', ('3', 'unknown-id'), ('9', '777'), ('a', 'Unknown-type alias')),
		]
		for namespace in (False, True):
			for fields in (headings + aliases, aliases + headings):
				with self.subTest(namespace=namespace, variants_first=fields[0] in aliases):
					metadata, wikitext = render(record(*fields, namespace=namespace))
					lines = names_lines(wikitext)
					for alias, expected, forbidden in (
						('Creator alias', 'Creator', ['Translator', 'Subject']),
						('Translator alias', 'Translator', ['Creator', 'Subject']),
						('Subject alias', 'Subject', ['Creator', 'Translator']),
					):
						line = next((line for line in lines if alias in line), '')
						self.assertIn(expected, line)
						for name in forbidden:
							self.assertNotIn(name, line)
					for alias in ('Unlinked alias', 'Missing-ID alias', 'Unknown-type alias'):
						line = next((line for line in lines if alias in line), '')
						self.assertTrue(line, alias)
						self.assertNotIn('Missing-ID creator', line)
						self.assertNotIn('Translator', line)
						self.assertNotIn('Subject', line)
					self.assertIn('777', '\n'.join(lines))
					self.assertEqual(metadata['authors'], ['Creator', 'Missing-ID creator'])

	def test_embedded_related_names_keep_type_relationship_code_and_context(self) -> None:
		"""Repeated $a under 400 belongs to the preceding 500 or 520 heading."""
		metadata, wikitext = render(record(
			field('700', ('3', 'person'), ('a', 'Creator')),
			field('990', ('3', 'person'), ('9', '500'), ('5', 'w'), ('a', 'Related person'),
				('9', '400'), ('a', 'First related alias'), ('a', 'Second related alias')),
			field('990', ('3', 'person'), ('9', '520'), ('a', 'Related family'),
				('9', '420'), ('a', 'First family variant'), ('a', 'Second family variant')),
		))
		lines = names_lines(wikitext)
		for name in ('First related alias', 'Second related alias'):
			line = next((line for line in lines if name in line), '')
			self.assertIn('Related person', line)
			self.assertIn('400', line)
		for name in ('First family variant', 'Second family variant'):
			line = next((line for line in lines if name in line), '')
			self.assertIn('Related family', line)
			self.assertIn('420', line)
		related_line = next((line for line in lines if 'Related person' in line and '500' in line), '')
		self.assertIn('$5=w', related_line)
		self.assertEqual(metadata['authors'], ['Creator'])
		self.assertEqual(len(metadata['contributors']), 1)

	def test_same_spelling_with_distinct_context_is_not_deduplicated(self) -> None:
		"""A direct variant and another person's variant must preserve both relations."""
		_, wikitext = render(record(
			field('700', ('3', 'person'), ('a', 'Creator')),
			field('990', ('3', 'person'), ('9', '400'), ('a', 'Shared spelling')),
			field('990', ('3', 'person'), ('9', '500'), ('a', 'Related person'),
				('9', '400'), ('a', 'Shared spelling')),
		))
		matching = [line for line in names_lines(wikitext) if 'Shared spelling' in line]
		self.assertEqual(len(matching), 2)
		self.assertEqual(sum('Related person' in line for line in matching), 1)

	def test_variant_template_separator_is_escaped(self) -> None:
		"""A literal name pipe must not add a Commons template parameter."""
		_, wikitext = render(record(
			field('700', ('3', 'person'), ('a', 'Creator')),
			field('990', ('3', 'person'), ('9', '400'), ('a', 'Literal|Name')),
		))
		self.assertIn('Literal{{!}}Name', wikitext)
		self.assertNotIn('Literal|Name', wikitext)

	def test_variant_markup_and_reference_codes_are_displayed_literally(self) -> None:
		"""New name data cannot invoke templates, links or HTML inside the description."""
		_, wikitext = render(record(
			field('700', ('3', 'person'), ('a', 'Creator')),
			field('990', ('3', 'person'), ('9', '400'),
				('a', "{{Name}} [[Alias]] <ref>Note</ref> & ''text''"), ('5', '{{code}}')),
		))
		section = '\n'.join(names_lines(wikitext))
		self.assertIn('&#123;&#123;Name&#125;&#125;', section)
		self.assertIn('&#91;&#91;Alias&#93;&#93;', section)
		self.assertIn('&lt;ref&gt;Note&lt;/ref&gt; &amp; &#39;&#39;text&#39;&#39;', section)
		self.assertIn('$5=&#123;&#123;code&#125;&#125;', section)

	def test_changed_authority_and_heading_reset_reference_context(self) -> None:
		"""ID changes and repeated names never inherit another heading's type or $5."""
		metadata, _ = render(record(
			field('700', ('3', 'first'), ('a', 'First creator')),
			field('700', ('3', 'second'), ('a', 'Second creator')),
			field('990', ('3', 'first'), ('9', '500'), ('5', 'w'), ('a', 'Related name'),
				('9', '400'), ('a', 'Related alias'), ('a', 'Another related alias'),
				('3', 'second'), ('a', 'Untyped name'),
				('9', '400'), ('a', 'Direct alias'), ('5', 'unknown'), ('a', 'Another direct alias')),
		))
		entries = {entry['name']: entry for entry in metadata['personal_name_variants']}
		self.assertEqual(entries['Related name']['codes'], ['w'])
		for name in ('Related alias', 'Another related alias'):
			self.assertEqual(entries[name]['main_names'], ['First creator'])
			self.assertEqual(entries[name]['context'], 'Related name')
			self.assertEqual(entries[name]['codes'], [])
		for name in ('Untyped name', 'Direct alias', 'Another direct alias'):
			self.assertEqual(entries[name]['main_names'], ['Second creator'])
			self.assertEqual(entries[name]['context'], '')
		self.assertEqual(entries['Untyped name']['type'], '')
		self.assertEqual(entries['Direct alias']['codes'], ['unknown'])
		self.assertEqual(entries['Another direct alias']['codes'], [])

	def test_duplicate_empty_and_untyped_names_with_a_trailing_authority(self) -> None:
		"""Keep untyped names, deduplicate exact references and ignore entries with no name."""
		alias = field('990', ('a', 'Untyped alias'), ('3', 'person'))
		metadata, wikitext = render(record(
			field('700', ('3', 'person'), ('a', 'Creator')),
			alias, alias,
			field('990', ('3', 'person'), ('9', '400'), ('c', 'qualifier'), ('f', '1900')),
			field('702', ('c', 'qualifier'), ('f', '1900')),
		))
		self.assertEqual(metadata['authors'], ['Creator'])
		self.assertEqual(len(metadata['contributors']), 1)
		self.assertEqual(len(metadata['personal_name_variants']), 1)
		self.assertEqual(metadata['personal_name_variants'][0]['main_names'], ['Creator'])
		self.assertIn('тип не указан', '\n'.join(names_lines(wikitext)))

	def test_personal_subject_subdivisions_survive_full_name_formatting(self) -> None:
		"""Adding full names retains a subject's form, topic, place and chronology."""
		metadata, _ = render(record(
			field('600', ('a', 'Smith'), ('b', 'John'), ('g', 'John'), ('c', 'writer'),
				('j', 'Letters'), ('x', 'Biography'), ('y', 'London'), ('z', '19th century')),
		))
		self.assertEqual(metadata['subjects'], [
			'Smith, John, writer — Letters — Biography — London — 19th century',
		])

	def test_upload_pipeline_submits_preserved_names_and_cleans_staging(self) -> None:
		'''The normal upload path submits name preservation using only mocked I/O.'''
		record_id = 'BY-NLB-br0000116590'
		metadata = upload_nlb.parse_marcxml(fixture(record_id))
		client = FakeNlbClient()
		uploader = FakeCommonsUploader()
		search_page = {
			'total_results': 1,
			'records': [{
				'marc_id': record_id, 'title': 'Ciekawyś? Przeczytaj!',
				'formats': [], 'file_ids': ['123'], 'mets_ids': [],
			}],
		}
		with tempfile.TemporaryDirectory() as tmp_dir:
			state_path = Path(tmp_dir) / 'state.json'
			staging_dir = Path(tmp_dir) / 'staging'
			with (
				mock.patch.object(client, 'fetch_search_page', side_effect=[search_page, {'total_results': 1, 'records': []}]),
				mock.patch.object(client, 'fetch_record_metadata', return_value=(
					{'title': 'Ciekawyś? Przeczytaj!', 'fields': []}, metadata,
				)),
				mock.patch.object(client, 'resolve_record_files', return_value=[{'file_id': '123', 'part_label': ''}]),
				mock.patch.object(upload_nlb, 'check_commons_by_hash', side_effect=lambda path, **_: (upload_nlb.calculate_sha1(path), [])),
			):
				counts = upload_nlb.iterate_and_upload(
					client=client, uploader=uploader, state_path=state_path, staging_dir=staging_dir, explicit_license=True,
				)
			self.assertEqual(counts['uploaded'], 1)
			wikitext = uploader.uploads[0]['wikitext']
			self.assertIn(NAMES_LABEL, wikitext)
			self.assertIn('Навум Прыгаворка (1808—1884)', wikitext)
			self.assertIn('|author=Дунін-Марцінкевіч, В. (Вінцэнт), пісьменнік (1808—1884)', wikitext)
			self.assertEqual(list(staging_dir.iterdir()), [])
			state = upload_nlb.load_state(state_path)
			self.assertEqual(state['items'][f'{record_id}:123']['status'], 'uploaded')


if __name__ == '__main__':
	unittest.main()
