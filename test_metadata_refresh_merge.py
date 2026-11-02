'''Refresh owned bibliographic fields while retaining Commons contributions and licensing.'''

import unittest

from metadata_refresh_merge import merge_generated_metadata


def description(text='Original catalog description', **fields):
	'''Build an archived generated Information template with readable field layout.'''
	return '{{Information\n|description=' + text + '\n' + ''.join(f'|{key}={value}\n' for key, value in fields.items()) + '}}'


def block(text, key='BY-NLB-one:1'):
	'''Build one exact per-scan metadata block owned by the uploader.'''
	return f'<!-- NLB-METADATA:{key}:BEGIN -->\n{text}\n<!-- NLB-METADATA:{key}:END -->'


class TestMetadataRefreshMerge(unittest.TestCase):
	'''Use archived/current/generated fixtures without any network access or Commons edits.'''

	def test_updates_unchanged_fields_and_preserves_every_byte_outside_information(self):
		'''Replace stale bibliographic values without copying new license or category text.'''
		baseline = description(date='[1836]', author='Catalog author') + '\n{{Old-license}}'
		generated = description('Expanded catalog description', date='1836', author='New catalog author') + '\n{{New-license}}'
		prefix = 'Human lead\n<!-- custom note -->\n'
		suffix = '\n=={{int:license-header}}==\n{{PD-old-70}}\n[[Category:Human category]]\n'
		current = prefix + description(date='[1836]', author='Catalog author') + suffix
		merged = merge_generated_metadata(current, baseline, generated)
		self.assertEqual(merged, prefix + description('Expanded catalog description', date='1836', author='New catalog author') + suffix)

	def test_human_changed_added_and_removed_fields_survive(self):
		'''Respect field-level contributions while still updating the untouched description.'''
		baseline = description(date='1836', author='Catalog author', source='Old source')
		current = description(date='1837', author='Human author', other_fields='Human notes')
		generated = description('New description', date='1838', author='New catalog author', source='Updated source', other_fields='Catalog notes')
		self.assertEqual(merge_generated_metadata(current, baseline, generated), description('New description', date='1837', author='Human author', other_fields='Human notes'))

	def test_new_fields_add_and_removed_unchanged_fields_disappear(self):
		'''A known generated field can retire; a genuinely new absent field can be introduced.'''
		baseline = description(author='Old author')
		generated = description('New description', date='1836')
		self.assertEqual(merge_generated_metadata(baseline, baseline, generated), generated)

	def test_human_description_change_or_removal_rejects_entire_page(self):
		'''Avoid combining new catalog claims with a description carrying human corrections.'''
		baseline = description(date='1836')
		for current in (description('Human corrected title', date='1836'), '{{Information|date=1836}}', '{{Information|Description=Original catalog description|date=1836}}'):
			with self.subTest(current=current), self.assertRaisesRegex(ValueError, 'description'):
				merge_generated_metadata(current, baseline, description('New title', date='1838'))

	def test_surrounding_field_whitespace_does_not_prevent_refresh(self):
		'''Outer whitespace formatting alone does not count as a human change to the value.'''
		baseline = description(date='1836')
		current = '{{Information\n|description= Original catalog description \n|date= 1836 \n}}'
		generated = description('Updated description', date='1838')
		self.assertEqual(merge_generated_metadata(current, baseline, generated), generated)

	def test_permission_is_preserved_even_when_old_generated_value_matches(self):
		'''Metadata refresh never changes the Information permission/license assertion.'''
		baseline = description(permission='{{PD-old-70}}')
		generated = description('New description', permission='{{Different-license}}')
		self.assertEqual(merge_generated_metadata(baseline, baseline, generated), description('New description', permission='{{PD-old-70}}'))
		self.assertEqual(merge_generated_metadata(description(), description(), generated), description('New description'))

	def test_missing_duplicate_or_ambiguous_information_rejects_page(self):
		'''Ownership must refer to a unique template and unambiguous parameter names.'''
		baseline = description()
		for malformed in ('Human text', baseline + baseline, '{{Information|description=One|description=Two}}'):
			for position in range(3):
				with self.subTest(malformed=malformed, position=position):
					texts = [baseline] * 3
					texts[position] = malformed
					with self.assertRaises(ValueError):
						merge_generated_metadata(*texts)

	def test_owned_metadata_blocks_refresh_and_unknown_blocks_survive(self):
		'''Only blocks supplied in the archived baseline can replace existing block text.'''
		baseline = description() + '\n' + block('Old metadata')
		current = baseline + '\n' + block('Human or unrelated metadata', 'BY-NLB-peer:2')
		generated = description('New description') + '\n' + block('New metadata')
		self.assertEqual(merge_generated_metadata(current, baseline, generated), generated + '\n' + block('Human or unrelated metadata', 'BY-NLB-peer:2'))

	def test_human_changed_owned_metadata_block_rejects_page(self):
		'''Pre-existing edits inside ownership markers are conflicts, not disposable text.'''
		baseline = description() + '\n' + block('Old metadata')
		current = description() + '\n' + block('Human corrected metadata')
		generated = description('New description') + '\n' + block('New metadata')
		with self.assertRaisesRegex(ValueError, 'block'):
			merge_generated_metadata(current, baseline, generated)

	def test_malformed_or_nested_managed_markers_reject_page(self):
		'''Do not interpret duplicate, incomplete or overlapping markers as owned sections.'''
		baseline = description()
		for markers in (block('First') + block('Second'), '<!-- NLB-METADATA:BY-NLB-one:1:BEGIN -->', block(block('Inner', 'BY-NLB-peer:2'))):
			with self.subTest(markers=markers), self.assertRaises(ValueError):
				merge_generated_metadata(baseline + markers, baseline, baseline)


if __name__ == '__main__':
	unittest.main()
