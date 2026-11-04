'''Exercise useful IIIF outline preservation without generating lists of every scan page.'''

import unittest

import upload
from viewer_outline import outline_entries


class TestViewerOutline(unittest.TestCase):
	'''Use embedded IIIF 2/3 fixtures to preserve hierarchy, language and literal labels.'''

	def test_nested_ranges_keep_context_and_do_not_copy_canvas_labels(self) -> None:
		'''Only the source's named navigation ranges become descriptive contents.'''
		manifest = {'structures': [{'id': 'book', 'type': 'Range', 'label': 'Book', 'items': [
			{'id': 'chapter-a', 'type': 'Range', 'label': 'Chapter A', 'items': [
				{'id': 'intro-a', 'type': 'Range', 'label': 'Introduction'},
				{'id': 'page-1', 'type': 'Canvas', 'label': '1'},
			]},
			{'id': 'chapter-b', 'type': 'Range', 'label': 'Chapter B', 'items': [
				{'id': 'intro-b', 'type': 'Range', 'label': 'Introduction'},
			]},
		]}], 'items': [{'type': 'Canvas', 'label': str(i)} for i in range(1000)]}
		self.assertEqual(outline_entries(manifest, str), [
			'Book', 'Book / Chapter A', 'Book / Chapter A / Introduction',
			'Book / Chapter B', 'Book / Chapter B / Introduction',
		])

	def test_flat_v2_ranges_resolve_in_source_order_without_duplicate_roots(self) -> None:
		'''References in flat IIIF 2 structures resolve locally and retain their parents.'''
		manifest = {'structures': [
			{'@id': 'toc', '@type': 'sc:Range', 'label': 'Contents', 'viewingHint': 'top', 'ranges': ['b', 'a']},
			{'@id': 'a', '@type': 'sc:Range', 'label': 'Part A', 'canvases': ['page-a']},
			{'@id': 'b', '@type': 'sc:Range', 'label': 'Part B', 'members': [
				{'@id': 'detail', '@type': 'sc:Range', 'label': 'Detail'},
				{'@id': 'page-b', '@type': 'sc:Canvas', 'label': 'Page B'},
			]},
			{'@id': 'detail', '@type': 'sc:Range', 'viewingHint': 'non-paged', 'canvases': ['page-b']},
		]}
		self.assertEqual(outline_entries(manifest, str), ['Contents', 'Contents / Part B', 'Contents / Part B / Detail', 'Contents / Part A'])

	def test_navigation_hints_omit_hidden_labels_and_thumbnail_navigation(self) -> None:
		'''Hidden wrappers retain visible children; thumbnail grids do not become contents.'''
		manifest = {'structures': [
			{'id': 'wrapper', 'type': 'Range', 'label': 'Hidden wrapper', 'behavior': ['no-nav'], 'items': [
				{'id': 'intro', 'type': 'Range', 'label': 'Introduction'},
			]},
			{'id': 'thumbs', 'type': 'Range', 'label': 'Thumbnail grid', 'behavior': ['thumbnail-nav'], 'items': [
				{'id': 'page', 'type': 'Range', 'label': '1'},
			]},
		]}
		self.assertEqual(outline_entries(manifest, str), ['Introduction'])

	def test_cycles_and_unresolved_references_do_not_block_other_sections(self) -> None:
		'''Invalid source cycles stop at their ancestry without inventing remote labels.'''
		manifest = {'structures': [
			{'@id': 'a', '@type': 'sc:Range', 'label': 'A', 'ranges': ['b']},
			{'@id': 'b', '@type': 'sc:Range', 'label': 'B', 'ranges': ['a', 'https://example.org/not-fetched']},
		]}
		entries = outline_entries(manifest, str)
		self.assertEqual(entries, ['A', 'A / B', 'B', 'B / A'])
		self.assertNotIn('https://example.org/not-fetched', ' '.join(entries))

	def test_meaningful_large_outline_is_not_truncated(self) -> None:
		'''Source-named sections survive even when an outline is longer than a typical book.'''
		manifest = {'structures': [{'id': str(i), 'type': 'Range', 'label': f'Named section {i}'} for i in range(250)]}
		entries = outline_entries(manifest, str)
		self.assertEqual(len(entries), 250)
		self.assertEqual(entries[-1], 'Named section 249')

	def test_language_maps_and_source_markup_stay_literal_on_commons(self) -> None:
		'''Rendered outline labels keep language variants and cannot invoke wiki templates.'''
		original = {'title': 'Book'}
		metadata = upload.with_viewer_metadata(original, {'structures': [{
			'id': 'intro', 'type': 'Range',
			'label': {'en': ['Introduction {{literal}}'], 'ru': ['Введение']},
		}]})
		text = upload.build_commons_description(metadata, {'date': '1800'}, 'BY-NLB-one', '1')
		self.assertIn('Viewer outline', text)
		self.assertIn('Введение', text)
		self.assertIn(upload.escape_wikitext('Introduction {{literal}}'), text)
		self.assertNotIn('{{literal}}', text)
		self.assertEqual(original, {'title': 'Book'})

	def test_absent_or_malformed_optional_structures_add_no_empty_field(self) -> None:
		'''Unsupported structural values remain raw-only without producing meaningless output.'''
		for structures in (None, {}, 'wrong shape', [None, {}, 'unresolved']):
			with self.subTest(structures=structures):
				self.assertEqual(outline_entries({'structures': structures}, str), [])
				self.assertNotIn('Viewer outline', str(upload.with_viewer_metadata({}, {'structures': structures})))

	def test_malformed_optional_range_id_does_not_discard_its_useful_label(self) -> None:
		'''An unusable reference identifier does not make descriptive label extraction fail.'''
		manifest = {'structures': [{'id': {}, 'type': 'Range', 'label': 'Introduction', 'behavior': None}]}
		self.assertEqual(outline_entries(manifest, str), ['Introduction'])


if __name__ == '__main__':
	unittest.main()
