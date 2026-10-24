#!/usr/bin/env python3
"""Exercise record and search-page progress on new, resumed and multipart runs."""

import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import upload
from test_upload import FakeCommonsUploader, FakeNlbClient


class TestProgress(unittest.TestCase):
	"""Counters describe catalog positions, including skipped records and multipart scans."""

	def run_pipeline(self, *, page: int = 1, total: int = 2, completed: bool = False) -> str:
		"""Run an isolated two-record result page with a selectable search position."""
		client = FakeNlbClient()
		records = client.fetch_search_page(1)['records']
		client.fetch_search_page = mock.Mock(side_effect=lambda **kwargs: {
			'total_results': total, 'records': records if kwargs['page'] == page else [],
		})
		with tempfile.TemporaryDirectory() as tmp_dir:
			root = Path(tmp_dir)
			state = upload.default_state()
			state['current_page'] = page
			if completed:
				state['items']['BY-NLB-rr111:1001'] = {'status': 'uploaded'}
			upload.save_state(root / 'state.json', state)
			with mock.patch('upload.SEARCH_PAGE_SIZE', 2), mock.patch(
				'upload.check_commons_by_hash', return_value=('a' * 40, []),
			), mock.patch('sys.stdout', io.StringIO()) as output:
				upload.iterate_and_upload(client, FakeCommonsUploader(), root / 'state.json', root / 'staging')
				return output.getvalue()

	def test_record_totals_are_not_pdf_totals(self) -> None:
		"""Two files of one record share its position while pages use the result count."""
		output = self.run_pipeline()
		self.assertIn('[record 1/2; page 1/1] [BY-NLB-rr111]', output)
		self.assertIn('[record 2/2; page 1/1] [BY-NLB-rr222:2001]', output)
		self.assertIn('[record 2/2; page 1/1] [BY-NLB-rr222:2002]', output)
		self.assertNotIn('record 3/2', output)

	def test_resume_uses_search_position_and_keeps_completed_records_in_progress(self) -> None:
		"""A resumed page starts at its catalog offset, even when its first scan is done."""
		output = self.run_pipeline(page=4, total=8, completed=True)
		self.assertIn('[record 7/8; page 4/4] [BY-NLB-rr111]', output)
		self.assertIn('[record 8/8; page 4/4] [BY-NLB-rr222:2001]', output)
		self.assertNotIn('[record 1/8', output)

	def test_missing_total_does_not_invent_a_denominator(self) -> None:
		"""Some catalog responses lack totals; keep useful positions without /0."""
		output = self.run_pipeline(total=0)
		self.assertIn('[record 1; page 1] [BY-NLB-rr111]', output)
		self.assertNotIn('/0', output)

	def test_partial_final_page_counts_as_a_page(self) -> None:
		"""A nonmultiple of the result limit needs a final page in the denominator."""
		output = self.run_pipeline(total=3)
		self.assertIn('[record 2/3; page 1/2]', output)


if __name__ == '__main__':
	unittest.main()
