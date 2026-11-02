'''Automatically update published metadata with archive ownership and revision guards.'''

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from preservation import ArchiveStore
from test_metadata_refresh_merge import description


class TestAutomaticMetadataRefresh(unittest.TestCase):
	'''Simulate current Commons pages and durable archive snapshots without network writes.'''

	def setUp(self):
		'''Create one uploaded baseline, a fresh snapshot, and a mutable Commons page.'''
		self.temporary = tempfile.TemporaryDirectory()
		self.addCleanup(self.temporary.cleanup)
		self.root = Path(self.temporary.name)
		self.title = 'File:Original.pdf'
		self.old = description(date='1836', author='Catalog author') + '\n{{License}}\n[[Category:Original]]'
		self.new = description('Expanded source description', date='1837', author='New author') + '\n{{New license must not copy}}'
		self.snapshot(self.old, fresh=False)
		self.fresh = self.snapshot(self.new)
		self.page = {'title': self.title, 'text': self.old, 'revid': 1, 'sha1': 'a' * 40, 'size_bytes': 123}
		self.reader = mock.Mock(side_effect=lambda title: self.page.copy())
		def edit(entry):
			'''Model one successful revision-protected Commons edit.'''
			self.assertEqual(entry['baserevid'], self.page['revid'])
			self.page.update(text=entry['new_text'], revid=self.page['revid'] + 1)
			return True
		self.writer = mock.Mock(side_effect=edit)

	def snapshot(self, text, *, fresh=True, key='BY-NLB-one', eligible=True, actual_upload=True):
		'''Write immutable generated descriptions, distinguishing publication from copied state.'''
		archive = ArchiveStore(self.root)
		archive.save_record(key, {}, {})
		archive.save_scan(key, '1', text, self.title, scan_identity={'eligible': eligible})
		entry = {'status': 'uploaded', 'sha1': 'a' * 40, 'size_bytes': 123, 'verification': {'page_id': 1},
			'updated_at': datetime.now(timezone.utc).isoformat() if actual_upload and not fresh else '2000-01-01T00:00:00+00:00'}
		if fresh:
			entry['metadata_refresh'] = {'status': 'metadata_ready' if eligible else 'metadata_review'}
		archive.record_result(key, '1', entry)
		return archive.snapshots[key]

	def run_refresh(self, snapshots=None, **kwargs):
		'''Use only explicitly selected fresh snapshots and mocked Commons reads/writes.'''
		from metadata_refresh import apply_uploaded_metadata
		return apply_uploaded_metadata(self.root, snapshots or [self.fresh], fetch_page=self.reader, edit_page=self.writer, **kwargs)

	def test_automatic_update_and_repeat_refresh_preserve_human_fields_and_outside_text(self):
		'''Apply once, retain human author/category/license, and use a receipt for later updates.'''
		self.page['text'] = self.old.replace('Catalog author', 'Human author')
		result = self.run_refresh()
		self.assertEqual(result['counts']['metadata_updated'], 1)
		for text in ('Expanded source description', '1837', 'Human author', '{{License}}', '[[Category:Original]]'):
			self.assertIn(text, self.page['text'])
		self.assertNotIn('New license', self.page['text'])
		self.assertEqual(self.run_refresh()['counts']['metadata_unchanged'], 1)
		self.writer.assert_called_once()
		self.fresh = self.snapshot(self.new.replace('Expanded source description', 'More catalog metadata'))
		self.assertEqual(self.run_refresh()['counts']['metadata_updated'], 1)
		self.assertIn('Human author', self.page['text'])
		self.assertIn('More catalog metadata', self.page['text'])

	def test_human_description_conflict_skips_without_manual_approval(self):
		'''Unrelated pages can continue while a human-corrected description remains untouched.'''
		self.page['text'] = self.old.replace('Original catalog description', 'Human corrected description')
		self.assertEqual(self.run_refresh()['counts']['metadata_conflict'], 1)
		self.writer.assert_not_called()

	def test_dry_run_produces_preview_without_receipts_or_edits(self):
		'''Preview is optional and cannot claim that a generated description was published.'''
		result = self.run_refresh(dry_run=True)
		self.assertEqual(result['counts']['metadata_preview'], 1)
		self.assertIn('Expanded source description', result['items'][0]['diff'])
		self.assertFalse(list((self.root / 'metadata-refresh/pages').glob('*.json')))
		self.writer.assert_not_called()

	def test_hash_size_or_revision_changes_block_writes(self):
		'''Check current source identity and repeat the check immediately before publishing.'''
		for field, value in (('sha1', 'b' * 40), ('size_bytes', 124), ('revid', 2)):
			with self.subTest(field=field):
				self.reader.side_effect = [self.page.copy(), {**self.page, field: value}]
				result = self.run_refresh()
				self.assertEqual(result['counts']['metadata_conflict'], 1)
				self.writer.assert_not_called()

	def test_failed_fresh_snapshot_does_not_reuse_an_older_eligible_description(self):
		'''Only this run's complete, eligible snapshots may supply new page metadata.'''
		failed = self.snapshot(self.new, eligible=False)
		result = self.run_refresh([failed])
		self.assertEqual(result['counts']['metadata_source_skipped'], 1)
		self.reader.assert_not_called()
		self.writer.assert_not_called()

	def test_copied_uploaded_status_is_not_a_publication_baseline(self):
		'''A refresh with inherited uploaded status cannot take ownership of human content.'''
		# Neither the fresh nor a copied historical snapshot actually published this text.
		fake = self.snapshot(description('Human corrected description'), fresh=False, actual_upload=False)
		self.page['text'] = description('Human corrected description')
		self.assertEqual(self.run_refresh()['counts']['metadata_conflict'], 1)
		self.writer.assert_not_called()

	def test_corrected_volume_uses_unique_valid_alternative_with_the_same_binary(self):
		'''An original wrong owner can be replaced by a newly scoped catalog association.'''
		blocked = self.snapshot(self.new, eligible=False)
		correct = self.snapshot(description('Correct volume three', date='1645'), key='BY-NLB-correct')
		result = self.run_refresh([blocked, correct])
		self.assertEqual(result['counts']['metadata_updated'], 1)
		self.assertIn('Correct volume three', self.page['text'])

	def test_competing_new_primary_records_remain_pending(self):
		'''No archive ordering rule may choose between conflicting replacement volumes.'''
		one = self.snapshot(description('Volume two'), key='BY-NLB-second')
		two = self.snapshot(description('Volume three'), key='BY-NLB-third')
		self.assertEqual(self.run_refresh([one, two])['counts']['metadata_conflict'], 1)
		self.writer.assert_not_called()

	def test_crash_after_edit_resumes_without_reapplying_or_losing_ownership(self):
		'''A pending receipt can recognize a successful edit even if the success write failed.'''
		from metadata_refresh import atomic_json
		def fail_success(path, value):
			'''Simulate a crash after the server accepted the new text.'''
			if value.get('status') == 'applied':
				raise OSError('disk unavailable')
			return atomic_json(path, value)
		with mock.patch('metadata_refresh.atomic_json', side_effect=fail_success):
			self.assertEqual(self.run_refresh()['counts']['metadata_edit_failed'], 1)
		self.assertEqual(self.run_refresh()['counts']['metadata_unchanged'], 1)
		self.writer.assert_called_once()


	def test_unowned_pages_receive_resumable_blocks_without_replacing_human_information(self):
		'''Duplicate Commons files keep their primary description and receive source evidence.'''
		import json
		for path in self.root.glob('records/*/snapshots/*/manifest.json'):
			manifest = json.loads(path.read_text())
			manifest['files']['1']['status'] = 'duplicate_sha1'
			path.write_text(json.dumps(manifest))
		self.page['text'] = description('Human description', date='Human date') + '\n{{License}}'
		original = self.page['text']
		self.assertEqual(self.run_refresh()['counts']['metadata_updated'], 1)
		self.assertTrue(self.page['text'].startswith(original))
		self.assertIn('NLB-METADATA:BY-NLB-one:1:BEGIN', self.page['text'])
		self.fresh = self.snapshot(self.new.replace('Expanded source description', 'Later source metadata'))
		self.assertEqual(self.run_refresh()['counts']['metadata_updated'], 1)
		self.assertTrue(self.page['text'].startswith(original))
		self.assertIn('Later source metadata', self.page['text'])
		self.page['text'] = self.page['text'].replace('Later source metadata', 'Human corrected block')
		self.fresh = self.snapshot(self.new.replace('Expanded source description', 'Newest source metadata'))
		self.assertEqual(self.run_refresh()['counts']['metadata_conflict'], 1)

	def test_invalid_source_hash_and_archive_object_are_reported_without_api_calls(self):
		'''Unverifiable inputs cannot become automatic edit targets.'''
		import json
		for field, value in (('sha1', 'bad'), ('size_bytes', 0), ('description', {'path': '../outside', 'sha256': 'bad'})):
			with self.subTest(field=field):
				fresh = self.snapshot(self.new)
				manifest = json.loads(fresh.read_text())
				manifest['files']['1'][field] = value
				fresh.write_text(json.dumps(manifest))
				self.assertEqual(self.run_refresh([fresh])['counts']['metadata_source_skipped'], 1)
		self.reader.assert_not_called()
		self.writer.assert_not_called()

	def test_commons_read_and_edit_failures_are_reported(self):
		'''A failed current identity or rejected write remains resumable rather than successful.'''
		self.reader.side_effect = ValueError('File unavailable')
		self.assertEqual(self.run_refresh()['counts']['metadata_conflict'], 1)
		self.writer.assert_not_called()
		self.reader.side_effect = lambda title: self.page.copy()
		self.writer.side_effect = None
		self.writer.return_value = False
		self.assertEqual(self.run_refresh()['counts']['metadata_edit_failed'], 1)

	def test_new_fields_preserve_human_removal_after_a_successful_receipt(self):
		'''Receipts store generated intent so retained human corrections never become owned.'''
		self.page['text'] = self.old.replace('Catalog author', 'Human author')
		result = self.run_refresh()
		self.assertEqual(result['items'][0]['preserved_fields'], ['author'])
		self.fresh = self.snapshot(self.new.replace('New author', 'Another catalog author'))
		self.assertEqual(self.run_refresh()['counts']['metadata_unchanged'], 1)
		self.assertIn('Human author', self.page['text'])


	def test_moved_file_retains_ownership_through_the_recorded_redirect(self):
		'''Canonical Commons titles may differ from older saved titles without losing ownership.'''
		self.page['title'] = 'File:Human renamed file.pdf'
		self.assertEqual(self.run_refresh()['counts']['metadata_updated'], 1)
		self.assertNotIn('Original catalog description', self.page['text'])
		self.assertIn('Expanded source description', self.page['text'])
		self.assertNotIn('NLB-METADATA', self.page['text'])

	def test_authentication_failure_stops_after_checkpointing_the_pending_edit(self):
		'''A broken login must not be retried for every page in a large refresh.'''
		class AuthenticationError(Exception):
			'''Model the MediaWiki assertion error independently of a configured account.'''
			code = 'assertuserfailed'
		self.writer.side_effect = AuthenticationError('Login expired')
		with self.assertRaises(AuthenticationError):
			self.run_refresh()
		self.writer.assert_called_once()


	def test_legacy_additive_block_refreshes_with_the_primary_description(self):
		'''A prior reconciliation section must not retain stale facts beside refreshed fields.'''
		from reconcile_metadata import _managed_block, _read_description
		import json
		old_snapshot = sorted(self.root.glob('records/*/snapshots/*/manifest.json'))[0]
		item = json.loads(old_snapshot.read_text())['files']['1']
		old_block = _managed_block('', 'BY-NLB-one', '1', _read_description(self.root, item['description']))
		self.page['text'] += '\n' + old_block
		self.assertEqual(self.run_refresh()['counts']['metadata_updated'], 1)
		self.assertNotIn('Original catalog description', self.page['text'])
		self.assertEqual(self.page['text'].count('Expanded source description'), 2)

	def test_human_changes_to_legacy_blocks_are_not_bootstrapped_as_owned(self):
		'''Archive ownership requires the complete prior block, not merely recognizable markers.'''
		from test_metadata_refresh_merge import block
		self.page['text'] += '\n' + block('Human commentary')
		self.assertEqual(self.run_refresh()['counts']['metadata_conflict'], 1)
		self.writer.assert_not_called()


class TestRefreshReads(unittest.TestCase):
	'''Transient metadata reads share a bounded retry budget; TLS/access errors propagate.'''

	@mock.patch('metadata_refresh.time.sleep')
	def test_read_retry_budget_and_permanent_failures(self, sleep):
		'''Three attempts recover transient failures without restarting authentication errors.'''
		import requests
		from metadata_refresh import read_refresh_page
		with mock.patch('metadata_refresh.read_commons_page', side_effect=[requests.ReadTimeout('slow'), {'title': 'File:Book.pdf'}]) as reader:
			self.assertEqual(read_refresh_page('File:Book.pdf')['title'], 'File:Book.pdf')
			sleep.assert_called_once_with(2)
		with mock.patch('metadata_refresh.read_commons_page', side_effect=ValueError('Incomplete')) as reader:
			with self.assertRaises(ValueError):
				read_refresh_page('File:Book.pdf')
			self.assertEqual(reader.call_count, 3)
		for error in (requests.exceptions.SSLError('TLS'), requests.HTTPError('Forbidden', response=mock.Mock(status_code=403))):
			with self.subTest(error=type(error).__name__), mock.patch('metadata_refresh.read_commons_page', side_effect=error) as reader:
				with self.assertRaises(type(error)):
					read_refresh_page('File:Book.pdf')
				self.assertEqual(reader.call_count, 1)
