#!/usr/bin/env python3
"""Offline regression tests for deferring exhausted Commons upload attempts."""

import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import upload as upload_nlb
from test_upload import FakeCommonsUploader, FakeNlbClient


class FakeAPIError(Exception):
	"""Represent a Commons API failure without importing or authenticating Pywikibot."""

	def __init__(self, code: str, info: str) -> None:
		"""Preserve the API code alongside the diagnostic message."""
		self.code = code
		super().__init__(f'{code}: {info}')


class TestDeferredUploads(unittest.TestCase):
	"""Keep failed uploads resumable while processing other available scans."""

	def test_exhaustion_saves_diagnostics_and_earliest_page(self) -> None:
		"""Persist the failed file identity and hash, clean staging, and preserve the earliest retry."""
		for earlier_page in (None, 2, 8):
			with self.subTest(earlier_page=earlier_page), tempfile.TemporaryDirectory() as tmp_dir:
				root = Path(tmp_dir)
				state = upload_nlb.default_state()
				state['current_page'] = 4
				if earlier_page is not None:
					state['retry_from_page'] = earlier_page
				client = FakeNlbClient()
				uploader = FakeCommonsUploader()
				last_error = FakeAPIError('stashfailed', 'Could not acquire lock.')
				error = upload_nlb.UploadRetriesExhausted(last_error, 3)
				uploader.upload = mock.Mock(side_effect=error)
				output = io.StringIO()
				with mock.patch('upload.check_commons_by_hash', return_value=('a' * 40, [])), mock.patch('sys.stdout', output):
					status = upload_nlb.process_single_scan(
						client=client, uploader=uploader, state=state,
						state_path=root / 'state.json', staging_dir=root / 'staging',
						marc_id='BY-NLB-rr111', file_id='1001', part_label='',
						record_meta={'title': 'Test record'}, marc_meta={},
					)
				self.assertEqual(status, 'upload_failed')
				saved = upload_nlb.load_state(root / 'state.json')
				entry = saved['items'][upload_nlb.item_state_key('BY-NLB-rr111', '1001')]
				self.assertEqual(entry['status'], 'upload_failed')
				self.assertEqual(entry['marc_id'], 'BY-NLB-rr111')
				self.assertEqual(entry['file_id'], '1001')
				self.assertEqual(entry['attempts'], 3)
				self.assertEqual(entry['error_type'], 'FakeAPIError')
				self.assertEqual(entry['error_code'], 'stashfailed')
				self.assertEqual(entry['error'], str(error))
				self.assertEqual(entry['sha1'], 'a' * 40)
				self.assertEqual(entry['size_bytes'], len(b'%PDF-1.4\nRecord=BY-NLB-rr111 File=1001\n%%EOF\n'))
				self.assertEqual(entry['commons_title'], f'File:{uploader.upload.call_args.args[1]}')
				self.assertTrue(entry['updated_at'])
				self.assertEqual(saved['retry_from_page'], min(4, earlier_page or 4))
				self.assertFalse(upload_nlb.is_item_completed(saved, 'BY-NLB-rr111', '1001'))
				self.assertEqual(list((root / 'staging').iterdir()), [])
				self.assertIn('Could not acquire lock.', output.getvalue())

	def test_batch_continues_and_restarts_without_reuploading_completed_scans(self) -> None:
		"""A failed early upload survives limits and dry runs until it succeeds on a later run."""
		for first_run_limit in (0, 1):
			with self.subTest(max_uploads=first_run_limit), tempfile.TemporaryDirectory() as tmp_dir:
				root = Path(tmp_dir)
				client = FakeNlbClient()
				uploader = FakeCommonsUploader()
				records = client.fetch_search_page(1)['records']
				client.fetch_search_page = mock.Mock(side_effect=lambda page, **_: {
					'total_results': 2, 'records': records[page - 1:page],
				})
				original_upload = uploader.upload
				fail_first = True

				def upload_with_failure(path: Path, *args: object) -> dict[str, object]:
					"""Fail only the first scan while allowing later files to upload normally."""
					if path.name == 'BY-NLB-rr111_1001.pdf' and fail_first:
						raise upload_nlb.UploadRetriesExhausted(FakeAPIError('stashfailed', 'Could not acquire lock.'), 3)
					return original_upload(path, *args)

				uploader.upload = mock.Mock(side_effect=upload_with_failure)
				kwargs = {
					'client': client, 'uploader': uploader,
					'state_path': root / 'state.json', 'staging_dir': root / 'staging',
				}
				with mock.patch('upload.SEARCH_PAGE_SIZE', 1), mock.patch(
					'upload.check_commons_by_hash', side_effect=lambda path, **_: (upload_nlb.calculate_sha1(path), []),
				), mock.patch('sys.stdout', io.StringIO()):
					counts = upload_nlb.iterate_and_upload(**kwargs, max_uploads=first_run_limit)
					self.assertEqual(counts['upload_failed'], 1)
					self.assertEqual(counts['uploaded'], first_run_limit or 2)
					self.assertEqual(client.downloaded_file_ids, ['1001', '2001'] if first_run_limit else ['1001', '2001', '2002'])
					self.assertEqual(upload_nlb.load_state(kwargs['state_path'])['retry_from_page'], 1)
					self.assertEqual(list(kwargs['staging_dir'].iterdir()), [])

					# Persistently failing files remain pending while completed files are skipped.
					client.downloaded_file_ids.clear()
					counts = upload_nlb.iterate_and_upload(**kwargs)
					self.assertEqual(counts['upload_failed'], 1)
					self.assertEqual(counts['uploaded'], 1 if first_run_limit else 0)
					self.assertEqual(client.downloaded_file_ids, ['1001', '2002'] if first_run_limit else ['1001'])
					pending = upload_nlb.load_state(kwargs['state_path'])['items']['BY-NLB-rr111:1001'].copy()

					fail_first = False
					client.downloaded_file_ids.clear()
					uploader.upload.reset_mock()
					counts = upload_nlb.iterate_and_upload(**kwargs, dry_run=True)
					self.assertEqual(counts['dry_run_ready'], 1)
					self.assertEqual(counts['upload_failed'], 0)
					self.assertEqual(client.downloaded_file_ids, ['1001'])
					uploader.upload.assert_not_called()
					state = upload_nlb.load_state(kwargs['state_path'])
					self.assertEqual(state['retry_from_page'], 1)
					self.assertEqual(state['items']['BY-NLB-rr111:1001'], pending)

					client.downloaded_file_ids.clear()
					counts = upload_nlb.iterate_and_upload(**kwargs)
					self.assertEqual(counts['upload_failed'], 0)
					self.assertEqual(counts['uploaded'], 1)
					self.assertEqual(client.downloaded_file_ids, ['1001'])
					self.assertEqual(len(uploader.uploads), 3)
					state = upload_nlb.load_state(kwargs['state_path'])
					self.assertTrue(upload_nlb.is_item_completed(state, 'BY-NLB-rr111', '1001'))
					self.assertIsNone(state.get('retry_from_page'))
					self.assertEqual(list(kwargs['staging_dir'].iterdir()), [])

	def test_multiple_failed_uploads_keep_the_earliest_page(self) -> None:
		"""Later upload failures cannot replace the page containing the first pending upload."""
		with tempfile.TemporaryDirectory() as tmp_dir:
			root = Path(tmp_dir)
			client = FakeNlbClient()
			uploader = FakeCommonsUploader()
			records = client.fetch_search_page(1)['records']
			client.fetch_search_page = mock.Mock(side_effect=lambda page, **_: {
				'total_results': 2, 'records': records[page - 1:page],
			})
			uploader.upload = mock.Mock(side_effect=upload_nlb.UploadRetriesExhausted(
				FakeAPIError('stashfailed', 'Could not acquire lock.'), 3,
			))
			with mock.patch('upload.SEARCH_PAGE_SIZE', 1), mock.patch(
				'upload.check_commons_by_hash', return_value=('b' * 40, []),
			), mock.patch('sys.stdout', io.StringIO()):
				counts = upload_nlb.iterate_and_upload(
					client=client, uploader=uploader,
					state_path=root / 'state.json', staging_dir=root / 'staging',
				)
			self.assertEqual(counts['upload_failed'], 3)
			self.assertEqual(counts['uploaded'], 0)
			self.assertEqual(client.downloaded_file_ids, ['1001', '2001', '2002'])
			state = upload_nlb.load_state(root / 'state.json')
			self.assertEqual(state['current_page'], 2)
			self.assertEqual(state['retry_from_page'], 1)
			self.assertEqual([entry['status'] for entry in state['items'].values()], ['upload_failed'] * 3)
			self.assertEqual(list((root / 'staging').iterdir()), [])

	def test_unrelated_upload_errors_still_abort_and_clean_staging(self) -> None:
		"""Authentication, permissions, disk errors and interrupts must remain visible."""
		for error in (
			FakeAPIError('badtoken', 'Invalid CSRF token.'),
			FakeAPIError('permissiondenied', 'Permission denied.'),
			FakeAPIError('stashfailed', 'Raw failure outside bounded retry handling.'),
			OSError('No space left on device'),
			KeyboardInterrupt(),
		):
			with self.subTest(error=str(error)), tempfile.TemporaryDirectory() as tmp_dir:
				root = Path(tmp_dir)
				uploader = FakeCommonsUploader()
				uploader.upload = mock.Mock(side_effect=error)
				with mock.patch('upload.check_commons_by_hash', return_value=('c' * 40, [])), mock.patch('sys.stdout', io.StringIO()):
					with self.assertRaises(type(error)) as raised:
						upload_nlb.process_single_scan(
							client=FakeNlbClient(), uploader=uploader, state=upload_nlb.default_state(),
							state_path=root / 'state.json', staging_dir=root / 'staging',
							marc_id='BY-NLB-rr111', file_id='1001', part_label='',
							record_meta={'title': 'Test record'}, marc_meta={},
						)
				self.assertIs(raised.exception, error)
				state = upload_nlb.load_state(root / 'state.json')
				self.assertEqual(state['items'], {})
				self.assertIsNone(state.get('retry_from_page'))
				self.assertEqual(list((root / 'staging').iterdir()), [])

	def test_upload_exhaustion_is_only_handled_at_the_upload_boundary(self) -> None:
		"""Do not defer an exception injected into unrelated download or hash operations."""
		for stage in ('download', 'hash'):
			with self.subTest(stage=stage), tempfile.TemporaryDirectory() as tmp_dir:
				root = Path(tmp_dir)
				client = FakeNlbClient()
				uploader = FakeCommonsUploader()
				uploader.upload = mock.Mock()
				error = upload_nlb.UploadRetriesExhausted(FakeAPIError('stashfailed', 'Could not acquire lock.'), 3)
				if stage == 'download':
					client.download_pdf_scan = mock.Mock(side_effect=error)
				with mock.patch('upload.check_commons_by_hash', side_effect=error), mock.patch('sys.stdout', io.StringIO()):
					with self.assertRaises(upload_nlb.UploadRetriesExhausted) as raised:
						upload_nlb.process_single_scan(
							client=client, uploader=uploader, state=upload_nlb.default_state(),
							state_path=root / 'state.json', staging_dir=root / 'staging',
							marc_id='BY-NLB-rr111', file_id='1001', part_label='',
							record_meta={'title': 'Test record'}, marc_meta={},
						)
				self.assertIs(raised.exception, error)
				uploader.upload.assert_not_called()
				state = upload_nlb.load_state(root / 'state.json')
				self.assertEqual(state['items'], {})
				self.assertIsNone(state.get('retry_from_page'))
				self.assertEqual(list((root / 'staging').iterdir()), [])


if __name__ == '__main__':
	unittest.main()
