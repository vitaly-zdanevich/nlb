'''Exercise fresh catalog discovery and explicit, integrity-checked continuation.'''

import copy
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from catalog_requests import CatalogRetriesExhausted
from preservation import ArchiveStore, atomic_json, file_integrity
from refresh_checkpoint import RefreshCheckpoint
from test_refresh_uploaded_metadata import RefreshCatalog, association
from test_upload import FakeCommonsUploader, FakeNlbClient
import upload


class TestContinue(unittest.TestCase):
	'''Retain upload identity while making resumption an explicit operator choice.'''

	def setUp(self):
		'''Keep every test's state, archive and output isolated from production.'''
		temporary = tempfile.TemporaryDirectory()
		self.addCleanup(temporary.cleanup)
		self.root = Path(temporary.name)
		self.state = self.root / 'state.json'
		self.addCleanup(mock.patch.stopall)
		mock.patch('sys.stdout', io.StringIO()).start()

	def test_default_discovers_earlier_new_records_without_reuploading_completed_files(self):
		'''An old cursor cannot hide newly inserted records from a fresh catalog scan.'''
		state = upload.default_state()
		state['current_page'] = 4
		for fid in ('2001', '2002'):
			state['items']['BY-NLB-rr222:' + fid] = association(verification={'sha1': 'a' * 40})
		upload.save_state(self.state, state)
		client, uploader = FakeNlbClient(), FakeCommonsUploader()
		with mock.patch('upload.check_commons_by_hash', side_effect=lambda path, **_: (upload.calculate_sha1(path), [])):
			counts = upload.iterate_and_upload(client, uploader, self.state, self.root / 'staging')
		self.assertEqual(counts['uploaded'], 1)
		self.assertEqual(client.downloaded_file_ids, ['1001'])
		self.assertEqual(len(uploader.uploads), 1)
		self.assertEqual(len(json.loads(self.state.read_text())['items']), 3)

	def test_continue_uses_saved_page_and_earliest_pending_page(self):
		'''Explicit continuation repeats its boundary page, preserving pending work.'''
		for pending, expected in ((None, 4), (2, 2)):
			with self.subTest(pending=pending):
				state = upload.default_state()
				state['current_page'] = 4
				if pending:
					state['retry_from_page'] = pending
				upload.save_state(self.state, state)
				client = FakeNlbClient()
				client.fetch_search_page = mock.Mock(return_value={'records': [], 'total_results': 0})
				upload.iterate_and_upload(client, FakeCommonsUploader(), self.state, self.root / 'staging', continue_run=True)
				self.assertEqual(client.fetch_search_page.call_args.kwargs['page'], expected)

	def test_new_access_restriction_keeps_historical_upload_identity(self):
		'''A later library access decision cannot erase an already published file's evidence.'''
		state = upload.default_state()
		previous = association(verification={'sha1': 'a' * 40})
		state['items']['BY-NLB-rr111:1001'] = previous
		upload.save_state(self.state, state)
		client = FakeNlbClient()
		client.archive = ArchiveStore(self.root / 'archive')
		client.check_doc_access = mock.Mock(return_value={'publicAccess': False})
		upload.iterate_and_upload(client, FakeCommonsUploader(), self.state, self.root / 'staging')
		item = json.loads(self.state.read_text())['items']['BY-NLB-rr111:1001']
		self.assertEqual(item['status'], 'restricted')
		self.assertEqual(item['previous_status'], 'uploaded')
		for key in ('commons_title', 'sha1', 'size_bytes', 'verification'):
			self.assertEqual(item[key], previous[key])
		self.assertEqual(client.archive.manifests['BY-NLB-rr111']['files']['1001'], item)
		self.assertEqual(client.downloaded_file_ids, [])

	def write_metadata_state(self):
		'''Select two independent source records, each with a proven Commons association.'''
		atomic_json(self.state, {'items': {'BY-NLB-one:1': association(), 'BY-NLB-two:2': association()}})
		return {
			'BY-NLB-one': ({'title': 'First', 'file_ids': ['1']}, {'date': '1800'}),
			'BY-NLB-two': ({'title': 'Second', 'file_ids': ['2']}, {'date': '1800'}),
		}

	def collect(self, client, **options):
		'''Run source collection without PDF transfer, upload or Commons editing.'''
		return upload.refresh_uploaded_metadata(client, self.state, self.root / 'staging', **options)

	def test_interrupted_collection_reuses_only_complete_records_with_continue(self):
		'''A partial record must be recollected, while a completed one keeps its snapshot.'''
		records = self.write_metadata_state()
		before = self.state.read_bytes()
		first = RefreshCatalog(self.root / 'archive', records)
		first.fetch_record_metadata.side_effect = lambda rid: copy.deepcopy(records[rid]) if rid == 'BY-NLB-one' else (_ for _ in ()).throw(KeyboardInterrupt())
		with self.assertRaises(KeyboardInterrupt):
			self.collect(first)
		snapshot = first.archive.snapshots['BY-NLB-one']
		second = RefreshCatalog(self.root / 'archive', records)
		counts = self.collect(second, continue_run=True)
		second.fetch_record_metadata.assert_called_once_with('BY-NLB-two')
		self.assertEqual(counts['metadata_ready'], 2)
		self.assertEqual(second.archive.snapshots['BY-NLB-one'], snapshot.resolve())
		self.assertEqual(self.state.read_bytes(), before)
		third = RefreshCatalog(self.root / 'archive', records)
		self.collect(third)
		self.assertEqual(third.fetch_record_metadata.call_count, 2)
		self.assertNotEqual(third.archive.snapshots['BY-NLB-one'], snapshot)

	def test_continue_retries_failed_records_and_completed_runs_start_fresh(self):
		'''An exhausted transient fetch cannot become a reusable successful boundary.'''
		records = self.write_metadata_state()
		first = RefreshCatalog(self.root / 'archive', records)
		first.fetch_record_metadata.side_effect = lambda rid: copy.deepcopy(records[rid]) if rid == 'BY-NLB-one' else (_ for _ in ()).throw(CatalogRetriesExhausted(TimeoutError('offline'), 5))
		self.assertEqual(self.collect(first)['metadata_failed'], 1)
		second = RefreshCatalog(self.root / 'archive', records)
		self.assertEqual(self.collect(second, continue_run=True)['metadata_ready'], 2)
		second.fetch_record_metadata.assert_called_once_with('BY-NLB-two')
		second.refresh_checkpoint.complete()
		third = RefreshCatalog(self.root / 'archive', records)
		self.collect(third, continue_run=True)
		self.assertEqual(third.fetch_record_metadata.call_count, 2)

	def test_changed_inputs_code_or_expired_collection_are_not_reused(self):
		'''Resume cannot silently publish metadata generated with stale or different inputs.'''
		for change in ('selection', 'code', 'age'):
			with self.subTest(change=change):
				records = self.write_metadata_state()
				first = RefreshCatalog(self.root / change, records)
				self.collect(first)
				if change == 'selection':
					state = json.loads(self.state.read_text())
					state['items']['BY-NLB-one:1']['commons_title'] = 'File:Different association.pdf'
					atomic_json(self.state, state)
				elif change == 'age':
					first.refresh_checkpoint.data['started_at'] = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
					atomic_json(first.refresh_checkpoint.path, first.refresh_checkpoint.data)
				second = RefreshCatalog(self.root / change, records)
				if change == 'code':
					second.archive.tool['sha256'] = 'changed'
				self.collect(second, continue_run=True)
				self.assertEqual(second.fetch_record_metadata.call_count, 2)

	def test_corrupt_snapshots_and_malformed_counters_are_recollected(self):
		'''Corruption must not skip records, escape the archive or poison totals.'''
		for mutation in ('checksum', 'missing', 'shape', 'path', 'counter_type', 'counter_negative', 'counter_unknown'):
			with self.subTest(mutation=mutation):
				records = self.write_metadata_state()
				client = RefreshCatalog(self.root / mutation, records)
				self.collect(client)
				checkpoint = client.refresh_checkpoint
				entry = checkpoint.data['records']['BY-NLB-one']
				path = client.archive.snapshots['BY-NLB-one']
				if mutation == 'missing':
					path.unlink()
				elif mutation in ('checksum', 'shape'):
					path.write_text('{"record_id":"BY-NLB-one","files":null}')
					if mutation == 'shape':
						entry['sha256'] = file_integrity(path)['sha256']
				elif mutation == 'path':
					entry['path'] = str(self.state)
				elif mutation == 'counter_type':
					entry['counts'] = []
				elif mutation == 'counter_negative':
					entry['counts']['metadata_ready'] = -1
				else:
					entry['counts']['unexpected'] = 1
				self.assertIsNone(checkpoint.restore('BY-NLB-one'))

	def test_report_collisions_are_rejected_before_any_work_in_every_mode(self):
		'''Preview and normal modes cannot replace state, source objects or receipts.'''
		for mode in ([], ['--dry-run'], ['--metadata-only'], ['--refresh-uploaded-metadata']):
			for target in ('state.json', 'archive/objects/test.bin', 'archive/metadata-refresh/collection.json'):
				with self.subTest(mode=mode, target=target), mock.patch('upload.NlbClient') as client, mock.patch('sys.stderr', io.StringIO()):
					with self.assertRaises(SystemExit) as error:
						upload.main([*mode, '--state-file', str(self.state), '--report', str(self.root / target)])
					self.assertEqual(error.exception.code, 2)
					client.assert_not_called()

	def test_refresh_outputs_cannot_overwrite_an_unusually_named_state_file(self):
		'''Diagnostics and checkpoints must preserve state even when the archive is damaged.'''
		archive = self.root / 'archive'
		for name in ('collection.json', 'archive-issues.json', 'report.json'):
			with self.subTest(name=name):
				state = archive / 'metadata-refresh' / name
				atomic_json(state, {'items': {'BY-NLB-one:1': association()}})
				before = state.read_bytes()
				with mock.patch('upload.NlbClient') as client, mock.patch('metadata_refresh.inspect_refresh_archive', return_value=[{'error': 'Missing archived description'}]) as preflight, mock.patch('sys.stderr', io.StringIO()):
					with self.assertRaises(SystemExit) as error:
						upload.main(['--refresh-uploaded-metadata', '--state-file', str(state), '--archive-dir', str(archive)])
					self.assertEqual(error.exception.code, 2)
					self.assertEqual(state.read_bytes(), before)
					client.assert_not_called()
					preflight.assert_not_called()


if __name__ == '__main__':
	unittest.main()
