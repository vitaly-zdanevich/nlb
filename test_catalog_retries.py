#!/usr/bin/env python3
"""Offline regressions for transient failures while reading NLB catalog pages."""

import unittest
from unittest import mock

import requests

import catalog_requests


class TestCatalogRetries(unittest.TestCase):
	"""Retry only transient reads, with bounded attempts and actionable diagnostics."""

	url = 'https://elib.nlb.by/elib/Record/BY-NLB-br0000172751'

	def response(self, status: int = 200) -> requests.Response:
		"""Build an offline response with Requests' real HTTP error classification."""
		response = requests.Response()
		response.status_code = status
		response.url = self.url
		response._content = b'Catalog response'
		response.close = mock.Mock()
		return response

	def test_success_forwards_request_options_without_sleeping(self) -> None:
		"""Keep request parameters and successful response ownership with the caller."""
		response = self.response()
		session = mock.Mock()
		session.get.return_value = response
		with mock.patch('catalog_requests.time.sleep') as sleep:
			actual = catalog_requests.get_catalog_response(
				session, self.url, timeout=60,
				params={'style': 'MARCXML'}, headers={'Accept': 'application/xml'},
			)
		self.assertIs(actual, response)
		session.get.assert_called_once_with(
			self.url, timeout=60,
			params={'style': 'MARCXML'}, headers={'Accept': 'application/xml'},
		)
		response.close.assert_not_called()
		sleep.assert_not_called()

	def test_gateway_timeout_retries_and_closes_failed_response(self) -> None:
		"""The reported HTTP 504 succeeds on retry instead of terminating the batch."""
		failed = self.response(504)
		succeeded = self.response()
		session = mock.Mock()
		session.get.side_effect = [failed, succeeded]
		log = mock.Mock()
		with mock.patch('catalog_requests.time.sleep') as sleep:
			actual = catalog_requests.get_catalog_response(session, self.url, timeout=60, log=log)
		self.assertIs(actual, succeeded)
		self.assertEqual(session.get.call_count, 2)
		failed.close.assert_called_once_with()
		succeeded.close.assert_not_called()
		sleep.assert_called_once_with(5)
		self.assertIn('1/5', log.call_args.args[0])
		self.assertIn('504', log.call_args.args[0])
		self.assertIn('5 seconds', log.call_args.args[0])

	def test_transient_http_statuses_share_one_backoff_budget(self) -> None:
		"""HTTP 500, 502, 503, and 504 retry with the configured bounded backoff."""
		responses = [self.response(status) for status in (500, 502, 503, 504, 200)]
		session = mock.Mock()
		session.get.side_effect = responses
		with mock.patch('catalog_requests.time.sleep') as sleep:
			actual = catalog_requests.get_catalog_response(session, self.url, timeout=60)
		self.assertIs(actual, responses[-1])
		self.assertEqual(session.get.call_count, 5)
		self.assertEqual(sleep.call_args_list, [mock.call(delay) for delay in (5, 10, 20, 40)])
		for response in responses[:-1]:
			response.close.assert_called_once_with()

	def test_persistent_gateway_timeout_exposes_original_failure(self) -> None:
		"""Exhaustion exposes the last HTTP error and attempt count for resume state."""
		responses = [self.response(504) for _ in range(5)]
		session = mock.Mock()
		session.get.side_effect = responses
		log = mock.Mock()
		with mock.patch('catalog_requests.time.sleep') as sleep:
			with self.assertRaises(catalog_requests.CatalogRetriesExhausted) as raised:
				catalog_requests.get_catalog_response(session, self.url, timeout=60, log=log)
		error = raised.exception
		self.assertEqual(error.attempts, 5)
		self.assertIsInstance(error.last_error, requests.HTTPError)
		self.assertIs(error.last_error.response, responses[-1])
		self.assertIs(error.__cause__, error.last_error)
		self.assertIn('504', str(error))
		self.assertIn('5 attempts', str(error))
		self.assertEqual(session.get.call_count, 5)
		self.assertEqual(sleep.call_args_list, [mock.call(delay) for delay in (5, 10, 20, 40)])
		self.assertEqual(log.call_count, 4)
		for response in responses:
			response.close.assert_called_once_with()

	def test_timeout_and_connection_failures_can_recover(self) -> None:
		"""Connection and read timeouts receive the same retries as HTTP failures."""
		for error_type in (requests.Timeout, requests.ConnectTimeout, requests.ReadTimeout, requests.ConnectionError):
			with self.subTest(error_type=error_type.__name__):
				session = mock.Mock()
				response = self.response()
				session.get.side_effect = [error_type('Temporary failure'), response]
				with mock.patch('catalog_requests.time.sleep') as sleep:
					actual = catalog_requests.get_catalog_response(session, self.url, timeout=60)
				self.assertIs(actual, response)
				sleep.assert_called_once_with(5)

	def test_network_exhaustion_has_the_same_total_attempt_limit(self) -> None:
		"""Repeated network timeouts stop after five attempts without a final sleep."""
		session = mock.Mock()
		last_error = requests.ReadTimeout('The read operation timed out')
		session.get.side_effect = last_error
		with mock.patch('catalog_requests.time.sleep') as sleep:
			with self.assertRaises(catalog_requests.CatalogRetriesExhausted) as raised:
				catalog_requests.get_catalog_response(session, self.url, timeout=60)
		self.assertEqual(raised.exception.attempts, 5)
		self.assertIs(raised.exception.last_error, last_error)
		self.assertEqual(session.get.call_count, 5)
		self.assertEqual(sleep.call_count, 4)

	def test_permanent_http_errors_are_not_retried(self) -> None:
		"""Authentication, rejection, missing records, and other statuses remain fatal."""
		for status in (400, 401, 403, 404, 429, 501, 505):
			with self.subTest(status=status):
				response = self.response(status)
				session = mock.Mock()
				session.get.return_value = response
				with mock.patch('catalog_requests.time.sleep') as sleep:
					with self.assertRaises(requests.HTTPError) as raised:
						catalog_requests.get_catalog_response(session, self.url, timeout=60)
				self.assertIs(raised.exception.response, response)
				response.close.assert_called_once_with()
				session.get.assert_called_once()
				sleep.assert_not_called()

	def test_ssl_programming_and_interruption_errors_are_not_retried(self) -> None:
		"""Retrying must not conceal certificate failures, code defects, or Ctrl-C."""
		for error in (requests.exceptions.SSLError('Certificate mismatch'), ValueError('Invalid input'), KeyboardInterrupt()):
			with self.subTest(error_type=type(error).__name__):
				session = mock.Mock()
				session.get.side_effect = error
				with mock.patch('catalog_requests.time.sleep') as sleep:
					with self.assertRaises(type(error)) as raised:
						catalog_requests.get_catalog_response(session, self.url, timeout=60)
				self.assertIs(raised.exception, error)
				session.get.assert_called_once()
				sleep.assert_not_called()

	def test_http_error_raised_by_session_also_closes_response(self) -> None:
		"""Requests response hooks may raise before returning a response to the helper."""
		failed = self.response(504)
		session = mock.Mock()
		succeeded = self.response()
		session.get.side_effect = [requests.HTTPError('Gateway timeout', response=failed), succeeded]
		with mock.patch('catalog_requests.time.sleep'):
			actual = catalog_requests.get_catalog_response(session, self.url, timeout=60)
		self.assertIs(actual, succeeded)
		failed.close.assert_called_once_with()


if __name__ == '__main__':
	unittest.main()
