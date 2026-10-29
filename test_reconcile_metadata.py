'''Verify review plans preserve Commons edits and require matching file identity.'''

import hashlib
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest import mock

from preservation import ArchiveStore
from reconcile_metadata import apply_plan, edit_commons_page, generate_plan, main, read_commons_page


SHA1 = '1' * 40
ORIGINAL = 'Human description\n=={{int:license-header}}==\n{{PD-old-70}}\n[[Category:Original]]\n'


def page(title='File:Shared.pdf', **overrides):
	'''Return one anonymous Commons revision and its current binary identity.'''
	return {'title': title, 'revid': 123, 'text': ORIGINAL, 'sha1': SHA1, **overrides}


def archive_scan(root, record='BY-NLB-test', file_id='1', text='Catalog text', extra_fields='|date=1890\n', **details):
	'''Save the same immutable description and identity used by the upload pipeline.'''
	archive = ArchiveStore(root)
	archive.begin_record(record)
	archive.save_scan(record, file_id, '{{Information\n|description={{ru|1=' + text + '}}\n' + extra_fields + '}}\n{{New-license-must-not-copy}}\n[[Category:New-must-not-copy]]', 'File:Generated.pdf')
	archive.record_result(record, file_id, {'status': 'duplicate_sha1', 'sha1': SHA1, 'duplicates': ['File:Shared.pdf'], **details})


class TestReconcileMetadata(unittest.TestCase):
	'''Use immutable fixtures and mocked reads/writes; never contact Commons.'''

	def test_plan_preserves_original_page_and_uses_actual_duplicate_target(self):
		'''Only a managed metadata section is added; existing licenses survive unchanged.'''
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			archive_scan(root)
			reader = mock.Mock(side_effect=page)
			plan = generate_plan(root, reader)
			entry, = plan['entries']
			reader.assert_called_once_with('File:Shared.pdf')
			self.assertEqual(entry['baserevid'], 123)
			self.assertEqual(entry['sha1'], SHA1)
			self.assertIn('Catalog text', entry['new_text'])
			self.assertIn('{{PD-old-70}}\n[[Category:Original]]', entry['new_text'])
			self.assertNotIn('New-license', entry['new_text'])
			self.assertNotIn('New-must-not-copy', entry['new_text'])
			self.assertIn('+', entry['diff'])

	def test_shared_pdf_retains_separate_record_blocks(self):
		'''Two catalog records for one binary contribute to a single revision-safe edit.'''
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			archive_scan(root, record='BY-NLB-first', text='First record')
			archive_scan(root, record='BY-NLB-second', text='Second record')
			plan = generate_plan(root, page)
			entry, = plan['entries']
			self.assertIn('First record', entry['new_text'])
			self.assertIn('Second record', entry['new_text'])
			self.assertEqual(len(entry['records']), 2)

	def test_reconciliation_retains_new_date_author_and_source_evidence(self):
		'''Generated Information fields are added as evidence without replacing old fields.'''
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			archive_scan(root, extra_fields='|date=[1890?]\n|author=Named author\n|source=[https://elib.nlb.by/Record/test Catalog]\n|other_versions=Related scan\n|permission=License must not copy\n')
			entry, = generate_plan(root, page)['entries']
			for value in ('[1890?]', 'Named author', '[https://elib.nlb.by/Record/test Catalog]', 'Related scan'):
				self.assertIn(value, entry['new_text'])
			self.assertNotIn('License must not copy', entry['new_text'])
			self.assertIn('{{PD-old-70}}', entry['new_text'])

	def test_latest_snapshot_replaces_its_block_and_is_idempotent(self):
		'''A later catalog correction updates only that record's managed section.'''
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			archive_scan(root, text='Old metadata')
			old_text = generate_plan(root, page)['entries'][0]['new_text']
			archive_scan(root, text='Updated metadata')
			plan = generate_plan(root, lambda title: page(title, text=old_text))
			new_text = plan['entries'][0]['new_text']
			self.assertNotIn('Old metadata', new_text)
			self.assertEqual(new_text.count('Updated metadata'), 1)
			self.assertEqual(generate_plan(root, lambda title: page(title, text=new_text))['entries'], [])

	def test_incomplete_latest_capture_is_reported_without_reusing_stale_metadata(self):
		'''A failed refresh cannot silently remove a record from the review report.'''
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			archive_scan(root, text='Earlier complete metadata')
			archive = ArchiveStore(root)
			archive.begin_record('BY-NLB-test')
			archive.save_record('BY-NLB-test', {}, {})
			plan = generate_plan(root, page)
			self.assertEqual(plan['entries'], [])
			self.assertEqual(plan['skipped'][0]['record_id'], 'BY-NLB-test')
			self.assertIn('refresh', plan['skipped'][0]['reason'])
			self.assertEqual(len(list(root.glob('records/*/snapshots/*/manifest.json'))), 2)

	def test_missing_or_changed_identity_does_not_generate_edits(self):
		'''Metadata cannot attach to a same-title file without a matching archived hash.'''
		for details, actual in (({'sha1': None}, SHA1), ({}, '2' * 40), ({'status': 'metadata_ready'}, SHA1)):
			with self.subTest(details=details, actual=actual), tempfile.TemporaryDirectory() as directory:
				root = Path(directory)
				archive_scan(root, **details)
				plan = generate_plan(root, lambda title: page(title, sha1=actual))
				self.assertEqual(plan['entries'], [])
				self.assertTrue(plan['skipped'])

	def test_modified_archive_description_is_rejected(self):
		'''Content-addressed metadata must pass SHA-256 verification before planning.'''
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			archive_scan(root)
			wiki, = root.glob('objects/*.wiki')
			wiki.write_text('Tampered')
			with self.assertRaisesRegex(ValueError, 'integrity'):
				generate_plan(root, page)

	def test_apply_checks_revision_text_binary_and_plan_before_writing(self):
		'''A stale or edited plan cannot overwrite concurrent Commons changes.'''
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			archive_scan(root)
			plan = generate_plan(root, page)
			for current in (page(revid=124), page(text='Concurrent edit'), page(sha1='2' * 40)):
				with self.subTest(current=current):
					writer = mock.Mock()
					with self.assertRaises(ValueError):
						apply_plan(plan, lambda title: current, writer)
					writer.assert_not_called()
			writer = mock.Mock(return_value=True)
			self.assertEqual(apply_plan(plan, page, writer), 1)
			writer.assert_called_once_with(plan['entries'][0])
			plan['entries'][0]['new_text'] += 'Unexpected modification'
			with self.assertRaisesRegex(ValueError, 'integrity'):
				apply_plan(plan, page, writer)

	def test_anonymous_reader_fetches_content_revision_and_sha1(self):
		'''The read API validates the file namespace, content slot and current image.'''
		session = mock.Mock()
		session.get.return_value.json.return_value = {'query': {'pages': [{'ns': 6, 'title': 'File:Shared.pdf', 'imageinfo': [{'sha1': SHA1}], 'revisions': [{'revid': 123, 'slots': {'main': {'content': ORIGINAL}}}]}]}}
		self.assertEqual(read_commons_page('File:Shared.pdf', session=session), page())
		self.assertEqual(session.get.call_args.kwargs['params']['prop'], 'revisions|imageinfo')

	def test_anonymous_reader_rejects_api_errors_and_incomplete_file_records(self):
		'''An API error, missing binary or suppressed revision is never editable evidence.'''
		session = mock.Mock()
		for payload in ({'error': {'code': 'maxlag'}}, {'query': {'pages': []}}, {'query': {'pages': [{'ns': 6, 'title': 'File:Shared.pdf', 'missing': True}]}}, {'query': {'pages': [{'ns': 0, 'title': 'Shared.pdf', 'imageinfo': [{'sha1': SHA1}], 'revisions': [{'revid': 1, 'slots': {'main': {'content': 'text'}}}]}]}}):
			with self.subTest(payload=payload):
				session.get.return_value.json.return_value = payload
				with self.assertRaises(ValueError):
					read_commons_page('File:Shared.pdf', session=session)
		with self.assertRaises(ValueError):
			read_commons_page('Main page', session=session)

	def test_invalid_managed_markers_and_object_paths_stop_planning(self):
		'''Malformed ownership markers and escaping object references cannot overwrite text.'''
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			archive_scan(root)
			with self.assertRaisesRegex(ValueError, 'Ambiguous'):
				generate_plan(root, lambda title: page(title, text='<!-- NLB-METADATA:BY-NLB-test:1:BEGIN --> Human text'))
			manifest_path, = root.glob('records/*/snapshots/*/manifest.json')
			manifest = json.loads(manifest_path.read_text())
			manifest['files']['1']['description']['path'] = '../outside.wiki'
			manifest_path.write_text(json.dumps(manifest))
			with self.assertRaisesRegex(ValueError, 'escapes'):
				generate_plan(root, page)

	def test_completed_titles_and_missing_targets_are_handled_explicitly(self):
		'''Uploaded titles use their actual page; incomplete associations remain skipped.'''
		for details, planned in (({'status': 'uploaded', 'commons_title': 'File:Shared.pdf'}, 1), ({'duplicates': []}, 0), ({'description': None}, 0)):
			with self.subTest(details=details), tempfile.TemporaryDirectory() as directory:
				root = Path(directory)
				archive_scan(root, **details)
				self.assertEqual(len(generate_plan(root, page)['entries']), planned)

	def test_apply_can_resume_after_an_already_applied_entry(self):
		'''A completed edit is recognized without rewriting it during a restarted plan.'''
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			archive_scan(root)
			plan = generate_plan(root, page)
			current = page(revid=124, text=plan['entries'][0]['new_text'])
			writer = mock.Mock()
			self.assertEqual(apply_plan(plan, lambda title: current, writer), 0)
			writer.assert_not_called()

	def test_real_pywikibot_edit_request_keeps_baserevid_and_nocreate(self):
		'''Exercise the installed editpage method with offline site and request objects.'''
		with tempfile.TemporaryDirectory(prefix='nlb-reconcile-pywikibot-') as directory, mock.patch.dict('os.environ', {'PYWIKIBOT_DIR': directory, 'PYWIKIBOT_NO_USER_CONFIG': '2'}):
			import pywikibot
			from pywikibot.site import APISite
		site = mock.MagicMock()
		site.has_right.return_value = True
		site.obsolete = False
		site.editpage = types.MethodType(APISite.editpage, site)
		site.simple_request.return_value.submit.return_value = {'edit': {'result': 'Success', 'newrevid': 124}}
		file_page = mock.MagicMock()
		file_page.latest_revision.timestamp = '2026-10-28T00:00:00Z'
		entry = {'title': 'File:Shared.pdf', 'new_text': 'Reviewed text', 'baserevid': 123}
		with mock.patch.object(pywikibot, 'Site', return_value=site), mock.patch.object(pywikibot, 'FilePage', return_value=file_page):
			self.assertTrue(edit_commons_page(entry))
		params = site.simple_request.call_args.kwargs
		self.assertEqual(params['baserevid'], 123)
		self.assertEqual(params['text'], 'Reviewed text')
		self.assertTrue(params['nocreate'])
		self.assertFalse(params['recreate'])
		# The installed library forwards a server-side conflict rather than retrying.
		from pywikibot.exceptions import APIError, EditConflictError
		site._ep_errors = APISite._ep_errors
		site.simple_request.return_value.submit.side_effect = APIError('editconflict', 'Changed revision')
		with mock.patch.object(pywikibot, 'Site', return_value=site), mock.patch.object(pywikibot, 'FilePage', return_value=file_page):
			with self.assertRaises(EditConflictError):
				edit_commons_page(entry)

	def test_rejected_write_and_unknown_plan_schema_stop_application(self):
		'''Write failures are reported; unsupported plan formats never reach the API.'''
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			archive_scan(root)
			plan = generate_plan(root, page)
			with self.assertRaisesRegex(RuntimeError, 'rejected'):
				apply_plan(plan, page, mock.Mock(return_value=False))
			plan['schema_version'] = 2
			with self.assertRaisesRegex(ValueError, 'schema'):
				apply_plan(plan, page, mock.Mock())

	def test_cli_plan_never_initializes_write_client(self):
		'''Generating the review artifact is the default operation and does not edit.'''
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			archive_scan(root)
			output = root / 'plan.json'
			with mock.patch('reconcile_metadata.read_commons_page', side_effect=page), mock.patch('reconcile_metadata.edit_commons_page') as writer:
				self.assertEqual(main(['--archive-dir', str(root), '--plan', str(output)]), 0)
				writer.assert_not_called()
			self.assertEqual(len(json.loads(output.read_text())['entries']), 1)

	def test_cli_apply_requires_its_explicit_action_and_uses_reviewed_file(self):
		'''The --apply branch reads its plan and does not regenerate an unreviewed edit.'''
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			archive_scan(root)
			plan = generate_plan(root, page)
			plan_path = root / 'reviewed.json'
			plan_path.write_text(json.dumps(plan))
			with mock.patch('reconcile_metadata.apply_plan', return_value=1) as apply, mock.patch.dict('os.environ', {}, clear=False):
				self.assertEqual(main(['--apply', str(plan_path), '--pywikibot-dir', str(root)]), 0)
				apply.assert_called_once_with(plan)


if __name__ == '__main__':
	unittest.main()
