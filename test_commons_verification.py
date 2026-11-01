'''Exercise post-publication Commons reads without downloading or publishing files.'''

import copy
import unittest
from unittest import mock

import requests

import upload


def verification_payload():
	'''Return the minimum complete response needed to confirm a published file.'''
	return {'query': {'pages': [{
		'title': 'File:Book.pdf', 'pageid': 12, 'lastrevid': 34,
		'imageinfo': [{'sha1': 'a' * 40, 'size': 123}],
	}]}}


class TestCommonsVerification(unittest.TestCase):
	'''Keep partially visible files pending until their complete identity is available.'''

	def setUp(self):
		'''Build an uploader with a scripted Commons API and no real waits.'''
		self.uploader = upload.CommonsUploader.__new__(upload.CommonsUploader)
		self.uploader.site = mock.Mock()
		self.submit = self.uploader.site.simple_request.return_value.submit
		self.sleep = mock.patch('time.sleep').start()
		self.addCleanup(mock.patch.stopall)

	def test_missing_page_id_is_read_again_after_publication(self):
		'''A valid imageinfo without pageid must not cause a false verification failure.'''
		incomplete = verification_payload()
		del incomplete['query']['pages'][0]['pageid']
		self.submit.side_effect = [incomplete, verification_payload()]
		info = self.uploader.file_info('File:Book.pdf')
		self.assertEqual((info['page_id'], info['revision_id']), (12, 34))
		self.assertEqual(self.uploader.site.simple_request.call_count, 2)
		self.sleep.assert_called_once()

	def test_incomplete_schema_recovers_only_after_complete_response(self):
		'''Missing revisions, imageinfo and query sections trigger fresh bounded reads.'''
		for field in ('lastrevid', 'imageinfo', 'title'):
			with self.subTest(field=field):
				incomplete = verification_payload()
				del incomplete['query']['pages'][0][field]
				self.submit.side_effect = [incomplete, verification_payload()]
				info = self.uploader.file_info('File:Book.pdf')
				self.assertEqual(info['sha1'], 'a' * 40)
		for incomplete in (None, [], {}, {'query': None}, {'query': {'pages': None}},
			{'query': {'pages': [None]}}, {'query': {'pages': []}}):
			with self.subTest(incomplete=incomplete):
				self.submit.side_effect = [incomplete, verification_payload()]
				self.assertEqual(self.uploader.file_info('File:Book.pdf')['size_bytes'], 123)

	def test_persistent_incomplete_metadata_is_a_bounded_failure(self):
		'''An absent page ID stays unverified with a useful diagnostic after five reads.'''
		incomplete = verification_payload()
		del incomplete['query']['pages'][0]['pageid']
		self.submit.return_value = incomplete
		with self.assertRaisesRegex(ValueError, '5 attempts.*pageid'):
			self.uploader.file_info('File:Book.pdf')
		self.assertEqual(self.submit.call_count, 5)
		self.assertEqual(self.sleep.call_args_list, [mock.call(2), mock.call(4), mock.call(8), mock.call(16)])

	def test_invalid_ids_hashes_sizes_and_imageinfo_are_not_accepted(self):
		'''Reject booleans, missing identities and malformed file metadata explicitly.'''
		page_cases = [
			{field: value}
			for field in ('pageid', 'lastrevid')
			for value in (None, True, False, 0, -1, '12', 1.5)
		]
		page_cases += [{'title': ''}, {'title': None}, {'title': 123},
			{'missing': True}, {'invalid': True}, {'ns': 0},
			{'imageinfo': None}, {'imageinfo': {}}, {'imageinfo': [None]},
			{'imageinfo': []}]
		page_cases += [{'imageinfo': [{'sha1': sha1, 'size': size}]}
			for sha1, size in [('bad', 123), (None, 123), ('a' * 40, True),
				('a' * 40, 0), ('a' * 40, -1), ('a' * 40, '123')]]
		for replacement in page_cases:
			with self.subTest(replacement=replacement):
				payload = verification_payload()
				payload['query']['pages'][0].update(replacement)
				self.submit.return_value = payload
				with self.assertRaises(ValueError):
					self.uploader.file_info('File:Book.pdf')

	def test_transient_transport_failure_recovers(self):
		'''Read-only connection and timeout failures can recover without another upload.'''
		for error in (requests.ConnectionError('reset'), requests.ReadTimeout('slow')):
			with self.subTest(error=type(error).__name__):
				self.submit.side_effect = [error, verification_payload()]
				self.assertEqual(self.uploader.file_info('File:Book.pdf')['page_id'], 12)

	def test_library_api_and_server_failures_keep_the_retry_boundary(self):
		'''Retry transient API/server errors but do not restart exhausted library budgets.'''
		from pywikibot.exceptions import APIError, FatalServerError, ServerError, ApiTimeoutError
		for error in (APIError('maxlag', 'Lagged'), ServerError('Temporary failure')):
			with self.subTest(error=type(error).__name__):
				self.submit.side_effect = [error, verification_payload()]
				self.assertEqual(self.uploader.file_info('File:Book.pdf')['page_id'], 12)
		for error in (APIError('badtoken', 'Expired'), FatalServerError('Fatal'), ApiTimeoutError('Retries exhausted')):
			with self.subTest(error=type(error).__name__):
				self.submit.reset_mock()
				self.submit.side_effect = error
				with self.assertRaises(type(error)):
					self.uploader.file_info('File:Book.pdf')
				self.assertEqual(self.submit.call_count, 1)

	def test_tls_and_nonretryable_http_errors_propagate_immediately(self):
		'''Certificate failures and access errors must not be hidden as replica delays.'''
		response = requests.Response()
		response.status_code = 403
		for error in (requests.exceptions.SSLError('certificate failed'),
			requests.HTTPError('forbidden', response=response), RuntimeError('bug')):
			with self.subTest(error=type(error).__name__):
				self.submit.reset_mock()
				self.submit.side_effect = error
				with self.assertRaises(type(error)) as raised:
					self.uploader.file_info('File:Book.pdf')
				self.assertIs(raised.exception, error)
				self.assertEqual(self.submit.call_count, 1)

	def test_schema_and_transport_failures_share_one_read_budget(self):
		'''Mixed failures cannot reset the application verification attempt counter.'''
		self.submit.side_effect = [
			{}, requests.ReadTimeout('slow'), {}, requests.ConnectionError('reset'), {},
		]
		with self.assertRaisesRegex(ValueError, '5 attempts'):
			self.uploader.file_info('File:Book.pdf')
		self.assertEqual(self.submit.call_count, 5)

	def test_complete_result_keeps_optional_provenance(self):
		'''Return Commons identity and optional location metadata without changing payloads.'''
		payload = verification_payload()
		payload['query']['pages'][0]['imageinfo'][0].update({
			'timestamp': '2026-11-01T00:00:00Z', 'url': 'https://upload.wikimedia.org/book.pdf',
		})
		original = copy.deepcopy(payload)
		self.submit.return_value = payload
		info = self.uploader.file_info('File:Book.pdf')
		self.assertEqual(info['commons_title'], 'File:Book.pdf')
		self.assertEqual(info['timestamp'], '2026-11-01T00:00:00Z')
		self.assertEqual(info['url'], 'https://upload.wikimedia.org/book.pdf')
		self.assertIn('checked_at', info)
		self.assertEqual(payload, original)
		self.assertEqual(self.submit.call_count, 1)
		self.sleep.assert_not_called()


if __name__ == '__main__':
	unittest.main()
