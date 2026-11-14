'''Keep a known Commons association independent of a later source-read attempt.'''

import copy
from typing import Any


PUBLISHED_STATUSES = frozenset({'uploaded', 'existing_title', 'duplicate_sha1', 'verification_failed'})
COMPLETED_PUBLICATIONS = PUBLISHED_STATUSES - {'verification_failed'}
BINARY_FIELDS = ('sha1', 'sha256', 'size_bytes', 'commons_title', 'duplicates', 'verification', 'pdf_path', 'pdf_validation')


def publication_evidence(item: dict[str, Any]) -> dict[str, Any]:
	'''Read durable publication evidence, including supported older flat state entries.

	Incomplete legacy evidence is retained without pretending that it is enough
	to verify a binary or edit Commons. Callers still validate required fields.
	'''
	stored = item.get('publication')
	if isinstance(stored, dict) and stored.get('status') in PUBLISHED_STATUSES:
		return copy.deepcopy(stored)
	status = item.get('status')
	if status not in PUBLISHED_STATUSES:
		status = item.get('previous_status')
	if status not in PUBLISHED_STATUSES:
		return {}
	return {'status': status, **{key: copy.deepcopy(item[key]) for key in (*BINARY_FIELDS, 'updated_at') if key in item}}


def with_publication_evidence(item: dict[str, Any]) -> dict[str, Any]:
	'''Read the canonical binary without filling its gaps from a different attempt.'''
	result = copy.deepcopy(item)
	publication = publication_evidence(item)
	if publication:
		for key in BINARY_FIELDS:
			result.pop(key, None)
		result.update({key: value for key, value in publication.items() if key != 'status'})
	return result


def retain_publication(previous: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
	'''Record an attempt without replacing an earlier published binary with failed work.

	Flat identity fields remain available to existing archive readers. A different
	newly downloaded binary is recorded separately on failure, so its hash/size
	cannot accidentally be combined with the old Commons title or verification.
	'''
	entry = copy.deepcopy(result)
	old = publication_evidence(previous)
	if entry.get('status') in COMPLETED_PUBLICATIONS:
		entry.pop('publication', None)
		entry['publication'] = publication_evidence(entry)
		entry.pop('previous_status', None)
		entry.pop('attempted_binary', None)
	elif old:
		attempt = {key: copy.deepcopy(entry[key]) for key in BINARY_FIELDS if key in entry}
		if any(key in attempt and attempt[key] != old.get(key) for key in ('sha1', 'sha256', 'size_bytes')):
			entry['attempted_binary'] = attempt
		elif previous.get('attempted_binary'):
			entry['attempted_binary'] = copy.deepcopy(previous['attempted_binary'])
		for key in BINARY_FIELDS:
			entry.pop(key, None)
			if key in old:
				entry[key] = copy.deepcopy(old[key])
		entry['publication'] = old
		entry['previous_status'] = old['status']
	elif entry.get('status') == 'verification_failed':
		entry['publication'] = publication_evidence(entry)
	return entry
