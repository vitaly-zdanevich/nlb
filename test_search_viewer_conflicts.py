'''Keep conflicting search scan links from blocking independent catalog records.'''

from pathlib import Path
import io
import json
import tempfile
import unittest
from unittest import mock
from urllib.parse import urlsplit

from catalog_requests import InvalidCatalogResponse
from catalog_validation import validate_search_html
from preservation import ArchiveStore
from test_refresh_uploaded_metadata import association
from test_shared_viewers import response
from test_upload import (
	MOCK_MARCXML_111, MOCK_METS_STRUCTURE_555, MOCK_RECORD_HTML_111,
	MOCK_RECORD_HTML_222, MOCK_SEARCH_HTML_PAGE_1,
)
import upload


class TestSearchViewerConflicts(unittest.TestCase):
	'''Use archived search hints and real record validation with offline transport.'''

	def setUp(self):
		'''Silence progress and retry sleeps without bypassing the catalog validators.'''
		self.addCleanup(mock.patch.stopall)
		self.output = io.StringIO()
		mock.patch('sys.stdout', self.output).start()
		mock.patch('catalog_requests.time.sleep').start()

	def test_observed_foreign_direct_links_keep_catalog_entries_but_no_scan_hints(self):
		'''The two page-three conflicts retain their own IDs and unaffected neighbors.'''
		body = (Path(__file__).parent / 'fixtures/search_foreign_direct_viewers.html').read_text()
		validate_search_html(response(body, 'https://elib.nlb.by/elib/Search/Results?page=3'))
		parsed = upload.parse_search_results_html(body)
		self.assertEqual(len(parsed['records']), 3)
		bad = [entry for entry in parsed['records'] if entry.get('viewer_error')]
		self.assertEqual([entry['marc_id'] for entry in bad], ['BY-NLB-br0000092366', 'BY-NLB-br0000092371'])
		for entry in bad:
			self.assertEqual((entry['file_ids'], entry['mets_ids'], entry['viewer_links']), ([], [], []))
			self.assertIn(entry['marc_id'], self.output.getvalue())
		self.assertTrue(parsed['records'][0]['mets_ids'])
		self.assertNotIn('viewer_error', parsed['records'][0])

	def test_any_conflict_discards_all_hints_for_that_entry(self):
		'''A plausible own-record link cannot launder another contradictory scan hint.'''
		for query in ('markID=BY-NLB-other&fileID=9999',
			'markID=BY-NLB-rr111&fileID=9999&fileId=8888',
			'markID=BY-NLB-rr111&metsID=777&metsId=888'):
			with self.subTest(query=query):
				body = MOCK_SEARCH_HTML_PAGE_1.replace('class="result-formats">',
					f'class="result-formats"><a href="https://elib.nlb.by:8070/viewer?{query}">Conflicting scan</a>', 1)
				validate_search_html(response(body, 'https://elib.nlb.by/elib/Search/Results'))
				entries = upload.parse_search_results_html(body)['records']
				self.assertEqual((entries[0]['file_ids'], entries[0]['mets_ids'], entries[0]['viewer_links']), ([], [], []))
				self.assertIn('viewer_error', entries[0])
				self.assertEqual(entries[1]['mets_ids'], ['555'])
				self.assertNotIn('viewer_error', entries[1])

	def test_missing_or_conflicting_independent_record_ids_still_reject_page(self):
		'''Only a consistent hidden catalog ID allows a viewer conflict to be isolated.'''
		body = MOCK_SEARCH_HTML_PAGE_1.replace('markID=BY-NLB-rr111', 'markID=BY-NLB-other')
		for invalid in (
			body.replace('<input type="hidden" value="BY-NLB-rr111" class="hiddenId" />', ''),
			body.replace('<div id="result0" class="result">', '<div id="result0" class="result"><input class="hiddenId" value="BY-NLB-wrong">'),
		):
			with self.subTest(body=invalid[:100]), self.assertRaises(InvalidCatalogResponse):
				validate_search_html(response(invalid, 'https://elib.nlb.by/elib/Search/Results'))
			with self.assertRaises(InvalidCatalogResponse):
				upload.parse_search_results_html(invalid)

	def test_primary_record_decides_scope_and_other_records_continue(self):
		'''A corrected primary supplies its own file; a persistent conflict defers only its record.'''
		search = MOCK_SEARCH_HTML_PAGE_1.replace('markID=BY-NLB-rr111', 'markID=BY-NLB-other').replace('fileID=1001', 'fileID=9999')
		for primary_corrected in (True, False):
			with self.subTest(primary_corrected=primary_corrected), tempfile.TemporaryDirectory() as directory:
				root = Path(directory)
				archive = ArchiveStore(root / 'archive')
				state_path = root / 'state.json'
				state = upload.default_state()
				state['items'] = {key: association(verification={'sha1': 'a' * 40}) for key in (
					'BY-NLB-rr111:1001', 'BY-NLB-rr222:2001', 'BY-NLB-rr222:2002')}
				previous = state['items']['BY-NLB-rr111:1001'].copy()
				upload.save_state(state_path, state)
				session = mock.Mock(headers={})

				def get(url, **options):
					'''Serve catalog and viewer metadata, rejecting unanticipated requests.'''
					path = urlsplit(url).path
					status = 200
					if path == '/elib/Search/Results':
						body = search
					elif path == '/elib/Record/BY-NLB-rr111':
						body = MOCK_RECORD_HTML_111 if primary_corrected else MOCK_RECORD_HTML_111.replace('markID=BY-NLB-rr111', 'markID=BY-NLB-other')
					elif path == '/elib/Record/BY-NLB-rr222':
						body = MOCK_RECORD_HTML_222
					elif path == '/elib/Record/BY-NLB-rr111/Export':
						body = MOCK_MARCXML_111
					elif path.endswith(('/Export', '/TOC')):
						body, status = '', 404
					elif path == '/iiif/access':
						self.assertIn(options['params']['marcID'], ('BY-NLB-rr111', 'BY-NLB-rr222'))
						body = {'publicAccess': True}
					elif path == '/mets//structure/state':
						body = MOCK_METS_STRUCTURE_555
					elif path == '/iiif/manifest':
						self.assertNotEqual(options['params']['fileId'], '9999')
						body = {'rendering': []}
					else:
						raise AssertionError('Unexpected request: ' + url)
					return response(body, url, status)

				session.get.side_effect = get
				client = upload.NlbClient(session=session, archive=archive, metadata_reads_only=True)
				client.download_pdf_scan = mock.Mock(side_effect=AssertionError('Unexpected PDF download'))
				uploader = mock.Mock()
				counts = upload.iterate_and_upload(client, uploader, state_path, root / 'staging')
				self.assertEqual(counts['search_failed'], 0)
				self.assertEqual(counts['metadata_failed'], 0 if primary_corrected else 1)
				self.assertEqual(counts['completed'], 3 if primary_corrected else 2)
				self.assertEqual(counts['uploaded'], 0)
				client.download_pdf_scan.assert_not_called()
				self.assertEqual(uploader.mock_calls, [])
				session.post.assert_not_called()
				saved = json.loads(state_path.read_text())
				self.assertEqual(set(saved['items']), set(state['items']))
				if primary_corrected:
					self.assertNotIn('BY-NLB-rr111', saved.get('record_failures', {}))
				else:
					self.assertEqual(saved['record_failures']['BY-NLB-rr111']['attempts'], 5)
					self.assertEqual(saved['items']['BY-NLB-rr111:1001'], previous)
				captured = [json.loads(path.read_text()) for path in (archive.root / 'searches').glob('*.json')]
				self.assertEqual(len(captured), 1)
				self.assertEqual((archive.root / captured[0]['path']).read_text(), search)
				self.assertNotIn('9999', archive.manifests['BY-NLB-rr111']['files'])
