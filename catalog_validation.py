'''Validate catalog response bodies before permissive metadata parsers consume them.

NLB sometimes serves PHP/database errors with HTTP 200. These schema checks run
inside the bounded GET retry loop, keeping unavailable metadata pending instead
of turning it into an empty record, an empty search, or denied access. Existing
BeautifulSoup and ElementTree parsers suffice; no transport retries are nested.
'''

import re
from typing import Any
from urllib.parse import parse_qs, urlsplit
import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup
import requests

from catalog_requests import InvalidCatalogResponse


MARC_NAMESPACE = '{http://www.loc.gov/MARC21/slim}'
RECORD_PATH = re.compile(r'/elib/(?:Record|Collection)/([^/?#"\'<>\s]+)')


def reject(response: requests.Response, reason: str) -> None:
	'''Attach the source response to a retryable validation failure.'''
	raise InvalidCatalogResponse(f'{reason}: {response.url}', response=response)


def html_soup(response: requests.Response) -> BeautifulSoup:
	'''Reject empty bodies and known application failures before parsing structure.'''
	text = response.text
	if not text.strip():
		reject(response, 'Empty catalog response')
	soup = BeautifulSoup(text, 'html.parser')
	if re.search(r'(?:Fatal error\s*:|Uncaught (?:PDOException|Exception)|SQLSTATE\[)', soup.get_text(' ', strip=True), re.I):
		reject(response, 'Catalog application error in HTTP response body')
	return soup


def check_html_identity(soup: BeautifulSoup, response: requests.Response, marc_id: str, *, required: bool) -> None:
	'''Check the main record identity without confusing sidebar or related-record links.'''
	main = soup.select_one('.record .mainbody, .record') or soup
	identifiers = [str(node.get('value', '')).strip() for node in main.select('input.hiddenId')]
	if identifiers:
		if set(identifiers) != {marc_id}:
			reject(response, 'HTML record identifier does not match the requested record')
		return
	# Older/lightweight record pages may identify themselves only in the viewer URL.
	viewer_ids = set()
	for link in main.find_all('a', href=True):
		url = urlsplit(link['href'])
		if 'viewer' not in url.path:
			continue
		params = parse_qs(url.query)
		viewer_ids.update(params.get('markID', []) + params.get('marcID', []))
	if viewer_ids and viewer_ids != {marc_id}:
		reject(response, 'HTML viewer identifier does not match the requested record')
	if required and not viewer_ids:
		reject(response, 'HTML record has no verifiable identifier')


def validate_record_html(response: requests.Response, marc_id: str) -> None:
	'''Require a record title, bibliographic rows, and the requested record identity.'''
	soup = html_soup(response)
	main = soup.select_one('.record .mainbody, .record') or soup
	title = main.select_one('h1, h3[property="name"], h2[property="name"]')
	rows = [row for row in main.select('table tr') if row.find('th') and row.find('td')]
	if title is None or not title.get_text(strip=True) or not rows:
		reject(response, 'Missing record title or bibliographic table')
	check_html_identity(soup, response, marc_id, required=True)


def validate_search_html(response: requests.Response) -> None:
	'''Distinguish complete result lists and explicit empty searches from failed pages.'''
	soup = html_soup(response)
	results = soup.select('.result')
	stats = soup.select_one('.search-stats, .search-controls')
	total_match = re.search(r'из\s*<strong>\s*([\d,\s]+)\s*</strong>', response.text)
	third_count = soup.select_one('.search-stats strong:nth-of-type(3), .search-controls strong:nth-of-type(3)')
	total_text = third_count.get_text() if third_count is not None else total_match.group(1) if total_match else ''
	digits = re.sub(r'\D', '', total_text)
	total = int(digits) if digits else None
	if not results:
		explicit_empty = stats is not None and re.search(
			r'(?:Нет результатов|No results|Няма вынікаў)', stats.get_text(' ', strip=True), re.I,
		)
		if total == 0 or explicit_empty:
			return
		reject(response, 'Search response lacks results and an explicit empty-results marker')
	if total is None or total < len(results):
		reject(response, 'Search result count is missing or inconsistent')
	seen = set()
	for result in results:
		hidden = result.select_one('input.hiddenId')
		identifier = str(hidden.get('value', '')).strip() if hidden else ''
		if not identifier:
			for link in result.find_all('a', href=True):
				match = RECORD_PATH.search(link['href'])
				if match:
					identifier = match.group(1)
					break
		if not identifier or identifier in seen:
			reject(response, 'Search result has a missing or repeated record identifier')
		seen.add(identifier)


def validate_marcxml(response: requests.Response, marc_id: str) -> None:
	'''Accept one MARCXML record or collection entry with matching 001 and a title.'''
	try:
		root = ET.fromstring(response.content)
	except ET.ParseError as error:
		raise InvalidCatalogResponse(f'Malformed MARCXML: {response.url}', response=response) from error
	if root.tag == f'{MARC_NAMESPACE}record':
		records = [root]
	elif root.tag == f'{MARC_NAMESPACE}collection':
		records = list(root.findall(f'{MARC_NAMESPACE}record'))
	else:
		records = []
	if len(records) != 1:
		reject(response, 'MARCXML must contain exactly one bibliographic record')
	record = records[0]
	identifiers = [node.text.strip() if node.text else '' for node in record.findall(f'{MARC_NAMESPACE}controlfield[@tag="001"]')]
	if identifiers != [marc_id]:
		reject(response, 'MARCXML 001 does not match the requested record')
	titles = record.findall(f'{MARC_NAMESPACE}datafield[@tag="200"]/{MARC_NAMESPACE}subfield[@code="a"]')
	if not any(node.text and node.text.strip() for node in titles):
		reject(response, 'MARCXML has no bibliographic title (200$a)')


def validate_toc_html(response: requests.Response, marc_id: str) -> None:
	'''Allow an empty contents tab, but reject an error page or another record's tab.'''
	soup = html_soup(response)
	if soup.select_one('.tab-pane.toc-tab, ul.toc') is None:
		reject(response, 'Missing contents-tab structure')
	check_html_identity(soup, response, marc_id, required=False)


def json_object(response: requests.Response) -> dict[str, Any]:
	'''Decode an object response without accepting JSON error strings or arrays.'''
	try:
		value = response.json()
	except requests.exceptions.JSONDecodeError as error:
		raise InvalidCatalogResponse(f'Malformed catalog JSON: {response.url}', response=response) from error
	if not isinstance(value, dict):
		reject(response, 'Catalog JSON must be an object')
	return value


def validate_access_json(response: requests.Response) -> None:
	'''Require an explicit boolean public-access decision from the access endpoint.'''
	payload = json_object(response)
	if type(payload.get('publicAccess')) is not bool:
		reject(response, 'Access response lacks a boolean publicAccess decision')


def validate_manifest(response: requests.Response) -> None:
	'''Accept the IIIF manifest containers used by NLB, rejecting JSON error bodies.

	NLB permits a rendering-only response; an empty rendering list still uses
	the documented file-bytes endpoint. IIIF Presentation 2 sequences and
	Presentation 3 items are also accepted without requiring optional metadata.
	https://iiif.io/api/presentation/2.1/#linking-properties
	https://iiif.io/api/presentation/3.0/#rendering
	'''
	payload = json_object(response)
	if 'error' in payload or not any(key in payload for key in ('rendering', 'items', 'sequences')):
		reject(response, 'IIIF manifest lacks rendering, items, or sequences')
	for key in ('rendering', 'items', 'sequences'):
		if key not in payload:
			continue
		entries = payload[key]
		if key == 'rendering' and isinstance(entries, dict):
			entries = [entries]
		if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
			reject(response, f'IIIF manifest has malformed {key}')
		if key == 'rendering':
			for entry in entries:
				identifier = entry.get('id', entry.get('@id'))
				if not isinstance(identifier, str) or not identifier.strip():
					reject(response, 'IIIF rendering lacks its resource identifier')
		elif key == 'sequences':
			for index, sequence in enumerate(entries):
				if index and isinstance(sequence.get('@id', sequence.get('id')), str) and 'canvases' not in sequence:
					continue
				canvases = sequence.get('canvases')
				if not isinstance(canvases, list) or any(not isinstance(canvas, dict) for canvas in canvases):
					reject(response, 'IIIF sequence lacks its canvases list')


def validate_mets_json(response: requests.Response, mets_id: str) -> None:
	'''Validate tree nodes and file identifiers before flattening downloadable entries.'''
	payload = json_object(response)
	if 'id' in payload and str(payload['id']) != str(mets_id):
		reject(response, 'METS identifier does not match the requested structure')
	if not isinstance(payload.get('nodes'), list):
		reject(response, 'METS response lacks a nodes list')
	pending = list(payload['nodes'])
	while pending:
		node = pending.pop()
		if not isinstance(node, dict) or not isinstance(node.get('data'), dict):
			reject(response, 'METS node lacks its data object')
		data = node['data']
		if not isinstance(data.get('nodeType'), str) or not data['nodeType']:
			reject(response, 'METS node lacks its node type')
		for key in ('visible', 'elibLocal'):
			if key in data and type(data[key]) is not bool:
				reject(response, f'METS node has a non-boolean {key}')
		for key in ('linkName', 'description'):
			if data.get(key) is not None and not isinstance(data[key], str):
				reject(response, f'METS node has a non-text {key}')
		if data['nodeType'] == 'FILE':
			file = data.get('file')
			if not isinstance(file, dict) or type(file.get('id')) not in (int, str) or not str(file['id']).isdigit():
				reject(response, 'METS file node lacks a valid file identifier')
		children = node.get('children', [])
		if not isinstance(children, list):
			reject(response, 'METS node has malformed children')
		pending.extend(children)
