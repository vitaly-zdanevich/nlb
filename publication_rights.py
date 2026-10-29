'''Conservative catalog evidence for publication filtering and Commons licenses.

This is an upload policy, not a country-by-country copyright determination.
Unknown or conflicting evidence requires review; an explicitly selected license
records the operator's attestation. Known creative contributors must all have
unambiguous death dates more than 100 years ago for automatic selection.

References:
https://commons.wikimedia.org/wiki/Template:PD-old-assumed-expired
https://commons.wikimedia.org/wiki/Template:PD-old-auto-expired
https://commons.wikimedia.org/wiki/Template:PD-US-expired
https://cdn.ifla.org/wp-content/uploads/files/assets/uca/unimarc_updates/BIBLIOGRAPHIC/u_b_100_update2020_online_final.pdf
'''

from datetime import datetime, timezone
import re
from typing import Any


DEFAULT_LICENSE = 'PD-scan|PD-old-assumed-expired'
YEAR_RE = re.compile(r'(?<!\d)(1\d{3}|20\d{2})(?!\d)')
# Roles follow UNIMARC Appendix B; uncertain roles are reviewed, never ignored.
# Editors are not silently recast as authors; an additional creative role counts.
# https://cdn.ifla.org/wp-content/uploads/files/assets/uca/unimarc_updates/BIBLIOGRAPHIC/u_b_appb_update2020_online_final.pdf
CREATIVE_ROLES = frozenset({
	'010', '030', '040', '070', '072', '075', '080', '090', '120', '130',
	'140', '150', '170', '180', '200', '212', '220', '230', '233', '245',
	'257', '290', '350', '360', '407', '430', '440', '480', '510', '520',
	'530', '560', '600', '690', '730', '770',
})
NONCREATIVE_ROLES = frozenset({'015', '050', '060', '065', '110', '160', '190', '240', '280', '310', '320', '340', '390', '400', '420', '450', '475', '490', '500', '580', '610', '620', '640', '650', '660'})


def _date_text(value: str) -> tuple[int | None, int | None, str]:
	'''Read explicit dates without converting approximations or reprints to facts.'''
	text = value.casefold().strip()
	years = [int(year) for year in YEAR_RE.findall(text)]
	if not years:
		return None, None, 'unknown date'
	if re.search(r'перепеч|репринт|факсим|reprint|facsim|reissue', text):
		return min(years), max(years), 'reprint requires review'
	if re.search(r'\?|около|прибл|примерно|circa|\bca\b|\bc\.', text):
		return None, None, 'uncertain publication date'
	if len(years) > 1 and re.search(r'позд|ранее|раньше|\bдо\b|после|before|after|later|earlier', text):
		return None, None, 'ambiguous qualified publication interval'
	if re.search(r'не\s+позднее|не\s+позже|not\s+later\s+than|on\s+or\s+before', text):
		return None, max(years), ''
	if re.search(r'\bдо\b|\bbefore\b|avant|vor\s', text):
		return None, max(years) - 1, ''
	if re.search(r'не\s+ранее|не\s+раньше|not\s+earlier\s+than', text):
		return min(years), None, 'open publication interval'
	if re.search(r'после|\bafter\b|nach\s', text):
		return min(years) + 1, None, 'open publication interval'
	if len(years) == 2 and re.search(r'\d{4}\s*[-–—/]\s*\d{4}|between.+and|между.+и', text):
		if years[0] > years[1]:
			return None, None, 'reversed publication interval'
		return years[0], years[1], ''
	if len(years) != 1:
		return None, None, 'ambiguous multiple publication years'
	if re.search(r'\d{4}\s*[-–—]\s*[\])]?\s*$', text):
		return years[0], None, 'open publication interval'
	# A shortened second year is not an exact single year.
	if re.search(r'\d{4}\s*[-–—/]\s*\d{1,3}(?!\d)', text):
		return None, None, 'abbreviated publication interval'
	return years[0], years[0], ''


def _coded_date(value: str) -> tuple[int | None, int | None, str]:
	'''Interpret UNIMARC 100 type and both date positions, including open ranges.'''
	if len(value) < 13:
		return None, None, 'incomplete MARC 100 date'
	kind = value[8]
	first = int(value[9:13]) if re.fullmatch(r'[12]\d{3}', value[9:13]) else None
	second = int(value[13:17]) if re.fullmatch(r'[12]\d{3}', value[13:17]) else None
	if kind in {'a', 'c'}:
		return first, None, 'open MARC publication interval'
	if kind in {'b', 'f', 'g', 'l'}:
		if second is None or (first is not None and first > second):
			return first, second, 'unresolved MARC publication interval'
		return first, second, ''
	if kind == 'e':
		return first, first, 'MARC reproduction requires review'
	if kind in {'d', 'h', 'i', 'j', 'k'} and first is not None:
		return first, first, ''
	return None, None, 'unknown MARC publication date'


def _marc_values(metadata: dict[str, Any], tag: str, code: str) -> list[str]:
	'''Read lossless subfields, falling back to older flattened metadata snapshots.'''
	if 'raw_marc_fields' in metadata:
		return [
			str(subfield.get('value', ''))
			for field in metadata['raw_marc_fields'] if field.get('tag') == tag
			if tag != '214' or field.get('ind2') == '1'
			for subfield in field.get('subfields', []) if subfield.get('code') == code
		]
	if tag == '214':
		# The legacy flattened representation has lost the required activity indicator.
		return []
	return [
		match.group(1)
		for field in metadata.get('raw_fields', []) if field.get('tag') == tag
		for match in re.finditer(r'\$' + re.escape(code) + r'\s+([^$]*)', field.get('value', ''))
	]


def _publication_evidence(record: dict[str, Any], marc: dict[str, Any]) -> dict[str, Any]:
	'''Combine compatible date intervals and retain conflicts and uncertainty.'''
	sources: list[tuple[str, str, bool]] = []
	for tag in ('210', '214'):
		sources.extend((f'MARC {tag}$d', value, False) for value in _marc_values(marc, tag, 'd') if value.strip())
	if not sources and marc.get('date'):
		sources.append(('MARC publication date', str(marc['date']), False))
	coded = _marc_values(marc, '100', 'a')
	sources.extend(('MARC 100$a', value, True) for value in coded)
	if not sources and marc.get('year'):
		sources.append(('MARC publication year', str(marc['year']), False))
	for field in record.get('fields', []):
		if field.get('name', '').strip() not in {'Опубликовано', 'Дата издания', 'Publication date'}:
			continue
		values = field.get('values') or [field.get('value', '')]
		sources.extend((f"HTML {field['name']}", str(value), False) for value in values if str(value).strip())
	evidence: list[str] = []
	intervals: list[tuple[int | None, int | None]] = []
	issues: list[str] = []
	for label, value, coded in sources:
		entry = f'{label}: {value}'
		if entry in evidence:
			continue
		evidence.append(entry)
		lower, upper, issue = _coded_date(value) if coded else _date_text(value)
		intervals.append((lower, upper))
		if issue:
			issues.append(issue)
	lowers = [lower for lower, _ in intervals if lower is not None]
	uppers = [upper for _, upper in intervals if upper is not None]
	# Summaries often repeat only a range's first year. Keep the widest compatible
	# interval so a coded beginning year cannot hide a later possible publication.
	lower = min(lowers) if lowers and len(lowers) == len(intervals) else None
	upper = max(uppers) if uppers and len(uppers) == len(intervals) else None
	if lowers and uppers and max(lowers) > min(uppers):
		issues.insert(0, 'conflicting publication dates')
		lower, upper = None, None
	return {'lower_year': lower, 'upper_year': upper, 'evidence': evidence, 'issues': issues}


def publication_year(record_meta: dict[str, Any], marc_meta: dict[str, Any]) -> int | None:
	'''Return a defensible lower publication year for filtering, never title numbers.

	Conflicting, approximate or reproduction evidence does not yield a scalar year.
	A bounded range yields its lower bound; before-only dates have no lower bound.
	'''
	evidence = _publication_evidence(record_meta, marc_meta)
	if any('conflict' in issue or 'uncertain' in issue or 'ambiguous' in issue or 'reprint' in issue or 'reproduction' in issue for issue in evidence['issues']):
		return None
	return evidence['lower_year']


def _creator_evidence(record: dict[str, Any], marc: dict[str, Any]) -> tuple[list[int | None], list[str], bool]:
	'''Resolve creative people only; owners and organizations never supply deaths.'''
	contributors = marc.get('contributors') or []
	if not contributors:
		names = list(marc.get('authors') or [])
		if not names:
			for field in record.get('fields', []):
				if field.get('name') in {'Автор', 'Авторы', 'Другие авторы'}:
					names.extend(field.get('values') or [field.get('value', '')])
		contributors = [{'name': str(name), 'tag': '700', 'roles': ['070']} for name in names]
	deaths: list[int | None] = []
	evidence: list[str] = []
	has_dates = False
	for person in contributors:
		roles = set(person.get('roles') or [])
		if roles and roles <= NONCREATIVE_ROLES:
			continue
		if not roles and person.get('tag') not in {'700', '701', '710', '711'}:
			continue
		name = str(person.get('name', ''))
		if person.get('kind') == 'organization' or person.get('tag') in {'710', '711', '712'}:
			deaths.append(None)
			has_dates = True
			evidence.append(f'Corporate creator requires review: {name}')
			continue
		if roles and not roles <= CREATIVE_ROLES | NONCREATIVE_ROLES:
			deaths.append(None)
			has_dates = True
			evidence.append(f'Unresolved contributor role: {name}')
			continue
		dates = person.get('dates')
		if dates is None:
			dates = [part for part in re.findall(r'\(([^()]*)\)', name) if YEAR_RE.search(part)]
		dates = list(dict.fromkeys(str(value).strip() for value in dates if str(value).strip()))
		death = None
		if dates:
			has_dates = True
			evidence.append(f"Creator dates ({name}): {'; '.join(dates)}")
			if len(dates) == 1:
				match = re.fullmatch(r'([12]\d{3})?\s*[-–—]\s*([12]\d{3})', dates[0])
				if match and (match.group(1) is None or int(match.group(1)) <= int(match.group(2))):
					death = int(match.group(2))
		deaths.append(death)
	return deaths, evidence, has_dates


def evaluate_publication(
	record_meta: dict[str, Any],
	marc_meta: dict[str, Any],
	requested_license: str | None = DEFAULT_LICENSE,
	current_year: int | None = None,
	*,
	explicit_license: bool = False,
) -> dict[str, Any]:
	'''Return auditable eligibility, selected license and publication-date evidence.

	An explicit custom license is an operator attestation, not an automatically
	verified copyright claim. The default path requires bounded, compatible dates,
	US publication expiry, and either >120 publication years with absent creator
	death evidence or >100 years after every resolved creative contributor's death.
	'''
	year = current_year if current_year is not None else datetime.now(timezone.utc).year
	license_name = requested_license or DEFAULT_LICENSE
	dates = _publication_evidence(record_meta, marc_meta)
	deaths, creator_evidence, has_dates = _creator_evidence(record_meta, marc_meta)
	decision = {
		'eligible': False, 'reason': '', 'license': license_name, 'basis': 'rights_review',
		'evidence': dates['evidence'] + creator_evidence,
		'lower_year': dates['lower_year'], 'upper_year': dates['upper_year'],
	}
	if explicit_license or license_name != DEFAULT_LICENSE:
		decision.update(eligible=True, reason='Explicit license supplied by operator', basis='user_attestation')
		decision['evidence'].append(f'Operator attestation: {license_name}')
		return decision
	if dates['issues']:
		decision['reason'] = 'rights_review: ' + '; '.join(dict.fromkeys(dates['issues']))
		return decision
	upper = dates['upper_year']
	if upper is None:
		decision['reason'] = 'rights_review: publication date is unknown or unbounded'
	elif upper > year - 96:
		decision['reason'] = 'rights_review: publication has not reached the United States expired-publication cutoff'
	elif has_dates:
		if not deaths or any(death is None for death in deaths):
			decision['reason'] = 'rights_review: creative contributor death dates are incomplete or uncertain'
		elif max(deaths) >= year - 100:
			decision['reason'] = 'rights_review: latest creator death does not meet the conservative >100-year policy'
		else:
			latest = max(deaths)
			decision.update(eligible=True, reason='All recorded creative deaths meet the conservative >100-year policy; publication meets the US cutoff', basis='known_death_dates', license=f'PD-scan|PD-old-auto-expired|deathyear={latest}')
	elif upper >= year - 120:
		decision['reason'] = 'rights_review: publication is not more than 120 years old'
	else:
		decision.update(eligible=True, reason='Bounded publication evidence is more than 120 years old; no recorded creative death dates', basis='assumed_age')
	return decision
