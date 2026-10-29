#!/usr/bin/env python3
'''Exercise catalog subject variants through the offline Commons upload pipeline.'''

from pathlib import Path
import tempfile
import unittest
from unittest import mock

import upload as upload_nlb
from test_upload import FakeCommonsUploader, FakeNlbClient


FIXTURE = Path(__file__).parent / 'tests' / 'fixtures' / 'BY-NLB-br0000159462-subject-variants.xml'
SUBJECTS_LABEL = 'Предметные термины и варианты по каталогу НББ (MARC 906)'
MAIN_SUBJECT = 'ПОЛЬСКАЕ ПАЎСТАННЕ 1830—1831'
VARIANTS = (
	'Лістападаўскае паўстанне 1830—1831',
	'Ноябрьское восстание 1830—1831',
	'Русско-польская война 1830—1831',
)


def subject_metadata() -> dict:
	"""Parse the public NLB excerpt used to reproduce the missing search terms."""
	return upload_nlb.parse_marcxml(FIXTURE.read_text(encoding='utf-8'))


class TestSubjectVariantsPipeline(unittest.TestCase):
	'''Preserve subject references without changing author or filename semantics.'''

	def test_full_pipeline_preserves_subject_variants_and_cleans_up_scans(self) -> None:
		'''Upload the source's references alongside ordinary records with no variants.'''
		metadata = subject_metadata()
		client = FakeNlbClient()
		uploader = FakeCommonsUploader()
		fetch_metadata = client.fetch_record_metadata

		def metadata_with_subjects(marc_id: str) -> tuple[dict, dict]:
			"""Replace one fake record's catalog data with the real subject excerpt."""
			record_meta, marc_meta = fetch_metadata(marc_id)
			if marc_id == 'BY-NLB-rr111':
				record_meta['title'] = metadata['title']
				record_meta['fields'] = [{'name': 'Автор', 'values': ['Catalog author']}]
				return record_meta, metadata
			return record_meta, marc_meta

		with tempfile.TemporaryDirectory() as tmp_dir:
			root = Path(tmp_dir)
			state_path = root / 'state.json'
			staging_dir = root / 'staging'
			with (
				mock.patch.object(client, 'fetch_record_metadata', side_effect=metadata_with_subjects),
				mock.patch('upload.check_commons_by_hash',
					side_effect=lambda path, **_: (upload_nlb.calculate_sha1(path), [])),
			):
				counts = upload_nlb.iterate_and_upload(
					client=client, uploader=uploader, state_path=state_path, staging_dir=staging_dir, explicit_license=True,
				)

			self.assertEqual(counts['uploaded'], 3)
			self.assertEqual(len(uploader.uploads), 3)
			first_upload = uploader.uploads[0]
			wikitext = first_upload['wikitext']
			self.assertIn(SUBJECTS_LABEL, wikitext)
			self.assertIn(MAIN_SUBJECT, wikitext)
			for variant in VARIANTS:
				self.assertIn(variant, wikitext)
				self.assertNotIn(variant, metadata['subjects'])
				self.assertNotIn(variant, metadata['authors'])
				self.assertNotIn(variant, first_upload['commons_filename'])
			self.assertIn('450', wikitext)
			self.assertIn('550', wikitext)
			self.assertEqual(
				[line for line in wikitext.splitlines() if line.startswith('|author=')],
				['|author=Catalog author'],
			)
			for uploaded in uploader.uploads[1:]:
				self.assertNotIn(SUBJECTS_LABEL, uploaded['wikitext'])
			self.assertEqual(list(staging_dir.iterdir()), [])
			state = upload_nlb.load_state(state_path)
			for marc_id, file_id in (
				('BY-NLB-rr111', '1001'), ('BY-NLB-rr222', '2001'), ('BY-NLB-rr222', '2002'),
			):
				self.assertTrue(upload_nlb.is_item_completed(state, marc_id, file_id))

	def test_html_subject_deduplication_keeps_catalog_variant_section(self) -> None:
		"""An HTML list of all main subjects must not suppress their 906 references."""
		metadata = subject_metadata()
		record_meta = {
			'title': metadata['title'],
			'fields': [{'name': 'Тематика', 'values': list(metadata['subjects'])}],
		}
		wikitext = upload_nlb.build_commons_description(
			record_meta, metadata, 'BY-NLB-br0000159462', '11529915',
		)
		self.assertNotIn('Тематика (MARC 6xx)', wikitext)
		self.assertIn(SUBJECTS_LABEL, wikitext)
		for variant in VARIANTS:
			self.assertIn(variant, wikitext)

	def test_metadata_without_variant_key_uses_source_field_fallback(self) -> None:
		'''Raw source fields preserve terms even when a specialized formatter is absent.'''
		metadata = subject_metadata()
		metadata.pop('subject_variants', None)
		wikitext = upload_nlb.build_commons_description(
			{'title': metadata['title'], 'fields': []}, metadata,
			'BY-NLB-br0000159462', '11529915',
		)
		self.assertIn(MAIN_SUBJECT, wikitext)
		self.assertIn('Тематика (MARC 6xx)', wikitext)
		self.assertNotIn(SUBJECTS_LABEL, wikitext)
		self.assertIn('Дополнительные сведения (MARC 906)', wikitext)
		for variant in VARIANTS:
			self.assertIn(variant, wikitext)


if __name__ == '__main__':
	unittest.main()
