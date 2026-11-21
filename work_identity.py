'''Identify explicitly titled bound works without fuzzy bibliographic matching.

Only FILE labels supply positive title evidence. A preceding TEXT heading may
belong to another work. Main titles must be distinctive and unique across all
files; author, subtitle and date qualifiers must also agree when supplied.
Original labels remain in the archived METS context.
'''

from collections import defaultdict
import re
import unicodedata

from publication_rights import publication_year


def words(value: str) -> tuple[str, ...]:
	'''Normalize typography and case, retaining accents, words and numbers.'''
	return tuple(re.findall(r'[^\W_]+', unicodedata.normalize('NFKC', value).casefold()))


def file_work_label(label: str) -> tuple[str, str, str]:
	'''Read the first quoted title or an unquoted title before a subtitle colon.

	A leading Arabic display index is not a volume. Text outside quotes is kept
	for later author/qualifier validation, never silently discarded.
	'''
	label = re.sub(r'^\s*\d+\.\s+', '', label).strip()
	if '\ufffd' in label:
		return '', '', ''
	quoted = re.search(r'«([^»]+)»|"([^"]+)"|“([^”]+)”', label)
	if quoted:
		return next(value for value in quoted.groups() if value is not None), label[:quoted.start()].strip(), label[quoted.end():].strip()
	main, separator, suffix = label.partition(':')
	return main.strip(), '', suffix.strip() if separator else ''


def index_work_titles(files: list[dict]) -> None:
	'''Recompute title uniqueness across every METS contributing to a record.

	Repeated occurrences of one ID must all name the same work. Two distinct
	PDFs bearing the same title stay ambiguous, even if their authors differ.
	'''
	owners: dict[tuple[str, ...], set[str]] = defaultdict(set)
	for entry in files:
		entry.pop('unique_work_title', None)
		for context in entry.get('mets_contexts', []):
			title, _, _ = file_work_label(context.get('file_link_name', ''))
			owners[words(title)].add(entry['file_id'])
	for entry in files:
		titles = {words(file_work_label(context.get('file_link_name', ''))[0]) for context in entry.get('mets_contexts', [])}
		if len(titles) == 1:
			title, = titles
			if len(title) >= 2 and len(' '.join(title)) >= 15 and len(owners[title]) == 1:
				entry['unique_work_title'] = ' '.join(title)


def creator_names(metadata: dict) -> set[tuple[str, ...]]:
	'''Use named authors/composers and explicit aliases, excluding related people.

	Parenthesized full given names can expand catalog initials. Both catalog
	(surname first) and natural name order are accepted; spelling is unchanged.
	'''
	names = set(metadata.get('authors') or [])
	for item in metadata.get('contributors', []):
		roles = set(item.get('roles') or [])
		if item.get('kind') == 'person' and (roles & {'070', '230'} or (not roles and item.get('tag') in {'700', '701'})):
			names.add(item['name'])
	primary = set(names)
	parallel = {item['name'] for item in metadata.get('personal_name_variants', [])
		if primary.intersection(item.get('main_names', [])) and item.get('type') == '500'
		and 'w' in item.get('codes', []) and not item.get('context')}
	for item in metadata.get('personal_name_variants', []):
		if (primary.intersection(item.get('main_names', []))
			and (not item.get('context') or item['context'] in parallel)
			and (item.get('type') == '400' or item['name'] in parallel)):
			names.add(item['name'])
	result = set()
	for name in names:
		name = re.sub(r'\([^)]*\d[^)]*\)', '', name).strip()
		result.add(words(name))
		match = re.match(r'^([^,]+),\s*([^()]+)\(([^()\d]+)\)', name)
		if match:
			result.add(words(match[1] + ' ' + match[3]))
			result.add(words(match[3] + ' ' + match[1]))
	return result


def qualifier_agrees(value: str, record_meta: dict, marc_meta: dict) -> bool:
	'''Require bibliographic qualifiers to agree; retain a known missing-pages note.'''
	value = value.strip(' \t:;.,—–-()')
	if not value or words(value) == ('адсутничае', 'пачатак', 'выдання'):
		return True
	date = re.fullmatch(r'(1\d{3}|20\d{2})\s*(?:rok|год)?', value, re.IGNORECASE)
	if date:
		return publication_year(record_meta, marc_meta) == int(date[1])
	qualifier = words(value)
	subtitle = words(marc_meta.get('subtitle') or '')
	return bool(qualifier and subtitle[:len(qualifier)] == qualifier)


def matches_unique_work(entry: dict, record_meta: dict, marc_meta: dict) -> bool:
	'''Match an unnumbered work only when all FILE occurrences support its identity.'''
	title = words(marc_meta.get('title') or '')
	if not title or '\ufffd' in (marc_meta.get('title') or '') or ' '.join(title) != entry.get('unique_work_title'):
		return False
	# Series/part requirements cannot be bypassed by a matching general title.
	if any(item.get('tag') == '461' and item.get('volume') for item in marc_meta.get('related_records', [])):
		return False
	if re.search(r'(?<!\w)(?:том|tom|tome|volume|vol\.?|band|bd\.?|[тt]\.|ч\.|cz\.?|część|part|ser\.?|series)\s*[\dIVXLCDM]+\b', marc_meta.get('title') or '', re.IGNORECASE):
		return False
	for item in [*record_meta.get('record_links', []), *marc_meta.get('related_records', [])]:
		if item.get('record_id') == record_meta.get('marc_id'):
			continue
		other = re.split(r'\s*[:/\[]', item.get('title') or '', maxsplit=1)[0]
		if words(other) == title:
			return False
	names = creator_names(marc_meta)
	contexts = entry.get('mets_contexts') or []
	for context in contexts:
		candidate, author, qualifier = file_work_label(context.get('file_link_name', ''))
		if words(candidate) != title or (author and words(author) not in names):
			return False
		if not all(qualifier_agrees(value, record_meta, marc_meta) for value in (qualifier, context.get('file_description', ''))):
			return False
	return bool(contexts)
