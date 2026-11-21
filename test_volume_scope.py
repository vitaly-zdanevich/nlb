'''Exercise exact volume/part parsing without relaxing other identity checks.'''

import unittest

from volume_scope import parse_volume_scope, volume_number


class TestVolumeScope(unittest.TestCase):
	'''Preserve every supported dimension and refuse incomplete designations.'''

	def test_explicit_volumes(self):
		'''Viewer labels and catalog abbreviations describe the same volume.'''
		for label, number in (
			('TOM II', 2), ('ТОМ III', 3), ('Т. 4', 4), ('T.2', 2),
			('Tome IV.', 4), ('Volume 12', 12), ('Vol. XII', 12),
			('Band 9', 9), ('Bd. IX', 9), (' [T. 4] ', 4),
		):
			with self.subTest(label=label):
				self.assertEqual(parse_volume_scope(label), ({'volume': number}, False))

	def test_compound_catalog_and_viewer_designations(self):
		'''Different punctuation must preserve the volume-IV part distinction.'''
		for label, part in (
			('TOM IV, CZĘŚĆ 1', 1), ('T. 4. [Cz. 1]', 1),
			('[T. 4. Cz. 2]', 2), ('TOM IV, CZĘŚĆ 2', 2),
			('Tom IV część II', 2), ('Том IV, часть II', 2),
			('Volume IV. Part 2', 2), ('Vol. IV, pt. II.', 2),
		):
			with self.subTest(label=label):
				self.assertEqual(parse_volume_scope(label), ({'volume': 4, 'part': part}, False))

	def test_standalone_parts_series_and_generic_labels_are_not_volumes(self):
		'''Scan chunks and unimplemented series do not establish volume scope.'''
		for label in ('', 'Contents', 'Tomato', 'Ч. 1', 'Cz. 2', 'CZĘŚĆ 1', 'Part II', 'Ser. 4', 'Ser. 4. T. 2', 'Serya 4', '1-92 с.'):
			with self.subTest(label=label):
				self.assertEqual(parse_volume_scope(label), ({}, False))

	def test_ranges_and_noncanonical_numbers_are_unsupported(self):
		'''Neither a range nor a malformed Roman numeral selects one volume.'''
		for label in ('TOM I–II', 'TOM 1-2', 'TOM I/II', 'TOM IIII', 'TOM IC', 'TOM 0', 'TOM -1', 'TOM 1.5', 'TOM ²', 'TOM ４', 'TOM'):
			with self.subTest(label=label):
				self.assertEqual(parse_volume_scope(label), ({}, True))

	def test_incomplete_or_invalid_part_invalidates_whole_scope(self):
		'''An invalid part cannot degrade to a usable volume-only match.'''
		for label in ('TOM IV, CZĘŚĆ', 'TOM IV, CZĘŚĆ 0', 'TOM IV, CZĘŚĆ IIII', 'TOM IV, CZĘŚĆ 1-2', 'TOM IV,', 'TOM IV, 1', 'TOM IVCZ. 1'):
			with self.subTest(label=label):
				self.assertEqual(parse_volume_scope(label), ({}, True))

	def test_extra_qualifiers_are_not_discarded(self):
		'''Descriptions and additional dimensions need independent interpretation.'''
		for label in ('TOM IV — 1860 rok', 'TOM IV, CZĘŚĆ 1 — 1860 rok', 'TOM IV, Ser. 2', 'TOM IV, CZĘŚĆ 1, CZĘŚĆ 2', 'TOM IV bis', 'TOM IV: Other work'):
			with self.subTest(label=label):
				self.assertEqual(parse_volume_scope(label), ({}, True))

	def test_unbalanced_or_misplaced_brackets_are_unsupported(self):
		'''Bibliographic brackets must surround complete supported expressions.'''
		for label in ('[TOM IV', 'TOM IV]', '[TOM IV, CZĘŚĆ 1', 'TOM IV, [CZĘŚĆ 1', 'TOM IV, CZĘŚĆ 1]', '[TOM IV] [CZĘŚĆ 1]', 'TOM [IV]'):
			with self.subTest(label=label):
				self.assertEqual(parse_volume_scope(label), ({}, True))

	def test_volume_number_compatibility(self):
		'''The existing positive-number and canonical-Roman rules are retained.'''
		for value, expected in (('1', 1), ('001', 1), ('xiv.', 14), ('MCMXCIX', 1999), ('0', None), ('IIII', None), ('I–II', None), ('', None)):
			with self.subTest(value=value):
				self.assertEqual(volume_number(value), expected)
