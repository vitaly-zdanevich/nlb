#!/usr/bin/env python3
"""Regression tests for related catalog records and attributed parent metadata."""

from pathlib import Path
import tempfile
import unittest
from unittest import mock

import upload as upload_nlb
from test_upload import FakeCommonsUploader, FakeNlbClient


FIXTURES = Path(__file__).parent / 'tests' / 'fixtures'
VOLUME_ID = 'BY-NLB-br0000061374'
PARENT_ID = 'BY-NLB-br0000066456'
GOGOL_ID = 'BY-NLB-br0000060109'
BOUND_ID = 'BY-NLB-br307362'


def fixture(record_id: str, suffix: str = 'xml') -> str:
	"""Read a minimal excerpt of a real NLB record."""
	return (FIXTURES / f'{record_id}-relationships.{suffix}').read_text()


def response(body: str, status: int = 200) -> upload_nlb.requests.Response:
	"""Create a real Requests response without performing network access."""
	result = upload_nlb.requests.Response()
	result.status_code = status
	result._content = body.encode('utf-8')
	result.encoding = 'utf-8'
	return result


def marc_record(record_id: str, fields: str, namespace: bool = True) -> str:
	"""Wrap synthetic fields in the same collection shape as the NLB export."""
	xmlns = ' xmlns="http://www.loc.gov/MARC21/slim"' if namespace else ''
	return (
		f'<collection{xmlns}><record><controlfield tag="001">{record_id}</controlfield>'
		f'{fields}</record></collection>'
	)


def relation(tag: str, record_id: str, extra: str = '') -> str:
	"""Build an embedded-001 relationship with optional source subfields."""
	return (
		f'<datafield tag="{tag}" ind1=" " ind2="0">'
		f'<subfield code="1">001{record_id}</subfield>{extra}</datafield>'
	)


def mock_client(records: dict[str, str], parent_result: object | None = None) -> tuple:
	"""Serve record HTML, XML and empty TOCs from a strictly mocked HTTP session."""
	session = mock.Mock(spec=upload_nlb.requests.Session)
	session.headers = {}

	def get(url: str, **kwargs: object) -> upload_nlb.requests.Response:
		"""Reject unexpected requests and dispatch source excerpts by their URL."""
		for record_id, xml in records.items():
			base = f'{upload_nlb.ELIB_BASE_URL}/elib/Record/{record_id}'
			if url == base:
				return response(f'<h1>{record_id}</h1>')
			if url == f'{base}/Export?style=MARCXML':
				if record_id == PARENT_ID and parent_result is not None:
					if isinstance(parent_result, Exception):
						raise parent_result
					return parent_result
				return response(xml)
			if url == f'{base}/TOC':
				return response('')
		raise AssertionError(f'Unexpected network request: {url}')

	session.get.side_effect = get
	return upload_nlb.NlbClient(session=session), session


class TestRelatedMetadata(unittest.TestCase):
	"""Keep catalog links and only missing common imprint data from a parent."""

	def test_real_volume_keeps_parent_identifier_and_title(self) -> None:
		"""The encyclopedia's 461 ID and matching 961 title survive together."""
		metadata = upload_nlb.parse_marcxml(fixture(VOLUME_ID))
		self.assertEqual(metadata.get('related_records'), [{
			'tag': '461',
			'record_id': PARENT_ID,
			'title': 'Энциклопедический словарь : [в 86 т.]',
			'volume': 'Т. 6 (11)',
			'copies': [],
		}])

	def test_real_bound_with_link_survives_on_commons(self) -> None:
		"""The Gogol companion gets a clickable ID and its HTML catalog title."""
		metadata = upload_nlb.parse_marcxml(fixture(GOGOL_ID))
		record = upload_nlb.parse_record_html(fixture(GOGOL_ID, 'html'), GOGOL_ID)
		self.assertEqual(metadata.get('related_records'), [{
			'tag': '481',
			'record_id': BOUND_ID,
			'title': '',
			'volume': '',
			'copies': [],
		}])
		self.assertEqual([item['record_id'] for item in record.get('record_links', [])], [BOUND_ID])
		wikitext = upload_nlb.build_commons_description(record, metadata, GOGOL_ID, '7397126')
		self.assertIn(f'[https://elib.nlb.by/elib/Record/{BOUND_ID} ', wikitext)
		self.assertIn('Веснянои ночи', wikitext)
		self.assertIn('481', wikitext)

	def test_real_volume_inherits_imprint_with_provenance(self) -> None:
		"""Parent city/publishers appear without replacing volume date or pages."""
		client, session = mock_client({
			VOLUME_ID: fixture(VOLUME_ID),
			PARENT_ID: fixture(PARENT_ID),
		})
		record, metadata = client.fetch_record_metadata(VOLUME_ID)
		inherited = {item['field']: item for item in metadata.get('inherited_metadata', [])}
		self.assertEqual(set(inherited), {'places', 'publishers'})
		self.assertEqual(inherited['places']['values'], ['С.-Петербург'])
		self.assertEqual(inherited['publishers']['values'], ['Ф. А. Брокгауз', 'И. А. Ефрон'])
		self.assertTrue(all(item['record_id'] == PARENT_ID for item in inherited.values()))
		self.assertEqual(metadata['places'], [])
		self.assertEqual(metadata['publishers'], [])
		self.assertEqual(metadata['date'], '1892')
		self.assertEqual(metadata['physical_description'], '[6], II, 488, [1] с., [9] л. ил., карт. : ил.')
		wikitext = upload_nlb.build_commons_description(record, metadata, VOLUME_ID, '3460841')
		for value in ['С.-Петербург', 'Ф. А. Брокгауз', 'И. А. Ефрон', 'из родительской записи', '210$a', '210$c']:
			self.assertIn(value, wikitext)
		self.assertIn(f'[https://elib.nlb.by/elib/Record/{PARENT_ID} ', wikitext)
		self.assertIn('|date=1892', wikitext)
		self.assertNotIn('1890―', wikitext)
		self.assertNotIn('25 см', wikitext)

	def test_local_title_enrichment_is_order_independent_and_deduplicated(self) -> None:
		"""961 may precede 461; self references and duplicate fields add no links."""
		parent = relation('461', PARENT_ID, '<subfield code="1">2001 </subfield><subfield code="v">Т. 6</subfield>')
		title = relation('961', PARENT_ID, '<subfield code="1">998  </subfield><subfield code="a">Parent</subfield>')
		for namespace in (True, False):
			with self.subTest(namespace=namespace):
				xml = marc_record(VOLUME_ID, title + parent + parent + relation('961', VOLUME_ID), namespace)
				self.assertEqual(upload_nlb.parse_marcxml(xml).get('related_records'), [{
					'tag': '461', 'record_id': PARENT_ID, 'title': 'Parent', 'volume': 'Т. 6', 'copies': [],
				}])

	def test_self_relationship_cannot_change_the_volume_title(self) -> None:
		"""An ignored self-link must not still act as the parent's title fallback."""
		xml = marc_record(VOLUME_ID,
			'<datafield tag="200"><subfield code="a">Volume title</subfield></datafield>'
			+ relation('461', VOLUME_ID,
				'<subfield code="1">2001 </subfield><subfield code="a">Wrong self-title</subfield>'))
		metadata = upload_nlb.parse_marcxml(xml)
		self.assertEqual(metadata['related_records'], [])
		self.assertEqual(metadata['title'], 'Volume title')
		self.assertEqual(metadata['series'], [])

	def test_local_title_enrichment_does_not_drop_copy_qualifiers(self) -> None:
		"""Collapsing a 961 display-title duplicate must preserve its copy scope."""
		xml = marc_record(VOLUME_ID, relation('461', PARENT_ID)
			+ relation('961', PARENT_ID, '''
				<subfield code="1">998  </subfield>
				<subfield code="a">Parent title</subfield>
				<subfield code="5">BY-HM0000:Copy 1</subfield>
			'''))
		metadata = upload_nlb.parse_marcxml(xml)
		copies = [copy for link in metadata['related_records'] for copy in link['copies']]
		self.assertIn('BY-HM0000:Copy 1', copies)
		self.assertTrue(any(link['tag'] == '961' and link['copies'] == ['BY-HM0000:Copy 1'] for link in metadata['related_records']))

	def test_distinct_local_display_title_is_preserved(self) -> None:
		"""961 can carry a longer display title than the embedded parent title."""
		xml = marc_record(VOLUME_ID,
			relation('461', PARENT_ID, '<subfield code="1">2001 </subfield><subfield code="a">Short title</subfield>')
			+ relation('961', PARENT_ID,
				'<subfield code="1">998  </subfield><subfield code="a">Longer title with responsibility</subfield>'))
		links = upload_nlb.parse_marcxml(xml)['related_records']
		self.assertEqual({link['title'] for link in links}, {'Short title', 'Longer title with responsibility'})

	def test_standard_identifier_order_does_not_change_link_associations(self) -> None:
		"""Standard $0 may follow $t; separate fields still retain separate titles."""
		xml = marc_record(VOLUME_ID, f'''
			<datafield tag="481">
				<subfield code="t">First title</subfield>
				<subfield code="0">{BOUND_ID}</subfield>
			</datafield>
			<datafield tag="481">
				<subfield code="0">{PARENT_ID}</subfield>
				<subfield code="t">Second title</subfield>
			</datafield>
		''')
		links = upload_nlb.parse_marcxml(xml)['related_records']
		self.assertEqual([(link['record_id'], link['title']) for link in links], [
			(BOUND_ID, 'First title'), (PARENT_ID, 'Second title'),
		])

	def test_embedded_contexts_do_not_confuse_titles_with_authors_or_places(self) -> None:
		"""Only embedded title fields supply titles; later 210/700 fields do not."""
		xml = marc_record(VOLUME_ID, relation('461', 'BY-NLB-br341069G17737', '''
			<subfield code="1">2001 </subfield>
			<subfield code="a">Parent title</subfield>
			<subfield code="e">Subtitle</subfield>
			<subfield code="v">Т. 1</subfield>
			<subfield code="1">210  </subfield>
			<subfield code="a">Unrelated place</subfield>
			<subfield code="1">700 1</subfield>
			<subfield code="a">Unrelated author</subfield>
		'''))
		links = upload_nlb.parse_marcxml(xml).get('related_records', [])
		self.assertEqual(len(links), 1)
		self.assertEqual(links[0]['record_id'], 'BY-NLB-br341069G17737')
		self.assertIn('Parent title', links[0]['title'])
		self.assertIn('Subtitle', links[0]['title'])
		self.assertNotIn('Unrelated', links[0]['title'])
		self.assertEqual(links[0]['volume'], 'Т. 1')

	def test_standard_links_and_copy_qualifiers_are_preserved(self) -> None:
		"""Standard $0/$t and title-only links retain their types and copy scope."""
		xml = marc_record(VOLUME_ID, '''
			<datafield tag="482">
				<subfield code="0">BY-NLB-br307362</subfield>
				<subfield code="a">Author, not title</subfield>
				<subfield code="t">Bound title</subfield>
				<subfield code="5">BY-HM0000:Copy 1</subfield>
				<subfield code="5">BY-HM0000:Copy 1</subfield>
			</datafield>
			<datafield tag="481"><subfield code="t">Title without ID</subfield></datafield>
		''' + relation('961', PARENT_ID))
		links = upload_nlb.parse_marcxml(xml).get('related_records', [])
		self.assertEqual(links, [
			{'tag': '482', 'record_id': BOUND_ID, 'title': 'Bound title', 'volume': '', 'copies': ['BY-HM0000:Copy 1']},
			{'tag': '481', 'record_id': '', 'title': 'Title without ID', 'volume': '', 'copies': []},
			{'tag': '961', 'record_id': PARENT_ID, 'title': '', 'volume': '', 'copies': []},
		])

	def test_html_links_are_limited_to_catalog_metadata(self) -> None:
		"""Ignore sidebar suggestions, external hosts, self links and malformed IDs."""
		html = f'''
			<a href="/elib/Record/BY-NLB-sidebar">Suggestion</a>
			<table><tr><th>Связанные документы:</th><td>
				<a href="/Record/{BOUND_ID}">Bound title</a>
				<a href="/Record/{BOUND_ID}">Bound title</a>
				<a href="https://elib.nlb.by/elib/Record/{PARENT_ID}">Parent title</a>
				<a href="https://example.org/Record/BY-NLB-external">External</a>
				<a href="/Record/{VOLUME_ID}">Self</a>
				<a href="/Record/../../foreign">Invalid</a>
				<a href="/elib/Author/Home?author=Other">Author</a>
			</td></tr></table>
		'''
		links = upload_nlb.parse_record_html(html, VOLUME_ID).get('record_links', [])
		self.assertEqual(links, [
			{'record_id': BOUND_ID, 'title': 'Bound title', 'label': 'Связанные документы'},
			{'record_id': PARENT_ID, 'title': 'Parent title', 'label': 'Связанные документы'},
		])

	def test_description_keeps_untyped_html_links_and_escapes_labels(self) -> None:
		"""A catalog title cannot close an external link or inject a template."""
		record = {
			'title': 'Record',
			'record_links': [{
				'record_id': BOUND_ID,
				'title': 'Bound ] | {{Injected}} <b>label</b>',
				'label': 'Связанные документы',
			}],
		}
		wikitext = upload_nlb.build_commons_description(record, {}, VOLUME_ID, '1')
		self.assertIn(f'[https://elib.nlb.by/elib/Record/{BOUND_ID} ', wikitext)
		self.assertNotIn('Bound ]', wikitext)
		self.assertNotIn('{{Injected}}', wikitext)
		self.assertNotIn('<b>label</b>', wikitext)

	def test_parent_cache_does_not_share_mutable_inherited_values(self) -> None:
		"""Repeated volumes reuse one XML request without sharing output lists."""
		second_id = 'BY-NLB-brVolume2'
		client, session = mock_client({
			VOLUME_ID: fixture(VOLUME_ID),
			second_id: fixture(VOLUME_ID).replace(VOLUME_ID, second_id),
			PARENT_ID: fixture(PARENT_ID),
		})
		_, first = client.fetch_record_metadata(VOLUME_ID)
		self.assertTrue(first.get('inherited_metadata'))
		for item in first['inherited_metadata']:
			item['values'].append('Changed by caller')
		_, second = client.fetch_record_metadata(second_id)
		self.assertTrue(second.get('inherited_metadata'))
		self.assertNotIn('Changed by caller', str(second['inherited_metadata']))
		parent_url = f'{upload_nlb.ELIB_BASE_URL}/elib/Record/{PARENT_ID}/Export?style=MARCXML'
		self.assertEqual(sum(call.args[0] == parent_url for call in session.get.call_args_list), 1)

	def test_only_missing_child_imprint_fields_are_inherited(self) -> None:
		"""A child's city or publisher always wins, independently for each field."""
		for field, code, other_field in [('places', 'a', 'publishers'), ('publishers', 'c', 'places')]:
			with self.subTest(field=field):
				child = marc_record(VOLUME_ID,
					f'<datafield tag="210"><subfield code="{code}">Child value</subfield></datafield>'
					+ relation('461', PARENT_ID))
				client, session = mock_client({VOLUME_ID: child, PARENT_ID: fixture(PARENT_ID)})
				_, metadata = client.fetch_record_metadata(VOLUME_ID)
				self.assertEqual(metadata[field], ['Child value'])
				self.assertEqual([item['field'] for item in metadata.get('inherited_metadata', [])], [other_field])

	def test_nonparent_ambiguous_self_and_invalid_links_are_not_inherited(self) -> None:
		"""Related copies and uncertain ancestry cannot supply a volume's imprint."""
		cases = {
			'bound after': relation('481', PARENT_ID),
			'bound with': relation('482', PARENT_ID),
			'local link': relation('961', PARENT_ID),
			'own record': relation('461', VOLUME_ID),
			'ambiguous': relation('461', PARENT_ID) + relation('461', 'BY-NLB-anotherParent'),
			'invalid': relation('461', '../foreign'),
		}
		for name, fields in cases.items():
			with self.subTest(case=name):
				client, session = mock_client({VOLUME_ID: marc_record(VOLUME_ID, fields)})
				_, metadata = client.fetch_record_metadata(VOLUME_ID)
				self.assertFalse(metadata.get('inherited_metadata'))
				self.assertTrue(all(f'/Record/{VOLUME_ID}' in call.args[0] for call in session.get.call_args_list))

	def test_complete_child_imprint_needs_no_parent_request(self) -> None:
		"""A volume with its own city and publisher retains its link without a fetch."""
		child = marc_record(VOLUME_ID, '''
			<datafield tag="210">
				<subfield code="a">Child city</subfield>
				<subfield code="c">Child publisher</subfield>
			</datafield>
		''' + relation('461', PARENT_ID))
		client, session = mock_client({VOLUME_ID: child})
		_, metadata = client.fetch_record_metadata(VOLUME_ID)
		self.assertEqual(metadata['places'], ['Child city'])
		self.assertEqual(metadata['publishers'], ['Child publisher'])
		self.assertEqual(metadata['related_records'][0]['record_id'], PARENT_ID)
		self.assertEqual(metadata['inherited_metadata'], [])
		self.assertTrue(all(f'/Record/{VOLUME_ID}' in call.args[0] for call in session.get.call_args_list))

	def test_inheritance_stops_at_direct_parent_even_when_parent_has_a_cycle(self) -> None:
		"""One-hop enrichment never follows a parent's link back to its child."""
		parent = marc_record(PARENT_ID,
			'<datafield tag="210"><subfield code="a">Parent city</subfield></datafield>'
			+ relation('461', VOLUME_ID))
		client, session = mock_client({VOLUME_ID: fixture(VOLUME_ID), PARENT_ID: parent})
		_, metadata = client.fetch_record_metadata(VOLUME_ID)
		self.assertEqual([item['values'] for item in metadata.get('inherited_metadata', [])], [['Parent city']])
		parent_calls = [call.args[0] for call in session.get.call_args_list if PARENT_ID in call.args[0]]
		self.assertEqual(parent_calls, [f'{upload_nlb.ELIB_BASE_URL}/elib/Record/{PARENT_ID}/Export?style=MARCXML'])

	def test_parent_failures_keep_links_without_claiming_inheritance(self) -> None:
		"""Unavailable, malformed or mismatched parent exports produce a warning."""
		cases = {
			'timeout': upload_nlb.requests.Timeout('parent timed out'),
			'not found': response('', 404),
			'html': response('<html>Service unavailable</html>'),
			'broken XML': response('<collection><record>'),
			'wrong ID': response(fixture(PARENT_ID).replace(PARENT_ID, 'BY-NLB-wrong')),
		}
		for name, parent_result in cases.items():
			with self.subTest(case=name):
				client, session = mock_client({
					VOLUME_ID: fixture(VOLUME_ID),
					PARENT_ID: fixture(PARENT_ID),
				}, parent_result=parent_result)
				with mock.patch.object(upload_nlb, 'log_progress') as log:
					_, metadata = client.fetch_record_metadata(VOLUME_ID)
				self.assertFalse(metadata.get('inherited_metadata'))
				self.assertEqual(metadata.get('related_records', [{}])[0].get('record_id'), PARENT_ID)
				self.assertTrue(log.called)
				self.assertIn(PARENT_ID, str(log.call_args_list))

	def test_upload_pipeline_preserves_parent_provenance_and_volume_specific_data(self) -> None:
		"""Submit enriched real metadata through the offline scan/upload lifecycle."""
		metadata_client, session = mock_client({
			VOLUME_ID: fixture(VOLUME_ID),
			PARENT_ID: fixture(PARENT_ID),
		})
		client = FakeNlbClient()
		uploader = FakeCommonsUploader()
		search_page = {
			'total_results': 1,
			'records': [{
				'marc_id': VOLUME_ID,
				'title': 'Венцано ― Винона',
				'formats': [],
				'file_ids': ['3460841'],
				'mets_ids': [],
			}],
		}
		with tempfile.TemporaryDirectory() as tmp_dir:
			state_path = Path(tmp_dir) / 'state.json'
			staging_dir = Path(tmp_dir) / 'staging'
			with (
				mock.patch.object(client, 'fetch_search_page', side_effect=[search_page, {'total_results': 1, 'records': []}]),
				mock.patch.object(client, 'fetch_record_metadata', side_effect=metadata_client.fetch_record_metadata),
				mock.patch.object(client, 'resolve_record_files', return_value=[{'file_id': '3460841', 'part_label': 'Ч. 3'}]),
				mock.patch.object(upload_nlb, 'check_commons_by_hash', return_value=('mock-sha1', [])),
			):
				counts = upload_nlb.iterate_and_upload(
					client=client,
					uploader=uploader,
					state_path=state_path,
					staging_dir=staging_dir,
				)
			self.assertEqual(counts['uploaded'], 1)
			self.assertEqual(client.downloaded_file_ids, ['3460841'])
			self.assertEqual(len(uploader.uploads), 1)
			wikitext = uploader.uploads[0]['wikitext']
			for text in (
				f'[https://elib.nlb.by/elib/Record/{PARENT_ID} {PARENT_ID}]',
				'из родительской записи',
				'С.-Петербург',
				'Ф. А. Брокгауз',
				'И. А. Ефрон',
				'|date=1892',
				'[6], II, 488, [1] с., [9] л. ил., карт. : ил.',
			):
				self.assertIn(text, wikitext)
			self.assertNotIn('1890―', wikitext)
			self.assertNotIn('25 см', wikitext)
			self.assertEqual(list(staging_dir.iterdir()), [])
			state = upload_nlb.load_state(state_path)
			self.assertEqual(state['items'][f'{VOLUME_ID}:3460841']['status'], 'uploaded')
			parent_url = f'{upload_nlb.ELIB_BASE_URL}/elib/Record/{PARENT_ID}/Export?style=MARCXML'
			self.assertEqual(sum(call.args[0] == parent_url for call in session.get.call_args_list), 1)


if __name__ == '__main__':
	unittest.main()
