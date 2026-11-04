'''Automatically update published metadata with archive ownership and revision guards.'''

from datetime import datetime, timezone
import json
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
		for field, value in (('sha1', 'b' * 40), ('size_bytes', 124), ('revid', 2), ('page_id', 99)):
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

	def test_page_id_receipt_survives_moves_after_successful_refreshes(self):
		'''A renamed page retains generated ownership and human author changes across runs.'''
		self.page['page_id'] = 42
		self.page['text'] = self.old.replace('Catalog author', 'Human author')
		self.assertEqual(self.run_refresh()['counts']['metadata_updated'], 1)
		for number in (2, 3):
			self.page['title'] = f'File:Renamed {number}.pdf'
			self.fresh = self.snapshot(self.new.replace('Expanded source description', f'Catalog revision {number}'))
			self.assertEqual(self.run_refresh()['counts']['metadata_updated'], 1)
			self.assertIn(f'Catalog revision {number}', self.page['text'])
			self.assertIn('Human author', self.page['text'])
		self.assertEqual([path.name for path in (self.root / 'metadata-refresh/pages').glob('*.json')], ['page-42.json'])
		self.page['text'] = self.page['text'].replace('Catalog revision 3', 'Human correction')
		self.fresh = self.snapshot(self.new.replace('Expanded source description', 'Catalog revision 4'))
		self.assertEqual(self.run_refresh()['counts']['metadata_conflict'], 1)
		self.assertIn('Human correction', self.page['text'])

	def test_legacy_receipt_migrates_only_after_its_old_title_resolves_to_the_page(self):
		'''An intermediate title can own a legacy receipt even when state records an older name.'''
		self.page['title'] = 'File:Intermediate name.pdf'
		self.assertEqual(self.run_refresh()['counts']['metadata_updated'], 1)
		self.page.update(title='File:Latest name.pdf', page_id=42)
		self.fresh = self.snapshot(self.new.replace('Expanded source description', 'Later catalog metadata'))
		self.reader.reset_mock()
		self.assertEqual(self.run_refresh()['counts']['metadata_updated'], 1)
		self.assertIn(mock.call('File:Intermediate name.pdf'), self.reader.call_args_list)
		self.assertTrue((self.root / 'metadata-refresh/pages/page-42.json').exists())
		self.assertIn('Later catalog metadata', self.page['text'])

	def test_pending_legacy_receipt_recovers_after_a_move_without_another_edit(self):
		'''An interrupted successful edit can migrate its receipt and retain stable page identity.'''
		from metadata_refresh import atomic_json
		def fail_success(path, value):
			'''Leave a pending title receipt after the mocked server has accepted the edit.'''
			if value.get('status') == 'applied':
				raise OSError('Interrupted receipt write')
			return atomic_json(path, value)
		with mock.patch('metadata_refresh.atomic_json', side_effect=fail_success):
			self.assertEqual(self.run_refresh()['counts']['metadata_edit_failed'], 1)
		self.page.update(title='File:Moved after interrupted edit.pdf', page_id=42)
		self.assertEqual(self.run_refresh()['counts']['metadata_unchanged'], 1)
		self.writer.assert_called_once()
		receipt = json.loads((self.root / 'metadata-refresh/pages/page-42.json').read_text())
		self.assertEqual(receipt['status'], 'applied')
		self.assertEqual(receipt['page_id'], 42)
		self.assertEqual(self.run_refresh()['counts']['metadata_unchanged'], 1)

	def test_same_binary_on_another_page_cannot_adopt_a_stable_receipt(self):
		'''Binary deduplication never grants ownership of a distinct Commons page.'''
		self.page['page_id'] = 42
		self.assertEqual(self.run_refresh()['counts']['metadata_updated'], 1)
		self.title = 'File:Separate copy.pdf'
		self.fresh = self.snapshot(self.new, key='BY-NLB-other')
		self.page.update(title=self.title, page_id=99, text=description('Human description'))
		self.assertEqual(self.run_refresh()['counts']['metadata_updated'], 1)
		self.assertTrue(self.page['text'].startswith(description('Human description')))
		self.assertIn('NLB-METADATA:BY-NLB-other:1', self.page['text'])

	def test_unresolved_legacy_receipt_does_not_reset_page_ownership(self):
		'''A failed redirect check leaves the page pending instead of guessing a new baseline.'''
		self.page['title'] = 'File:Intermediate name.pdf'
		self.assertEqual(self.run_refresh()['counts']['metadata_updated'], 1)
		self.page.update(title='File:Latest name.pdf', page_id=42)
		self.fresh = self.snapshot(self.new.replace('Expanded source description', 'Later metadata'))
		def resolve(title):
			'''Model a successful original redirect but unavailable intermediate-title lookup.'''
			if title == 'File:Intermediate name.pdf':
				raise ValueError('Old title is unavailable')
			return self.page.copy()
		self.reader.side_effect = resolve
		self.writer.reset_mock()
		result = self.run_refresh()
		self.assertEqual(result['counts']['metadata_conflict'], 1)
		self.assertIn('Cannot verify prior metadata receipt', result['items'][0]['error'])
		self.writer.assert_not_called()

	def test_damaged_receipt_does_not_fall_back_to_an_older_baseline(self):
		'''Corrupt ownership evidence is a per-page conflict, not permission to bootstrap again.'''
		self.page['page_id'] = 42
		self.assertEqual(self.run_refresh()['counts']['metadata_updated'], 1)
		(self.root / 'metadata-refresh/pages/page-42.json').write_text('{broken')
		self.fresh = self.snapshot(self.new.replace('Expanded source description', 'Later metadata'))
		self.writer.reset_mock()
		self.assertEqual(self.run_refresh()['counts']['metadata_conflict'], 1)
		self.writer.assert_not_called()

	def test_malformed_receipt_fields_fail_closed_without_editing(self):
		'''Broken identity, ownership, hashes and timestamps cannot authorize a new edit.'''
		self.page['page_id'] = 42
		self.assertEqual(self.run_refresh()['counts']['metadata_updated'], 1)
		path = self.root / 'metadata-refresh/pages/page-42.json'
		receipt = json.loads(path.read_text())
		for field, value in (
			('sha1', 'invalid'), ('sha1', 'b' * 40), ('page_id', True), ('page_id', 43),
			('title', None), ('generated_text', 'Modified without checksum'),
			('expected_text_sha256', 'invalid'), ('updated_at', '2026-01-01T00:00:00'),
			('updated_at', None), ('aliases', 'File:Not a list.pdf'), ('aliases', [None]),
			('primary_key', []), ('mode', 'unknown'),
		):
			with self.subTest(field=field, value=value):
				path.write_text(json.dumps({**receipt, field: value}))
				self.writer.reset_mock()
				result = self.run_refresh()
				self.assertEqual(result['counts']['metadata_conflict'], 1)
				self.writer.assert_not_called()
		path.write_text('[]')
		self.assertEqual(self.run_refresh()['counts']['metadata_conflict'], 1)
		path.write_text(json.dumps({key: value for key, value in receipt.items() if key != 'page_id'}))
		self.assertEqual(self.run_refresh()['counts']['metadata_conflict'], 1)

	def test_corrupt_unrelated_receipt_does_not_block_an_independent_page(self):
		'''A damaged receipt with another identity remains unrelated to the selected target.'''
		path = self.root / 'metadata-refresh/pages/page-999.json'
		path.parent.mkdir(parents=True)
		path.write_text('{broken')
		self.page['page_id'] = 42
		self.assertEqual(self.run_refresh()['counts']['metadata_updated'], 1)
		self.assertTrue(path.exists())

	def test_legacy_receipt_for_another_page_with_identical_bytes_is_not_an_owner(self):
		'''A legacy receipt must follow a real redirect; matching file hashes are insufficient.'''
		self.assertEqual(self.run_refresh()['counts']['metadata_updated'], 1)
		original_page = self.page.copy()
		self.title = 'File:Separate copy.pdf'
		self.fresh = self.snapshot(self.new, key='BY-NLB-other')
		self.page.update(title=self.title, page_id=99, text=description('Human description'))
		self.reader.side_effect = lambda title: (original_page if title == original_page['title'] else self.page).copy()
		self.assertEqual(self.run_refresh()['counts']['metadata_updated'], 1)
		self.assertTrue(self.page['text'].startswith(description('Human description')))
		self.assertIn('NLB-METADATA:BY-NLB-other:1', self.page['text'])

	def test_preflight_reports_corrupt_descriptions_and_manifest_shapes(self):
		'''Read-only preflight identifies ownership damage before expensive source reads.'''
		from metadata_refresh import inspect_refresh_archive
		old_snapshot = sorted(self.root.glob('records/BY-NLB-one/snapshots/*/manifest.json'))[0]
		original = json.loads(old_snapshot.read_text())
		cases = (
			{'record_id': 'BY-NLB-wrong', 'files': {}},
			{**original, 'files': []},
			{**original, 'files': {'1': None}},
			{**original, 'files': {'1': {**original['files']['1'], 'duplicates': None}}},
			{**original, 'files': {'1': {**original['files']['1'], 'description': {'path': '../outside', 'sha256': 'bad'}}}},
		)
		for malformed in cases:
			with self.subTest(manifest=malformed):
				body = json.dumps(malformed)
				old_snapshot.write_text(body)
				issues = inspect_refresh_archive(self.root)
				self.assertEqual(len(issues), 1)
				self.assertEqual(issues[0]['record_id'], 'BY-NLB-one')
				self.assertEqual(old_snapshot.read_text(), body)
				self.assertEqual(self.run_refresh()['counts']['metadata_conflict'], 1)
		old_snapshot.write_text(json.dumps(original))
		(self.root / original['files']['1']['description']['path']).write_text('Damaged object')
		issues = inspect_refresh_archive(self.root)
		self.assertIn('checksum mismatch', issues[0]['error'])
		self.assertEqual(self.run_refresh()['counts']['metadata_conflict'], 1)
		self.writer.assert_not_called()

	def test_missing_publication_object_blocks_only_related_pages(self):
		'''Independent source/page groups continue while damaged publication evidence is reported.'''
		from metadata_refresh import inspect_refresh_archive
		old_snapshot = sorted(self.root.glob('records/BY-NLB-one/snapshots/*/manifest.json'))[0]
		old_item = json.loads(old_snapshot.read_text())['files']['1']
		(self.root / old_item['description']['path']).unlink()
		other_title = 'File:Independent.pdf'
		other_old, other_new = description('Independent original'), description('Independent updated')
		other_page = {'title': other_title, 'text': other_old, 'revid': 1, 'sha1': 'b' * 40, 'size_bytes': 456, 'page_id': 77}
		for fresh in (False, True):
			archive = ArchiveStore(self.root)
			archive.save_record('BY-NLB-independent', {}, {})
			archive.save_scan('BY-NLB-independent', '2', other_new if fresh else other_old, other_title, scan_identity={'eligible': True})
			entry = {'status': 'uploaded', 'sha1': other_page['sha1'], 'size_bytes': 456, 'verification': {'page_id': 77}, 'updated_at': datetime.now(timezone.utc).isoformat()}
			if fresh:
				entry['metadata_refresh'] = {'status': 'metadata_ready'}
			archive.record_result('BY-NLB-independent', '2', entry)
		other_snapshot = archive.snapshots['BY-NLB-independent']
		self.reader.side_effect = lambda title: (other_page if title == other_title else self.page).copy()
		self.writer.side_effect = lambda entry: other_page.update(text=entry['new_text']) or True
		issues = inspect_refresh_archive(self.root)
		self.assertEqual(len(issues), 1)
		self.assertEqual(issues[0]['key'], 'BY-NLB-one:1')
		result = self.run_refresh([self.fresh, other_snapshot])
		self.assertEqual(result['counts']['metadata_conflict'], 1)
		self.assertEqual(result['counts']['metadata_updated'], 1)
		self.assertEqual(result['archive_issues'], issues)
		self.assertEqual(self.page['text'], self.old)
		self.assertEqual(other_page['text'], other_new)
		self.assertEqual(self.writer.call_args.args[0]['title'], other_title)

	def test_malformed_historical_manifest_is_isolated_by_record(self):
		'''An unreadable peer snapshot cannot abort unrelated pages or become ownership evidence.'''
		broken = self.root / 'records/BY-NLB-unrelated/snapshots/old/manifest.json'
		broken.parent.mkdir(parents=True)
		broken.write_text('{broken')
		result = self.run_refresh()
		self.assertEqual(result['counts']['metadata_updated'], 1)
		self.assertEqual(result['archive_issues'][0]['record_id'], 'BY-NLB-unrelated')
		broken = self.root / 'records/BY-NLB-one/snapshots/broken/manifest.json'
		broken.parent.mkdir(parents=True)
		broken.write_text('{broken')
		self.fresh = self.snapshot(self.new.replace('Expanded source description', 'Later metadata'))
		self.writer.reset_mock()
		self.assertEqual(self.run_refresh()['counts']['metadata_conflict'], 1)
		self.writer.assert_not_called()

	def test_malformed_fresh_snapshot_is_reported_without_aborting_other_sources(self):
		'''Broken fresh inputs remain in the report without silently selecting old descriptions.'''
		broken = self.root / 'records/BY-NLB-unrelated/snapshots/broken/manifest.json'
		broken.parent.mkdir(parents=True)
		broken.write_text('{broken')
		result = self.run_refresh([broken, self.fresh])
		self.assertEqual(result['counts']['metadata_source_skipped'], 1)
		self.assertEqual(result['counts']['metadata_updated'], 1)

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

	def test_canonical_publication_baseline_and_receipt_preserve_raw_archive(self):
		'''Legacy decomposed Unicode and removed C0 characters do not create false conflicts.'''
		from metadata_refresh import read_description
		self.old = description('Cafe\u0301\x01 catalog', author='Rene\u0301')
		old_snapshot = self.snapshot(self.old, fresh=False)
		manifest = json.loads(old_snapshot.read_text())
		manifest['files']['1']['status'] = 'verification_failed'
		old_snapshot.write_text(json.dumps(manifest))
		# Remove only the unrelated original fixture so this is the actual publication baseline.
		for path in self.root.glob('records/BY-NLB-one/snapshots/*/manifest.json'):
			if path not in (old_snapshot, self.fresh):
				path.unlink()
		self.page['text'] = description('Café catalog', author='René')
		self.new = description('Updated Cafe\u0301 catalog', author='Another catalog author')
		self.fresh = self.snapshot(self.new)
		self.assertEqual(self.run_refresh()['counts']['metadata_updated'], 1)
		self.assertIn('Updated Café catalog', self.page['text'])
		self.assertEqual(read_description(self.root, manifest['files']['1']['description']), self.old)
		self.fresh = self.snapshot(self.new.replace('Updated', 'Latest'))
		self.assertEqual(self.run_refresh()['counts']['metadata_updated'], 1)
		self.assertIn('Latest Café catalog', self.page['text'])

	def test_normalized_legacy_additive_block_is_still_owned(self):
		'''Legacy block ownership compares server canonical text after exact archive verification.'''
		from reconcile_metadata import _managed_block, _read_description
		self.old = description('Cafe\u0301\x01 catalog')
		old_snapshot = self.snapshot(self.old, fresh=False)
		for path in self.root.glob('records/BY-NLB-one/snapshots/*/manifest.json'):
			if path not in (old_snapshot, self.fresh):
				path.unlink()
		item = json.loads(old_snapshot.read_text())['files']['1']
		old_block = _managed_block('', 'BY-NLB-one', '1', _read_description(self.root, item['description']))
		self.page['text'] = description('Café catalog') + '\n' + old_block.replace('Cafe\u0301\x01', 'Café')
		self.assertEqual(self.run_refresh()['counts']['metadata_updated'], 1)
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
