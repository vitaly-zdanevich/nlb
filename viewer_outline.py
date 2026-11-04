'''Render embedded IIIF table-of-contents labels without listing every scan page.

Use the source's Range hierarchy rather than infer chapters from Canvas labels.
The original manifest stays in the archive; this module neither fetches linked
resources nor interprets labels as wiki markup or as catalog/rights evidence.

https://iiif.io/api/presentation/3.0/#54-range
https://iiif.io/api/presentation/2.1/#56-range
'''

from typing import Any, Callable


def range_children(node: dict) -> list[Any]:
	'''Return only explicit child ranges from IIIF 3 items or IIIF 2 members/ranges.'''
	children = []
	for key in ('items', 'members', 'ranges'):
		values = node.get(key)
		if not isinstance(values, list):
			continue
		for value in values:
			if isinstance(value, dict) and value.get('type', value.get('@type')) in ('Range', 'sc:Range'):
				children.append(value)
			elif key == 'ranges' and isinstance(value, str):
				children.append(value)
	return children


def outline_entries(manifest: dict, label_text: Callable[[Any], str]) -> list[str]:
	'''Keep complete named range paths, resolving only references embedded in this manifest.

	IIIF 2 can list child ranges beside their parents; IIIF 3 nests them. Both
	forms retain hierarchy and repeated labels in different contexts. Reference
	cycles terminate without recursive calls. Canvas labels and thumbnail-only
	navigation are omitted; no meaningful outline is truncated for display.
	'''
	structures = manifest.get('structures')
	if not isinstance(structures, list):
		return []
	index: dict[str, dict] = {}
	referenced: set[str] = set()
	pending = list(reversed(structures))
	seen_nodes = set()
	while pending:
		node = pending.pop()
		if not isinstance(node, dict) or node.get('type', node.get('@type')) not in ('Range', 'sc:Range'):
			continue
		if id(node) in seen_nodes:
			continue
		seen_nodes.add(id(node))
		identifier = node.get('id', node.get('@id'))
		if isinstance(identifier, str):
			# Complete definitions supplement reference objects without replacing
			# their useful labels with an absent field.
			index[identifier] = {**index.get(identifier, {}), **node}
		children = range_children(node)
		for child in children:
			child_id = child if isinstance(child, str) else child.get('id', child.get('@id'))
			if isinstance(child_id, str):
				referenced.add(child_id)
		pending.extend(reversed(children))

	roots = [node for node in structures if isinstance(node, dict)
		and node.get('type', node.get('@type')) in ('Range', 'sc:Range')
		and (not isinstance(node.get('id', node.get('@id')), str)
			or node.get('id', node.get('@id')) not in referenced)]
	# A cyclic source may have no unreferenced root; retain its labels once per
	# available path, stopping whenever a node would re-enter its own ancestry.
	stack = [(node, (), frozenset()) for node in reversed(roots or structures)]
	entries: list[str] = []
	seen_entries: set[str] = set()
	visited_paths: set[tuple] = set()
	while stack:
		node, parents, ancestors = stack.pop()
		identifier = node if isinstance(node, str) else node.get('id', node.get('@id')) if isinstance(node, dict) else None
		if isinstance(identifier, str):
			node = index.get(identifier, node)
		if not isinstance(node, dict) or node.get('type', node.get('@type')) not in ('Range', 'sc:Range'):
			continue
		identity = identifier if isinstance(identifier, str) else id(node)
		if identity in ancestors or (identity, parents) in visited_paths:
			continue
		visited_paths.add((identity, parents))
		behavior = node.get('behavior', [])
		if isinstance(behavior, str):
			behavior = [behavior]
		if not isinstance(behavior, list):
			behavior = []
		if 'thumbnail-nav' in behavior:
			continue
		label = label_text(node['label']).strip() if node.get('label') else ''
		path = parents
		if label and 'no-nav' not in behavior:
			path = (*parents, label)
			entry = ' / '.join(path)
			if entry not in seen_entries:
				entries.append(entry)
				seen_entries.add(entry)
		stack.extend((child, path, ancestors | {identity}) for child in reversed(range_children(node)))
	return entries
