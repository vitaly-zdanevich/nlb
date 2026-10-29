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
		self.client.fetch_mets_files.assert_called_once_with('555')

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
