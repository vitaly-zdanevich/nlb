'''Preserve proven shared-viewer relationships without trusting their volume attribution.'''

from collections import Counter
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from urllib.parse import urlsplit

import requests

from catalog_requests import CatalogRetriesExhausted, InvalidCatalogResponse
from catalog_validation import validate_record_html
from preservation import ArchiveStore
from scan_identity import assess_scan_identity
from test_scan_identity import node
import upload


CHILD = 'BY-NLB-child'
OWNER = 'BY-NLB-owner'
METS = '123'


def record_html(record_id=CHILD, *, owner=OWNER, mets=METS, backlinks=()):
	'''Model identified primary record markup with a shared METS link and related records.'''
	links = ' '.join(f'<a href="/elib/Record/{child}">{child}</a>' for child in backlinks)
	return f'''<div class="record"><div class="mainbody"><input class="hiddenId" value="{record_id}">
<h1>{record_id} T. 2</h1><table><tr><th>Title</th><td>{record_id} T. 2</td></tr>
<tr><th>Related Items</th><td>{links}</td></tr></table>
<a href="https://elib.nlb.by:8070/viewer/html/?markID={owner}&amp;metsID={mets}">E-document</a></div></div>'''


def response(body, url, status=200):
	'''Return an already consumed HTTP response to exercise real parsers and retries.'''
	result = requests.Response()
	result.url, result.status_code, result.encoding = url, status, 'utf-8'
	result._content = (body if isinstance(body, str) else json.dumps(body)).encode()
	result._content_consumed = True
	return result


class TestSharedViewers(unittest.TestCase):
	'''Check independently identified records, reciprocal evidence and per-scan scope.'''

	def setUp(self):
		'''Mock transport while leaving source parsing, resolution and validation real.'''
		self.session = mock.Mock(headers={})
		self.client = upload.NlbClient(session=self.session, metadata_reads_only=True)
		self.client.fetch_mets_files = mock.Mock(return_value=upload.extract_files_from_mets_structure({'nodes': [
			node('TEXT', 'Том II'), node('FILE', 'Ч. 1', 1),
		]}))
		self.owner_html = record_html(OWNER, backlinks=(CHILD,))
		self.session.get.side_effect = lambda url, **kwargs: response(self.owner_html, url)
		self.addCleanup(mock.patch.stopall)
		mock.patch('catalog_requests.time.sleep').start()
		mock.patch('sys.stdout', io.StringIO()).start()

	def resolve(self, body=None, search=None):
		'''Parse the child record before asking the client to verify its shared viewer.'''
		metadata = upload.parse_record_html(body or record_html(), CHILD)
		return self.client.resolve_record_files(search or {}, metadata), metadata

	def test_foreign_mets_requires_independent_record_identity_and_never_admits_direct_files(self):
		'''A different METS anchor is a candidate only after the requested hidden ID matches.'''
		body = record_html()
		validate_record_html(response(body, 'https://elib.nlb.by/elib/Record/' + CHILD), CHILD)
		parsed = upload.parse_record_html(body, CHILD)
		self.assertEqual(parsed['viewer_links'][0]['mark_id'], OWNER)
		self.assertEqual(parsed['marc_id'], CHILD)
		for bad in (body.replace(f'value="{CHILD}"', 'value="BY-NLB-wrong"'),
			body.replace(f'<input class="hiddenId" value="{CHILD}">', ''),
			body.replace('metsID=123', 'fileID=1'), body.replace('metsID=123', 'metsID=123&amp;fileID=1'),
			body.replace('metsID=123', 'metsID=123&amp;metsId=456')):
			with self.subTest(body=bad), self.assertRaises(InvalidCatalogResponse):
				validate_record_html(response(bad, 'https://elib.nlb.by/elib/Record/' + CHILD), CHILD)

	def test_real_catalog_relationships_are_recovered_without_approving_ambiguous_volumes(self):
		'''All ten observed records preserve their shared anchors and remain subject to scope checks.'''
		fixtures = json.loads((Path(__file__).parent / 'fixtures/shared_viewer_anchors.json').read_text())['records']
		outcomes = Counter()
		expected = {
			'BY-NLB-br0000059063': '12541593', 'BY-NLB-br0000059074': '12541601',
			'BY-NLB-br0000059079': '12541585', 'BY-NLB-br0000059083': '12541596',
			'BY-NLB-br0000434173': '11885648',
		}
		for case in fixtures:
			with self.subTest(record=case['record_id']):
				client = upload.NlbClient(session=self.session, metadata_reads_only=True)
				self.session.get.side_effect = lambda url, **kwargs: response(case['owner_html'], url)
				client.fetch_mets_files = mock.Mock(return_value=upload.extract_files_from_mets_structure(case['mets']))
				metadata = upload.parse_record_html(case['record_html'], case['record_id'])
				files = client.resolve_record_files({}, metadata)
				for item in files:
					self.assertEqual(item['shared_viewers'][0]['viewer_record_id'], case['owner_id'])
					decision = assess_scan_identity(item, metadata, case['marc_meta'])
					self.assertEqual(decision['eligible'], item['file_id'] == expected.get(case['record_id']))
					outcomes[decision['reason']] += 1
		self.assertEqual(sum(outcomes.values()), 45)
		self.assertEqual(outcomes[''], 5)

	def test_missing_or_unrelated_owner_evidence_defers_before_resolving_files(self):
		'''Unproven owner chains, foreign links and sidebar backlinks cannot establish sharing.'''
		for body in (record_html(OWNER), record_html(OWNER, mets='other', backlinks=(CHILD,)),
			record_html(OWNER, owner='BY-NLB-third', backlinks=(CHILD,)),
			record_html(OWNER) + f'<aside><table><tr><th>Related</th><td><a href="/elib/Record/{CHILD}">Other</a></td></tr></table></aside>',
			self.owner_html.replace(f'href="/elib/Record/{CHILD}"', f'href="https://other.invalid/elib/Record/{CHILD}"')):
			with self.subTest(body=body):
				self.client._shared_viewer_records = {}
				self.owner_html = body
				with self.assertRaises(CatalogRetriesExhausted) as raised:
					self.resolve()
				self.assertEqual(raised.exception.attempts, 1)
				self.client.fetch_mets_files.assert_not_called()

	def test_search_shared_viewer_must_be_corroborated_by_the_current_record(self):
		'''A stale search link cannot add a foreign METS absent from the fresh child record.'''
		search = upload.parse_search_results_html('<div class="search-stats">из <strong>1</strong></div>'
			+ record_html().replace('class="record"', 'class="result"'))['records'][0]
		files, _ = self.resolve(search=search)
		self.assertTrue(files[0]['shared_viewers'])
		self.client.fetch_mets_files.reset_mock()
		current = upload.parse_record_html(record_html(owner=CHILD), CHILD)
		with self.assertRaisesRegex(CatalogRetriesExhausted, 'absent from the current'):
			self.client.resolve_record_files(search, current)
		self.client.fetch_mets_files.assert_not_called()

	def test_cached_owner_is_archived_for_each_child_and_backlinks_are_rechecked(self):
		'''A cached successful relationship cannot silently authorize a different child.'''
		with tempfile.TemporaryDirectory() as directory:
			archive = ArchiveStore(Path(directory))
			self.client.archive = archive
			archive.begin_record(CHILD)
			self.resolve()
			other = 'BY-NLB-other'
			archive.begin_record(other)
			with self.assertRaises(CatalogRetriesExhausted):
				self.client.resolve_record_files({}, upload.parse_record_html(record_html(other), other))
			self.session.get.assert_called_once()
			first = archive.manifests[CHILD]['sources'][0]
			second = archive.manifests[other]['sources'][0]
			self.assertEqual(first, second)
			self.assertEqual((archive.root / first['path']).read_text(), self.owner_html)

	def test_missing_owner_remains_subordinate_metadata_failure(self):
		'''A missing shared anchor never impersonates deletion of the requested child.'''
		self.session.get.side_effect = lambda url, **kwargs: response('', url, 404)
		with self.assertRaises(upload.CatalogResourceMissing) as raised:
			self.resolve()
		self.assertNotIsInstance(raised.exception, upload.CatalogRecordMissing)
		self.client.fetch_mets_files.assert_not_called()

	def test_invalid_owner_identifier_defers_without_constructing_a_request(self):
		'''Raw source IDs cannot change the path of an owner-record request.'''
		with self.assertRaises(CatalogRetriesExhausted):
			self.resolve(record_html(owner='../other'))
		self.session.get.assert_not_called()

	def test_shared_anchor_forces_catalog_and_file_volume_agreement(self):
		'''The relationship alone cannot approve an unlabeled or wrong-volume scan.'''
		files, metadata = self.resolve()
		entry = files[0]
		self.assertTrue(assess_scan_identity(entry, metadata, {})['eligible'])
		self.assertFalse(assess_scan_identity(entry, {'title': 'Book'}, {})['eligible'])
		self.assertFalse(assess_scan_identity(entry, {'title': 'Book T. 1'}, {})['eligible'])
		unscoped = {'file_id': '1', 'shared_viewers': entry['shared_viewers']}
		self.assertFalse(assess_scan_identity(unscoped, metadata, {})['eligible'])
		unsupported = {**unscoped, 'mets_contexts': [{'headings': ['Том I–II']}]}
		self.assertEqual(assess_scan_identity(unsupported, metadata, {})['reason'], 'Unsupported or uncertain volume notation')

	def test_refresh_preserves_child_metadata_and_defers_unproven_or_wrong_volume_scans(self):
		'''The complete collection path uses child access and bibliography, retaining old state.'''
		for reciprocal in (True, False):
			with self.subTest(reciprocal=reciprocal), tempfile.TemporaryDirectory() as directory:
				root = Path(directory)
				archive = ArchiveStore(root / 'archive')
				state_path = root / 'state.json'
				previous = {'status': 'uploaded', 'sha1': 'a' * 40, 'size_bytes': 123, 'commons_title': 'File:Existing.pdf'}
				state_path.write_text(json.dumps({'items': {f'{CHILD}:{fid}': previous for fid in ('1', '2')}}) + '\n\n')
				before = state_path.read_bytes()
				session = mock.Mock(headers={})

				def get(url, **kwargs):
					'''Serve each real catalog boundary and reject unexpected recursive owner reads.'''
					path = urlsplit(url).path
					if path.endswith('/Export'):
						self.assertIn(CHILD, path)
						body = f'''<record xmlns="http://www.loc.gov/MARC21/slim"><controlfield tag="001">{CHILD}</controlfield>
<datafield tag="200"><subfield code="a">{CHILD} T. 2</subfield></datafield>
<datafield tag="210"><subfield code="d">1800</subfield></datafield></record>'''
					elif path.endswith('/TOC'):
						self.assertIn(CHILD, path)
						body = '<ul class="toc"></ul>'
					elif path.endswith('/Record/' + CHILD):
						body = record_html()
					elif path.endswith('/Record/' + OWNER):
						body = record_html(OWNER, backlinks=(CHILD,) if reciprocal else ())
					elif path == '/iiif/access':
						self.assertEqual(kwargs['params']['marcID'], CHILD)
						body = {'publicAccess': True}
					elif path == '/mets//structure/state':
						body = {'nodes': [node('TEXT', 'Том I'), node('FILE', 'Ч. 1', 1), node('TEXT', 'Том II'), node('FILE', 'Ч. 1', 2)]}
					elif path == '/iiif/manifest':
						self.assertEqual(kwargs['params'], {'marcID': CHILD, 'fileId': '2'})
						body = {'rendering': []}
					else:
						raise AssertionError('Unexpected source request: ' + url)
					return response(body, url)

				session.get.side_effect = get
				client = upload.NlbClient(session=session, archive=archive, metadata_reads_only=True)
				client.download_pdf_scan = mock.Mock(side_effect=AssertionError('Unexpected PDF transfer'))
				with mock.patch('upload.CommonsUploader') as uploader:
					counts = upload.refresh_uploaded_metadata(client, state_path, root / 'staging')
				self.assertEqual(state_path.read_bytes(), before)
				uploader.assert_not_called()
				client.download_pdf_scan.assert_not_called()
				session.post.assert_not_called()
				files = archive.manifests[CHILD]['files']
				for item in files.values():
					self.assertEqual(item['sha1'], previous['sha1'])
					self.assertEqual(item['commons_title'], previous['commons_title'])
				if reciprocal:
					self.assertEqual((counts['metadata_ready'], counts['metadata_review']), (1, 1))
					self.assertNotIn('description', files['1'])
					text = (archive.root / files['2']['description']['path']).read_text()
					self.assertIn(CHILD, text)
					self.assertNotIn(OWNER + ' T. 2', text)
					self.assertTrue(files['2']['scan_identity']['context']['shared_anchor'])
				else:
					self.assertEqual(counts['metadata_failed'], 2)
					self.assertFalse(any('description' in item for item in files.values()))
