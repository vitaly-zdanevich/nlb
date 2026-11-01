'''Keep NLB viewer structure and check its catalog-to-scan volume associations.

NLB supplies proprietary JSON, including standalone TEXT headings for following
siblings. File paths and FILE labels such as Ч. 1 identify scan chunks, not books.
Unsupported or contradictory volume evidence remains available for human review.
'''

import re
from typing import Any


VOLUME_PREFIX = r'(?:том|tom|tome|volume|vol\.?|band|bd\.?|[тt]\.)'
VOLUME_HEADING = re.compile(r'^' + VOLUME_PREFIX + r'\s*(.+)$', re.IGNORECASE)
TITLE_VOLUME = re.compile(r'(?<!\w)' + VOLUME_PREFIX + r'\s*([0-9]+|[IVXLCDM]+)\.?\s*$', re.IGNORECASE)
ROMAN = re.compile(r'M{0,3}(CM|CD|D?C{0,3})(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})')


def volume_number(value: str) -> int | None:
	'''Accept positive Arabic or canonical Roman numbers, excluding ranges.'''
	value = value.strip().rstrip('.').upper()
	if value.isascii() and value.isdecimal():
		return int(value) or None
	if not value or not ROMAN.fullmatch(value):
		return None
	values = {'I': 1, 'V': 5, 'X': 10, 'L': 50, 'C': 100, 'D': 500, 'M': 1000}
	total = previous = 0
	for letter in reversed(value):
		current = values[letter]
		total += -current if current < previous else current
		previous = max(previous, current)
	return total


def heading_volume(label: str) -> tuple[int | None, bool]:
	'''Distinguish a generic heading from an unsupported explicit volume heading.'''
	match = VOLUME_HEADING.fullmatch(label.strip())
	if not match:
		return None, False
	number = volume_number(match[1])
	return number, number is None


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
					context = {'headings': headings, 'volume_headings': [*parent_scopes, *sibling_scope], 'file_label': label, 'file_location': location}
					if context not in entry['mets_contexts']:
						entry['mets_contexts'].append(context)
			if children:
				walk(children, [*headings, *([label] if label else [])], [*parent_scopes, *sibling_scope])

	walk(structure.get('nodes') or [], [], [])
	for entry in by_id.values():
		entry.update(mets_volumes=sorted(volumes), mets_unresolved_volume=unresolved)
	return list(by_id.values())


def assess_scan_identity(file_entry: dict, record_meta: dict, marc_meta: dict) -> dict[str, Any]:
	'''Require consistent volume scope for grouped or bound-with viewers.

	A sole viewer volume can describe an ordinary record without a volume suffix.
	Multiple volumes or bound-with records require an explicit catalog match.
	'''
	catalog_volumes: set[int] = set()
	unsupported = False
	related = marc_meta.get('related_records') or []
	bound_with = any(item.get('tag') in ('481', '482') for item in related)
	for item in related:
		if item.get('tag') == '461':
			number, unknown = heading_volume(item.get('volume') or '')
			if number is not None:
				catalog_volumes.add(number)
			unsupported |= unknown
	for title in (record_meta.get('title'), marc_meta.get('title')):
		match = TITLE_VOLUME.search(title or '')
		if match:
			number = volume_number(match[1])
			if number is not None:
				catalog_volumes.add(number)
			else:
				unsupported = True
	contexts = file_entry.get('mets_contexts') or []
	viewer_volumes = set(file_entry.get('mets_volumes') or [])
	scopes = []
	for context in contexts:
		current = set()
		for label in context.get('volume_headings', context['headings']):
			number, unknown = heading_volume(label)
			if number is not None:
				current.add(number)
			unsupported |= unknown
		scopes.append(current)
	context = {'catalog_volumes': sorted(catalog_volumes), 'viewer_volumes': sorted(viewer_volumes), 'bound_with': bound_with, 'file': file_entry}
	reason = ''
	if (unsupported and (viewer_volumes or bound_with)) or file_entry.get('mets_unresolved_volume'):
		reason = 'Unsupported or uncertain volume notation'
	elif len(catalog_volumes) > 1:
		reason = 'Catalog volume statements disagree'
	elif len({(tuple(item['headings']), item['file_location']) for item in contexts}) > 1:
		reason = 'The same file occurs in conflicting viewer contexts'
	elif any(len(scope) > 1 for scope in scopes):
		reason = 'Nested viewer volume headings disagree'
	elif viewer_volumes or bound_with:
		file_volumes = set().union(*scopes) if scopes else set()
		if len(file_volumes) != 1:
			reason = 'Shared viewer has no explicit volume for this scan'
		elif not catalog_volumes and (len(viewer_volumes) > 1 or bound_with):
			reason = 'Shared viewer requires an explicit catalog volume'
		elif catalog_volumes and file_volumes != catalog_volumes:
			reason = 'Viewer volume differs from the catalog record'
	return {'eligible': not reason, 'reason': reason, 'context': context}
