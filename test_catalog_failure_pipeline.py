#!/usr/bin/env python3
"""Offline batch regressions for exhausted catalog requests and resumable state."""

from contextlib import contextmanager
import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import requests

from catalog_requests import CatalogRetriesExhausted
import upload as upload_nlb
from test_upload import FakeCommonsUploader, FakeNlbClient


class TestCatalogFailurePipeline(unittest.TestCase):
	"""Keep unavailable records pending while retaining completed scan progress."""

	@contextmanager
	def environment(self):
		"""Provide a two-page offline catalog and isolated state with no API writes."""
		with tempfile.TemporaryDirectory() as tmp_dir:
			root = Path(tmp_dir)
			client = FakeNlbClient()
			uploader = FakeCommonsUploader()
			records = client.fetch_search_page(1)['records']
			client.fetch_search_page = mock.Mock(side_effect=lambda page, **_: {
				'total_results': 2, 'records': records[page - 1:page],
			})
			kwargs = {
				'client': client, 'uploader': uploader,
				'state_path': root / 'state.json', 'staging_dir': root / 'staging',
			}
			with mock.patch('upload.SEARCH_PAGE_SIZE', 1), mock.patch(
				'upload.check_commons_by_hash', side_effect=lambda path, **_: (upload_nlb.calculate_sha1(path), []),
			), mock.patch('sys.stdout', io.StringIO()):
				yield client, uploader, kwargs

	def gateway_failure(self) -> CatalogRetriesExhausted:
		"""Represent the reported persistent record-page gateway timeout."""
		response = requests.Response()
		response.status_code = 504
		return CatalogRetriesExhausted(requests.HTTPError('504 Gateway Time-out', response=response), 5)

	def test_record_failure_continues_and_recovery_preserves_completed_scans(self) -> None:
		"""Access, HTML/XML, and METS failures survive limits and dry runs until recovery."""
		for stage in ('check_doc_access', 'fetch_record_metadata', 'resolve_record_files'):
			for limit in (0, 1):
				with self.subTest(stage=stage, limit=limit), self.environment() as (client, uploader, kwargs):
					original = getattr(client, stage)
					failure = self.gateway_failure()
					fail_first = True

					def catalog_read(*args):
						"""Fail the first record at one selected catalog stage only."""
						marc_id = args[0]['marc_id'] if stage == 'resolve_record_files' else args[0]
						if fail_first and marc_id == 'BY-NLB-rr111':
							raise failure
						return original(*args)

					setattr(client, stage, mock.Mock(side_effect=catalog_read))
					counts = upload_nlb.iterate_and_upload(**kwargs, max_uploads=limit)
					self.assertEqual(counts['metadata_failed'], 1)
					self.assertEqual(counts['uploaded'], limit or 2)
					self.assertEqual(client.downloaded_file_ids, ['2001'] if limit else ['2001', '2002'])
					state = upload_nlb.load_state(kwargs['state_path'])
					pending = state['record_failures']['BY-NLB-rr111'].copy()
					self.assertEqual(pending['status'], 'metadata_failed')
					self.assertEqual(pending['attempts'], 5)
					self.assertEqual(pending['error_type'], 'HTTPError')
					self.assertEqual(pending['http_status'], 504)
					self.assertIn('504 Gateway Time-out', pending['error'])
					self.assertTrue(pending['updated_at'])
					self.assertEqual(state['retry_from_page'], 1)
					self.assertFalse(any(key.startswith('BY-NLB-rr111:') for key in state['items']))

					fail_first = False
					client.downloaded_file_ids.clear()
					upload_count = len(uploader.uploads)
					counts = upload_nlb.iterate_and_upload(**kwargs, dry_run=True)
					self.assertEqual(counts['dry_run_ready'], 2 if limit else 1)
					self.assertEqual(len(uploader.uploads), upload_count)
					state = upload_nlb.load_state(kwargs['state_path'])
					self.assertEqual(state['record_failures']['BY-NLB-rr111'], pending)
					self.assertEqual(state['retry_from_page'], 1)

					client.downloaded_file_ids.clear()
					counts = upload_nlb.iterate_and_upload(**kwargs)
					self.assertEqual(counts['metadata_failed'], 0)
					self.assertEqual(counts['uploaded'], 2 if limit else 1)
					self.assertEqual(client.downloaded_file_ids, ['1001', '2002'] if limit else ['1001'])
					state = upload_nlb.load_state(kwargs['state_path'])
					self.assertNotIn('BY-NLB-rr111', state.get('record_failures', {}))
					self.assertNotIn('retry_from_page', state)
					self.assertEqual(len(state['items']), 3)

	def test_completed_direct_record_clears_old_failure_without_catalog_reads(self) -> None:
		"""An already completed direct record resolves stale failure state except in dry runs."""
		for dry_run in (False, True):
			with self.subTest(dry_run=dry_run), self.environment() as (client, _, kwargs):
				state = upload_nlb.default_state()
				state['items']['BY-NLB-rr111:1001'] = {'status': 'uploaded'}
				state['record_failures'] = {'BY-NLB-rr111': {'status': 'metadata_failed'}}
				state['retry_from_page'] = 1
				upload_nlb.save_state(kwargs['state_path'], state)
				original = client.check_doc_access

				def check_access(marc_id):
					"""Assert completed direct files require no new record request."""
					self.assertNotEqual(marc_id, 'BY-NLB-rr111')
					return original(marc_id)

				client.check_doc_access = check_access
				upload_nlb.iterate_and_upload(**kwargs, dry_run=dry_run)
				state = upload_nlb.load_state(kwargs['state_path'])
				self.assertEqual('BY-NLB-rr111' in state.get('record_failures', {}), dry_run)

	def test_restricted_and_year_filtered_records_clear_old_failure_except_dry_runs(self) -> None:
		"""Resolved access/year exclusions remove stale failure diagnostics only in normal runs."""
		for outcome in ('restricted', 'before_year', 'after_year'):
			for dry_run in (False, True):
				with self.subTest(outcome=outcome, dry_run=dry_run), self.environment() as (client, _, kwargs):
					from_year = 1891 if outcome == 'before_year' else None
					until_year = 1889 if outcome == 'after_year' else upload_nlb.DEFAULT_UNTIL_YEAR
					kwargs.update(from_year=from_year, until_year=until_year)
					pending = {'status': 'metadata_failed', 'error': 'Previous 504 Gateway Time-out'}
					state = upload_nlb.default_state()
					state.update(from_year=from_year, until_year=until_year, current_page=2, retry_from_page=1)
					state['record_failures'] = {'BY-NLB-rr111': pending.copy()}
					upload_nlb.save_state(kwargs['state_path'], state)
					if outcome == 'restricted':
						client.check_doc_access = mock.Mock(side_effect=lambda marc_id: {
							'publicAccess': marc_id != 'BY-NLB-rr111',
						})
					counts = upload_nlb.iterate_and_upload(**kwargs, dry_run=dry_run)
					self.assertEqual(counts['metadata_failed'], 0)
					self.assertEqual(counts['restricted'], 1 if outcome == 'restricted' else 0)
					self.assertNotIn('1001', client.downloaded_file_ids)
					state = upload_nlb.load_state(kwargs['state_path'], from_year=from_year, until_year=until_year)
					if dry_run:
						self.assertEqual(state['record_failures']['BY-NLB-rr111'], pending)
						self.assertEqual(state['retry_from_page'], 1)
					else:
						self.assertNotIn('BY-NLB-rr111', state.get('record_failures', {}))
						self.assertNotIn('retry_from_page', state)

	def test_multiple_record_failures_keep_earliest_retry_page(self) -> None:
		"""A later failure cannot move the retry cursor past an earlier pending record."""
		with self.environment() as (client, _, kwargs):
			client.fetch_record_metadata = mock.Mock(side_effect=self.gateway_failure())
			counts = upload_nlb.iterate_and_upload(**kwargs)
			self.assertEqual(counts['metadata_failed'], 2)
			state = upload_nlb.load_state(kwargs['state_path'])
			self.assertEqual(state['retry_from_page'], 1)
			self.assertEqual(set(state['record_failures']), {'BY-NLB-rr111', 'BY-NLB-rr222'})
			self.assertEqual(state['items'], {})

	def test_search_failure_stops_on_failed_page_and_resumes(self) -> None:
		"""An exhausted search request returns a summary and retains its exact next page."""
		for failed_page in (1, 2):
			with self.subTest(failed_page=failed_page), self.environment() as (client, _, kwargs):
				original = client.fetch_search_page
				fail_search = True

				def search(page, **options):
					"""Check that state is saved before the request which might fail."""
					self.assertEqual(upload_nlb.load_state(kwargs['state_path'])['current_page'], page)
					if page == failed_page and fail_search:
						raise self.gateway_failure()
					return original(page=page, **options)

				client.fetch_search_page = mock.Mock(side_effect=search)
				counts = upload_nlb.iterate_and_upload(**kwargs)
				self.assertEqual(counts['search_failed'], 1)
				self.assertEqual(counts['uploaded'], failed_page - 1)
				state = upload_nlb.load_state(kwargs['state_path'])
				self.assertEqual(state['current_page'], failed_page)
				self.assertEqual(client.fetch_search_page.call_count, failed_page)
				fail_search = False
				client.downloaded_file_ids.clear()
				client.fetch_search_page.reset_mock()
				counts = upload_nlb.iterate_and_upload(**kwargs)
				self.assertEqual(counts['search_failed'], 0)
				self.assertEqual(counts['uploaded'], 4 - failed_page)
				self.assertEqual(client.fetch_search_page.call_args_list[0].kwargs['page'], failed_page)
				self.assertEqual(client.downloaded_file_ids, ['1001', '2001', '2002'] if failed_page == 1 else ['2001', '2002'])

	def test_search_failure_keeps_earlier_pending_record(self) -> None:
		"""Stopping on a later search page must not discard an earlier record retry."""
		with self.environment() as (client, _, kwargs):
			original = client.fetch_search_page
			client.fetch_search_page = mock.Mock(side_effect=lambda page, **options: (
				original(page=page, **options) if page == 1 else self.raise_failure()
			))
			client.fetch_record_metadata = mock.Mock(side_effect=self.gateway_failure())
			counts = upload_nlb.iterate_and_upload(**kwargs)
			self.assertEqual(counts['search_failed'], 1)
			self.assertEqual(counts['metadata_failed'], 1)
			state = upload_nlb.load_state(kwargs['state_path'])
			self.assertEqual(state['current_page'], 2)
			self.assertEqual(state['retry_from_page'], 1)
			self.assertIn('BY-NLB-rr111', state['record_failures'])

	def raise_failure(self):
		"""Raise a reusable transient failure from a callable search response."""
		raise self.gateway_failure()

	def test_unclassified_errors_remain_fatal(self) -> None:
		"""Do not turn SSL, permanent HTTP, disk, auth, or interruption errors into skips."""
		response = requests.Response()
		response.status_code = 403
		errors = (
			requests.exceptions.SSLError('Certificate mismatch'),
			requests.HTTPError('Forbidden', response=response),
			OSError('Disk is full'), RuntimeError('badtoken: Invalid CSRF token.'), KeyboardInterrupt(),
		)
		for stage in ('fetch_search_page', 'check_doc_access', 'fetch_record_metadata', 'resolve_record_files'):
			for error in errors:
				with self.subTest(stage=stage, error_type=type(error).__name__), self.environment() as (client, _, kwargs):
					setattr(client, stage, mock.Mock(side_effect=error))
					with self.assertRaises(type(error)) as raised:
						upload_nlb.iterate_and_upload(**kwargs)
					self.assertIs(raised.exception, error)
					state = upload_nlb.load_state(kwargs['state_path'])
					self.assertEqual(state.get('record_failures', {}), {})
					self.assertEqual(state['items'], {})


if __name__ == '__main__':
	unittest.main()
