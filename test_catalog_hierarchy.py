'''Preserve NLB's advertised dynamic record hierarchy through the real catalog client.'''

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import requests

from catalog_requests import CatalogRetriesExhausted
from preservation import ArchiveStore
import upload


RECORD = 'BY-NLB-br0000370146'
PARENT = 'BY-NLB-br0000369995'
BASE = 'https://elib.nlb.by'
RECORD_URL = f'{BASE}/elib/Record/{RECORD}'
FIXTURE = Path(__file__).parent / 'tests' / 'fixtures' / f'{RECORD}-hierarchy.json'
TREE = json.loads(FIXTURE.read_text())
TAB = f'''<div id="hierarchyTree"><input class="hiddenRecordId" value="{RECORD}">
<input class="hiddenHierarchyId" value="{PARENT}"><input class="hiddenContext" value="Record"></div>'''
HTML = f'''<div class="record"><div class="mainbody"><h1>Algebra</h1><input class="hiddenId" value="{RECORD}">
<table><tr><th>Дата издания</th><td>1778</td></tr></table>
<ul class="nav-tabs"><li><a href="/elib/Record/{RECORD}/HierarchyTree#tabnav">Структура документа</a></li></ul>
</div></div>'''


def response(url: str, body: str, status: int = 200) -> requests.Response:
	'''Build a consumed response without cookies, credentials or network traffic.'''
	result = requests.Response()
	result.url, result.status_code, result.encoding = url, status, 'utf-8'
	result._content, result._content_consumed = body.encode('utf-8'), True
	return result


class TestCatalogHierarchy(unittest.TestCase):
	'''Use a sanitized observed Algebra tree and isolated malformed responses.'''

	def setUp(self) -> None:
		'''Keep request dispatch, retry behavior and capture real while mocking HTTP.'''
		self.html, self.tab, self.tree = HTML, TAB, copy.deepcopy(TREE)
		self.session = mock.Mock(spec=requests.Session)
		self.session.headers = {}
		self.session.get.side_effect = self.get
		self.client = upload.NlbClient(session=self.session, metadata_reads_only=True)
		self.sleep = mock.patch('catalog_requests.time.sleep').start()
		self.log = mock.patch('upload.log_progress').start()
		self.addCleanup(mock.patch.stopall)

	def get(self, url: str, **kwargs: object) -> requests.Response:
		'''Reject unexpected resources so advertised-only fetching remains testable.'''
		if url == RECORD_URL:
			return response(url, self.html)
		if url == RECORD_URL + '/Export?style=MARCXML':
			return response(url, 'No export', 404)
		if url == RECORD_URL + '/TOC':
			return response(url, '<div class="tab-pane toc-tab"></div>')
		if url == RECORD_URL + '/HierarchyTree':
			return response(url, self.tab)
		if url == BASE + '/elib/Hierarchy/GetTreeJSON':
			self.assertEqual(kwargs.get('params'), {'hierarchyID': PARENT, 'id': RECORD})
			resolved = requests.Request('GET', url, params=kwargs['params']).prepare().url
			return response(resolved, json.dumps(self.tree, ensure_ascii=False))
		raise AssertionError(f'Unexpected request: {url}')

	def test_observed_tree_is_archived_and_all_relationships_are_readable(self) -> None:
		'''The hierarchy contributes linked titles and edges without supplying creators or dates.'''
		with tempfile.TemporaryDirectory() as directory:
			archive = ArchiveStore(Path(directory))
			self.client.archive = archive
			archive.begin_record(RECORD)
			record, marc = self.client.fetch_record_metadata(RECORD)
			archive.save_record(RECORD, record, marc)
			manifest = archive.manifests[RECORD]
			self.assertEqual(len(manifest['sources']), 5)
			captured = {source['url']: (archive.root / source['path']).read_bytes() for source in manifest['sources']}
			self.assertEqual(captured[RECORD_URL + '/HierarchyTree'], TAB.encode('utf-8'))
			tree_url = next(url for url in captured if '/GetTreeJSON?' in url)
			self.assertEqual(json.loads(captured[tree_url]), TREE)
			nodes = record['catalog_hierarchies'][0]['nodes']
			self.assertEqual([(node['record_id'], node['parent_id']) for node in nodes], [
				(PARENT, None), (RECORD, PARENT), ('BY-NLB-br0000370151', PARENT),
			])
			text = upload.build_commons_description(record, marc, RECORD, '7837808')
			self.assertIn('Catalog hierarchy', text)
			self.assertIn('Część 1', text)
			self.assertIn('Część 2', text)
			self.assertEqual(text.count(' → '), 2)
			self.assertIn(f'[{BASE}/elib/Collection/{PARENT}/HierarchyTree?', text)
			self.assertIn('current record', text)
			self.assertIn('|date=1778\n', text)
			self.assertNotIn('|author=', text)
			self.assertNotIn('"children"', text)
			self.assertNotIn('"li_attr"', text)
		self.session.post.assert_not_called()

	def test_no_advertised_tab_does_not_add_network_requests(self) -> None:
		'''Sidebar hierarchy links do not imply a hierarchy for the main record.'''
		self.html = HTML.replace('/HierarchyTree#tabnav', '/Holdings#tabnav') + f'<aside><ul class="nav-tabs"><a href="/elib/Record/{RECORD}/HierarchyTree">Sidebar</a></ul></aside>'
		record, _ = self.client.fetch_record_metadata(RECORD)
		self.assertNotIn('catalog_hierarchies', record)
		self.assertEqual(self.session.get.call_count, 3)

	def test_wrong_or_external_advertised_tab_is_never_followed(self) -> None:
		'''An untrusted navigation target causes a retryable record error before another GET.'''
		for href in ('https://example.org/elib/Record/' + RECORD + '/HierarchyTree',
			'/elib/Record/BY-NLB-other/HierarchyTree', f'/elib/Record/{RECORD}/HierarchyTree?recordID=BY-NLB-other'):
			with self.subTest(href=href):
				self.html = HTML.replace(f'/elib/Record/{RECORD}/HierarchyTree#tabnav', href)
				self.session.get.reset_mock()
				with self.assertRaises(CatalogRetriesExhausted):
					self.client.fetch_record_metadata(RECORD)
				self.assertEqual(self.session.get.call_count, 5)
				self.assertTrue(all(call.args == (RECORD_URL,) for call in self.session.get.call_args_list))

	def test_incomplete_advertised_tab_cannot_silently_omit_hierarchy(self) -> None:
		'''Missing tree structure or wrong hidden identity exhausts the normal retry budget.'''
		for body in ('<h1>Unavailable</h1>', TAB.replace(RECORD, 'BY-NLB-other'), TAB.replace('value="Record"', 'value="Collection"'), TAB.replace(PARENT, 'bad/id')):
			with self.subTest(body=body):
				self.tab = body
				with self.assertRaises(CatalogRetriesExhausted) as raised:
					self.client.fetch_record_metadata(RECORD)
				self.assertEqual(raised.exception.attempts, 5)

	def test_tree_rejects_wrong_ids_external_links_unloaded_children_and_missing_record(self) -> None:
		'''Unexpected hierarchy data stays pending instead of generating incomplete relationships.'''
		bad_root = {**TREE, 'id': 'BY-NLB-other'}
		bad_identity = copy.deepcopy(TREE)
		bad_identity['children'][0]['li_attr']['data-recordid'] = 'BY-NLB-other'
		foreign_link = copy.deepcopy(TREE)
		foreign_link['children'][0]['a_attr']['href'] = 'https://example.org/elib/Record/' + RECORD
		wrong_link = copy.deepcopy(TREE)
		wrong_link['children'][0]['a_attr']['href'] = '/elib/Record/BY-NLB-other'
		lazy_tree = {**TREE, 'children': True}
		missing_record = {**TREE, 'children': [TREE['children'][1]]}
		duplicate = {**TREE, 'children': [TREE['children'][0], TREE['children'][0]]}
		for payload in ({}, [], bad_root, bad_identity, foreign_link, wrong_link, lazy_tree, missing_record, duplicate):
			with self.subTest(payload=payload):
				self.tree = payload
				with self.assertRaises(CatalogRetriesExhausted):
					self.client.fetch_record_metadata(RECORD)

	def test_transient_tree_schema_failure_is_preserved_and_retried(self) -> None:
		'''A temporary malformed tree remains in the archive before a successful retry.'''
		with tempfile.TemporaryDirectory() as directory:
			archive = ArchiveStore(Path(directory))
			archive.begin_record(RECORD)
			self.client.archive = archive
			calls = 0
			def recover(url: str, **kwargs: object) -> requests.Response:
				'''Fail only the first tree request, leaving all other source reads untouched.'''
				nonlocal calls
				if url.endswith('/GetTreeJSON'):
					calls += 1
					if calls == 1:
						return response(url, '<html>Database unavailable</html>')
				return self.get(url, **kwargs)
			self.session.get.side_effect = recover
			record, _ = self.client.fetch_record_metadata(RECORD)
			self.assertEqual(len(record['catalog_hierarchies'][0]['nodes']), 3)
			self.assertEqual(len(archive.manifests[RECORD]['sources']), 6)
			self.sleep.assert_called_once_with(5)

	def test_source_titles_and_qualifiers_are_literal(self) -> None:
		'''Human-readable tree labels cannot invoke wiki templates or categories.'''
		self.tree['children'][0]['text'] = 'Część 1 [[Category:Injected]]'
		self.tree['children'][0]['a_attr']['title'] = '{{Injected}} 2025'
		record, marc = self.client.fetch_record_metadata(RECORD)
		text = upload.build_commons_description(record, marc, RECORD, '7837808')
		self.assertIn(upload.escape_wikitext('[[Category:Injected]]'), text)
		self.assertIn(upload.escape_wikitext('{{Injected}} 2025'), text)
		self.assertNotIn('[[Category:Injected]]', text)
		self.assertNotIn('{{Injected}}', text)
		self.assertIn('|date=1778\n', text)


if __name__ == '__main__':
	unittest.main()
