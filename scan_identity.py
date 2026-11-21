'''Keep NLB viewer structure and check its catalog-to-scan volume associations.

NLB supplies proprietary JSON, including standalone TEXT headings for following
siblings. Bare part labels such as Ч. 1 identify scan chunks, not books.
Unsupported or contradictory volume evidence remains available for human review.
'''

import re
from typing import Any

from volume_scope import parse_volume_scope
from work_identity import index_work_titles, matches_unique_work

VOLUME_PREFIX = r'(?:том|tome|tom|volume|vol\.?|band|bd\.?|[тt]\.)'
TITLE_VOLUME = re.compile(r'(?<!\w)\[?' + VOLUME_PREFIX + r'\s*(?=[0-9IVXLCDM])', re.IGNORECASE)
TITLE_PART = re.compile(r'(?<!\w)(?:cz\.|część|часть|ч\.|part|pt\.?)\s*[0-9IVXLCDM]+\b', re.IGNORECASE)
TITLE_SERIES = re.compile(r'(?<!\w)(?:ser\.?|series|seria|serya|серия|сер\.)\s*[0-9IVXLCDM]+\b', re.IGNORECASE)
GENERIC_WORK_HEADINGS = {'contents', 'содержание', 'конволют', 'канвалют', 'convolut', 'spis treści'}


def title_scope(title: str) -> tuple[dict[str, int], bool]:
	'''Read a final catalog volume/part expression without dropping series evidence.'''
	matches = list(TITLE_VOLUME.finditer(title))
	scope, unsupported = parse_volume_scope(title[matches[-1].start():].split(':', 1)[0]) if matches else ({}, False)
	unsupported |= bool(TITLE_SERIES.search(title))
	parts = list(TITLE_PART.finditer(title))
	if parts and (not scope.get('part') or any(part.start() < matches[-1].start() for part in parts)):
		unsupported = True
	return scope, unsupported


def heading_volume(label: str) -> tuple[int | None, bool]:
	'''Distinguish a generic heading from an unsupported explicit volume heading.'''
	scope, unsupported = parse_volume_scope(label)
	return scope.get('volume'), unsupported


def index_file_volumes(files: list[dict]) -> None:
	'''Do not select a bare FILE volume repeated for different works or PDFs.

	Existing TEXT-scoped chunks remain grouped by their headings. Newly accepted
	FILE volume labels require a unique volume/part scope across all viewers.
	'''
	owners: dict[tuple, set[str]] = {}
	file_scopes: dict[str, set[tuple]] = {}
	for entry in files:
		entry.pop('ambiguous_file_volume', None)
		for context in entry.get('mets_contexts', []):
			scope = {}
			for label in [*context.get('volume_headings', context['headings']), context.get('file_link_name', ''), context.get('file_description', '')]:
				parsed, _ = parse_volume_scope(label)
				scope.update(parsed)
			if scope:
				key = tuple(sorted(scope.items()))
				owners.setdefault(key, set()).add(entry['file_id'])
				if any(parse_volume_scope(context.get(field, ''))[0] for field in ('file_link_name', 'file_description')):
					file_scopes.setdefault(entry['file_id'], set()).add(key)
	for entry in files:
		if any(len(owners[scope]) > 1 for scope in file_scopes.get(entry['file_id'], set())):
			entry['ambiguous_file_volume'] = True


def flatten_mets(structure: dict[str, Any]) -> list[dict[str, Any]]:
	'''Preserve ordered sibling/ancestor headings and every occurrence of a file ID.'''
	by_id: dict[str, dict[str, Any]] = {}
	volumes: set[int] = set()
	unresolved = False

	def walk(nodes: list, parents: list[str], parent_scopes: list[str]) -> None:
		'''A standalone TEXT scopes following siblings until the next heading.'''
		nonlocal unresolved
		sibling_heading: list[str] = []
		sibling_scope: list[str] = []
		for node in nodes:
			if not isinstance(node, dict):
				continue
			data = node.get('data') or {}
			if not data.get('visible', True):
				if data.get('nodeType') == 'TEXT' and not node.get('children'):
					# A hidden heading cannot make following files inherit the
					# preceding book's metadata merely because its label is omitted.
					sibling_heading, sibling_scope = [], []
				continue
			label = ' — '.join(str(data.get(key) or '').strip() for key in ('linkName', 'description') if str(data.get(key) or '').strip())
			children = node.get('children') or []
			kind = data.get('nodeType')
			if kind != 'FILE':
				scope_label = str(data.get('linkName') or data.get('description') or '').strip()
				number, unsupported = heading_volume(scope_label)
				if number is not None:
					volumes.add(number)
				unresolved |= unsupported
				# Nested groups have their own extent; they replace sibling context
				# within the group but cannot change scope after returning to this level.
				if children:
					inherit_sibling = kind != 'TEXT' and number is None and not unsupported
					walk(children,
						[*parents, *(sibling_heading if inherit_sibling else []), *([label] if label else [])],
						[*parent_scopes, *(sibling_scope if inherit_sibling else []), scope_label])
				elif kind == 'TEXT':
					sibling_heading = [label] if label else []
					sibling_scope = [scope_label] if scope_label else []
				continue
			headings = [*parents, *sibling_heading]
			if not data.get('elibLocal'):
				file_info = data.get('file') or {}
				raw_id = file_info.get('id')
				fid = str(raw_id).strip() if raw_id is not None else ''
				if fid:
					location = str(file_info.get('fileLocation') or '')
					entry = by_id.setdefault(fid, {'file_id': fid, 'part_label': ' / '.join([*headings, *([label] if label else [])]), 'file_location': location, 'mets_contexts': []})
					context = {'headings': headings, 'volume_headings': [*parent_scopes, *sibling_scope], 'file_label': label, 'file_location': location,
						'file_link_name': str(data.get('linkName') or '').strip(), 'file_description': str(data.get('description') or '').strip()}
					for scope_label in (context['file_link_name'], context['file_description']):
						number, _ = heading_volume(scope_label)
						if number is not None:
							volumes.add(number)
					if context not in entry['mets_contexts']:
						entry['mets_contexts'].append(context)
			if children:
				walk(children, [*headings, *([label] if label else [])], [*parent_scopes, *sibling_scope])

	walk(structure.get('nodes') or [], [], [])
	for entry in by_id.values():
		entry.update(mets_volumes=sorted(volumes), mets_unresolved_volume=unresolved)
	index_work_titles(list(by_id.values()))
	index_file_volumes(list(by_id.values()))
	return list(by_id.values())


def assess_scan_identity(file_entry: dict, record_meta: dict, marc_meta: dict) -> dict[str, Any]:
	'''Require consistent volume/part scope or a uniquely identified bound work.

	A sole viewer volume can describe an ordinary record without a volume suffix.
	Multiple volumes, foreign viewer anchors and bound-with records require an
	explicit catalog match even when the viewer omits volume headings. An exact
	FILE work title can identify an unnumbered work; it cannot override volumes.
	'''
	catalog_volumes: set[int] = set()
	catalog_parts: set[int] = set()
	unsupported = False
	related = marc_meta.get('related_records') or []
	bound_with = any(item.get('tag') in ('481', '482') for item in related)
	shared_anchor = bool(file_entry.get('shared_viewers'))
	for item in related:
		if item.get('tag') == '461':
			label = item.get('volume') or ''
			scope, unknown = parse_volume_scope(label)
			if scope:
				catalog_volumes.add(scope['volume'])
				if 'part' in scope:
					catalog_parts.add(scope['part'])
			unsupported |= unknown or bool(label and not scope)
	for title in (record_meta.get('title'), marc_meta.get('title')):
		scope, unknown = title_scope(title or '')
		if scope:
			catalog_volumes.add(scope['volume'])
			if 'part' in scope:
				catalog_parts.add(scope['part'])
		unsupported |= unknown
	contexts = file_entry.get('mets_contexts') or []
	viewer_volumes = set(file_entry.get('mets_volumes') or [])
	scopes = []
	unverified_work_heading = False
	unsupported_file_volume = False
	for context in contexts:
		current = {'volume': set(), 'part': set()}
		labels = [*context.get('volume_headings', context['headings']),
			context.get('file_link_name', ''), context.get('file_description', '')]
		for label in labels:
			scope, unknown = parse_volume_scope(label)
			for key, value in scope.items():
				current[key].add(value)
			unsupported |= unknown
		unsupported_file_volume |= any(parse_volume_scope(context.get(field, ''))[1] for field in ('file_link_name', 'file_description'))
		if any(parse_volume_scope(context.get(field, ''))[0] for field in ('file_link_name', 'file_description')):
			unverified_work_heading |= any(label.strip(' :').casefold() not in GENERIC_WORK_HEADINGS
				and not parse_volume_scope(label)[0] for label in context.get('volume_headings', context['headings']) if label.strip())
		scopes.append(current)
	context = {'catalog_volumes': sorted(catalog_volumes), 'catalog_parts': sorted(catalog_parts), 'viewer_volumes': sorted(viewer_volumes), 'bound_with': bound_with, 'shared_anchor': shared_anchor, 'file': file_entry}
	reason = ''
	if unsupported_file_volume or (unsupported and (viewer_volumes or bound_with or shared_anchor)) or file_entry.get('mets_unresolved_volume'):
		reason = 'Unsupported or uncertain volume notation'
	elif len(catalog_volumes) > 1 or len(catalog_parts) > 1:
		reason = 'Catalog volume statements disagree'
	elif len({(tuple(item['headings']), tuple(item.get('volume_headings', [])), item['file_location'], item['file_label']) for item in contexts}) > 1:
		reason = 'The same file occurs in conflicting viewer contexts'
	elif any(len(values) > 1 for scope in scopes for values in scope.values()):
		reason = 'Nested viewer volume headings disagree'
	elif file_entry.get('ambiguous_file_volume'):
		reason = 'FILE volume label identifies more than one PDF'
	elif unverified_work_heading:
		reason = 'FILE volume belongs to an unverified work heading'
	elif viewer_volumes or bound_with or shared_anchor:
		file_volumes = set().union(*(scope['volume'] for scope in scopes))
		file_parts = set().union(*(scope['part'] for scope in scopes))
		if not catalog_volumes and not file_volumes and matches_unique_work(file_entry, record_meta, marc_meta):
			context['matched_work_title'] = file_entry['unique_work_title']
		elif len(file_volumes) != 1:
			reason = 'Shared viewer has no explicit volume for this scan'
		elif not catalog_volumes and (len(viewer_volumes) > 1 or bound_with or shared_anchor):
			reason = 'Shared viewer requires an explicit catalog volume'
		elif catalog_volumes and file_volumes != catalog_volumes:
			reason = 'Viewer volume differs from the catalog record'
		elif catalog_parts != file_parts:
			reason = 'Viewer part differs from or is missing in the catalog record'
	return {'eligible': not reason, 'reason': reason, 'context': context}
