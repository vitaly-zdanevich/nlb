'''Merge generated bibliographic metadata against its previous published baseline.

Use mwparserfromhell to identify template parameters without altering unrelated
wikitext. Human-edited fields survive; edited descriptions or owned metadata
blocks are conflicts because combining them with new catalog claims is unsafe.
'''

import re
from typing import Any

import mwparserfromhell


MARKER = re.compile(r'<!-- NLB-METADATA:([A-Za-z0-9_-]+:[A-Za-z0-9_-]+):(BEGIN|END) -->')


def information(text: str) -> Any:
	'''Require exactly one top-level Information template with unique named fields.'''
	templates = [item for item in mwparserfromhell.parse(text).filter_templates(recursive=False)
		if item.name.strip().casefold() == 'information']
	if len(templates) != 1:
		raise ValueError('Expected one unambiguous Information template')
	template = templates[0]
	names = [parameter.name.strip().casefold() for parameter in template.params]
	if len(names) != len(set(names)):
		raise ValueError('Duplicate Information parameter names')
	return template


def managed_blocks(text: str) -> dict[str, str]:
	'''Validate complete, unique, nonoverlapping managed metadata sections.'''
	matches = list(MARKER.finditer(text))
	if text.count('<!-- NLB-METADATA:') != len(matches):
		raise ValueError('Malformed metadata block marker')
	blocks = {}
	opened = None
	for match in matches:
		key, kind = match.groups()
		if kind == 'BEGIN':
			if opened is not None or key in blocks:
				raise ValueError('Nested or repeated metadata block')
			opened = match
		else:
			if opened is None or opened[1] != key:
				raise ValueError('Unmatched metadata block marker')
			blocks[key] = text[opened.start():match.end()]
			opened = None
	if opened is not None:
		raise ValueError('Incomplete metadata block')
	return blocks


def merge_generated_metadata(current_text: str, baseline_text: str, generated_text: str) -> str:
	'''Refresh unchanged generated fields, preserving licensing and human contributions.'''
	current, baseline, generated = [information(text) for text in (current_text, baseline_text, generated_text)]
	fields = [{item.name.strip(): item for item in template.params} for template in (current, baseline, generated)]
	present, old, new = fields
	def value(parameters: dict, key: str) -> str | None:
		'''Compare content independently of surrounding template-field whitespace.'''
		return str(parameters[key].value).strip() if key in parameters else None
	if 'description' not in old or 'description' not in new or value(present, 'description') not in (value(old, 'description'), value(new, 'description')):
		raise ValueError('Human-edited or missing Information description')
	before = str(current)
	for name in set(old) | set(new):
		if name == 'permission':
			continue
		if name in old and value(present, name) == value(old, name):
			if name in new:
				present[name].value = str(new[name].value)
			else:
				current.remove(present[name])
	# Preserve generator order when appending genuinely new parameters.
	for name, parameter in new.items():
		if name != 'permission' and name not in old and name not in present:
			current.add(str(parameter.name), str(parameter.value), preserve_spacing=False)
	result = current_text.replace(before, str(current), 1)
	return merge_metadata_blocks(result, baseline_text, generated_text)


def merge_metadata_blocks(current_text: str, baseline_text: str, generated_text: str) -> str:
	'''Update only proven managed sections on pages whose primary description is unowned.'''
	result = current_text
	present_blocks, old_blocks, new_blocks = [managed_blocks(text) for text in (current_text, baseline_text, generated_text)]
	for key, old_block in old_blocks.items():
		if key not in present_blocks:
			continue  # A human removed this section.
		if present_blocks[key] not in (old_block, new_blocks.get(key)):
			raise ValueError('Human-edited metadata block: ' + key)
		result = result.replace(present_blocks[key], new_blocks.get(key, ''), 1)
	for key, new_block in new_blocks.items():
		if key in old_blocks:
			continue
		if key in present_blocks:
			if present_blocks[key] != new_block:
				raise ValueError('Unowned metadata block: ' + key)
		else:
			result += '\n' + new_block
	return result
