'''Parse complete bibliographic volume and part labels from NLB metadata.

Only explicit volume expressions establish a scope. Standalone parts can be
scan chunks, and series require a separate identity dimension, so neither is
interpreted here. Callers must retain and reject unparsed catalog designations.
'''

import re


_VOLUME_PREFIX = r'(?:том|tome|tom|volume|vol\.?|band|bd\.?|[тt]\.)'
_PART_PREFIX = r'(?:część|cz\.|часть|ч\.|part|pt\.?)'
_NUMBER = r'(?:[0-9]+|[IVXLCDM]+)'
_VOLUME_TOKEN = re.compile(
	r'^' + _VOLUME_PREFIX + r'\s*(?P<number>' + _NUMBER + r')(?P<rest>.*)$',
	re.IGNORECASE,
)
_VOLUME_START = re.compile(
	r'^(?:(?:том|tome|tom|volume|vol|band|bd)(?=\s|[.,]|$)|[тt]\.)',
	re.IGNORECASE,
)
_PART = re.compile(_PART_PREFIX + r'\s*(' + _NUMBER + r')\.?', re.IGNORECASE)
_SEPARATOR = re.compile(r'(?:\.\s*|\s*,\s*|\s+)')
_ROMAN = re.compile(r'M{0,3}(CM|CD|D?C{0,3})(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})')


def volume_number(value: str) -> int | None:
	'''Accept positive Arabic or canonical Roman numbers, excluding ranges.'''
	value = value.strip().rstrip('.').upper()
	if value.isascii() and value.isdecimal():
		return int(value) or None
	if not value or not _ROMAN.fullmatch(value):
		return None
	values = {'I': 1, 'V': 5, 'X': 10, 'L': 50, 'C': 100, 'D': 500, 'M': 1000}
	total = previous = 0
	for letter in reversed(value):
		current = values[letter]
		total += -current if current < previous else current
		previous = max(previous, current)
	return total


def parse_volume_scope(label: str) -> tuple[dict[str, int], bool]:
	'''Return a complete volume/optional-part scope and unsupported-volume flag.

	Catalog brackets may enclose the whole expression or its part designation.
	Ranges, invalid numerals and extra qualifiers invalidate the entire scope;
	part evidence is never silently discarded from an otherwise valid volume.
	A label without an explicit volume returns an empty scope and a false flag.
	'''
	text = label.strip()
	if text.startswith('[') and text.endswith(']'):
		text = text[1:-1].strip()
	match = _VOLUME_TOKEN.fullmatch(text)
	if not match:
		return {}, bool(_VOLUME_START.match(text.lstrip('[').lstrip()))
	volume = volume_number(match['number'])
	if volume is None:
		return {}, True
	rest = match['rest']
	if rest.strip() in ('', '.'):
		return {'volume': volume}, False
	separator = _SEPARATOR.match(rest)
	if not separator:
		return {}, True
	part_label = rest[separator.end():].strip()
	if part_label.startswith('[') and part_label.endswith(']'):
		part_label = part_label[1:-1].strip()
	part_match = _PART.fullmatch(part_label)
	if not part_match:
		return {}, True
	part = volume_number(part_match[1])
	if part is None:
		return {}, True
	return {'volume': volume, 'part': part}, False
