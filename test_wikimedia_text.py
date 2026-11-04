'''Check publication normalization while keeping the original archive unchanged.'''

import copy
import json
from pathlib import Path
import tempfile
import unittest

import requests

from preservation import ArchiveStore
import upload
from wikimedia_text import normalize_wikitext


class TestWikimediaText(unittest.TestCase):
	'''Normalize only generated text and explicit comparison copies.'''

	def test_nfc_controls_and_compatibility_characters(self) -> None:
		'''Drop forbidden C0 codes without folding meaningful typography or whitespace.'''
		controls = ''.join(chr(code) for code in range(32) if code not in (9, 10, 13))
		text = 'Cafe\u0301\t\n\rﬁ Ⅰ\u200d' + controls
		self.assertEqual(normalize_wikitext(text), 'Café\t\n\rﬁ Ⅰ\u200d')
		self.assertEqual(normalize_wikitext(normalize_wikitext(text)), normalize_wikitext(text))
		# Removing an illegal separator can expose a new combining sequence.
		self.assertEqual(normalize_wikitext('e\x01\u0301'), 'é')

	def test_renderer_keeps_original_metadata_and_response_bytes(self) -> None:
		'''Commons gets valid composed text while both source and parsed archive retain originals.'''
		body = '<h1>Cafe\u0301\x01</h1><table><tr><th>Note</th><td>[[literal]]\x02 Cafe\u0301</td></tr></table>'
		record = upload.parse_record_html(body, 'BY-NLB-test')
		marc = upload.parse_marcxml('<record><datafield tag="200"><subfield code="a">Cafe\u0301</subfield></datafield></record>')
		original = copy.deepcopy((record, marc))
		with tempfile.TemporaryDirectory() as directory:
			archive = ArchiveStore(Path(directory))
			archive.begin_record('BY-NLB-test')
			response = requests.Response()
			response.status_code = 200
			response.url = 'https://elib.nlb.by/elib/Record/BY-NLB-test'
			response._content = body.encode('utf-8')
			archive.capture_response(response)
			archive.save_record('BY-NLB-test', record, marc)
			text = upload.build_commons_description(record, marc, 'BY-NLB-test', '123', category='Cafe\u0301')
			manifest = archive.manifests['BY-NLB-test']
			self.assertEqual((archive.root / manifest['sources'][0]['path']).read_bytes(), body.encode('utf-8'))
			parsed = json.loads((archive.root / manifest['parsed']['path']).read_text())
			self.assertEqual(parsed, {'html': original[0], 'marc': original[1]})
		self.assertEqual((record, marc), original)
		self.assertIn('Cafe\u0301', marc['raw_marc_fields'][0]['subfields'][0]['value'])
		self.assertIn('Café', text)
		self.assertIn('[[Category:Café]]', text)
		self.assertNotIn('Cafe\u0301', text)
		self.assertNotIn('\x01', text)
		self.assertNotIn('\x02', text)
		self.assertIn('&#91;&#91;literal&#93;&#93;', text)

	def test_trusted_links_remain_escaped_after_normalization(self) -> None:
		'''Preformatted link markup is normalized without being escaped a second time.'''
		link = upload.format_source_link('https://example.org/source', 'Cafe\u0301 [[literal]]')
		text = upload.format_metadata_field('Source', [link])
		self.assertIn('[https://example.org/source Café &#91;&#91;literal&#93;&#93;]', text)
		self.assertNotIn('&amp;#91;', text)


if __name__ == '__main__':
	unittest.main()
