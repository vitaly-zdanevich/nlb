'''Verify statistics outages and expired viewer sessions without network access.'''

import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import requests

import upload
from test_pdf_validation import pdf_bytes
from test_upload import FakeCommonsUploader


class TestViewerStatistics(unittest.TestCase):
	'''Exercise real request preparation, cookie scoping and PDF validation.'''

	def setUp(self) -> None:
		'''Create an exported mock browser session and isolated download storage.'''
		self.directory = tempfile.TemporaryDirectory()
		self.addCleanup(self.directory.cleanup)
		self.root = Path(self.directory.name)
		self.cookies = self.root / 'cookies.txt'
		self.cookies.write_text(
			'# Netscape HTTP Cookie File\n'
			'elib.nlb.by\tFALSE\t/\tTRUE\t0\telib_session_id\told-session-secret\n'
			'elib.nlb.by\tFALSE\t/\tTRUE\t0\tJSESSIONID\tauth-secret\n',
		)
		self.session = requests.Session()
		self.addCleanup(self.session.close)
		self.client = upload.NlbClient(session=self.session, viewer_cookies=self.cookies)
		self.payload = pdf_bytes()
		self.seen = []
		self.failures = {}
		self.posts = 0

	def send(self, request, **kwargs) -> requests.Response:
		'''Serve queued endpoint failures and otherwise return valid mock responses.'''
		self.seen.append(request)
		path = upload.urlsplit(request.url).path
		response = requests.Response()
		response.url, response.request = request.url, request
		response.status_code = 200
		response._content_consumed = True
		response.close = mock.Mock(wraps=response.close)
		response._content = b'{"rendering":[]}'
		if request.method == 'POST':
			self.posts += 1
			self.session.cookies.set('elib_session_id', f'guest-session-{self.posts}', domain='elib.nlb.by', path='/')
		elif '/iiif/file/' in path:
			response._content = self.payload
			response.headers['Content-Length'] = str(len(self.payload))
		failures = self.failures.get(path, [])
		if failures:
			failure = failures.pop(0)
			if isinstance(failure, Exception):
				raise failure
			response.status_code, response._content = failure
		return response

	def expired(self) -> tuple[int, bytes]:
		'''Match the observed NLB response using a synthetic secret session ID.'''
		return 500, json.dumps({'message': 'Session with id: old-session-secret not exists'}).encode()

	def download(self) -> int:
		'''Download a mocked public scan through the production client.'''
		return self.client.download_pdf_scan('BY-NLB-br0000053932', '3454679', self.root / 'scan.pdf')

	def test_statistics_outages_preserve_cookies_and_still_download_pdf(self) -> None:
		'''Both failed events are attempted once without retries or a guest downgrade.'''
		for status in (500, 502, 503, 504):
			with self.subTest(status=status):
				self.seen.clear()
				self.failures = {
					'/stats/event/PAGE_VIEWS': [(status, b'temporary outage')],
					'/stats/event/IIIF_DOWNLOAD_ATTEMPT': [(status, b'[]')],
				}
				with mock.patch.object(self.session, 'send', side_effect=self.send), mock.patch(
					'upload.time.sleep',
				) as sleep, mock.patch('sys.stdout', io.StringIO()) as output:
					self.assertEqual(self.download(), len(self.payload))
				self.assertEqual((self.root / 'scan.pdf').read_bytes(), self.payload)
				self.assertEqual(len(self.seen), 4)
				self.assertEqual(self.posts, 0)
				sleep.assert_not_called()
				self.assertEqual(output.getvalue().count('continuing to the PDF request'), 2)
				for request in self.seen:
					self.assertIn('elib_session_id=old-session-secret', request.headers['Cookie'])
					self.assertIn('JSESSIONID=auth-secret', request.headers['Cookie'])

	def test_statistics_network_errors_still_allow_pdf_validation(self) -> None:
		'''Timeouts and broken transfers are not fatal to unrelated PDF requests.'''
		for failure in (requests.Timeout(), requests.ConnectionError(), requests.exceptions.ChunkedEncodingError()):
			with self.subTest(failure=type(failure).__name__):
				self.failures = {'/stats/event/PAGE_VIEWS': [failure]}
				with mock.patch.object(self.session, 'send', side_effect=self.send), mock.patch('sys.stdout', io.StringIO()):
					self.assertEqual(self.download(), len(self.payload))

	def test_pdf_failure_after_statistics_outage_keeps_the_download_retry_budget(self) -> None:
		'''Optional event failures cannot exhaust or reset retries for PDF HTTP 500s.'''
		self.failures = {
			'/stats/event/PAGE_VIEWS': [(500, b'outage')] * 5,
			'/iiif/file/3454679/bytes': [(500, b'PDF unavailable')] * 5,
		}
		with mock.patch.object(self.session, 'send', side_effect=self.send), mock.patch('upload.time.sleep') as sleep, mock.patch('sys.stdout', io.StringIO()):
			with self.assertRaises(upload.DownloadRetriesExhausted) as raised:
				self.download()
		self.assertEqual(raised.exception.attempts, 5)
		self.assertEqual(len(self.seen), 20)
		self.assertEqual(sleep.call_args_list, [mock.call(n) for n in (5, 10, 20, 40)])
		self.assertFalse((self.root / 'scan.pdf').exists())

	def test_expired_export_stops_without_disclosing_or_replacing_cookies(self) -> None:
		'''The known missing-session error cannot recover by restarting with the same file.'''
		for event in ('PAGE_VIEWS', 'IIIF_DOWNLOAD_ATTEMPT'):
			with self.subTest(event=event):
				self.seen.clear()
				self.failures = {'/stats/event/' + event: [self.expired()]}
				with mock.patch.object(self.session, 'send', side_effect=self.send), mock.patch('upload.time.sleep') as sleep:
					with self.assertRaises(upload.ViewerSessionExpired) as raised:
						self.download()
				self.assertIn('export fresh cookies', str(raised.exception))
				self.assertNotIn('old-session-secret', str(raised.exception))
				self.assertEqual(self.posts, 0)
				sleep.assert_not_called()
				self.assertFalse(any('/iiif/file/' in request.url for request in self.seen))

	def test_guest_session_can_be_renewed_once(self) -> None:
		'''A known expired guest session can be renewed without browser credentials.'''
		self.client = upload.NlbClient(session=self.session)
		self.failures = {'/stats/event/PAGE_VIEWS': [self.expired()]}
		with mock.patch.object(self.session, 'send', side_effect=self.send), mock.patch('upload.time.sleep') as sleep, mock.patch('sys.stdout', io.StringIO()):
			self.assertEqual(self.download(), len(self.payload))
		self.assertEqual(self.posts, 2)
		self.assertIn('elib_session_id=guest-session-2', self.seen[-1].headers['Cookie'])
		sleep.assert_not_called()

	def test_guest_renewal_shares_the_pdf_attempt_budget(self) -> None:
		'''One expired-session attempt leaves four attempts for a failing PDF.'''
		self.client = upload.NlbClient(session=self.session)
		self.failures = {
			'/stats/event/PAGE_VIEWS': [self.expired()],
			'/iiif/file/3454679/bytes': [(500, b'PDF unavailable')] * 4,
		}
		with mock.patch.object(self.session, 'send', side_effect=self.send), mock.patch('upload.time.sleep') as sleep, mock.patch('sys.stdout', io.StringIO()):
			with self.assertRaises(upload.DownloadRetriesExhausted) as raised:
				self.download()
		self.assertEqual(raised.exception.attempts, 5)
		self.assertEqual(self.posts, 2)
		self.assertEqual(sum('/iiif/file/' in request.url for request in self.seen), 4)
		self.assertEqual(sleep.call_args_list, [mock.call(n) for n in (10, 20, 40)])

	def test_expiry_on_last_attempt_does_not_expand_retry_budget(self) -> None:
		'''An expired session on attempt five cannot start a sixth attempt.'''
		self.client = upload.NlbClient(session=self.session)
		self.failures = {'/iiif/manifest': [(500, b'outage')] * 4}
		self.failures['/stats/event/PAGE_VIEWS'] = [self.expired()]
		with mock.patch.object(self.session, 'send', side_effect=self.send), mock.patch('upload.time.sleep'), mock.patch('sys.stdout', io.StringIO()):
			with self.assertRaises(upload.ViewerSessionExpired):
				self.download()
		self.assertEqual(self.posts, 1)
		self.assertEqual(sum('/iiif/manifest' in request.url for request in self.seen), 5)

	def test_failed_guest_renewal_does_not_loop(self) -> None:
		'''A second missing-session error stops after the single renewal attempt.'''
		self.client = upload.NlbClient(session=self.session)
		self.failures = {'/stats/event/PAGE_VIEWS': [self.expired(), self.expired()]}
		with mock.patch.object(self.session, 'send', side_effect=self.send), mock.patch('sys.stdout', io.StringIO()):
			with self.assertRaises(upload.ViewerSessionExpired):
				self.download()
		self.assertEqual(self.posts, 2)

	def test_permanent_statistics_errors_and_tls_failures_remain_fatal(self) -> None:
		'''Do not reinterpret authorization rejections or invalid certificates as outages.'''
		for failure in ((403, b'Forbidden'), (400, b'Invalid request'), requests.exceptions.SSLError('bad certificate')):
			with self.subTest(failure=failure):
				self.failures = {'/stats/event/PAGE_VIEWS': [failure]}
				with mock.patch.object(self.session, 'send', side_effect=self.send):
					with self.assertRaises(requests.RequestException):
						self.download()

	def test_event_responses_are_closed(self) -> None:
		'''Close successful, unavailable and expired-session responses alike.'''
		for status, payload in ((200, b'{}'), (500, b'outage'), self.expired()):
			with self.subTest(status=status, payload=payload):
				response = requests.Response()
				response.status_code, response._content = status, payload
				response._content_consumed = True
				response.close = mock.Mock(wraps=response.close)
				with mock.patch.object(self.session, 'get', return_value=response), mock.patch('sys.stdout', io.StringIO()):
					try:
						self.client._record_viewer_event('PAGE_VIEWS', 'record', 'file')
					except upload.ViewerSessionExpired:
						pass
				response.close.assert_called_once_with()

	def test_expired_session_keeps_scan_pending_and_cleans_temporary_pdf(self) -> None:
		'''An expired export stops the batch while preserving a resumable record.'''
		self.failures = {'/stats/event/PAGE_VIEWS': [self.expired()]}
		state = upload.default_state()
		state['current_page'] = 2
		with mock.patch.object(self.session, 'send', side_effect=self.send), mock.patch('sys.stdout', io.StringIO()):
			with self.assertRaises(upload.ViewerSessionExpired):
				upload.process_single_scan(
					client=self.client, uploader=FakeCommonsUploader(), state=state,
					state_path=self.root / 'state.json', staging_dir=self.root / 'staging',
					marc_id='BY-NLB-br0000053932', file_id='3454679', part_label='',
					record_meta={'title': 'Book'}, marc_meta={'date': '1848'},
				)
		saved = upload.load_state(self.root / 'state.json')
		entry = saved['items']['BY-NLB-br0000053932:3454679']
		self.assertEqual(entry['status'], 'download_failed')
		self.assertEqual(entry['error_type'], 'ViewerSessionExpired')
		self.assertEqual(saved['retry_from_page'], 2)
		self.assertEqual(list((self.root / 'staging').iterdir()), [])

	def test_cli_reports_expired_session_without_traceback(self) -> None:
		'''Return a nonzero status and actionable message when a cookie export expires.'''
		with mock.patch('upload.NlbClient'), mock.patch('upload.iterate_and_upload', side_effect=upload.ViewerSessionExpired()), mock.patch('sys.stdout', io.StringIO()) as output:
			self.assertEqual(upload.main(['--dry-run', '--archive-dir', str(self.root / 'archive')]), 2)
		self.assertIn('Stopped:', output.getvalue())
		self.assertIn('export fresh cookies', output.getvalue())
		self.assertNotIn('Traceback', output.getvalue())
