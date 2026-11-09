'''Validate NLB's advertised VuFind hierarchy and retain readable record relationships.

The public hierarchy tab names the tree fetched by GetTreeJSON. Tree nodes are
catalog relationships, not proof that a PDF belongs to a volume and not a source
of inferred publication dates, authors or copyright status. Original HTML/JSON
responses are captured by the caller's normal catalog request path.
'''

import re
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup
import requests

from catalog_requests import InvalidCatalogResponse


# The same public catalog also hosts Central Scientific Library (BY-CNB) records.
RECORD_ID = re.compile(r'BY-(?:NLB|CNB)-[A-Za-z0-9_-]+')


def reject(response: requests.Response, reason: str) -> None:
	'''Make incomplete advertised hierarchy data retryable instead of silently omitting it.'''
	raise InvalidCatalogResponse(f'{reason}: {response.url}', response=response)


def advertised_hierarchy(response: requests.Response, record_id: str, base_url: str) -> str | None:
	'''Select a hierarchy tab only from record navigation and require the same library/record.'''
	soup = BeautifulSoup(response.text, 'html.parser')
	main = soup.select_one('.record .mainbody') or soup.select_one('.record') or soup
	base = urlsplit(base_url)
	for anchor in main.select('.nav-tabs a[href]'):
		url = urlsplit(urljoin(base_url, anchor['href']))
		if not url.path.rstrip('/').endswith('/HierarchyTree'):
			continue
		path = re.fullmatch(r'/elib/(Record|Collection)/([^/]+)/HierarchyTree/?', url.path)
		if url.scheme != base.scheme or url.netloc != base.netloc or not path or path[2] != record_id:
			reject(response, 'Hierarchy tab does not belong to the requested catalog record')
		if any(value != record_id for value in parse_qs(url.query).get('recordID', [])):
			reject(response, 'Hierarchy tab recordID does not match the requested record')
		return urlunsplit((url.scheme, url.netloc, url.path, url.query, ''))
	return None


def hierarchy_identifiers(response: requests.Response, record_id: str) -> list[str]:
	'''Match declared tree identity and context to its Record or Collection tab route.'''
	path = re.fullmatch(r'/elib/(Record|Collection)/([^/]+)/HierarchyTree/?', urlsplit(response.url).path)
	if not path or path[2] != record_id:
		reject(response, 'Hierarchy tab route does not match the requested record')
	soup = BeautifulSoup(response.text, 'html.parser')
	tree = soup.select_one('#hierarchyTree')
	if tree is None:
		reject(response, 'Missing advertised hierarchy tree container')
	def values(selector: str) -> list[str]:
		'''Read all values from one declared tree identity input class.'''
		return [str(node.get('value', '')).strip() for node in tree.select(selector)]
	if values('input.hiddenRecordId') != [record_id] or values('input.hiddenContext') != [path[1]]:
		reject(response, 'Hierarchy tab record or context does not match the requested record')
	identifiers = values('input.hiddenHierarchyId')
	if not identifiers or any(not RECORD_ID.fullmatch(identifier) for identifier in identifiers):
		reject(response, 'Hierarchy tab has no valid tree identifier')
	return list(dict.fromkeys(identifiers))


def parse_hierarchy(response: requests.Response, record_id: str, hierarchy_id: str, base_url: str) -> dict[str, Any]:
	'''Validate a complete tree containing the requested record, retaining each parent edge.'''
	try:
		payload = response.json()
	except ValueError as error:
		raise InvalidCatalogResponse(f'Malformed hierarchy JSON: {response.url}', response=response) from error
	if isinstance(payload, list) and len(payload) == 1:
		payload = payload[0]
	if not isinstance(payload, dict) or payload.get('id') != hierarchy_id:
		reject(response, 'Hierarchy JSON root does not match the advertised tree')
	base = urlsplit(base_url)
	nodes = []
	seen = set()
	pending = [(payload, None)]
	while pending:
		node, parent = pending.pop()
		if not isinstance(node, dict):
			reject(response, 'Hierarchy contains a malformed node')
		identifier = node.get('id')
		attributes, link = node.get('li_attr'), node.get('a_attr')
		if (not isinstance(identifier, str) or not RECORD_ID.fullmatch(identifier)
			or not isinstance(attributes, dict) or attributes.get('data-recordid') != identifier
			or not isinstance(link, dict) or not isinstance(link.get('href'), str)):
			reject(response, 'Hierarchy node lacks consistent record identifiers and a catalog link')
		if identifier in seen:
			reject(response, 'Hierarchy repeats a record with ambiguous parentage')
		seen.add(identifier)
		url = urlsplit(urljoin(base_url, link['href']))
		path = re.fullmatch(r'/elib/(Record|Collection)/([^/]+)(?:/HierarchyTree)?/?', url.path)
		if url.scheme != base.scheme or url.netloc != base.netloc or not path or path[2] != identifier:
			reject(response, 'Hierarchy node links outside its identified catalog record')
		label = node.get('text')
		if not isinstance(label, str):
			reject(response, 'Hierarchy node lacks a descriptive title')
		if label.strip():
			label = BeautifulSoup(label, 'html.parser').get_text(' ', strip=True)
			if not label:
				reject(response, 'Hierarchy node title contains no text')
		else:
			# Observed trees include explicitly untitled nodes. Keep their identity
			# and parent edge visible rather than inventing a title or dropping them.
			label = identifier
		title = link.get('title', '')
		if not isinstance(title, str):
			reject(response, 'Hierarchy node has a non-text title qualifier')
		nodes.append({'record_id': identifier, 'parent_id': parent, 'label': label,
			'title': title.strip(), 'url': urlunsplit(url), 'kind': path[1]})
		children = node.get('children', [])
		if children is False:
			children = []
		if not isinstance(children, list):
			reject(response, 'Hierarchy contains unloaded or malformed child records')
		pending.extend((child, identifier) for child in reversed(children))
	if record_id not in seen:
		reject(response, 'Hierarchy JSON omits the requested record')
	return {'hierarchy_id': hierarchy_id, 'record_id': record_id, 'nodes': nodes}
