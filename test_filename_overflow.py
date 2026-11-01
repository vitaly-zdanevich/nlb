#!/usr/bin/env python3
'''Keep readable filenames within their UTF-8 budget without losing scan identity.'''

from html import unescape
import unittest

import upload


class TestFilenameOverflow(unittest.TestCase):
	'''Exercise exact byte boundaries, multibyte titles and preserved descriptions.'''

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
