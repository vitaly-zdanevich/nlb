'''Offline coverage for bounded viewer outages and per-operation recovery.'''

import unittest

import requests

from viewer_api import ViewerAPIHealth, ViewerAPIUnavailable


def http_error(status: int) -> requests.HTTPError:
	'''Build a real failed response containing text that must stay out of diagnostics.'''
	response = requests.Response()
	response.status_code = status
	response.url = 'https://example.invalid/viewer?cookie=private-value'
	response._content = b'private-response-body'
	return requests.HTTPError('private-error-message', response=response)


class TestViewerAPIHealth(unittest.TestCase):
	'''Exercise outage decisions without HTTP requests, retries or sleeps.'''

	def test_distinct_record_threshold_stops_with_safe_diagnostic(self) -> None:
		'''Three unavailable records stop the run while preserving the original cause.'''
		health = ViewerAPIHealth()
		error = http_error(404)
		health.failed('access', 'record-one', error)
		health.failed('access', 'record-two', error)
		with self.assertRaises(ViewerAPIUnavailable) as raised:
			health.failed('access', 'record-three', error)
		self.assertEqual(raised.exception.operation, 'access')
		self.assertEqual(raised.exception.record_count, 3)
		self.assertIs(raised.exception.last_error, error)
		self.assertIs(raised.exception.__cause__, error)
		message = str(raised.exception)
		for text in ('API unavailable', 'access', 'HTTP 404', '--continue', 'Commons edits'):
			self.assertIn(text, message)
		for secret in ('private-value', 'private-response-body', 'private-error-message', 'example.invalid'):
			self.assertNotIn(secret, message)

	def test_isolated_missing_record_then_success_starts_a_new_streak(self) -> None:
		'''A valid response separates independent source failures into different streaks.'''
		health = ViewerAPIHealth()
		error = http_error(404)
		health.failed('access', 'record-one', error)
		health.succeeded('access')
		health.failed('access', 'record-two', error)
		health.failed('access', 'record-three', error)
		with self.assertRaises(ViewerAPIUnavailable):
			health.failed('access', 'record-four', error)

	def test_repeated_attempts_for_one_record_count_only_once(self) -> None:
		'''Several failed scans from one record cannot by themselves prove an outage.'''
		health = ViewerAPIHealth()
		error = http_error(410)
		for _ in range(6):
			health.failed('manifest', 'record-one', error)
		health.failed('manifest', 'record-two', error)
		health.failed('manifest', 'record-one', error)
		with self.assertRaises(ViewerAPIUnavailable):
			health.failed('manifest', 'record-three', error)

	def test_operations_keep_independent_streaks(self) -> None:
		'''Access success and unrelated failures cannot hide repeated manifest failures.'''
		health = ViewerAPIHealth()
		error = http_error(404)
		health.failed('manifest', 'record-one', error)
		health.failed('access', 'record-two', error)
		health.succeeded('access')
		health.failed('manifest', 'record-two', error)
		health.failed('access', 'record-three', http_error(403))
		with self.assertRaises(ViewerAPIUnavailable) as raised:
			health.failed('manifest', 'record-three', error)
		self.assertEqual(raised.exception.operation, 'manifest')

	def test_only_unavailable_http_statuses_count(self) -> None:
		'''Permanent authorization and other HTTP errors retain their own handling.'''
		for status in (404, 410, 500, 502, 503, 504):
			with self.subTest(status=status), self.assertRaises(ViewerAPIUnavailable):
				ViewerAPIHealth(threshold=1).failed('access', 'record-one', http_error(status))
		for status in (400, 401, 403, 405, 408, 429, 501, 505):
			with self.subTest(status=status):
				ViewerAPIHealth(threshold=1).failed('access', 'record-one', http_error(status))

	def test_network_errors_count_except_certificate_failures(self) -> None:
		'''Timeouts and connection outages count without incorporating error messages.'''
		for error in (requests.Timeout('private-timeout'), requests.ConnectionError('private-connection')):
			with self.subTest(error=type(error).__name__), self.assertRaises(ViewerAPIUnavailable) as raised:
				ViewerAPIHealth(threshold=1).failed('access', 'record-one', error)
			self.assertNotIn(str(error), str(raised.exception))
			self.assertIn(type(error).__name__, str(raised.exception))
		ViewerAPIHealth(threshold=1).failed('access', 'record-one', requests.exceptions.SSLError('certificate failure'))

	def test_unclassified_failure_clears_only_its_operation(self) -> None:
		'''An unrelated failure breaks old evidence rather than making a later outage appear larger.'''
		for error in (
			http_error(403), requests.HTTPError('No response'), requests.exceptions.SSLError('certificate'),
			requests.exceptions.ChunkedEncodingError('transfer'), ValueError('schema'),
		):
			with self.subTest(error=type(error).__name__):
				health = ViewerAPIHealth(threshold=2)
				health.failed('access', 'record-one', http_error(404))
				health.failed('access', 'record-two', error)
				health.failed('access', 'record-three', http_error(404))
				with self.assertRaises(ViewerAPIUnavailable):
					health.failed('access', 'record-four', http_error(404))

	def test_threshold_requires_a_positive_integer(self) -> None:
		'''Reject invalid thresholds instead of silently changing outage decisions.'''
		for threshold in (0, -1, 1.5, True, '3'):
			with self.subTest(threshold=threshold), self.assertRaises(ValueError):
				ViewerAPIHealth(threshold=threshold)


if __name__ == '__main__':
	unittest.main()
