#!/usr/bin/env python3
"""Offline integration coverage for NlbClient's catalog retry boundaries."""

import json
import unittest
from unittest import mock

import requests

from catalog_requests import CatalogRetriesExhausted
import upload as upload_nlb
from test_upload import MOCK_MARCXML_111, MOCK_METS_STRUCTURE_555, MOCK_RECORD_HTML_111, MOCK_SEARCH_HTML_PAGE_1


class TestNlbCatalogClient(unittest.TestCase):
	"""Exercise actual metadata parsing while replacing only the HTTP transport."""

	marc_id = 'BY-NLB-rr111'
	record_url = f'{upload_nlb.ELIB_BASE_URL}/elib/Record/{marc_id}'
	toc_html = '<ul class="toc"><li>Содержание: 1) Глава 1; 2) Глава 2</li></ul>'

	def setUp(self) -> None:
		"""Provide isolated request mocks and eliminate real retry delays and logging."""
		self.session = mock.Mock()
		self.session.headers = {}
		self.session.post.return_value = self.response(200, '')
		self.client = upload_nlb.NlbClient(session=self.session, timeout=17)
		self.sleep = mock.patch('catalog_requests.time.sleep').start()
		self.addCleanup(mock.patch.stopall)
		mock.patch('upload.log_progress').start()

	def response(self, status: int = 200, text: str = '', url: str | None = None) -> requests.Response:
		"""Return a real Requests response without opening sockets or reading cookies."""
		response = requests.Response()
		response.status_code = status
		response.url = url or self.record_url
		response.encoding = 'utf-8'
		response._content = text.encode('utf-8')
		response.close = mock.Mock()
		return response

	def metadata_responses(self) -> list[requests.Response]:
		"""Serve the record HTML, MARC export, and fallback contents tab in order."""
		return [
			self.response(text=MOCK_RECORD_HTML_111),
			self.response(text=MOCK_MARCXML_111, url=f'{self.record_url}/Export?style=MARCXML'),
			self.response(text=self.toc_html, url=f'{self.record_url}/TOC'),
		]

	def test_record_gateway_timeout_recovers_without_losing_metadata(self) -> None:
		"""Retry the reported record-page 504 before parsing the complete metadata."""
		failed = self.response(504)
		self.session.get.side_effect = [failed, *self.metadata_responses()]
		record, marc = self.client.fetch_record_metadata(self.marc_id)
		self.assertEqual(record['title'], 'Минские губернские ведомости : официальная часть')
		self.assertEqual(marc['control_number'], self.marc_id)
		self.assertEqual(marc['publishers'], ['Губернская типография'])
		self.assertEqual(record['contents'], ['1) Глава 1', '2) Глава 2'])
		self.assertEqual(self.session.get.call_args_list, [
			mock.call(self.record_url, timeout=17),
			mock.call(self.record_url, timeout=17),
			mock.call(f'{self.record_url}/Export?style=MARCXML', timeout=17),
			mock.call(f'{self.record_url}/TOC', timeout=17),
		])
		failed.close.assert_called_once_with()
		self.sleep.assert_called_once_with(5)
		self.session.post.assert_not_called()

	def test_marc_and_contents_exhaustion_propagate_without_partial_metadata(self) -> None:
		"""Exhausted MARCXML and TOC reads must defer the record instead of dropping fields."""
		for stage_index in (1, 2):
			with self.subTest(stage=('MARCXML', 'TOC')[stage_index - 1]):
				self.session.get.reset_mock()
				self.sleep.reset_mock()
				responses = self.metadata_responses()
				failed_url = responses[stage_index].url
				failures = [self.response(504, url=failed_url) for _ in range(5)]
				self.session.get.side_effect = [*responses[:stage_index], *failures]
				with self.assertRaises(CatalogRetriesExhausted) as raised:
					self.client.fetch_record_metadata(self.marc_id)
				self.assertEqual(raised.exception.attempts, 5)
				self.assertIs(raised.exception.last_error.response, failures[-1])
				self.assertEqual(self.session.get.call_count, stage_index + 5)
				self.assertEqual(self.session.get.call_args.args[0], failed_url)
				self.assertEqual(self.sleep.call_args_list, [mock.call(delay) for delay in (5, 10, 20, 40)])
				for failure in failures:
					failure.close.assert_called_once_with()

	def test_optional_missing_export_and_contents_are_allowed(self) -> None:
		"""An explicit HTTP 404 permits HTML-only metadata and an absent contents tab."""
		missing_export = self.response(404, url=f'{self.record_url}/Export?style=MARCXML')
		missing_contents = self.response(404, url=f'{self.record_url}/TOC')
		self.session.get.side_effect = [self.response(text=MOCK_RECORD_HTML_111), missing_export, missing_contents]
		record, marc = self.client.fetch_record_metadata(self.marc_id)
		self.assertEqual(record['title'], 'Минские губернские ведомости : официальная часть')
		self.assertFalse(marc['control_number'])
		self.assertFalse(record.get('contents'))
		self.sleep.assert_not_called()
		missing_export.close.assert_called_once_with()
		missing_contents.close.assert_called_once_with()

	def test_required_record_page_404_remains_fatal(self) -> None:
		"""A missing main record is not treated like an optional missing export."""
		missing_record = self.response(404)
		self.session.get.return_value = missing_record
		with self.assertRaises(requests.HTTPError) as raised:
			self.client.fetch_record_metadata(self.marc_id)
		self.assertIs(raised.exception.response, missing_record)
		self.session.get.assert_called_once_with(self.record_url, timeout=17)
		self.sleep.assert_not_called()

	def test_permanent_forbidden_and_ssl_failures_are_not_silenced(self) -> None:
		"""Optional export/TOC fallback must not swallow authorization or certificate errors."""
		for stage_index in range(3):
			for ssl_error in (False, True):
				with self.subTest(stage_index=stage_index, ssl_error=ssl_error):
					self.session.get.reset_mock()
					responses = self.metadata_responses()
					failure = requests.exceptions.SSLError('Certificate mismatch') if ssl_error else self.response(403)
					self.session.get.side_effect = [*responses[:stage_index], failure]
					with self.assertRaises(requests.exceptions.SSLError if ssl_error else requests.HTTPError):
						self.client.fetch_record_metadata(self.marc_id)
					self.assertEqual(self.session.get.call_count, stage_index + 1)
					self.sleep.assert_not_called()

	def test_search_access_and_mets_gets_retry_without_repeating_session_post(self) -> None:
		"""All catalog read endpoints use bounded retries; viewer initialization posts once."""
		stages = (
			('search', MOCK_SEARCH_HTML_PAGE_1),
			('access', json.dumps({'publicAccess': True})),
			('mets', json.dumps(MOCK_METS_STRUCTURE_555)),
		)
		for stage, payload in stages:
			with self.subTest(stage=stage):
				self.session.get.reset_mock()
				self.session.post.reset_mock()
				self.sleep.reset_mock()
				self.client._viewer_session_initialized = False
				failed = self.response(504)
				self.session.get.side_effect = [failed, self.response(text=payload)]
				if stage == 'search':
					result = self.client.fetch_search_page(page=1)
					self.assertEqual(result['total_results'], 2)
					self.assertEqual(result['records'][0]['marc_id'], self.marc_id)
					self.session.post.assert_not_called()
				elif stage == 'access':
					self.assertEqual(self.client.check_doc_access(self.marc_id), {'publicAccess': True})
					self.session.post.assert_called_once()
					self.assertEqual(self.session.get.call_args.kwargs['params'], {'marcID': self.marc_id})
				else:
					files = self.client.fetch_mets_files('555')
					self.assertEqual([entry['file_id'] for entry in files], ['2001', '2002'])
					self.session.post.assert_called_once()
				self.assertEqual(self.session.get.call_count, 2)
				self.assertEqual(self.session.get.call_args_list[0], self.session.get.call_args_list[1])
				self.assertEqual(self.session.get.call_args.kwargs['timeout'], 17)
				failed.close.assert_called_once_with()
				self.sleep.assert_called_once_with(5)

	def test_viewer_session_post_failure_is_not_replayed_by_get_retry_policy(self) -> None:
		"""The catalog helper must not unexpectedly retry a failed session-creation POST."""
		failure = self.response(504)
		self.session.post.return_value = failure
		with self.assertRaises(requests.HTTPError) as raised:
			self.client.check_doc_access(self.marc_id)
		self.assertIs(raised.exception.response, failure)
		self.session.post.assert_called_once()
		self.session.get.assert_not_called()
		self.sleep.assert_not_called()


if __name__ == '__main__':
	unittest.main()
