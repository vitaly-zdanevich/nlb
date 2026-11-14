#!/usr/bin/env python3
'''Reject successful HTTP responses that would silently lose catalog metadata.'''

import copy
import json
import unittest
from unittest import mock

import requests

from catalog_requests import CatalogRetriesExhausted, InvalidCatalogResponse
from catalog_validation import validate_manifest
import upload as upload_nlb
from test_upload import MOCK_MARCXML_111, MOCK_METS_STRUCTURE_555, MOCK_RECORD_HTML_111, MOCK_SEARCH_HTML_PAGE_1


class TestCatalogValidation(unittest.TestCase):
	'''Exercise body validation through the real client and bounded GET policy.'''

	marc_id = 'BY-NLB-rr111'
	fatal_html = '<br><b>Fatal error</b>: Uncaught PDOException: too many clients already'
	toc_html = '<div class="tab-pane toc-tab"><ul><li>Дополнительное содержание</li></ul></div>'

	def setUp(self) -> None:
		'''Mock transport and retry delays, keeping schema checks and parsers real.'''
		self.session = mock.Mock()
		self.session.headers = {}
		self.client = upload_nlb.NlbClient(session=self.session)
		self.client._viewer_session_initialized = True
		self.sleep = mock.patch('catalog_requests.time.sleep').start()
		mock.patch('upload.log_progress').start()
		self.addCleanup(mock.patch.stopall)

	def response(self, body: str, status: int = 200) -> requests.Response:
		'''Build a consumed response with observable cleanup and original source bytes.'''
		response = requests.Response()
		response.status_code = status
		response.url = f'https://elib.nlb.by/elib/Record/{self.marc_id}'
		response.encoding = 'utf-8'
		response._content = body.encode('utf-8')
		response.close = mock.Mock()
		return response

	def test_record_error_page_recovers_instead_of_discarding_metadata(self) -> None:
		'''The observed PHP error with HTTP 200 must retry before parsing record data.'''
		failed = self.response(self.fatal_html)
		self.session.get.side_effect = [
			failed, self.response(MOCK_RECORD_HTML_111), self.response(MOCK_MARCXML_111), self.response(self.toc_html),
		]
		record, marc = self.client.fetch_record_metadata(self.marc_id)
		self.assertEqual(record['title'], 'Минские губернские ведомости : официальная часть')
		self.assertEqual(marc['control_number'], self.marc_id)
		self.assertEqual(record['contents'], ['Дополнительное содержание'])
		self.sleep.assert_called_once_with(5)
		failed.close.assert_called_once_with()

	def test_empty_invalid_or_wrong_record_responses_exhaust_as_pending(self) -> None:
		'''HTML identity, XML structure/identity, and contents must survive successful status.'''
		cases = (
			('record', self.fatal_html),
			('record', '<html><h1>Service unavailable</h1></html>'),
			('record', MOCK_RECORD_HTML_111.replace('BY-NLB-rr111', 'BY-NLB-other')),
			('marc', ''),
			('marc', '<broken'),
			('marc', '<collection xmlns="http://www.loc.gov/MARC21/slim"/>'),
			('marc', MOCK_MARCXML_111.replace('BY-NLB-rr111', 'BY-NLB-other')),
			('toc', self.fatal_html),
			('toc', '<html><h1>Sign in</h1></html>'),
		)
		for stage, body in cases:
			with self.subTest(stage=stage, body=body[:70]):
				self.session.get.reset_mock()
				self.sleep.reset_mock()
				prefix = [] if stage == 'record' else [self.response(MOCK_RECORD_HTML_111)]
				if stage == 'toc':
					prefix.append(self.response(MOCK_MARCXML_111))
				failures = [self.response(body) for _ in range(5)]
				self.session.get.side_effect = [*prefix, *failures]
				with self.assertRaises(CatalogRetriesExhausted) as raised:
					self.client.fetch_record_metadata(self.marc_id)
				self.assertEqual(raised.exception.attempts, 5)
				self.assertIs(raised.exception.last_error.response, failures[-1])
				self.assertEqual(self.sleep.call_args_list, [mock.call(delay) for delay in (5, 10, 20, 40)])
				for response in failures:
					response.close.assert_called_once_with()

	def test_search_error_or_partial_results_do_not_end_catalog(self) -> None:
		'''Reject server errors and result entries without identifiers instead of skipping pages.'''
		for body in (self.fatal_html, '<html></html>', MOCK_SEARCH_HTML_PAGE_1.replace('class="hiddenId"', '').replace('/elib/Record/', '/broken/')):
			with self.subTest(body=body[:70]):
				self.session.get.side_effect = [self.response(body) for _ in range(5)]
				with self.assertRaises(CatalogRetriesExhausted):
					self.client.fetch_search_page(1)

	def test_explicit_empty_search_and_contents_are_valid(self) -> None:
		'''An expected empty-results marker or empty TOC container is not a server error.'''
		self.session.get.return_value = self.response('<div class="search-stats">из <strong>0</strong></div>')
		self.assertEqual(self.client.fetch_search_page(1), {'total_results': 0, 'records': []})
		self.session.get.return_value = self.response('<div class="search-controls"><h3>Нет результатов!</h3></div>')
		self.assertEqual(self.client.fetch_search_page(1), {'total_results': 0, 'records': []})
		self.session.get.side_effect = [
			self.response(MOCK_RECORD_HTML_111), self.response(MOCK_MARCXML_111),
			self.response('<div class="tab-pane toc-tab"></div>'),
		]
		self.client.fetch_record_metadata(self.marc_id)
		self.sleep.assert_not_called()

	def test_toc_is_captured_even_when_marc_contains_contents(self) -> None:
		'''A MARC 327 entry does not justify discarding the separately published contents tab.'''
		marc = MOCK_MARCXML_111.replace('</record>', '<datafield tag="327"><subfield code="a">MARC chapter</subfield></datafield></record>')
		self.session.get.side_effect = [self.response(MOCK_RECORD_HTML_111), self.response(marc), self.response(self.toc_html)]
		record, metadata = self.client.fetch_record_metadata(self.marc_id)
		self.assertEqual(metadata['contents'], ['MARC chapter'])
		self.assertEqual(record['contents'], ['Дополнительное содержание'])
		self.assertEqual(self.session.get.call_count, 3)

	def test_toc_retains_paragraphs_alongside_list_entries(self) -> None:
		'''Preserve non-list contents text without exposing scripts or style declarations.'''
		self.session.get.side_effect = [
			self.response(MOCK_RECORD_HTML_111), self.response(MOCK_MARCXML_111),
			self.response('<div class="tab-pane toc-tab"><p>Preface</p><ul><li>Chapter</li></ul>Appendix<script>secret()</script><style>.hidden{}</style></div>'),
		]
		record, _ = self.client.fetch_record_metadata(self.marc_id)
		self.assertEqual(record['contents'], ['Preface', 'Chapter', 'Appendix'])

	def test_access_requires_real_boolean(self) -> None:
		'''False-like strings, numbers, null, and error objects cannot grant or deny access.'''
		for payload in ({}, {'publicAccess': 'false'}, {'publicAccess': 0}, {'publicAccess': None}, [], '<html>Error</html>'):
			with self.subTest(payload=payload):
				body = payload if isinstance(payload, str) else json.dumps(payload)
				self.session.get.side_effect = [self.response(body) for _ in range(5)]
				with self.assertRaises(CatalogRetriesExhausted):
					self.client.check_doc_access(self.marc_id)
		for allowed in (False, True):
			self.session.get.side_effect = None
			self.session.get.return_value = self.response(json.dumps({'publicAccess': allowed}))
			self.assertIs(self.client.check_doc_access(self.marc_id)['publicAccess'], allowed)

	def test_manifest_validates_supported_iiif_shapes_and_error_responses(self) -> None:
		'''Rendering-only, Presentation 2 and Presentation 3 manifests retain optional metadata.'''
		valid = (
			{'rendering': []}, {'rendering': [{'id': '/iiif/file/1001/bytes'}]},
			{'rendering': [{'@id': '/iiif/file/1001/bytes'}]},
			{'rendering': {'@id': '/iiif/file/1001/bytes'}},
			{'items': [{'id': 'canvas1', 'type': 'Canvas'}]},
			{'sequences': [{'canvases': [{'@id': 'canvas1'}]}]},
			{'sequences': [{'canvases': [{'@id': 'canvas1'}]}, {'@id': 'alternate-sequence'}]},
		)
		for payload in valid:
			with self.subTest(valid=payload):
				validate_manifest(self.response(json.dumps(payload)))
		invalid = ({}, {'error': 'Expired', 'rendering': []}, {'rendering': {}}, {'rendering': [None]},
			{'rendering': [{}]}, {'items': [None]}, {'sequences': [{}]})
		for payload in invalid:
			with self.subTest(invalid=payload):
				with self.assertRaises(InvalidCatalogResponse):
					validate_manifest(self.response(json.dumps(payload)))

	def test_mets_schema_cannot_silently_drop_files(self) -> None:
		'''Malformed trees, missing file IDs, and wrong boolean types remain pending.'''
		missing_id = copy.deepcopy(MOCK_METS_STRUCTURE_555)
		del missing_id['nodes'][0]['children'][0]['data']['file']['id']
		bad_visible = copy.deepcopy(MOCK_METS_STRUCTURE_555)
		bad_visible['nodes'][0]['data']['visible'] = 'false'
		for payload in ({}, [], {'nodes': {}}, {'nodes': [None]}, missing_id, bad_visible):
			with self.subTest(payload=payload):
				self.session.get.side_effect = [self.response(json.dumps(payload)) for _ in range(5)]
				with self.assertRaises(CatalogRetriesExhausted):
					self.client.fetch_mets_files('555')

	def test_file_resolution_fetches_each_mets_once_and_preserves_part_label(self) -> None:
		'''A direct viewer link must not hide the richer label in its matching METS entry.'''
		self.client.fetch_mets_files = mock.Mock(return_value=[{'file_id': '2001', 'part_label': 'Том 1 / Часть 1'}])
		files = self.client.resolve_record_files(
			{'file_ids': ['2001'], 'mets_ids': ['555']}, {'file_ids': ['2001'], 'mets_ids': ['555']},
		)
		self.assertEqual(files, [{'file_id': '2001', 'part_label': 'Том 1 / Часть 1'}])
		self.client.fetch_mets_files.assert_called_once_with('555', marc_id=None)

	def test_record_scope_excludes_suggestions_and_keeps_holdings(self) -> None:
		'''Only the requested record supplies scans, authors, title and copy metadata.'''
		main = f'''<input class="hiddenId" value="{self.marc_id}"><h1>Current book</h1>
<table><tr><th>Опубликовано:</th><td>1890</td></tr></table>
<a href="https://elib.nlb.by:8070/viewer?markID={self.marc_id}&amp;fileID=1001">Scan</a>
<div class="record-tabs"><div class="tab-content"><div class="holdings-tab">
<table><tr><th>Шифр</th><th>Место нахождения</th></tr><tr><td>Rare 123</td><td>Vault A</td></tr></table>
</div></div></div>'''
		sidebar = '''<aside class="sidebar"><input class="hiddenId" value="BY-NLB-other">
<h1>Suggested book</h1><table><tr><th>Автор:</th><td>Unrelated author</td></tr></table>
<a href="https://elib.nlb.by:8070/viewer?markID=BY-NLB-other&amp;fileID=2002">Other scan</a></aside>'''
		for body in (
			f'<div class="record"><div class="mainbody">{main}</div>{sidebar}</div>',
			f'<div class="record">{sidebar}<div class="mainbody">{main}{sidebar}</div></div>',
			f'<aside><div class="record"><div class="mainbody">{sidebar}</div></div></aside><div class="record"><div class="mainbody">{main}</div></div>',
			f'<div class="record">{main}{sidebar}</div>', f'{main}{sidebar}',
		):
			with self.subTest(body=body[:80]):
				response = self.response(body)
				self.session.get.side_effect = [response, self.response('', 404), self.response('', 404)]
				archive = self.client.archive = mock.Mock()
				record, marc = self.client.fetch_record_metadata(self.marc_id)
				files = self.client.resolve_record_files({}, record)
				self.assertEqual([item['file_id'] for item in files], ['1001'])
				self.assertEqual(record['title'], 'Current book')
				self.assertEqual(record['viewer_links'][0]['mark_id'], self.marc_id)
				text = upload_nlb.build_commons_description(record, marc, self.marc_id, '1001')
				self.assertNotIn('Unrelated author', text)
				self.assertNotIn('Suggested book', text)
				self.assertIn('Rare 123', text)
				self.assertIn('Vault A', text)
				# Scope applies to derived metadata, never to the preserved source bytes.
				self.assertIn('Unrelated author', response.text)
				archive.capture_response.assert_any_call(response)

	def test_explicit_viewer_conflicts_retry_even_with_correct_hidden_record_id(self) -> None:
		'''A valid record marker cannot excuse a different or ambiguous scan identity.'''
		queries = (
			'markID=BY-NLB-other&fileID=1001',
			f'markID={self.marc_id}&marcID=BY-NLB-other&fileID=1001',
			f'markID={self.marc_id}&markID=BY-NLB-other&fileID=1001',
			f'markID={self.marc_id}&fileID=1001&fileId=2002',
			f'markID={self.marc_id}&metsID=555&metsId=666',
		)
		for query in queries:
			with self.subTest(query=query):
				body = f'''<div class="record"><div class="mainbody"><input class="hiddenId" value="{self.marc_id}">
<h1>Current book</h1><table><tr><th>Date</th><td>1890</td></tr></table>
<a href="https://elib.nlb.by:8070/viewer?{query}">Scan</a></div></div>'''
				self.session.get.reset_mock()
				self.session.get.side_effect = [self.response(body) for _ in range(5)]
				with self.assertRaises(CatalogRetriesExhausted):
					self.client.fetch_record_metadata(self.marc_id)
				self.assertEqual(self.session.get.call_count, 5)
				with self.assertRaises(InvalidCatalogResponse):
					upload_nlb.parse_record_html(body, self.marc_id)

	def test_search_scope_and_shared_viewer_provenance(self) -> None:
		'''A shared METS identifier stays valid while another record's sidebar is ignored.'''
		body = MOCK_SEARCH_HTML_PAGE_1.replace('<div class="result-formats">', '''<aside>
<input class="hiddenId" value="BY-NLB-other"><a class="title" href="/elib/Record/BY-NLB-other">Other</a>
<a href="https://elib.nlb.by:8070/viewer?markID=BY-NLB-other&amp;fileID=9999">Other scan</a>
</aside><div class="result-formats">''')
		self.session.get.return_value = self.response(body)
		records = self.client.fetch_search_page(1)['records']
		self.assertEqual(records[0]['file_ids'], ['1001'])
		self.assertEqual(records[1]['mets_ids'], ['555'])
		self.assertEqual(records[1]['viewer_links'][0]['mark_id'], 'BY-NLB-rr222')
		self.assertEqual(records[1]['viewer_links'][0]['mets_id'], '555')
		self.assertNotIn('Other', records[0]['title'])
		conflicting = MOCK_SEARCH_HTML_PAGE_1.replace('markID=BY-NLB-rr111', 'markID=BY-NLB-other')
		self.session.get.side_effect = [self.response(conflicting) for _ in range(5)]
		with self.assertRaises(CatalogRetriesExhausted):
			self.client.fetch_search_page(1)

	def test_archive_observes_invalid_and_missing_responses_before_validation(self) -> None:
		'''Capture error evidence and optional 404s as well as successful source bytes.'''
		archive = mock.Mock()
		self.client.archive = archive
		responses = [self.response(self.fatal_html), self.response(MOCK_RECORD_HTML_111), self.response('', 404), self.response('', 404)]
		self.session.get.side_effect = responses
		self.client.fetch_record_metadata(self.marc_id)
		self.assertEqual(archive.capture_response.call_args_list, [mock.call(response) for response in responses])

	def test_invalid_body_and_transport_failures_share_one_attempt_budget(self) -> None:
		'''Retries never multiply when a failing endpoint alternates HTTP, body, and network errors.'''
		failure = self.response(self.fatal_html)
		self.session.get.side_effect = [
			requests.Timeout('Slow server'), self.response('', 503), failure,
			requests.exceptions.ChunkedEncodingError('Interrupted response'), failure,
		]
		with self.assertRaises(CatalogRetriesExhausted) as raised:
			self.client.fetch_search_page(1)
		self.assertIsInstance(raised.exception.last_error, InvalidCatalogResponse)
		self.assertEqual(self.session.get.call_count, 5)
		self.assertEqual(self.sleep.call_args_list, [mock.call(delay) for delay in (5, 10, 20, 40)])

	def test_archive_failure_does_not_allow_unpreserved_metadata(self) -> None:
		'''Disk failures abort before metadata is used and are not retried as HTTP failures.'''
		self.client.archive = mock.Mock()
		self.client.archive.capture_response.side_effect = OSError('Archive disk full')
		self.session.get.return_value = self.response(MOCK_RECORD_HTML_111)
		with self.assertRaisesRegex(OSError, 'Archive disk full'):
			self.client.fetch_record_metadata(self.marc_id)
		self.session.get.assert_called_once()
		self.sleep.assert_not_called()


if __name__ == '__main__':
	unittest.main()
