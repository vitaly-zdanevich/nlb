#!/usr/bin/env python3
'''Keep readable filenames within their UTF-8 budget without losing scan identity.'''

from html import unescape
import json
from pathlib import Path
import unittest

import upload


class TestFilenameOverflow(unittest.TestCase):
	'''Exercise exact byte boundaries, multibyte titles and preserved descriptions.'''

	def test_damaged_source_labels_do_not_stop_filename_generation(self) -> None:
		'''METS 130279 contains literal U+FFFD bytes; preserve them outside filenames.

		Captured from https://elib.nlb.by:8070/mets//structure/state?id=130279
		on 2026-11-16; source SHA-256:
		f6b24c0e5841738ef8ff306c9e7f82fbd36081fe42951d5ef06821c9c24d2a1d.
		'''
		structure = json.loads((Path(__file__).parent / 'fixtures/damaged_viewer_labels.json').read_text())
		files = upload.extract_files_from_mets_structure(structure)
		metadata = {'title': 'Миниа, месяц март', 'date': '1750'}
		names = []
		for entry in files:
			label, file_id = entry['part_label'], entry['file_id']
			self.assertIn('\ufffd', label)
			name = upload.build_commons_filename({}, metadata, 'BY-NLB-br0000686157', file_id, label)
			self.assertEqual(name, f'Миниа, месяц март (1750) -- BY-NLB-br0000686157 -- {file_id}.pdf')
			self.assertIn(label, unescape(upload.build_commons_description({}, metadata, 'BY-NLB-br0000686157', file_id, label)))
			names.append(name)
		self.assertEqual(len(set(names)), 2)
		self.assertNotIn('\ufffd', upload.build_commons_filename({}, {'title': 'Damaged \ufffd title'}, 'record', '123'))

	def test_short_filename_keeps_year_record_and_file_identifiers(self) -> None:
		'''The compact suffix only applies when the full filename exceeds 240 bytes.'''
		self.assertEqual(
			upload.build_commons_filename({}, {'title': 'Example', 'date': '1850'}, 'BY-NLB-test', '123'),
			'Example (1850) -- BY-NLB-test -- 123.pdf',
		)

	def test_exact_240_byte_filename_keeps_full_suffix(self) -> None:
		'''A full filename at the limit does not discard useful identifiers.'''
		suffix = ' (1850) -- BY-NLB-test -- 123.pdf'
		title = 'A' * (240 - len(suffix.encode('utf-8')))
		filename = upload.build_commons_filename({}, {'title': title, 'date': '1850'}, 'BY-NLB-test', '123')
		self.assertEqual(filename, title + suffix)
		self.assertEqual(len(filename.encode('utf-8')), 240)

	def test_overflow_omits_metadata_suffix_before_truncating_title(self) -> None:
		'''Dropping the year and record ID can preserve the complete title.'''
		suffix = ' (1850) -- BY-NLB-test -- 123.pdf'
		title = 'A' * (240 - len(suffix.encode('utf-8'))) + '.'
		filename = upload.build_commons_filename({}, {'title': title, 'date': '1850'}, 'BY-NLB-test', '123')
		self.assertEqual(filename, title + ' -- 123.pdf')

	def test_long_multibyte_title_preserves_complete_codepoints(self) -> None:
		'''Truncation retains the longest whole-codepoint prefix that fits the suffix.'''
		for character in ('A', 'Б', '書', '𝄞'):
			with self.subTest(character=character):
				title = character * 300
				filename = upload.build_commons_filename({}, {'title': title, 'date': '1850'}, 'BY-NLB-test', '123')
				self.assertTrue(filename.endswith(' -- 123.pdf'))
				prefix = filename.removesuffix(' -- 123.pdf')
				self.assertTrue(title.startswith(prefix))
				self.assertLessEqual(len(filename.encode('utf-8')), 240)
				self.assertGreater(len((prefix + character + ' -- 123.pdf').encode('utf-8')), 240)
				self.assertNotIn('\ufffd', filename)

	def test_multipart_scans_keep_distinct_names_after_title_truncation(self) -> None:
		'''The file ID distinguishes parts even when their labels fall beyond the cut.'''
		names = [
			upload.build_commons_filename({}, {'title': 'Длинное название ' * 30}, 'BY-NLB-test', file_id, f'Part {index}')
			for index, file_id in enumerate(('123', '124'), start=1)
		]
		self.assertNotEqual(*names)
		self.assertTrue(names[0].endswith(' -- 123.pdf'))
		self.assertTrue(names[1].endswith(' -- 124.pdf'))
		self.assertEqual(names[0].removesuffix(' -- 123.pdf'), names[1].removesuffix(' -- 124.pdf'))

	def test_oversized_file_identifier_cannot_produce_an_overlong_filename(self) -> None:
		'''Refuse an impossible suffix instead of truncating away scan identity.'''
		with self.assertRaisesRegex(ValueError, 'file ID is too long'):
			upload.build_commons_filename({}, {'title': 'Example'}, 'BY-NLB-test', '1' * 240)

	def test_filename_shortening_preserves_full_description_metadata(self) -> None:
		'''The full title, part, publication date and record ID remain readable on Commons.'''
		title = 'Original 1820 title ' * 30
		metadata = {'title': title.strip(), 'date': '1850'}
		filename = upload.build_commons_filename({}, metadata, 'BY-NLB-test', '123', 'Second part')
		wikitext = unescape(upload.build_commons_description({}, metadata, 'BY-NLB-test', '123', 'Second part'))
		self.assertTrue(filename.startswith('Original 1820 title'))
		self.assertTrue(filename.endswith(' -- 123.pdf'))
		self.assertNotIn('1850', filename)
		self.assertNotIn('BY-NLB-test', filename)
		self.assertIn(metadata['title'], wikitext)
		self.assertIn('Second part', wikitext)
		self.assertIn('|date=1850\n', wikitext)
		self.assertIn('BY-NLB-test', wikitext)


if __name__ == '__main__':
	unittest.main()
