#!/usr/bin/env python3
'''Preserve NLB catalog metadata and upload eligible scans to Wikimedia Commons.

This script discovers digitized documents in the Electronic Library of the National
Library of Belarus (https://elib.nlb.by), resolves single-file records and multi-part
METS structures, archives source metadata, and validates original PDF bytes before
checking Commons by SHA-1. New uploads use Pywikibot credentials and are verified
against the stored Commons hash and size. Temporary scans are removed; archived
metadata, optional original PDFs, and resumable progress remain available.
'''

import argparse
import ast
import copy
from datetime import datetime, timezone
import hashlib
from http.cookiejar import LoadError, MozillaCookieJar
import json
import os
from pathlib import Path
import re
import signal
import time
from typing import Any, Iterator
from urllib.parse import parse_qs, parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit
import xml.etree.ElementTree as ET
import warnings

from bs4 import BeautifulSoup, Comment
import mwparserfromhell
import requests

from catalog_requests import CatalogRetriesExhausted, InvalidCatalogResponse, get_catalog_response
from commons_verification import read_file_info
from scan_identity import assess_scan_identity, flatten_mets
from preservation import ArchiveStore, ArchiveWriteError, file_integrity
from pdf_validation import InvalidPDFError, manifest_page_count, validate_pdf
from publication_rights import evaluate_publication, publication_year
from wikimedia_text import normalize_wikitext
from viewer_api import ViewerAPIHealth, ViewerAPIUnavailable
from catalog_validation import (
	validate_access_json, validate_manifest, validate_manifest_identity, validate_marcxml, validate_mets_json,
	validate_record_html, validate_search_html, validate_toc_html,
)


ELIB_BASE_URL = 'https://elib.nlb.by'
VIEWER_BASE_URL = 'https://elib.nlb.by:8070'
VIEWER_BASIC_AUTH = 'Basic ZWxpYjppdTIzeC0yMzQ='
COMMONS_API_URL = 'https://commons.wikimedia.org/w/api.php'
COMMONS_USER_AGENT = 'nlb-commons-uploader (https://github.com/vitaly-zdanevich/nlb)'
DEFAULT_USER_AGENT = 'Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0'
DEFAULT_PYWIKIBOT_DIR = '/home/vitaly/.pywikibot'
DEFAULT_CATEGORY = 'elib.nlb.by'
DEFAULT_LICENSE = 'PD-scan|PD-old-assumed-expired'
DEFAULT_UNTIL_YEAR = 1900
SEARCH_PAGE_SIZE = 100
UPLOAD_CHUNK_SIZE = 4 * 1024 * 1024
UPLOAD_MAX_RETRIES = 10
DOWNLOAD_MAX_ATTEMPTS = 5
DOWNLOAD_RETRY_DELAY = 5
DOWNLOAD_TRANSIENT_STATUSES = frozenset({500, 502, 503, 504})
HASH_CHECK_MAX_ATTEMPTS = 5
HASH_CHECK_RETRY_DELAY = 5

ILLEGAL_COMMONS_TITLE_CHARS_RE = re.compile(r'[\x00-\x1f\x7f#<>\[\]|{}]')
RECORD_ID_RE = re.compile(r'/elib/(?:Record|Collection)/([^/?#"\'<>\s]+)')
NLB_RECORD_ID_RE = re.compile(r'BY-NLB-[A-Za-z0-9_-]+')
YEAR_RE = re.compile(r'(?<!\d)(1\d{3}|20\d{2})(?!\d)')

TERMINAL_STATUSES = frozenset({
	'uploaded',
	'duplicate_sha1',
	'existing_title',
	'restricted',
})

RETRYABLE_UPLOAD_API_ERROR_CODES = frozenset({
	'internal-error',
	'internal_api_error_DBConnectionError',
	'internal_api_error_MediaWiki\\Upload\\Exception\\UploadChunkFileException',
	'internal_api_error_Shellbox\\ShellboxError',
	'lockmanager-fail-conflict',
	'lockmanager-fail-releaselock',
	'maxlag',
	'ratelimited',
	'readonly',
	'stashfailed',
	'uploadstash-bad-path',
	'uploadstash-exception',
	'uploadstash-file-not-found',
})

RETRYABLE_UPLOAD_API_ERROR_PREFIXES = (
	'backend-fail-',
	'uploadstash-',
)

MARC_NS = {'marc': 'http://www.loc.gov/MARC21/slim'}

# NLB expands authority references into local fields matching these source tags.
PERSONAL_NAME_VARIANT_TAGS = {'900': '600', '902': '602', '990': '700', '991': '701', '992': '702'}
PERSONAL_NAME_REFERENCE_LABELS = {
	'400': 'Вариант имени',
	'500': 'Связанная запись имени',
	'420': 'Вариант родового имени',
	'520': 'Связанная запись родового имени',
}
CATALOG_VARIANT_TAGS = {
	'906': ('606', 'subject_variants'),
	'907': ('607', 'geographic_subject_variants'),
	'910': ('710', 'organization_variants'),
	'911': ('711', 'organization_variants'),
	'912': ('712', 'organization_variants'),
}
CATALOG_REFERENCE_LABELS = {
	'450': 'Вариант предметного термина',
	'550': 'Связанный предметный термин',
	'430': 'Вариант заглавия',
	'530': 'Связанное заглавие',
	'415': 'Вариант географического названия',
	'515': 'Связанное географическое название',
	'410': 'Вариант названия организации',
	'510': 'Связанная организация',
}
CATALOG_VARIANT_SECTIONS = {
	'subject_variants': 'Предметные термины и варианты по каталогу НББ (MARC 906)',
	'geographic_subject_variants': 'Географические предметные названия и варианты по каталогу НББ (MARC 907)',
	'organization_variants': 'Названия организаций и связанные записи по каталогу НББ (MARC 910–912)',
}

# 461 points to a parent set; 481/482 link physically bound items, not parents.
# https://cdn.ifla.org/wp-content/uploads/files/assets/uca/unimarc_updates/BIBLIOGRAPHIC/u-b_4-_linking_entry_block_update.pdf
RELATED_RECORD_LABELS = {
	'461': 'Родительская запись',
	'464': 'Составная часть',
	'481': 'Также в этом переплете',
	'482': 'Приплетено к',
	'488': 'Другая связанная запись',
	'961': 'Связанная запись',
}
PARENT_IMPRINT_FIELDS = {
	'places': 'Место издания (MARC 210$a / 214$a)',
	'publishers': 'Издатель (MARC 210$c / 214$c)',
}

# Manufacture subfields are separate from publication places, publishers and dates.
# https://www.ifla.org/files/assets/uca/unimarc_updates/BIBLIOGRAPHIC/u-b_210_update.pdf
UNIMARC_MANUFACTURING_LABELS = {
	'e': 'Место изготовления ($e)',
	'f': 'Адрес изготовителя ($f)',
	'g': 'Типография / изготовитель ($g)',
	'h': 'Дата изготовления ($h)',
}

# UNIMARC Appendix B, not MARC 21 relator codes; unknown codes stay visible.
# https://cdn.ifla.org/wp-content/uploads/files/assets/uca/unimarc_updates/BIBLIOGRAPHIC/u_b_appb_update2020_online_final.pdf
UNIMARC_ROLE_LABELS = {
	'000': 'Функция не установлена',
	'070': 'Автор',
	'080': 'Автор вступления, примечаний и т. п.',
	'220': 'Составитель',
	'340': 'Редактор',
	'390': 'Предыдущий владелец',
	'475': 'Выпускающая организация',
	'650': 'Издатель',
	'730': 'Переводчик',
}


def default_state() -> dict[str, Any]:
	'''Return a fresh resumable state dictionary.'''
	return {
		'version': 1,
		'scan_identity_version': 1,
		'from_year': None,
		'until_year': DEFAULT_UNTIL_YEAR,
		'search_url': None,
		'current_page': 1,
		'current_record_id': None,
		'current_item_key': None,
		'updated_at': None,
		'items': {},
	}


def load_state(
	path: Path,
	until_year: int | None = DEFAULT_UNTIL_YEAR,
	from_year: int | None = None,
	search_url: str | None = None,
) -> dict[str, Any]:
	'''Load the persistent state file or initialize a new state structure.'''
	if not path.exists():
		state = default_state()
		state['from_year'] = from_year
		state['until_year'] = until_year
		state['search_url'] = search_url
		return state
	data = json.loads(path.read_text(encoding='utf-8'))
	state = default_state()
	state.update(data)
	if (
		state.get('until_year') != until_year
		or state.get('from_year') != from_year
		or state.get('search_url') != search_url
	):
		state['from_year'] = from_year
		state['until_year'] = until_year
		state['search_url'] = search_url
		state['current_page'] = 1
		state.pop('retry_from_page', None)
	if not isinstance(state.get('items'), dict):
		state['items'] = {}
	# Revisit older associations once: a verified binary does not establish its
	# catalog volume. Preserve prior results until the fresh scope check runs.
	if not data.get('scan_identity_version') and state['items']:
		state['retry_from_page'] = 1
	state['scan_identity_version'] = 1
	# Older releases treated HTML/error responses as terminal non-PDF records.
	# Their original page was not saved, so revisit the selected catalog once.
	for entry in state['items'].values():
		if entry.get('status') == 'not_pdf':
			entry['status'] = 'download_failed'
			entry['error'] = 'Legacy non-PDF result needs another download and structural validation'
			state['retry_from_page'] = 1
	return state


def save_state(path: Path, state: dict[str, Any]) -> None:
	'''Atomically write the state file so interrupts never corrupt progress.'''
	if state.get('_read_only'):
		return
	path.parent.mkdir(parents=True, exist_ok=True)
	state['updated_at'] = datetime.now(timezone.utc).isoformat()
	temp_path = path.with_suffix(path.suffix + '.new')
	temp_path.write_text(
		json.dumps(state, ensure_ascii=False, indent=2) + '\n',
		encoding='utf-8',
	)
	temp_path.replace(path)


def item_state_key(marc_id: str, file_id: str) -> str:
	"""Build a unique state key for a specific NLB MARC record and file ID."""
	return f'{marc_id}:{file_id}'


def is_item_completed(state: dict[str, Any], marc_id: str, file_id: str) -> bool:
	"""Return True if the given (marc_id, file_id) has already reached a terminal state."""
	entry = state.get('items', {}).get(item_state_key(marc_id, file_id), {})
	return entry.get('status') in TERMINAL_STATUSES


def extract_year_bounds_from_search_url(search_url: str | None) -> tuple[int | None, int | None]:
	"""Extract (publishDatefrom, publishDateto) integer year bounds from an NLB search URL."""
	if not search_url:
		return None, None
	params = parse_qs(urlsplit(search_url).query)
	from_raw = (params.get('publishDatefrom') or [''])[0].strip()
	to_raw = (params.get('publishDateto') or [''])[0].strip()
	from_year = int(from_raw) if from_raw.isdigit() else None
	to_year = int(to_raw) if to_raw.isdigit() else None
	return from_year, to_year


def build_search_page_url(
	page: int,
	from_year: int | None = None,
	to_year: int | None = DEFAULT_UNTIL_YEAR,
	search_url: str | None = None,
) -> str:
	"""Construct the paginated NLB Search/Results URL from year bounds or a custom URL."""
	if search_url:
		parts = urlsplit(search_url)
		raw_pairs = parse_qsl(parts.query, keep_blank_values=True)
		filtered_pairs: list[tuple[str, str]] = []
		has_date_from = False
		has_date_to = False
		for key, val in raw_pairs:
			if key in {'page', 'limit'}:
				continue
			if key == 'publishDatefrom':
				has_date_from = True
				if from_year is not None:
					val = str(from_year)
			elif key == 'publishDateto':
				has_date_to = True
				if to_year is not None:
					val = str(to_year)
			filtered_pairs.append((key, val))
		if from_year is not None and not has_date_from:
			filtered_pairs.append(('publishDatefrom', str(from_year)))
		if to_year is not None and not has_date_to:
			filtered_pairs.append(('publishDateto', str(to_year)))
		filtered_pairs.append(('limit', str(SEARCH_PAGE_SIZE)))
		filtered_pairs.append(('page', str(page)))
		scheme = parts.scheme or 'https'
		netloc = parts.netloc or 'elib.nlb.by'
		path = parts.path or '/elib/Search/Results'
		return urlunsplit((scheme, netloc, path, urlencode(filtered_pairs), ''))

	params = [
		('type', 'AllFields'),
		('isElib', 'true'),
		('daterange[]', 'publishDate'),
		('publishDatefrom', str(from_year) if from_year is not None else ''),
		('publishDateto', str(to_year) if to_year is not None else ''),
		('limit', str(SEARCH_PAGE_SIZE)),
		('page', str(page)),
	]
	return f'{ELIB_BASE_URL}/elib/Search/Results?' + urlencode(params)


def parse_viewer_link(href: str) -> dict[str, str | None] | None:
	"""Extract markID, fileID, or metsID from an NLB viewer URL."""
	if 'viewer' not in href:
		return None
	parts = urlsplit(href)
	params = parse_qs(parts.query)
	mark_id = (params.get('markID') or params.get('marcID') or [None])[0]
	file_id = (params.get('fileID') or params.get('fileId') or [None])[0]
	mets_id = (params.get('metsID') or params.get('metsId') or [None])[0]
	if not mark_id and not file_id and not mets_id:
		return None
	return {
		'mark_id': mark_id,
		'file_id': file_id,
		'mets_id': mets_id,
	}


def parse_search_results_html(html: str) -> dict[str, Any]:
	"""Parse a VuFind Search/Results HTML page into total count and record entries."""
	soup = BeautifulSoup(html, 'html.parser')
	total_results = 0
	stats_node = soup.select_one('.search-stats strong:nth-of-type(3), .search-controls strong:nth-of-type(3)')
	if stats_node is None:
		match = re.search(r'из\s*<strong>\s*([\d,\s]+)\s*</strong>', html)
		if match:
			digits = re.sub(r'\D', '', match.group(1))
			total_results = int(digits) if digits else 0
	else:
		digits = re.sub(r'\D', '', stats_node.get_text())
		total_results = int(digits) if digits else 0

	records: list[dict[str, Any]] = []
	seen_ids: set[str] = set()
	for result_node in soup.select('.result'):
		hidden_id = result_node.select_one('input.hiddenId')
		marc_id = hidden_id.get('value', '').strip() if hidden_id else ''
		if not marc_id:
			for link in result_node.find_all('a', href=True):
				rec_match = RECORD_ID_RE.search(link['href'])
				if rec_match:
					marc_id = rec_match.group(1)
					break
		if not marc_id or marc_id in seen_ids:
			continue
		seen_ids.add(marc_id)

		title_link = result_node.select_one('a.title')
		title = title_link.get_text(' ', strip=True) if title_link else ''
		formats = [
			fmt.get_text(' ', strip=True)
			for fmt in result_node.select('.result-formats .format')
			if fmt.get_text(' ', strip=True)
		]

		file_ids: list[str] = []
		mets_ids: list[str] = []
		for link in result_node.find_all('a', href=True):
			parsed = parse_viewer_link(link['href'])
			if not parsed:
				continue
			if parsed['file_id'] and parsed['file_id'] not in file_ids:
				file_ids.append(parsed['file_id'])
			if parsed['mets_id'] and parsed['mets_id'] not in mets_ids:
				mets_ids.append(parsed['mets_id'])

		records.append({
			'marc_id': marc_id,
			'title': title,
			'formats': formats,
			'file_ids': file_ids,
			'mets_ids': mets_ids,
		})

	return {
		'total_results': total_results,
		'records': records,
	}


def extract_files_from_mets_structure(structure: dict[str, Any]) -> list[dict[str, Any]]:
	'''Flatten viewer files while retaining sibling headings and association evidence.'''
	return flatten_mets(structure)


def extract_cell_values(cell: Any) -> list[str]:
	'''Extract individual line values from an HTML table cell, preserving <br> and block breaks.'''
	fragment = BeautifulSoup(str(cell), 'html.parser')
	for control in fragment.select('script, style, button, .placehold, .moreHoldingItemsInfo'):
		control.decompose()
	for comment in fragment.find_all(string=lambda text: isinstance(text, Comment)):
		comment.extract()
	for br in fragment.find_all('br'):
		br.replace_with('\n')
	for block in fragment.find_all(['div', 'p', 'li', 'tr']):
		block.insert_before('\n')
		block.insert_after('\n')
	values: list[str] = []
	for line in fragment.get_text().splitlines():
		cleaned = re.sub(r'\s+', ' ', line).strip()
		if cleaned and cleaned not in values:
			values.append(cleaned)
	return values


def extract_cell_links(cell: Any, marc_id: str) -> list[dict[str, str]]:
	'''Preserve descriptive HTTP links while excluding transient holdings controls.'''
	fragment = BeautifulSoup(str(cell), 'html.parser')
	for control in fragment.select('script, style, button, .placehold, .moreHoldingItemsInfo, .btn'):
		control.decompose()
	links: list[dict[str, str]] = []
	for anchor in fragment.find_all('a', href=True):
		if anchor['href'].startswith('#'):
			continue
		url = urljoin(f'{ELIB_BASE_URL}/elib/Record/{marc_id}', anchor['href'])
		if urlsplit(url).scheme not in {'http', 'https'}:
			continue
		link = {'url': url, 'text': re.sub(r'\s+', ' ', anchor.get_text(' ', strip=True)) or url}
		if link not in links:
			links.append(link)
	return links


def parse_record_html(html: str, marc_id: str) -> dict[str, Any]:
	'''Extract metadata and catalog links from table cells, excluding sidebar suggestions.'''
	soup = BeautifulSoup(html, 'html.parser')
	title_node = soup.select_one('h1, h3[property="name"], h2[property="name"]')
	title = title_node.get_text(' ', strip=True) if title_node else ''

	fields: list[dict[str, Any]] = []
	record_links: list[dict[str, str]] = []
	for row in soup.select('table tr'):
		headers = row.find_all('th', recursive=False)
		cells = row.find_all('td', recursive=False)
		if not headers and cells:
			# Holdings use one row of column headings followed by one row per copy.
			# Direct children avoid reading an entire nested table as another copy.
			table = row.find_parent('table')
			column_row = next((candidate for candidate in table.find_all('tr')
				if candidate.find_parent('table') is table and candidate.find_all('th', recursive=False)), None)
			columns = column_row.find_all('th', recursive=False) if column_row else []
			if len(columns) > 1 and len(columns) == len(cells):
				values = [f"{heading.get_text(' ', strip=True)}: {'; '.join(extract_cell_values(cell))}"
					for heading, cell in zip(columns, cells) if extract_cell_values(cell)]
				if values:
					field = {'name': 'Хранение / экземпляр', 'value': '\n'.join(values), 'values': values}
					links = [
						{**link, 'text': f"{heading.get_text(' ', strip=True)}: {link['text']}"}
						for heading, cell in zip(columns, cells) for link in extract_cell_links(cell, marc_id)
					]
					if links:
						field['links'] = links
					fields.append(field)
			continue
		header = headers[0] if len(headers) == 1 else None
		cell = cells[0] if len(cells) == 1 else None
		if not header or not cell:
			continue
		key = re.sub(r'\s+', ' ', header.get_text(' ', strip=True)).rstrip(':').strip()
		values = extract_cell_values(cell)
		if not key or not values:
			continue
		field = {
			'name': key,
			'value': '\n'.join(values),
			'values': values,
		}
		fields.append(field)
		for cell_link in extract_cell_links(cell, marc_id):
			absolute_url = cell_link['url']
			url = urlsplit(absolute_url)
			field.setdefault('links', []).append(cell_link)
			if url.netloc and url.hostname != urlsplit(ELIB_BASE_URL).hostname:
				continue
			if url.scheme and url.scheme not in {'http', 'https'}:
				continue
			match = re.fullmatch(r'/(?:elib/)?Record/(BY-NLB-[A-Za-z0-9_-]+)/?', url.path)
			if not match or match.group(1) == marc_id:
				continue
			link = {
				'record_id': match.group(1),
				'title': cell_link['text'],
				'label': key,
			}
			if link not in record_links:
				record_links.append(link)

	file_ids: list[str] = []
	mets_ids: list[str] = []
	for link in soup.find_all('a', href=True):
		parsed = parse_viewer_link(link['href'])
		if not parsed:
			continue
		if parsed['file_id'] and parsed['file_id'] not in file_ids:
			file_ids.append(parsed['file_id'])
		if parsed['mets_id'] and parsed['mets_id'] not in mets_ids:
			mets_ids.append(parsed['mets_id'])

	return {
		'marc_id': marc_id,
		'title': title,
		'fields': fields,
		'record_links': record_links,
		'file_ids': file_ids,
		'mets_ids': mets_ids,
	}


def extract_contents_entries(text: str) -> list[str]:
	"""Split a raw contents string (e.g. MARC 327 or TOC) into structured section entries."""
	stripped = text.strip()
	for prefix in ('содержание:', 'содержание :', 'contents:'):
		if stripped.lower().startswith(prefix):
			stripped = stripped[len(prefix):].strip()
			break
	parts = [p.strip() for p in re.split(r';\s+|\n+', stripped) if p.strip()]
	return parts if parts else ([stripped] if stripped else [])


def split_marc_alternate_headings(
	subfields: list[tuple[str, str]],
	codes: tuple[str, ...],
) -> list[str]:
	"""Split NLB aliases at $9 or repeated main terms, keeping each heading's subdivisions.

	NLB 908 uses $a for the main term; 926 uses $d. Administrative subfields
	are excluded, and codes specify the display order within each heading.
	"""
	headings: list[str] = []
	group: dict[str, list[str]] = {}
	for code, value in subfields + [('9', '')]:
		if code == '9' or (code == codes[0] and code in group):
			parts = [part for heading_code in codes for part in group.get(heading_code, [])]
			if parts:
				headings.append(' — '.join(parts))
			group = {}
		if code in codes:
			group.setdefault(code, []).append(value)
	return headings


def format_personal_name(submap: dict[str, list[str]]) -> str:
	"""Keep initials, expanded forenames, numerals, qualifiers and dates together.

	UNIMARC 600/700–702 use $b for the forename and $g for its expansion;
	$c is repeatable and $d contains numerals. Corporate names use other rules.
	https://cdn.ifla.org/wp-content/uploads/files/assets/uca/unimarc_updates/BIBLIOGRAPHIC/u-b_700_update.pdf
	"""
	name = ' '.join(submap.get('a', []))
	if not name:
		return ''
	if submap.get('d'):
		name += ' ' + ' '.join(submap['d'])
	given = ' '.join(submap.get('b', []))
	expansion = ' '.join(submap.get('g', []))
	if given:
		name += f', {given}'
	if expansion and expansion != given:
		name += f' ({expansion})' if given else f', {expansion}'
	if submap.get('c'):
		name += ', ' + ', '.join(submap['c'])
	if submap.get('f'):
		name += ' (' + ' '.join(submap['f']) + ')'
	return name


def parse_personal_name_variants(tag: str, subfields: list[tuple[str, str]]) -> list[dict[str, Any]]:
	"""Unpack NLB name references without promoting related people to aliases.

	A $9 changes the reference type; repeated $a starts another heading of
	that type. Variants after a 5xx heading belong to that related heading.
	Local $5 codes are scoped to one heading, not interpreted as book copies.
	A single $3 applies throughout the field; changes between multiple IDs
	reset the reference type and context. Main headings are resolved later.
	"""
	authority_ids = list(dict.fromkeys(value for code, value in subfields if code == '3'))
	authority_id = authority_ids[0] if authority_ids else ''
	reference_type = ''
	context = ''
	group: dict[str, list[str]] = {}
	entries: list[dict[str, Any]] = []

	def finish() -> None:
		"""Save one heading with its own qualifiers and the preceding related heading."""
		nonlocal context
		name = format_personal_name(group)
		if name:
			entries.append({
				'tag': tag, 'authority_id': authority_id, 'type': reference_type,
				'name': name, 'codes': list(dict.fromkeys(group.get('5', []))),
				'context': context if reference_type.startswith('4') else '',
			})
			if reference_type.startswith('5'):
				context = name
		group.clear()

	for code, value in subfields:
		if code == '3':
			if value != authority_id:
				finish()
				authority_id, reference_type, context = value, '', ''
		elif code == '9':
			finish()
			reference_type = value
			if not value.startswith('4'):
				context = ''
		elif code in {'a', 'b', 'c', 'd', 'f', 'g', '5'}:
			if code == 'a' and group.get('a'):
				finish()
			group.setdefault(code, []).append(value)
	finish()
	return entries


def parse_catalog_variants(tag: str, subfields: list[tuple[str, str]]) -> list[dict[str, Any]]:
	"""Unpack local subject/organization references with related headings and variants.

	$9 and repeated $a delimit headings; $3 identifies the corresponding authority.
	$2 describes the field's vocabulary, while $5 belongs to one heading.
	Keep other subfields explicitly, including $k on embedded title references.
	"""
	authority_id = next((value for code, value in subfields if code == '3'), '')
	schemes = list(dict.fromkeys(value for code, value in subfields if code == '2'))
	reference_type, context = '', ''
	group: list[tuple[str, str]] = []
	entries: list[dict[str, Any]] = []
	heading_codes = ('a', 'b', 'g', 'c', 'd', 'e', 'f') if tag in {'910', '911', '912'} else ('a', 'j', 'x', 'y', 'z')

	def finish() -> None:
		"""Save a heading with its subdivisions and scoped relationship codes."""
		nonlocal context
		if any(code == 'a' for code, _ in group):
			term = ' — '.join(value for key in heading_codes for code, value in group if code == key)
			for code, value in group:
				if code not in (*heading_codes, '5'):
					term += f'; ${code}={value}'
			entries.append({
				'tag': tag, 'authority_id': authority_id, 'type': reference_type, 'term': term,
				'codes': list(dict.fromkeys(value for code, value in group if code == '5')),
				'schemes': schemes, 'context': context if reference_type.startswith('4') else '',
			})
			if reference_type.startswith('5'):
				context = term
		group.clear()

	for code, value in subfields:
		if code == '3':
			if value != authority_id:
				finish()
				authority_id, reference_type, context = value, '', ''
		elif code == '9':
			finish()
			reference_type = value
			if not value.startswith('4'):
				context = ''
		elif code != '2':
			if code == 'a' and any(key == 'a' for key, _ in group):
				finish()
			group.append((code, value))
	finish()
	return entries


def parse_related_marc_field(tag: str, subfields: list[tuple[str, str]]) -> list[dict[str, Any]]:
	"""Read embedded 001/200 links or standard $0/$t links without mistaking authors for titles.

	NLB's local 961 uses embedded 998$a as a display title. It can enrich a
	matching 461, but does not itself establish a parent relationship.
	"""
	links: list[dict[str, Any]] = []
	record_id = ''
	titles: list[str] = []
	subtitles: list[str] = []
	volumes: list[str] = []
	copies: list[str] = []
	embedded_tag = ''

	def finish() -> None:
		"""Keep one target's identifying text and copy qualifiers together."""
		title = ' ; '.join(titles)
		if subtitles:
			title = ' : '.join([part for part in (title, ' : '.join(subtitles)) if part])
		if record_id or title or volumes:
			links.append({
				'tag': tag, 'record_id': record_id, 'title': title,
				'volume': ' ; '.join(volumes), 'copies': list(dict.fromkeys(copies)),
			})

	for code, value in subfields:
		if code == '1':
			embedded_tag = value[:3]
			if embedded_tag != '001':
				continue
			if record_id:
				finish()
				titles, subtitles, volumes, copies = [], [], [], []
			record_id = value[3:].strip()
		elif code == '0' and not embedded_tag:
			if record_id:
				finish()
				titles, subtitles, volumes, copies = [], [], [], []
			record_id = value
		elif (code == 't' and not embedded_tag) or (
			code == 'a' and (embedded_tag in {'200', '998'} or (tag == '961' and not embedded_tag))
		):
			titles.append(value)
		elif code == 'e' and embedded_tag in {'200', '998'}:
			subtitles.append(value)
		elif code == 'v' and embedded_tag in {'', '001', '200', '998'}:
			volumes.append(value)
		elif code == '5':
			copies.append(value)
	finish()
	return links


def format_imprint_statement(tag: str, ind1: str, ind2: str, subfields: list[tuple[str, str]]) -> str:
	'''Keep each UNIMARC imprint's ordered place, organization and date together.

	UNIMARC 214 uses indicator 2 value 0 for publication and 1 for production;
	these differ from MARC 21 field 264. Unrecognized indicators stay explicit.
	https://cdn.ifla.org/wp-content/uploads/files/assets/uca/unimarc_updates/BIBLIOGRAPHIC/u_b_214_update2019_online_final.pdf
	'''
	labels = MARC_SUBFIELD_LABELS[tag]
	codes = 'abcd' if tag == '210' else 'abcdrs'
	parts = [f'{labels.get(code, "Подполе")} (${code}): {value}' for code, value in subfields if code in codes]
	if not parts:
		return ''
	sequence = {'0': 'Промежуточные сведения', '1': 'Текущие / последние сведения'}.get(ind1)
	if ind1.strip() and sequence is None:
		sequence = f'Последовательность сведений — индикатор 1: {ind1}'
	if sequence:
		parts.insert(0, sequence)
	if tag == '214':
		activity = {'0': 'Публикация', '1': 'Производство', '2': 'Распространение',
			'3': 'Изготовление', '4': 'Авторское право / охрана'}.get(ind2)
		parts.insert(0, activity or f'Тип сведений не установлен (индикатор 2: {ind2.strip() or "пустой"})')
	elif ind2.strip():
		# Keep a source qualifier without applying 214 or MARC 21 semantics to 210.
		parts.insert(0, f'Индикатор 2: {ind2}')
	return '; '.join(parts)


def parse_marcxml(xml_text: str) -> dict[str, Any]:
	'''Parse UNIMARC/BELMARC metadata, retaining contributor roles and copy associations.

	Explicit $4 roles determine authorship. Without a role, only primary/equal
	responsibility fields (700/701/710/711) retain the author fallback.
	Manufacture subfields 210$e/$f/$g/$h stay together in source order per field,
	separate from publication metadata; only identical statements are deduplicated.
	'''
	result: dict[str, Any] = {
		'control_number': '',
		'title': '',
		'subtitle': '',
		'responsibility': '',
		'responsibilities': [],
		'edition': '',
		'publication_terms': [],
		'places': [],
		'publishers': [],
		'imprint_statements': [],
		'manufacturing_statements': [],
		'date': '',
		'year': '',
		'physical_description': '',
		'physical_descriptions': [],
		'series': [],
		'related_records': [],
		'inherited_metadata': [],
		'notes': [],
		'contents': [],
		'subjects': [],
		'subject_variants': [],
		'geographic_subject_variants': [],
		'organization_variants': [],
		'genres': [],
		'geo_places': [],
		'alternate_genres': [],
		'alternate_geo_places': [],
		'personal_name_variants': [],
		'shelfmarks': [],
		'authors': [],
		'contributors': [],
		'languages': [],
		'original_languages': [],
		'intermediate_languages': [],
		'summary_languages': [],
		'country': '',
		'udc': [],
		'raw_fields': [],
		'raw_marc_fields': [],
		'leader': '',
	}
	if not xml_text.strip():
		return result

	try:
		root = ET.fromstring(xml_text)
	except ET.ParseError:
		return result

	record = root if root.tag in {'record', '{' + MARC_NS['marc'] + '}record'} else root.find('.//marc:record', MARC_NS)
	if record is None:
		record = root.find('.//record')
	if record is None:
		return result
	# Preserve the XML-decoded source structure before normalizing display values.
	# Exact response bytes (including XML encoding/attributes) are archived separately.
	for element in record:
		kind = element.tag.rsplit('}', 1)[-1]
		if kind == 'leader':
			result['leader'] = element.text or ''
		elif kind == 'controlfield':
			result['raw_marc_fields'].append({
				'kind': kind, 'tag': element.get('tag', ''), 'value': element.text or '',
			})
		elif kind == 'datafield':
			result['raw_marc_fields'].append({
				'kind': kind, 'tag': element.get('tag', ''),
				'ind1': element.get('ind1', ''), 'ind2': element.get('ind2', ''),
				'subfields': [
					{'code': subfield.get('code', ''), 'value': subfield.text or ''}
					for subfield in element if subfield.tag.rsplit('}', 1)[-1] == 'subfield'
				],
			})

	for ctrl in record.findall('marc:controlfield', MARC_NS) + record.findall('controlfield'):
		tag = (ctrl.attrib.get('tag') or '').strip()
		val = (ctrl.text or '').strip()
		if tag == '001' and val:
			result['control_number'] = val
		if tag and val:
			result['raw_fields'].append({'tag': tag, 'value': val})

	# Resolve aliases after all fields so $3 associations do not depend on XML order.
	authority_headings: dict[tuple[str, str], list[str]] = {}
	alternate_headings: dict[tuple[str, str], list[str]] = {}
	datafields = record.findall('marc:datafield', MARC_NS) or record.findall('datafield')
	for df in datafields:
		tag = (df.attrib.get('tag') or '').strip()
		subfields_nodes = df.findall('marc:subfield', MARC_NS) or df.findall('subfield')
		submap: dict[str, list[str]] = {}
		subfields: list[tuple[str, str]] = []
		parts_rendered: list[str] = []
		for sf in subfields_nodes:
			code = (sf.attrib.get('code') or '').strip()
			text = re.sub(r'\s+', ' ', (sf.text or '').strip())
			if not text:
				continue
			submap.setdefault(code, []).append(text)
			subfields.append((code, text))
			parts_rendered.append(f'${code} {text}')

		if parts_rendered:
			result['raw_fields'].append({'tag': tag, 'value': ' '.join(parts_rendered)})
		if tag in RELATED_RECORD_LABELS:
			result['related_records'].extend(parse_related_marc_field(tag, subfields))
		if tag in PERSONAL_NAME_VARIANT_TAGS:
			result['personal_name_variants'].extend(parse_personal_name_variants(tag, subfields))
		if tag in CATALOG_VARIANT_TAGS:
			result[CATALOG_VARIANT_TAGS[tag][1]].extend(parse_catalog_variants(tag, subfields))
		personal_name = format_personal_name(submap) if tag in {'600', '602', '700', '701', '702'} else ''
		if personal_name:
			for authority_id in submap.get('3', []):
				headings = authority_headings.setdefault((tag, authority_id), [])
				if personal_name not in headings:
					headings.append(personal_name)

		if tag == '010':
			labels = {'a': 'ISBN', 'b': 'Уточнение / переплет', 'd': 'Условия приобретения / цена', 'z': 'Ошибочный ISBN'}
			statement = '; '.join(f'{labels.get(code, "$" + code)}: {value}' for code, value in subfields)
			if statement not in result['publication_terms']:
				result['publication_terms'].append(statement)
		elif tag == '100':
			coded = ' '.join(submap.get('a', []))
			if len(coded) >= 13:
				year_candidate = coded[9:13]
				if year_candidate.isdigit():
					result['year'] = year_candidate
		elif tag == '101':
			for lang in submap.get('a', []):
				if lang not in result['languages']:
					result['languages'].append(lang)
			for lang in submap.get('c', []):
				if lang not in result['original_languages']:
					result['original_languages'].append(lang)
			for lang in submap.get('b', []):
				if lang not in result['intermediate_languages']:
					result['intermediate_languages'].append(lang)
			for lang in submap.get('d', []):
				if lang not in result['summary_languages']:
					result['summary_languages'].append(lang)
		elif tag == '102':
			if submap.get('a') and not result['country']:
				result['country'] = submap['a'][0]
		elif tag == '200':
			if submap.get('a') and not result['title']:
				result['title'] = ' ; '.join(submap['a'])
			if submap.get('e') and not result['subtitle']:
				result['subtitle'] = ' : '.join(submap['e'])
			for resp in submap.get('f', []) + submap.get('g', []):
				if resp not in result['responsibilities']:
					result['responsibilities'].append(resp)
		elif tag == '205':
			if submap.get('a') and not result['edition']:
				result['edition'] = ' '.join(submap['a'])
		elif tag == '210':
			statement = format_imprint_statement(tag, df.get('ind1', ''), df.get('ind2', ''), subfields)
			if statement and {'tag': tag, 'text': statement} not in result['imprint_statements']:
				result['imprint_statements'].append({'tag': tag, 'text': statement})
			for place in submap.get('a', []):
				if place not in result['places']:
					result['places'].append(place)
			for pub in submap.get('c', []):
				if pub not in result['publishers']:
					result['publishers'].append(pub)
			if submap.get('d') and not result['date']:
				result['date'] = ', '.join(submap['d'])
			statement = '; '.join(
				f'{UNIMARC_MANUFACTURING_LABELS[code]}: {text}'
				for code, text in subfields if code in UNIMARC_MANUFACTURING_LABELS
			)
			if statement and statement not in result['manufacturing_statements']:
				result['manufacturing_statements'].append(statement)
		elif tag == '214':
			statement = format_imprint_statement(tag, df.get('ind1', ''), df.get('ind2', ''), subfields)
			if statement and {'tag': tag, 'text': statement} not in result['imprint_statements']:
				result['imprint_statements'].append({'tag': tag, 'text': statement})
			# UNIMARC 214 uses 0 for publication; 1 means production.
			if df.get('ind2') == '0':
				for code, key in (('a', 'places'), ('c', 'publishers')):
					for value in submap.get(code, []):
						if value not in result[key]:
							result[key].append(value)
				if not result['date']:
					result['date'] = ', '.join(submap.get('d', []))
		elif tag == '215':
			main_parts = submap.get('a', []) + submap.get('c', []) + submap.get('d', [])
			acc_parts = submap.get('e', [])
			phys_entry = ' : '.join(main_parts)
			if acc_parts:
				acc_str = ' + '.join(acc_parts)
				phys_entry = f'{phys_entry} + {acc_str}' if phys_entry else acc_str
			if phys_entry and phys_entry not in result['physical_descriptions']:
				result['physical_descriptions'].append(phys_entry)
		elif tag in {'225', '461'}:
			# Standard 461$a is an author; only embedded 200$a or standard $t is a title.
			series_parts = submap.get('a', []) + submap.get('v', [])
			if tag == '461':
				links = [
					link for link in parse_related_marc_field(tag, subfields)
					if not link['record_id'] or link['record_id'] != result['control_number']
				]
				series_parts = [link['title'] for link in links if link['title']] + [
					link['volume'] for link in links if link['volume']
				]
			if series_parts:
				entry = ' ; '.join(series_parts)
				if entry not in result['series']:
					result['series'].append(entry)
		elif tag == '327':
			for val in submap.get('a', []):
				for entry in extract_contents_entries(val):
					if entry not in result['contents']:
						result['contents'].append(entry)
		elif tag.startswith('3'):
			for note in submap.get('a', []):
				# Retain the note's own copy scope without assigning it to a contributor.
				if submap.get('5'):
					note += f"; экземпляр в каталоге: {'; '.join(dict.fromkeys(submap['5']))}"
				if note not in result['notes']:
					result['notes'].append(note)
		elif tag == '608':
			genre_parts = (
				submap.get('a', [])
				+ submap.get('x', [])
				+ submap.get('y', [])
				+ submap.get('z', [])
			)
			if genre_parts:
				genre = ' — '.join(genre_parts)
				if genre not in result['genres']:
					result['genres'].append(genre)
				for authority_id in submap.get('3', []):
					headings = authority_headings.setdefault((tag, authority_id), [])
					if genre not in headings:
						headings.append(genre)
		elif tag in {'600', '602'}:
			if personal_name:
				subdivisions = [value for code in ('j', 'x', 'y', 'z') for value in submap.get(code, [])]
				subject = ' — '.join([personal_name, *subdivisions])
				if subject not in result['subjects']:
					result['subjects'].append(subject)
		elif tag in {'605', '606', '607', '690'}:
			subj_parts = (
				submap.get('a', [])
				+ submap.get('b', [])
				+ submap.get('d', [])
				+ submap.get('f', [])
				+ (submap.get('j', []) if tag in {'606', '607'} else [])
				+ submap.get('x', [])
				+ submap.get('y', [])
				+ submap.get('z', [])
			)
			if subj_parts:
				subj = ' — '.join(subj_parts)
				if subj not in result['subjects']:
					result['subjects'].append(subj)
				if tag in {'606', '607'}:
					for authority_id in submap.get('3', []):
						headings = authority_headings.setdefault((tag, authority_id), [])
						if subj not in headings:
							headings.append(subj)
		elif tag == '620':
			geo_parts = (
				submap.get('d', [])
				+ submap.get('c', [])
				+ submap.get('b', [])
				+ submap.get('a', [])
			)
			if geo_parts:
				geo = ' — '.join(geo_parts)
				if geo not in result['geo_places']:
					result['geo_places'].append(geo)
				for authority_id in submap.get('3', []):
					headings = authority_headings.setdefault((tag, authority_id), [])
					if geo not in headings:
						headings.append(geo)
		elif tag in {'908', '926'}:
			main_tag = '608' if tag == '908' else '620'
			codes = ('a', 'x', 'y', 'z') if tag == '908' else ('d', 'c', 'b', 'a')
			variants = split_marc_alternate_headings(subfields, codes)
			for authority_id in submap.get('3', ['']):
				headings = alternate_headings.setdefault((main_tag, authority_id), [])
				for variant in variants:
					if variant not in headings:
						headings.append(variant)
		elif tag == '675':
			for udc in submap.get('a', []):
				if udc not in result['udc']:
					result['udc'].append(udc)
		elif tag == '899':
			for shelfmark in submap.get('p', []):
				clean_shelfmark = shelfmark.rstrip(',').strip()
				if clean_shelfmark and clean_shelfmark not in result['shelfmarks']:
					result['shelfmarks'].append(clean_shelfmark)
		elif tag in {'700', '701', '702', '710', '711', '712'}:
			name = personal_name
			if tag in {'710', '711', '712'}:
				name = ', '.join(value for code in ('a', 'b', 'g', 'c', 'd', 'e') for value in submap.get(code, []))
				if name and submap.get('f'):
					name += ' (' + ' '.join(submap['f']) + ')'
			if not name:
				continue
			if tag in {'710', '711', '712'}:
				for authority_id in submap.get('3', []):
					headings = authority_headings.setdefault((tag, authority_id), [])
					if name not in headings:
						headings.append(name)
			roles = list(dict.fromkeys(submap.get('4', [])))
			contributor = {
				'name': name,
				'tag': tag,
				'kind': 'organization' if tag in {'710', '711', '712'} else 'person',
				'dates': list(submap.get('f', [])),
				'roles': roles,
				'copies': list(dict.fromkeys(submap.get('5', []))),
			}
			if contributor not in result['contributors']:
				result['contributors'].append(contributor)
			is_author = '070' in roles or (not roles and tag in {'700', '701', '710', '711'})
			if is_author and name not in result['authors']:
				result['authors'].append(name)

	personal_variants: list[dict[str, Any]] = []
	for variant in result['personal_name_variants']:
		main_tag = PERSONAL_NAME_VARIANT_TAGS[variant['tag']]
		variant['main_names'] = authority_headings.get((main_tag, variant['authority_id']), [])
		if variant not in personal_variants:
			personal_variants.append(variant)
	result['personal_name_variants'] = personal_variants

	for key in CATALOG_VARIANT_SECTIONS:
		variants: list[dict[str, Any]] = []
		for variant in result[key]:
			main_tag = CATALOG_VARIANT_TAGS[variant['tag']][0]
			variant['main_headings'] = authority_headings.get((main_tag, variant['authority_id']), [])
			if variant not in variants:
				variants.append(variant)
		result[key] = variants

	for link in result['related_records']:
		if link['tag'] == '461' and not link['title'] and link['record_id']:
			link['title'] = ' ; '.join(dict.fromkeys(
				other['title'] for other in result['related_records']
				if other['tag'] == '961' and other['record_id'] == link['record_id'] and other['title']
			))
	related_records: list[dict[str, Any]] = []
	for link in result['related_records']:
		if link['record_id'] and link['record_id'] == result['control_number']:
			continue
		# A local display field is redundant only when it adds no title or copy detail.
		if link['tag'] == '961' and link['record_id'] and not link['copies'] and not link['volume'] and any(
			other['tag'] == '461' and other['record_id'] == link['record_id']
			and (not link['title'] or other['title'] == link['title'])
			for other in result['related_records']
		):
			continue
		if link not in related_records:
			related_records.append(link)
	result['related_records'] = related_records
	parent_title = next((link['title'] for link in related_records if link['tag'] == '461' and link['title']), '')
	# Preserve the existing title fallback for NLB display fields without a record ID.
	if not parent_title:
		parent_title = next((link['title'] for link in related_records if link['tag'] == '961' and not link['record_id']), '')

	for (main_tag, authority_id), variants in alternate_headings.items():
		main_headings = authority_headings.get((main_tag, authority_id), [])
		new_variants = [variant for variant in variants if variant not in main_headings]
		if not new_variants:
			continue
		key = 'alternate_genres' if main_tag == '608' else 'alternate_geo_places'
		if main_headings:
			entries = [f"{' ; '.join(main_headings)}: {'; '.join(new_variants)}"]
		else:
			entries = new_variants
		for entry in entries:
			if entry not in result[key]:
				result[key].append(entry)

	if parent_title and parent_title not in result['title']:
		result['title'] = f"{parent_title}. {result['title']}" if result['title'] else parent_title

	if not result['year'] and result['date']:
		match = YEAR_RE.search(result['date'])
		if match:
			result['year'] = match.group(1)

	if result['responsibilities']:
		result['responsibility'] = ' ; '.join(result['responsibilities'])

	if result['physical_descriptions']:
		result['physical_description'] = ' ; '.join(result['physical_descriptions'])

	return result


def resolve_publication_year(record_meta: dict[str, Any], marc_meta: dict[str, Any]) -> int | None:
	'''Return an evidenced publication year, never a historical year from the title.'''
	return publication_year(record_meta, marc_meta)


def sanitize_commons_filename(filename: str) -> str:
	"""Sanitize title characters and query-like punctuation rejected by Commons.

	For titles matching ``.*[?&]+[^=]+=[^&]+.*``, replace equals signs with
	hyphens without increasing the UTF-8 byte length. Other titles keep their
	ampersands, question marks and equals signs; source metadata is unchanged.
	"""
	sanitized = filename.replace('[', '(').replace(']', ')')
	sanitized = ILLEGAL_COMMONS_TITLE_CHARS_RE.sub('_', sanitized)
	sanitized = sanitized.replace("'", 'Ꞌ')
	sanitized = sanitized.replace('/', '-')
	sanitized = sanitized.replace('\\', '-')
	sanitized = sanitized.replace(':', ' -')
	sanitized = re.sub(r'[_\s]+([.,])', r'\1', sanitized)
	sanitized = re.sub(r'\s+', ' ', sanitized).strip()
	if re.search(r'[?&]+[^=]+=[^&]+', sanitized):
		sanitized = sanitized.replace('=', '-')
	return sanitized


def build_commons_filename(
	record_meta: dict[str, Any],
	marc_meta: dict[str, Any],
	marc_id: str,
	file_id: str,
	part_label: str = '',
) -> str:
	'''Keep full names up to 240 UTF-8 bytes, using only the file ID on overflow.

	Overflow drops the appended year and record ID before shortening the title.
	The file ID keeps multipart scans distinct; full metadata stays in the description.
	'''
	marc_title = (marc_meta.get('title') or '').strip()
	html_title = (record_meta.get('title') or '').strip()
	if html_title and (not marc_title or (len(html_title) > len(marc_title) and not marc_meta.get('subtitle'))):
		base_title = html_title
	else:
		base_title = marc_title or html_title or marc_id
	if marc_meta.get('subtitle') and marc_meta['subtitle'] not in base_title:
		base_title = f"{base_title} - {marc_meta['subtitle']}"
	if part_label:
		base_title = f'{base_title} ({part_label})'

	year = resolve_publication_year(record_meta, marc_meta)
	year_suffix = f' ({year})' if year and str(year) not in base_title else ''
	suffix = f'{year_suffix} -- {marc_id} -- {file_id}.pdf'

	clean_base = sanitize_commons_filename(base_title)
	clean_suffix = ' ' + sanitize_commons_filename(suffix)
	full_filename = sanitize_commons_filename(f'{clean_base}{clean_suffix}')
	if len(full_filename.encode('utf-8')) <= 240:
		return full_filename

	clean_suffix = ' ' + sanitize_commons_filename(f'-- {file_id}.pdf')
	max_base_bytes = 240 - len(clean_suffix.encode('utf-8'))
	if max_base_bytes < 1:
		raise ValueError('The file ID is too long for a Commons filename')
	if len(clean_base.encode('utf-8')) > max_base_bytes:
		# Ignoring a partial final UTF-8 sequence preserves complete source codepoints.
		clean_base = clean_base.encode('utf-8')[:max_base_bytes].decode('utf-8', errors='ignore').rstrip(' .,-_')

	return sanitize_commons_filename(f'{clean_base}{clean_suffix}')


class RenderedWikitext(str):
	'''Mark formatter-generated markup whose source values have already been escaped.

	Only formatting helpers construct this type; parsed catalog values remain plain
	strings. This prevents both source markup execution and double escaping of links.
	'''


def escape_wikitext(value: str) -> str:
	'''Render literal catalog text safely inside templates, lists and link labels.'''
	if isinstance(value, RenderedWikitext):
		return RenderedWikitext(normalize_wikitext(value))
	return normalize_wikitext(value).translate(str.maketrans({
		'&': '&amp;', '<': '&lt;', '>': '&gt;', '[': '&#91;', ']': '&#93;',
		'{': '&#123;', '}': '&#125;', '|': '{{!}}', "'": '&#39;',
		'_': '&#95;', '~': '&#126;',
		'\n': '<br>', '\r': '',
	}))


def format_metadata_field(label: str, values: list[str]) -> str:
	'''Format a metadata field as a single line or nested multi-line bullet list.'''
	# str.strip() removes str subclasses, so preserve the trusted formatter marker.
	cleaned = [RenderedWikitext(val.strip()) if isinstance(val, RenderedWikitext) else val.strip()
		for val in values if val and val.strip()]
	if len(cleaned) == 1:
		return f"* '''{escape_wikitext(label)}:''' {escape_wikitext(cleaned[0])}"
	sub_lines = [f'** {escape_wikitext(val)}' for val in cleaned]
	return f"* '''{escape_wikitext(label)}:'''\n" + '\n'.join(sub_lines)


def format_contributor(contributor: dict[str, Any]) -> str:
	"""Describe all roles and copy associations; the metadata formatter escapes this text."""
	roles = [
		f'{UNIMARC_ROLE_LABELS[code]} ({code})' if code in UNIMARC_ROLE_LABELS else f'Роль UNIMARC {code}'
		for code in contributor['roles']
	]
	if not roles:
		roles = [f"Роль не указана (MARC {contributor['tag']})"]
	entry = f"{contributor['name']} — {'; '.join(roles)}"
	if contributor['copies']:
		entry += f"; экземпляр в каталоге: {'; '.join(contributor['copies'])}"
	return entry


def format_personal_name_variant(variant: dict[str, Any]) -> str:
	'''Attribute each local name and escape its text while retaining reference codes.'''
	main = '; '.join(variant['main_names']) or 'основное имя не установлено'
	entry = f"MARC {variant['tag']}; {main}"
	if variant['authority_id']:
		entry += f"; ID: {variant['authority_id']}"
	label = PERSONAL_NAME_REFERENCE_LABELS.get(variant['type'], 'Имя из каталога')
	type_label = '$9=' + variant['type'] if variant['type'] else 'тип не указан'
	entry += f" — {label} ({type_label}): {variant['name']}"
	if variant['context']:
		entry += f"; относится к связанной записи: {variant['context']}"
	for code in variant['codes']:
		entry += f'; подполе $5={code}'
	return RenderedWikitext(escape_catalog_link_text(entry))


def escape_catalog_link_text(value: str) -> str:
	'''Render catalog labels literally so brackets or markup cannot alter a record link.'''
	return escape_wikitext(value)


def format_catalog_variant(variant: dict[str, Any]) -> str:
	'''Render catalog references literally without adding related terms as book subjects.'''
	main = '; '.join(variant['main_headings']) or 'основная запись не установлена'
	entry = f"MARC {variant['tag']}; {main}"
	if variant['authority_id']:
		entry += f"; ID: {variant['authority_id']}"
	label = CATALOG_REFERENCE_LABELS.get(variant['type'], 'Запись из каталога')
	type_label = '$9=' + variant['type'] if variant['type'] else 'тип не указан'
	entry += f" — {label} ({type_label}): {variant['term']}"
	if variant['context']:
		entry += f"; относится к связанной записи: {variant['context']}"
	for code in variant['codes']:
		entry += f'; подполе $5={code}'
	for scheme in variant['schemes']:
		entry += f'; система $2={scheme}'
	return RenderedWikitext(escape_catalog_link_text(entry))


def format_catalog_reference(record_id: str, title: str = '') -> str:
	'''Link only valid NLB IDs to the canonical catalog URL; keep other IDs as text.'''
	reference = (
		f'[{ELIB_BASE_URL}/elib/Record/{record_id} {record_id}]'
		if NLB_RECORD_ID_RE.fullmatch(record_id) else escape_catalog_link_text(record_id)
	)
	if title:
		reference += (' — ' if reference else '') + escape_catalog_link_text(title)
	return RenderedWikitext(reference)


def format_related_records(record_meta: dict[str, Any], marc_meta: dict[str, Any], marc_id: str) -> list[str]:
	'''Render typed MARC relationships, enriching labels from matching HTML catalog links.'''
	html_links = record_meta.get('record_links', [])
	entries: list[str] = []
	represented_ids: set[str] = set()
	for link in marc_meta.get('related_records', []):
		record_id = link['record_id']
		if record_id == marc_id:
			continue
		title = link['title'] or next((item['title'] for item in html_links if item['record_id'] == record_id), '')
		entry = f"{RELATED_RECORD_LABELS[link['tag']]} (MARC {link['tag']}): {format_catalog_reference(record_id, title)}"
		if link['volume']:
			entry += '; часть: ' + escape_catalog_link_text(link['volume'])
		if link['copies']:
			entry += '; экземпляр в каталоге: ' + escape_catalog_link_text('; '.join(link['copies']))
		if entry not in entries:
			entries.append(RenderedWikitext(entry))
		represented_ids.add(record_id)
	for link in html_links:
		if link['record_id'] != marc_id and link['record_id'] not in represented_ids:
			entry = f"{escape_catalog_link_text(link['label'])}: {format_catalog_reference(link['record_id'], link['title'])}"
			if entry not in entries:
				entries.append(RenderedWikitext(entry))
	return entries


# The appendix retains fields that the bibliographic formatters do not understand.
# Codes are interpreted only within their source field; unfamiliar codes keep their
# original tag/code instead of being assigned a speculative meaning.
MARC_FIELD_LABELS = {
	'001': 'Идентификатор записи', '005': 'Время изменения записи',
	'010': 'ISBN и условия приобретения', '011': 'ISSN',
	'100': 'Общие кодированные данные', '101': 'Языки', '102': 'Страна публикации',
	'105': 'Кодированные данные о тексте', '106': 'Форма документа',
	'200': 'Заглавие и ответственность', '205': 'Сведения об издании',
	'210': 'Публикация и изготовление', '214': 'Публикация и изготовление',
	'215': 'Физическое описание', '225': 'Серия', '327': 'Содержание',
	'600': 'Персоналия как предмет', '601': 'Организация как предмет',
	'602': 'Род / семья как предмет', '605': 'Заглавие как предмет',
	'606': 'Предметные термины', '607': 'Географический предмет', '608': 'Форма / жанр',
	'615': 'Категория национального документа', '620': 'Географические сведения',
	'675': 'УДК', '700': 'Автор', '701': 'Соавтор', '702': 'Другой участник',
	'710': 'Организация', '711': 'Организация с равной ответственностью', '712': 'Другая организация',
	'801': 'Источник каталогизации', '856': 'Электронный ресурс',
	'899': 'Хранение и экземпляр', '900': 'Ссылки на персоналию', '902': 'Ссылки на род / семью',
	'990': 'Ссылки на автора', '991': 'Ссылки на соавтора', '992': 'Ссылки на участника',
	**RELATED_RECORD_LABELS,
}
MARC_SUBFIELD_LABELS = {
	'010': {'a': 'ISBN', 'b': 'Уточнение / переплет', 'd': 'Цена / условия приобретения', 'z': 'Ошибочный ISBN'},
	'200': {'a': 'Заглавие', 'b': 'Вид материала', 'd': 'Параллельное заглавие', 'e': 'Подзаголовок', 'f': 'Ответственность', 'g': 'Другая ответственность', 'h': 'Обозначение части', 'i': 'Название части'},
	'210': {'a': 'Место издания', 'b': 'Адрес издателя', 'c': 'Издатель', 'd': 'Дата издания', **UNIMARC_MANUFACTURING_LABELS},
	'214': {'a': 'Место', 'b': 'Адрес', 'c': 'Организация', 'd': 'Дата',
		'r': 'Выходные данные с основного источника', 's': 'Выходные данные из колофона'},
	'215': {'a': 'Объем', 'c': 'Иллюстрации', 'd': 'Размер', 'e': 'Сопроводительный материал'},
	'615': {'a': 'Категория документа', 'n': 'Код категории'},
	'801': {'a': 'Страна', 'b': 'Организация', 'c': 'Дата каталогизации', 'g': 'Правила каталогизации'},
	'856': {'u': 'Адрес ресурса', 'y': 'Название ссылки', 'z': 'Примечание'},
	'899': {'a': 'Библиотека', 'b': 'Подразделение / фонд', 'j': 'Место хранения', 'p': 'Шифр', 'z': 'Примечание'},
}
MARC_FORMATTED_CODES = {
	'010': set('abdz'), '101': set('abcd'), '102': {'a'}, '200': set('aefg'),
	'205': {'a'}, '210': set('abcdefgh'), '214': set('abcdrs'), '215': set('acde'), '225': set('av'),
	'327': {'a'}, '600': set('abcdfgjxyz'), '602': set('abcdfgjxyz'),
	'605': set('abdfxyz'), '606': set('abdfjxyz'), '607': set('abdfjxyz'),
	'608': set('axyz'), '620': set('abcd'), '675': {'a'}, '690': set('abdfxyz'),
	'899': {'p'}, '908': set('axyz'), '926': set('abcd'),
	**{tag: set('abcdfg45') for tag in ('700', '701', '702')},
	**{tag: set('abcdefg45') for tag in ('710', '711', '712')},
	**{tag: set('abcdfg359') for tag in PERSONAL_NAME_VARIANT_TAGS},
	**{tag: set('abgcdefjxyz2359') for tag in CATALOG_VARIANT_TAGS},
}


def marc_subfield_label(tag: str, code: str) -> str:
	'''Name only documented field-specific meanings, retaining the original code.'''
	labels = MARC_SUBFIELD_LABELS.get(tag, {})
	if tag in {'600', '602', '700', '701', '702'}:
		labels = {'a': 'Имя / фамилия', 'b': 'Инициалы / имя', 'c': 'Уточнение', 'd': 'Номер', 'f': 'Даты жизни', 'g': 'Полная форма имени', '4': 'Роль', '5': 'Экземпляр'}
	elif tag in {'601', '710', '711', '712'}:
		labels = {'a': 'Организация', 'b': 'Подразделение', 'c': 'Уточнение', 'd': 'Номер', 'e': 'Место', 'f': 'Дата', 'g': 'Инвертированная часть названия', '4': 'Роль', '5': 'Экземпляр'}
	common = {'0': 'Идентификатор записи', '2': 'Система / словарь', '3': 'Идентификатор авторитетной записи', '6': 'Связь полей', '9': 'Локальный код'}
	label = labels.get(code, common.get(code, 'Подполе'))
	return f'{label} (${code})'


def format_marc_supplement(marc_meta: dict[str, Any], rendered_metadata: str) -> list[str]:
	'''Display unrepresented source values with field context, including embedded records.

	Known formatters may suppress a subfield only when its literal value actually
	appears in their output. Unknown fields/codes are always shown. Repeated fields
	retain order and context; only identical supplemental statements are collapsed.
	'''
	# Match visible text, excluding link destinations and template names. Short
	# codes must match a whole token: country RU must not disappear into rus.
	visible_metadata = mwparserfromhell.parse(rendered_metadata).strip_code()
	def represented(value: str) -> bool:
		'''Recognize complete visible values without treating markup as source data.'''
		literal = mwparserfromhell.parse(escape_wikitext(value)).strip_code()
		return re.search(r'(?<!\w)' + re.escape(literal) + r'(?!\w)', visible_metadata, re.IGNORECASE) is not None

	entries: list[str] = []
	for field in marc_meta.get('raw_marc_fields', []):
		tag = field['tag']
		if field['kind'] == 'controlfield':
			value = field['value'].strip()
			if value and tag != '001':
				entries.append(format_metadata_field(f'{MARC_FIELD_LABELS.get(tag, "Контрольные данные")} (MARC {tag})', [value]))
			continue
		subfields = [(item['code'], re.sub(r'\s+', ' ', item['value']).strip()) for item in field['subfields'] if item['value'].strip()]
		if any(code == '1' for code, _ in subfields):
			# $1 changes the embedded field, so $a can mean a title, author or place.
			groups: list[tuple[str, list[tuple[str, str]]]] = [(tag, [])]
			for code, value in subfields:
				if code == '1' and re.match(r'^\d{3}', value):
					embedded_tag = value[:3]
					groups.append((embedded_tag, [('0', value[3:].strip())] if embedded_tag == '001' and value[3:].strip() else []))
				else:
					groups[-1][1].append((code, value))
			# Deduplicate whole source fields, never individual embedded author/title
			# lines: the same contributor may belong to several distinct linked works.
			linked_details = []
			for embedded_tag, values in groups:
				if values:
					label = MARC_FIELD_LABELS.get(embedded_tag, 'Сведения')
					statement = '; '.join(f'{marc_subfield_label(embedded_tag, code)}: {value}' for code, value in values)
					linked_details.append(f'{label} (MARC {tag} → {embedded_tag}): {statement}')
			if linked_details:
				label = MARC_FIELD_LABELS.get(tag, 'Связанная запись')
				entries.append(format_metadata_field(f'{label} (MARC {tag})', linked_details))
			continue
		if tag in {'210', '214'}:
			statement = format_imprint_statement(tag, field.get('ind1', ''), field.get('ind2', ''), subfields)
			if statement and not represented(statement):
				# Individual values appearing elsewhere do not preserve their grouping.
				entries.append(format_metadata_field(f'Выходные данные (MARC {tag})', [statement]))
		formatted_codes = MARC_FORMATTED_CODES.get(tag, {'a', '5'} if tag.startswith('3') else set())
		remaining = []
		for code, value in subfields:
			parts = extract_contents_entries(value) if tag == '327' and code == 'a' else [value.rstrip(',') if tag == '899' and code == 'p' else value]
			if code not in formatted_codes or not all(represented(part) for part in parts):
				remaining.append((code, value))
		if not remaining:
			continue
		# Retain an identifying heading beside extensions such as authority/copy IDs.
		context = next((value for code, value in subfields if code == 'a'), '')
		if context and ('a', context) not in remaining:
			remaining.insert(0, ('a', context))
		label = MARC_FIELD_LABELS.get(tag, 'Примечание' if tag.startswith('3') else 'Дополнительные сведения')
		entries.append(format_metadata_field(f'{label} (MARC {tag})', [
			'; '.join(f'{marc_subfield_label(tag, code)}: {value}' for code, value in remaining),
		]))
	return list(dict.fromkeys(entries))


def format_source_link(url: str, text: str) -> RenderedWikitext:
	'''Create an HTTP(S) link, encoding characters that delimit MediaWiki markup.'''
	if urlsplit(url).scheme not in {'http', 'https'}:
		return RenderedWikitext(escape_wikitext(text))
	return RenderedWikitext(f'[{quote(url, safe=":/?&=#+%~")} {escape_wikitext(text)}]')


def is_circulation_field(label: str) -> bool:
	'''Identify NLB's temporary copy status and inventory/availability summary labels.

	Apply only to HTML field names or explicitly labeled holdings columns. The
	original parsed fields and response bytes stay in the archive, and the same
	words in titles, notes, rights statements or other catalog values stay visible.
	'''
	label = ' '.join(label.split()).rstrip(':').strip().casefold()
	return label == 'статус экземпляра' or re.fullmatch(
		r'всего\s*:\s*[0-9]+(?:\s*,\s*доступно\s*:\s*[0-9]+)?', label,
	) is not None


def build_commons_description(
	record_meta: dict[str, Any],
	marc_meta: dict[str, Any],
	marc_id: str,
	file_id: str,
	part_label: str = '',
	category: str = DEFAULT_CATEGORY,
	license_template: str = DEFAULT_LICENSE,
) -> str:
	'''Build Commons wikitext with subtitles and catalog-attributed contributor roles.

	Catalog copy references are preserved, but are not matched to the scanned copy.
	Plain bracketed years display without brackets; the original notation stays visible.
	'''
	title = (record_meta.get('title') or marc_meta.get('title') or marc_id).strip()
	subtitle = (marc_meta.get('subtitle') or '').strip()
	if subtitle and subtitle.casefold() not in title.casefold():
		title = f'{title}: {subtitle}'
	if part_label:
		title = f'{title} ({part_label})'
	# The date parameter must not turn uncertain source dates into exact years.
	date_statement = marc_meta.get('date') or next((
		field.get('value') or '; '.join(field.get('values', []))
		for field in record_meta.get('fields', []) if field.get('name') == 'Дата издания'
	), '')
	if not date_statement:
		year = resolve_publication_year(record_meta, marc_meta)
		date_statement = str(year) if year else ''
	# Brackets alone mark a supplied catalog date, not an approximate year.
	supplied_year = re.fullmatch(r'\[([0-9]{4})\]', date_statement.strip())
	date_value = supplied_year.group(1) if supplied_year else escape_wikitext(date_statement)

	metadata_lines: list[str] = [f"'''{escape_wikitext(title)}'''"]
	if marc_meta.get('responsibility'):
		metadata_lines.append(format_metadata_field('Ответственность / Responsibility', [marc_meta['responsibility']]))
	contributor_lines: list[str] = []
	if marc_meta.get('contributors'):
		contributor_lines.append(format_metadata_field(
			'Участники и роли по каталогу НББ (MARC 7xx$4)',
			[format_contributor(contributor) for contributor in marc_meta['contributors']],
		))
		if any('390' in contributor['roles'] for contributor in marc_meta['contributors']):
			contributor_lines.append(
				"''Сведения о прежних владельцах приведены по каталогу НББ; "
				"их связь с отсканированным экземпляром не установлена.''"
			)
	if marc_meta.get('personal_name_variants'):
		contributor_lines.append(format_metadata_field(
			'Имена и варианты по каталогу НББ (MARC 900/902/990–992)',
			[format_personal_name_variant(variant) for variant in marc_meta['personal_name_variants']],
		))
	if part_label:
		metadata_lines.append(format_metadata_field('Часть / Выпуск / Part', [part_label]))
	related_entries = format_related_records(record_meta, marc_meta, marc_id)

	metadata_lines.append("\n'''Библиографические сведения / Bibliographic details'''\n")
	if supplied_year:
		metadata_lines.append(format_metadata_field(
			'Дата издания по каталогу (исходная запись) / Catalog publication date (original notation)',
			[date_statement],
		))
	seen_pairs: set[tuple] = set()
	for field in record_meta.get('fields', []):
		name = field.get('name', '').strip()
		if is_circulation_field(name):
			continue
		raw_values = field.get('values')
		if isinstance(raw_values, list):
			values = [str(v).strip() for v in raw_values if str(v).strip()]
		else:
			values = [v.strip() for v in str(field.get('value', '')).splitlines() if v.strip()]
		field_links = field.get('links', [])
		if name == 'Хранение / экземпляр':
			values = [value for value in values if not is_circulation_field(value.partition(':')[0])]
			field_links = [link for link in field_links if not is_circulation_field(link['text'].partition(':')[0])]
		if not name or not values:
			continue
		pair = (name, tuple(values), tuple((link['text'], link['url']) for link in field_links))
		if pair in seen_pairs:
			continue
		seen_pairs.add(pair)
		links_by_text: dict[str, set[str]] = {}
		for link in field_links:
			links_by_text.setdefault(link['text'], set()).add(link['url'])
		inline_texts = {value for value in values if len(links_by_text.get(value, set())) == 1}
		display_values = [
			format_source_link(next(iter(links_by_text[value])), value) if value in inline_texts else value
			for value in values
		]
		metadata_lines.append(format_metadata_field(name, display_values))
		links = [format_source_link(link['url'], link['text']) for link in field_links if link['text'] not in inline_texts]
		if links:
			metadata_lines.append(format_metadata_field(f'{name} — ссылки в каталоге', list(dict.fromkeys(links))))

	imprints = marc_meta.get('imprint_statements', [])
	if imprints:
		metadata_lines.append(format_metadata_field('Выходные данные / Imprint', [
			f"MARC {item['tag']} — {item['text']}" for item in imprints
		]))
	if marc_meta.get('places') and not imprints:
		metadata_lines.append(format_metadata_field('Место издания (MARC 210$a)', marc_meta['places']))
	if marc_meta.get('geo_places'):
		places_normalized = {p.strip().lower() for p in marc_meta.get('places', [])}
		new_geo = [g for g in marc_meta['geo_places'] if g.strip().lower() not in places_normalized]
		if new_geo:
			metadata_lines.append(format_metadata_field('Место издания (MARC 620)', new_geo))
	if marc_meta.get('alternate_geo_places'):
		metadata_lines.append(format_metadata_field('Варианты географических названий (MARC 926)', marc_meta['alternate_geo_places']))
	if marc_meta.get('publishers') and not imprints:
		metadata_lines.append(format_metadata_field('Издатель (MARC 210$c)', marc_meta['publishers']))
	for inherited in marc_meta.get('inherited_metadata', []):
		reference = format_catalog_reference(inherited['record_id'], inherited['title'])
		label = RenderedWikitext(PARENT_IMPRINT_FIELDS[inherited['field']] + ' — из родительской записи ' + reference)
		metadata_lines.append(format_metadata_field(label, inherited['values']))
	if marc_meta.get('manufacturing_statements'):
		metadata_lines.append(format_metadata_field('Изготовление (MARC 210)', marc_meta['manufacturing_statements']))
	if marc_meta.get('edition'):
		metadata_lines.append(format_metadata_field('Издание (MARC 205)', [marc_meta['edition']]))
	if marc_meta.get('publication_terms'):
		metadata_lines.append(format_metadata_field(
			'Условия приобретения и переплет издания по каталогу НББ (MARC 010)',
			marc_meta['publication_terms'],
		))
	phys_list = marc_meta.get('physical_descriptions') or (
		[marc_meta['physical_description']] if marc_meta.get('physical_description') else []
	)
	if phys_list:
		metadata_lines.append(format_metadata_field('Физическое описание (MARC 215)', phys_list))
	if marc_meta.get('series'):
		metadata_lines.append(format_metadata_field('Серия (MARC 225/461)', marc_meta['series']))
	if marc_meta.get('languages'):
		label = 'Код языка (MARC 101$a)' if (
			marc_meta.get('original_languages')
			or marc_meta.get('intermediate_languages')
			or marc_meta.get('summary_languages')
		) else 'Код языка (MARC 101)'
		metadata_lines.append(format_metadata_field(label, marc_meta['languages']))
	if marc_meta.get('original_languages'):
		metadata_lines.append(format_metadata_field('Код языка оригинала (MARC 101$c)', marc_meta['original_languages']))
	if marc_meta.get('intermediate_languages'):
		metadata_lines.append(format_metadata_field('Код языка промежуточного перевода (MARC 101$b)', marc_meta['intermediate_languages']))
	if marc_meta.get('summary_languages'):
		metadata_lines.append(format_metadata_field('Код языка резюме (MARC 101$d)', marc_meta['summary_languages']))
	if marc_meta.get('country'):
		metadata_lines.append(format_metadata_field('Код страны (MARC 102)', [marc_meta['country']]))
	if contributor_lines:
		metadata_lines.extend(["\n'''Лица и организации / People and organizations'''\n", *contributor_lines])
	if any(marc_meta.get(key) for key in ('subjects', 'genres', 'alternate_genres', *CATALOG_VARIANT_SECTIONS)):
		metadata_lines.append("\n'''Тематика и жанр / Subjects and genres'''\n")
	if marc_meta.get('subjects'):
		existing_subjects = {
			val.strip().lower()
			for field in record_meta.get('fields', [])
			if field.get('name', '').strip() in ('Предмет', 'Тематика')
			for val in (field.get('values') or [field.get('value', '')])
		}
		new_subjects = [
			s for s in marc_meta['subjects']
			if s.strip().lower() not in existing_subjects
		]
		if new_subjects:
			metadata_lines.append(format_metadata_field('Тематика (MARC 6xx)', new_subjects))
	for key, label in CATALOG_VARIANT_SECTIONS.items():
		if marc_meta.get(key):
			metadata_lines.append(format_metadata_field(label, [format_catalog_variant(variant) for variant in marc_meta[key]]))
	if marc_meta.get('genres'):
		existing_genres = {
			val.strip().lower().replace(' - ', ' — ')
			for field in record_meta.get('fields', [])
			if field.get('name', '').strip() == 'Жанр'
			for val in (field.get('values') or [field.get('value', '')])
		}
		new_genres = [
			g for g in marc_meta['genres']
			if g.strip().lower().replace(' - ', ' — ') not in existing_genres
		]
		if new_genres:
			metadata_lines.append(format_metadata_field('Форма / жанр (MARC 608)', new_genres))
	if marc_meta.get('alternate_genres'):
		metadata_lines.append(format_metadata_field('Варианты названий формы / жанра (MARC 908)', marc_meta['alternate_genres']))
	contents = list(dict.fromkeys([*marc_meta.get('contents', []), *record_meta.get('contents', [])]))
	if contents or marc_meta.get('notes'):
		metadata_lines.append("\n'''Содержание и примечания / Contents and notes'''\n")
	if contents:
		metadata_lines.append(format_metadata_field('Содержание (MARC 327 / оглавление каталога)', contents))
	if marc_meta.get('notes'):
		metadata_lines.append(format_metadata_field('Примечания (MARC 3xx)', marc_meta['notes']))
	if marc_meta.get('udc'):
		metadata_lines.append(format_metadata_field('УДК (MARC 675)', marc_meta['udc']))
	if marc_meta.get('shelfmarks'):
		existing_shelfmarks = {
			val.strip().rstrip(',').lower()
			for field in record_meta.get('fields', [])
			if 'шифр' in field.get('name', '').lower()
			for val in (field.get('values') or [field.get('value', '')])
		}
		new_shelfmarks = [
			s for s in marc_meta['shelfmarks']
			if s.strip().rstrip(',').lower() not in existing_shelfmarks
		]
		if new_shelfmarks:
			metadata_lines.append(format_metadata_field('Шифр хранения (MARC 899)', new_shelfmarks))
	if related_entries:
		metadata_lines.append(format_metadata_field('Связанные записи по каталогу НББ', related_entries))
	for hierarchy in record_meta.get('catalog_hierarchies', []):
		labels = {}
		for node in hierarchy['nodes']:
			title = node['label']
			if node.get('title') and node['title'] != title:
				title += ' — ' + node['title']
			labels[node['record_id']] = format_source_link(node['url'], f"{title} ({node['record_id']})")
		entries = []
		for node in hierarchy['nodes']:
			label = labels[node['record_id']]
			if node['parent_id']:
				label = labels[node['parent_id']] + ' → ' + label
			if node['record_id'] == marc_id:
				label += ' — текущая запись / current record'
			entries.append(RenderedWikitext(label))
		metadata_lines.append(format_metadata_field('Иерархия записей каталога / Catalog hierarchy', entries))
	supplement = format_marc_supplement(marc_meta, '\n'.join(metadata_lines) + '\n' + date_value)
	if supplement:
		metadata_lines.extend(["\n'''Дополнительные сведения каталога / Additional catalog details'''\n", *supplement])

	metadata_lines.append("\n'''Источник и идентификаторы / Source and identifiers'''\n")
	metadata_lines.append(f"* '''NLB Record ID (marcID):''' <code>{escape_wikitext(marc_id)}</code>")
	metadata_lines.append(f"* '''NLB File ID (fileID):''' <code>{escape_wikitext(file_id)}</code>")

	description_body = '\n'.join(metadata_lines)

	authors_list = list(marc_meta.get('authors') or [])
	# Untyped source lists must not turn known former owners or editors back into authors.
	if not authors_list and not marc_meta.get('contributors'):
		if marc_meta.get('responsibility'):
			authors_list.append(marc_meta['responsibility'])
		else:
			for field in record_meta.get('fields', []):
				if field.get('name') in {'Авторы', 'Автор', 'Другие авторы'}:
					raw_vals = field.get('values') or str(field.get('value', '')).splitlines()
					for val in raw_vals:
						if str(val).strip():
							authors_list.append(str(val).strip())
	author_line = f"|author={'<br>'.join(escape_wikitext(a) for a in authors_list)}\n" if authors_list else ''

	record_url = f'{ELIB_BASE_URL}/elib/Record/{quote(marc_id, safe="")}'
	marcxml_url = f'{record_url}/Export?style=MARCXML'
	viewer_url = f'{VIEWER_BASE_URL}/viewer?' + urlencode({'markID': marc_id, 'fileID': file_id})
	source_value = (
		format_source_link(record_url, f'Электронная библиотека Национальной библиотеки Беларуси ({marc_id})') + '; '
		+ format_source_link(viewer_url, f'цифровой скан (fileID {file_id})') + '; '
		+ format_source_link(marcxml_url, 'MARCXML')
	)

	normalized_category = category.strip()
	return normalize_wikitext(
		'=={{int:filedesc}}==\n'
		'{{Information\n'
		f'|description={{{{ru|1={description_body}}}}}\n'
		f'|date={date_value}\n'
		f'|source={source_value}\n'
		f'{author_line}'
		'|other_versions=\n'
		'}}\n\n'
		'=={{int:license-header}}==\n'
		f'{{{{{license_template}}}}}\n\n'
		f'[[Category:{normalized_category}]]\n'
	)


def calculate_sha1(file_path: Path) -> str:
	"""Compute the lowercase hexadecimal SHA-1 digest of a local file."""
	with file_path.open('rb') as handle:
		return hashlib.file_digest(handle, 'sha1').hexdigest()


def parse_commons_allimages_response(payload: Any, expected_sha1: str) -> list[str]:
	"""Validate Commons results; only a complete empty result proves absence."""
	if not isinstance(payload, dict):
		raise ValueError('Invalid Commons allimages response: expected a JSON object')
	if 'error' in payload or 'errors' in payload:
		raise ValueError(f"Commons allimages API error: {payload.get('error', payload.get('errors'))}")
	query = payload.get('query')
	if not isinstance(query, dict) or not isinstance(query.get('allimages'), list):
		raise ValueError('Invalid Commons allimages response: expected query.allimages list')
	matches = query['allimages']
	if not matches and payload.get('continue'):
		raise ValueError('Incomplete Commons allimages response: empty results with continuation')
	titles: list[str] = []
	for item in matches:
		if not isinstance(item, dict):
			raise ValueError('Invalid Commons allimages response: expected a file object')
		sha1 = item.get('sha1')
		if not isinstance(sha1, str) or sha1.lower() != expected_sha1.lower():
			raise ValueError(f'Unexpected SHA-1 in Commons response: {sha1} != {expected_sha1}')
		title = item.get('title') or item.get('name')
		if not isinstance(title, str) or not title.strip():
			raise ValueError('Invalid Commons allimages response: missing file title')
		titles.append(title)
	return titles


def check_commons_by_hash(
	file_path: Path,
	session: requests.Session | None = None,
) -> tuple[str, list[str]]:
	'''Hash the file once and query Commons directly, retrying transient failures.

	Timeouts, broken connections, invalid JSON, rate limits and server failures
	receive bounded retries, honoring maxlag and numeric Retry-After responses.
	An inconclusive lookup raises instead of allowing an upload.
	'''
	digest = calculate_sha1(file_path)
	http = session or requests
	attempt = 1
	while True:
		try:
			with http.get(
				COMMONS_API_URL,
				params={
					'action': 'query',
					'list': 'allimages',
					'format': 'json',
					'aisha1': digest,
					'aiprop': 'sha1',
					'maxlag': 5,
				},
				headers={'User-Agent': COMMONS_USER_AGENT},
				timeout=30,
			) as response:
				response.raise_for_status()
				payload = response.json()
				api_error = payload.get('error') if isinstance(payload, dict) else None
				code = api_error.get('code') if isinstance(api_error, dict) else None
				if code in {'maxlag', 'ratelimited'}:
					raise requests.exceptions.RetryError(f'Commons hash lookup deferred: {code}', response=response)
				return digest, parse_commons_allimages_response(payload, digest)
		except (
			requests.exceptions.Timeout,
			requests.exceptions.ConnectionError,
			requests.exceptions.ChunkedEncodingError,
			requests.exceptions.JSONDecodeError,
			requests.exceptions.HTTPError,
			requests.exceptions.RetryError,
		) as error:
			if isinstance(error, requests.exceptions.SSLError):
				raise
			if isinstance(error, requests.exceptions.HTTPError) and (
				error.response is None or error.response.status_code not in {429, 500, 502, 503, 504}
			):
				raise
			if attempt >= HASH_CHECK_MAX_ATTEMPTS:
				raise
			delay = HASH_CHECK_RETRY_DELAY * 2 ** (attempt - 1)
			if error.response is not None:
				try:
					delay = max(delay, int(error.response.headers.get('Retry-After', '0')))
				except (ValueError, TypeError):
					pass
			log_progress(
				f'[SHA-1 {digest}] Commons lookup attempt {attempt}/{HASH_CHECK_MAX_ATTEMPTS} '
				f'failed ({type(error).__name__}: {error}); retrying in {delay} seconds'
			)
			time.sleep(delay)
			attempt += 1


def load_viewer_cookies(path: Path) -> requests.cookies.RequestsCookieJar:
	"""Import unexpired viewer cookies from a browser's Netscape-format export.

	Preserve domain, path, secure and HttpOnly attributes. Blank or zero expiry
	represents a browser session cookie. Require a viewer session applicable to
	the IIIF endpoint so initialization cannot replace it with a guest session.
	"""
	exported = MozillaCookieJar(path)
	try:
		with warnings.catch_warnings():
			# The standard parser's warning and LoadError can expose malformed cookie lines.
			warnings.filterwarnings('ignore', message=r'http\.cookiejar bug!')
			exported.load(ignore_discard=True, ignore_expires=True)
	except LoadError:
		raise ValueError('Invalid viewer cookie file; export NLB cookies in Netscape cookies.txt format.') from None

	cookies = requests.cookies.RequestsCookieJar()
	viewer_session = requests.cookies.RequestsCookieJar()
	viewer_host = urlsplit(VIEWER_BASE_URL).hostname
	for cookie in exported:
		domain = cookie.domain.lstrip('.').lower()
		# Requests sends host-only parent cookies to subdomains; exclude those here.
		if domain != viewer_host and not (cookie.domain_specified and domain == 'nlb.by'):
			continue
		if cookie.expires == 0:
			cookie.expires = None
			cookie.discard = True
		if cookie.is_expired():
			continue
		cookies.set_cookie(cookie)
		if cookie.name == 'elib_session_id' and cookie.value:
			viewer_session.set_cookie(cookie)
	request = requests.Request('GET', f'{VIEWER_BASE_URL}/iiif/manifest').prepare()
	if not requests.cookies.get_cookie_header(viewer_session, request):
		raise ValueError(
			'Viewer cookies lack a current elib_session_id for elib.nlb.by; '
			'sign in, open the library viewer, and export fresh cookies.'
		)
	return cookies


class DownloadRetriesExhausted(RuntimeError):
	"""A scan's transient download failures used up its bounded retry attempts."""

	def __init__(self, last_error: requests.exceptions.RequestException, attempts: int) -> None:
		"""Retain the original failure and attempt count for logging and resumable state."""
		self.last_error = last_error
		self.attempts = attempts
		super().__init__(f'Download failed after {attempts} attempts ({type(last_error).__name__}): {last_error}')


class ViewerSessionExpired(RuntimeError):
	'''The viewer explicitly rejected a session that no longer exists server-side.'''

	def __init__(self) -> None:
		'''Explain recovery without copying the session identifier from the response.'''
		super().__init__(
			'NLB viewer session no longer exists on the server. With --viewer-cookies, '
			'restarting reloads the same expired session: sign in, open the NLB viewer, '
			'export fresh cookies, and restart with the new file. '
			'Without exported cookies, retry later; automatic session renewal did not recover the download.'
		)


class CatalogResourceMissing(requests.HTTPError):
	'''A required catalog or viewer metadata GET returned HTTP 404 or 410.'''


class CatalogRecordMissing(CatalogResourceMissing):
	'''The primary catalog record itself returned HTTP 404 or 410.'''


class NlbClient:
	'''HTTP client for browsing elib.nlb.by metadata and downloading IIIF PDF scans.'''

	def __init__(
		self,
		session: requests.Session | None = None,
		timeout: int = 60,
		viewer_cookies: Path | None = None,
		archive: Any | None = None,
		metadata_reads_only: bool = False,
	) -> None:
		'''Initialize HTTP, optional source capture, and an exported signed-in viewer session.'''
		self.session = session or requests.Session()
		self.session.headers.setdefault('User-Agent', DEFAULT_USER_AGENT)
		self.timeout = timeout
		self.archive = archive
		self.metadata_reads_only = metadata_reads_only
		self._viewer_session_initialized = False
		self._viewer_cookies_imported = viewer_cookies is not None
		self._viewer_api_health = ViewerAPIHealth()
		self._parent_metadata_cache: dict[str, dict[str, Any]] = {}
		self._parent_response_cache: dict[str, requests.Response] = {}
		if viewer_cookies is not None:
			self.session.cookies.update(load_viewer_cookies(viewer_cookies))
			self._viewer_session_initialized = True

	def _get_catalog(self, url: str, *, optional: bool = False, **kwargs: Any) -> requests.Response:
		'''Capture and validate catalog GETs; only optional missing resources allow HTTP 404.'''
		try:
			return get_catalog_response(
				self.session, url, timeout=self.timeout, log=log_progress,
				observe=self.archive.capture_response if self.archive is not None else None, **kwargs,
			)
		except requests.HTTPError as error:
			if optional and error.response is not None and error.response.status_code == 404:
				return error.response
			if error.response is not None and error.response.status_code in (404, 410):
				raise CatalogResourceMissing(str(error), response=error.response, request=error.request) from error
			raise

	def _get_viewer(self, operation: str, record_id: str, url: str, **kwargs: Any) -> requests.Response:
		'''Stop a widespread viewer outage after preserving each failed HTTP response.

		One missing record or scan remains a retryable metadata failure. Repeated
		failures across distinct records stop the batch; unrelated working viewer
		operations cannot reset the failing endpoint's health counter.
		'''
		try:
			response = self._get_catalog(url, **kwargs)
		except (CatalogRetriesExhausted, requests.RequestException) as error:
			self._viewer_api_health.failed(operation, record_id, getattr(error, 'last_error', error))
			raise
		self._viewer_api_health.succeeded(operation)
		return response

	def fetch_search_page(
		self,
		page: int,
		from_year: int | None = None,
		to_year: int | None = DEFAULT_UNTIL_YEAR,
		search_url: str | None = None,
	) -> dict[str, Any]:
		'''Fetch and parse a single page of digitized search results.'''
		if search_url:
			rec_match = RECORD_ID_RE.search(search_url)
			if rec_match:
				if page > 1:
					return {'total_results': 1, 'records': []}
				return {
					'total_results': 1,
					'records': [{
						'marc_id': rec_match.group(1),
						'title': '',
						'formats': [],
						'file_ids': [],
						'mets_ids': [],
					}],
				}
		url = build_search_page_url(
			page=page,
			from_year=from_year,
			to_year=to_year,
			search_url=search_url,
		)
		response = self._get_catalog(url, validate=validate_search_html)
		return parse_search_results_html(response.text)

	def fetch_record_metadata(self, marc_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
		'''Preserve record HTML, MARCXML, contents and any advertised catalog hierarchy.'''
		from catalog_hierarchy import advertised_hierarchy, hierarchy_identifiers, parse_hierarchy

		def validate_record(response: requests.Response) -> None:
			'''Check both record identity and an advertised hierarchy's target before use.'''
			validate_record_html(response, marc_id)
			advertised_hierarchy(response, marc_id, ELIB_BASE_URL)

		record_url = f'{ELIB_BASE_URL}/elib/Record/{marc_id}'
		try:
			record_resp = self._get_catalog(record_url, validate=validate_record)
		except requests.HTTPError as error:
			if error.response is not None and error.response.status_code in (404, 410):
				raise CatalogRecordMissing(str(error), response=error.response, request=error.request) from error
			raise
		record_meta = parse_record_html(record_resp.text, marc_id)

		marcxml_url = f'{ELIB_BASE_URL}/elib/Record/{marc_id}/Export?style=MARCXML'
		marc_resp = self._get_catalog(
			marcxml_url, optional=True, validate=lambda response: validate_marcxml(response, marc_id),
		)
		marc_meta = parse_marcxml(marc_resp.text) if marc_resp.status_code == 200 else parse_marcxml('')
		toc_url = f'{ELIB_BASE_URL}/elib/Record/{marc_id}/TOC'
		toc_resp = self._get_catalog(
			toc_url, optional=True, validate=lambda response: validate_toc_html(response, marc_id),
		)
		if toc_resp.status_code == 200:
			toc_soup = BeautifulSoup(toc_resp.text, 'html.parser')
			for container in toc_soup.select('.tab-pane.toc-tab, ul.toc'):
				for script in container.select('script, style'):
					script.decompose()
				# Contents can be paragraphs or plain text instead of a list.
				for text in extract_cell_values(container):
					for entry in extract_contents_entries(text):
						if entry not in record_meta.setdefault('contents', []):
							record_meta['contents'].append(entry)
		hierarchy_url = advertised_hierarchy(record_resp, marc_id, ELIB_BASE_URL)
		if hierarchy_url:
			hierarchy_page = self._get_catalog(hierarchy_url,
				validate=lambda response: hierarchy_identifiers(response, marc_id))
			for hierarchy_id in hierarchy_identifiers(hierarchy_page, marc_id):
				tree = self._get_catalog(f'{ELIB_BASE_URL}/elib/Hierarchy/GetTreeJSON',
					params={'hierarchyID': hierarchy_id, 'id': marc_id},
					validate=lambda response: parse_hierarchy(response, marc_id, hierarchy_id, ELIB_BASE_URL))
				record_meta.setdefault('catalog_hierarchies', []).append(
					parse_hierarchy(tree, marc_id, hierarchy_id, ELIB_BASE_URL))
		self.inherit_parent_imprint(marc_id, marc_meta)
		return record_meta, marc_meta

	def inherit_parent_imprint(self, marc_id: str, marc_meta: dict[str, Any]) -> None:
		'''Attribute missing places/publishers to one direct parent, without merging child data.

	Bound-with and local 961 links never trigger inheritance. Only successful,
	ID-verified parent exports are cached; no parent-of-parent traversal occurs.
	Explicitly missing parents retain the relationship; transient failures defer
	the child rather than silently discard available parent metadata.
	'''
		missing = [field for field in PARENT_IMPRINT_FIELDS if not marc_meta.get(field)]
		parents = {
			link['record_id'] for link in marc_meta.get('related_records', [])
			if link['tag'] == '461'
		}
		if not missing or len(parents) != 1:
			return
		parent_id = next(iter(parents))
		if parent_id == marc_id or not NLB_RECORD_ID_RE.fullmatch(parent_id):
			return
		if parent_id not in self._parent_metadata_cache:
			url = f'{ELIB_BASE_URL}/elib/Record/{parent_id}/Export?style=MARCXML'
			log_progress(f'[{marc_id}] Fetching parent metadata: {url}')
			response = self._get_catalog(
				url, optional=True, validate=lambda response: validate_marcxml(response, parent_id),
			)
			if response.status_code == 404:
				log_progress(f'[{marc_id}] Parent metadata unavailable for {parent_id}: HTTP 404')
				return
			self._parent_metadata_cache[parent_id] = parse_marcxml(response.text)
			self._parent_response_cache[parent_id] = response
		elif self.archive is not None:
			# Associate the cached parent source with each child that uses its imprint.
			self.archive.capture_response(self._parent_response_cache[parent_id])
		parent_meta = self._parent_metadata_cache[parent_id]
		marc_meta['inherited_metadata'] = [
			{
				'field': field, 'values': list(parent_meta[field]),
				'record_id': parent_id, 'title': parent_meta['title'],
			}
			for field in missing if parent_meta.get(field)
		]

	def ensure_viewer_session(self, force: bool = False) -> None:
		'''Initialize download statistics sessions; public metadata GETs do not need one.'''
		if getattr(self, 'metadata_reads_only', False):
			return
		if self._viewer_session_initialized and not force:
			return
		now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
		payload = {
			'userLogin': 'guest',
			'createDate': now_str,
			'browser': 'Firefox',
			'os': 'Linux',
			'device': None,
			'city': 'Minsk',
			'country': 'Belarus',
		}
		resp = self.session.post(
			f'{VIEWER_BASE_URL}/stats/session',
			json=payload,
			headers={'Authorization': VIEWER_BASIC_AUTH},
			timeout=self.timeout,
		)
		resp.raise_for_status()
		self._viewer_session_initialized = True

	def fetch_mets_files(self, mets_id: str, *, marc_id: str | None = None) -> list[dict[str, str]]:
		'''Fetch METS files, attributing shared viewer failures to the catalog record.'''
		self.ensure_viewer_session()
		resp = self._get_viewer('METS structure', marc_id or mets_id,
			f'{VIEWER_BASE_URL}/mets//structure/state?id={mets_id}',
			headers={'Authorization': VIEWER_BASIC_AUTH},
			validate=lambda response: validate_mets_json(response, mets_id),
		)
		return extract_files_from_mets_structure(resp.json())

	def check_doc_access(self, marc_id: str) -> dict[str, Any]:
		'''Query /iiif/access to determine whether a record is open-access.'''
		self.ensure_viewer_session()
		resp = self._get_viewer('IIIF access', marc_id,
			f'{VIEWER_BASE_URL}/iiif/access',
			params={'marcID': marc_id},
			headers={'Authorization': VIEWER_BASIC_AUTH},
			validate=validate_access_json,
		)
		return resp.json()

	def resolve_record_files(
		self, search_entry: dict[str, Any], record_meta: dict[str, Any],
	) -> list[dict[str, Any]]:
		'''Keep all METS scope evidence, including when a file also has a direct link.'''
		by_id: dict[str, dict[str, Any]] = {}
		for file_id in [*search_entry.get('file_ids', []), *record_meta.get('file_ids', [])]:
			by_id.setdefault(file_id, {'file_id': file_id, 'part_label': ''})
		all_volumes: set[int] = set()
		unresolved = False
		for mets_id in dict.fromkeys([*search_entry.get('mets_ids', []), *record_meta.get('mets_ids', [])]):
			for item in self.fetch_mets_files(mets_id, marc_id=record_meta.get('marc_id') or search_entry.get('marc_id')):
				entry = by_id.setdefault(item['file_id'], {'file_id': item['file_id'], 'part_label': ''})
				for key in ('part_label', 'file_location'):
					if item.get(key) and not entry.get(key):
						entry[key] = item[key]
				for context in item.get('mets_contexts', []):
					context = {**context, 'mets_id': str(mets_id)}
					if context not in entry.setdefault('mets_contexts', []):
						entry['mets_contexts'].append(context)
				all_volumes.update(item.get('mets_volumes', []))
				unresolved |= item.get('mets_unresolved_volume', False)
		for entry in by_id.values():
			if all_volumes or unresolved or 'mets_contexts' in entry:
				entry.update(mets_volumes=sorted(all_volumes), mets_unresolved_volume=unresolved)
		return list(by_id.values())

	def fetch_scan_manifest(self, marc_id: str, file_id: str) -> dict[str, Any]:
		'''Capture identity-checked viewer metadata even for scans already on Commons.'''
		self.ensure_viewer_session()
		response = self._get_viewer('IIIF manifest', marc_id,
			f'{VIEWER_BASE_URL}/iiif/manifest',
			params={'marcID': marc_id, 'fileId': file_id},
			headers={'Authorization': VIEWER_BASIC_AUTH},
			validate=lambda response: validate_manifest(response, marc_id, file_id),
		)
		manifest = response.json()
		if not hasattr(self, '_scan_manifests'):
			self._scan_manifests = {}
		self._scan_manifests[(marc_id, file_id)] = manifest
		return manifest

	def download_pdf_scan(self, marc_id: str, file_id: str, target_path: Path) -> int:
		'''Retry transient download failures with backoff, discarding partial PDFs.

		Each GET/stream attempt starts from byte zero within one retry budget.
		Renew an explicitly missing guest session once within that budget; imported
		sessions require a fresh browser export instead of replacing authentication.
		Statistics outages alone do not block the PDF, matching the NLB viewer.
		Repeated complete invalid PDFs with the same validation context are deferred
		after two copies; partial transfers and changed bodies retain the full budget.
		'''
		if getattr(self, 'metadata_reads_only', False):
			raise ValueError('This client is configured for metadata reads only')
		self.ensure_viewer_session()
		attempt = 1
		session_renewed = False
		rejected_pdfs = set()
		while True:
			try:
				return self._download_pdf_scan_once(marc_id, file_id, target_path)
			except ViewerSessionExpired:
				if self._viewer_cookies_imported or session_renewed or attempt >= DOWNLOAD_MAX_ATTEMPTS:
					raise
				log_progress(f'[{marc_id}:{file_id}] Renewing the expired guest viewer session once.')
				self.ensure_viewer_session(force=True)
				session_renewed = True
				attempt += 1
			except (
				InvalidPDFError,
				InvalidCatalogResponse,
				requests.exceptions.JSONDecodeError,
				requests.exceptions.Timeout,
				requests.exceptions.ConnectionError,
				requests.exceptions.ChunkedEncodingError,
				requests.exceptions.HTTPError,
			) as error:
				if isinstance(error, requests.exceptions.SSLError):
					raise
				if isinstance(error, requests.exceptions.HTTPError):
					if error.response is None or error.response.status_code not in DOWNLOAD_TRANSIENT_STATUSES:
						raise
					error.response.close()
				target_path.unlink(missing_ok=True)
				getattr(self, '_scan_manifests', {}).pop((marc_id, file_id), None)
				rejected = getattr(error, 'rejected_pdf', None)
				if rejected is not None:
					identity = (rejected['sha256'], rejected['manifest_pages'])
					if identity in rejected_pdfs:
						log_progress(
							f'[{marc_id}:{file_id}] Same complete PDF failed strict validation twice '
							'with the same manifest page count; deferring for review.'
						)
						raise DownloadRetriesExhausted(error, attempt) from error
					rejected_pdfs.add(identity)
				if attempt >= DOWNLOAD_MAX_ATTEMPTS:
					raise DownloadRetriesExhausted(error, attempt) from error
				delay = DOWNLOAD_RETRY_DELAY * 2 ** (attempt - 1)
				log_progress(
					f'[{marc_id}:{file_id}] Download attempt {attempt}/{DOWNLOAD_MAX_ATTEMPTS} '
					f'failed ({type(error).__name__}: {error}); retrying in {delay} seconds'
				)
				time.sleep(delay)
				attempt += 1

	def _record_viewer_event(self, event: str, marc_id: str, file_id: str) -> None:
		'''Send viewer statistics once, distinguishing expired sessions from outages.

		NLB's viewer tolerates statistics failures. A missing session is different:
		the bytes endpoint also rejects it. Never print the response's session ID.
		Authorization and TLS errors remain fatal; PDF validation still applies.
		'''
		response = None
		try:
			response = self.session.get(
				f'{VIEWER_BASE_URL}/stats/event/{event}',
				params={'marcID': marc_id, 'fileId': file_id},
				headers={'Authorization': VIEWER_BASIC_AUTH}, timeout=self.timeout,
			)
			if response.status_code == 500:
				try:
					payload = response.json()
				except ValueError:
					payload = None
				message = payload.get('message') if isinstance(payload, dict) else None
				if isinstance(message, str) and re.fullmatch(r'Session with id: .+ not exists', message):
					raise ViewerSessionExpired()
			response.raise_for_status()
		except (
			requests.exceptions.Timeout, requests.exceptions.ConnectionError,
			requests.exceptions.ChunkedEncodingError, requests.exceptions.HTTPError,
		) as error:
			if isinstance(error, requests.exceptions.SSLError):
				raise
			if isinstance(error, requests.HTTPError) and (
				error.response is None or error.response.status_code not in DOWNLOAD_TRANSIENT_STATUSES
			):
				raise
			log_progress(
				f'[{marc_id}:{file_id}] Viewer statistics {event} unavailable '
				f'({type(error).__name__}); continuing to the PDF request.'
			)
		finally:
			if response is not None:
				response.close()

	def _download_pdf_scan_once(self, marc_id: str, file_id: str, target_path: Path) -> int:
		'''Log the header's size before streaming; retain bounded HTTP error details.

		Use decimal MB for progress when Content-Length is valid. Unknown sizes
		are omitted, and valid lengths still guard against incomplete transfers.
		'''
		auth_headers = {'Authorization': VIEWER_BASIC_AUTH}

		manifest = getattr(self, '_scan_manifests', {}).get((marc_id, file_id))
		if manifest is None:
			manifest_resp = self.session.get(
				f'{VIEWER_BASE_URL}/iiif/manifest',
				params={'marcID': marc_id, 'fileId': file_id},
				headers=auth_headers, timeout=self.timeout,
			)
			if self.archive is not None:
				self.archive.capture_response(manifest_resp)
			manifest_resp.raise_for_status()
			validate_manifest(manifest_resp, marc_id, file_id)
			manifest = manifest_resp.json()
		else:
			validate_manifest_identity(manifest, marc_id, file_id)

		download_path = f'/iiif/file/{file_id}/bytes'
		renderings = manifest.get('rendering') or []
		if isinstance(renderings, dict):
			renderings = [renderings]
		for rendering in renderings:
			link = rendering.get('id') or rendering.get('@id')
			if link and (rendering.get('format') == 'application/pdf' or rendering.get('type') is None):
				download_path = str(link)
				break

		download_url = urljoin(VIEWER_BASE_URL, download_path)
		parsed_download = urlsplit(download_url)
		if parsed_download.netloc != urlsplit(VIEWER_BASE_URL).netloc or parsed_download.scheme != 'https':
			raise InvalidPDFError('Manifest PDF URL is outside the NLB viewer')
		self._record_viewer_event('PAGE_VIEWS', marc_id, file_id)
		self._record_viewer_event('IIIF_DOWNLOAD_ATTEMPT', marc_id, file_id)

		with self.session.get(
			download_url,
			headers={
				'Authorization': VIEWER_BASIC_AUTH,
				'Accept': 'application/octet-stream',
				'Referer': f'{VIEWER_BASE_URL}/viewer?markID={marc_id}&fileID={file_id}',
			},
			stream=True,
			timeout=self.timeout,
		) as stream_resp:
			try:
				stream_resp.raise_for_status()
			except requests.exceptions.HTTPError as error:
				# Reading diagnostic text must not turn a rejection into a retryable timeout.
				try:
					preview = next(stream_resp.iter_content(chunk_size=512), b'')
					message = ' '.join(preview.decode('utf-8', errors='replace').split())
				except requests.exceptions.RequestException:
					message = ''
				if message:
					error.args = (f'{error}; NLB response: {message}',)
				raise
			try:
				expected_length = int(stream_resp.headers.get('Content-Length', ''))
			except (TypeError, ValueError):
				expected_length = None
			if expected_length is not None and expected_length < 0:
				expected_length = None
			size_label = f' ({expected_length / 1_000_000:.1f} MB)' if expected_length is not None else ''
			viewer_url = f'{VIEWER_BASE_URL}/viewer?markID={marc_id}&fileID={file_id}'
			record_url = f'{ELIB_BASE_URL}/elib/Record/{marc_id}'
			log_progress(f'[{marc_id}:{file_id}] Downloading scan{size_label}:\n{viewer_url} (record: {record_url})')
			bytes_written = 0
			first_chunk = b''
			with target_path.open('wb') as out_file:
				for chunk in stream_resp.iter_content(chunk_size=262144):
					if not chunk:
						continue
					if not first_chunk:
						first_chunk = chunk[:16]
					out_file.write(chunk)
					bytes_written += len(chunk)

		if expected_length is not None and expected_length != bytes_written:
			raise requests.exceptions.ChunkedEncodingError(
				f'Incomplete PDF transfer for {marc_id}:{file_id}: got {bytes_written} of {expected_length} bytes'
			)
		if not first_chunk.startswith(b'%PDF-'):
			raise InvalidPDFError(f'Downloaded content for {marc_id}:{file_id} is not a valid PDF: {first_chunk!r}')
		try:
			validation = validate_pdf(target_path, manifest)
		except InvalidPDFError as error:
			# Only completed PDF-prefixed streams qualify. Partial transfers and login
			# pages never become stable PDF evidence or consume the shorter retry budget.
			manifest_pages = manifest_page_count(manifest)
			if self.archive is not None:
				try:
					error.rejected_pdf = self.archive.preserve_rejected_pdf(
						marc_id, file_id, target_path, source_url=download_url,
						manifest=manifest, manifest_pages=manifest_pages, error=error,
					)
				except OSError as archive_error:
					raise ArchiveWriteError(str(archive_error)) from archive_error
			else:
				error.rejected_pdf = {**file_integrity(target_path), 'manifest_pages': manifest_pages}
			raise
		if not hasattr(self, 'pdf_validation'):
			self.pdf_validation = {}
		self.pdf_validation[(marc_id, file_id)] = validation
		return bytes_written


def is_retryable_upload_error(error: Exception) -> bool:
	"""Return True if a Pywikibot or network exception is transient and retryable."""
	error_code = getattr(error, 'code', None)
	if isinstance(error_code, str):
		if error_code in RETRYABLE_UPLOAD_API_ERROR_CODES:
			return True
		if error_code.startswith(RETRYABLE_UPLOAD_API_ERROR_PREFIXES):
			return True
	if type(error).__name__ in {'ConnectionError', 'ServerError', 'TimeoutError'}:
		return True
	return False


def extract_duplicate_filenames(error: Exception) -> list[str]:
	"""Extract existing Commons filenames from a Pywikibot duplicate UploadError."""
	message = getattr(error, 'message', None) or getattr(error, 'info', None) or str(error)
	match = re.search(r'\[[^\]]*\]', message)
	if not match:
		return []
	try:
		duplicates = ast.literal_eval(match.group(0))
	except (SyntaxError, ValueError):
		return []
	if not isinstance(duplicates, list):
		return []
	return [name for name in duplicates if isinstance(name, str)]


def patch_mime_multipart_from_mangling(mime_multipart_cls: Any) -> None:
	"""Disable Unix mbox '>From ' mangling in Pywikibot's multipart MIME serializer."""
	if getattr(mime_multipart_cls, '_no_from_mangle_patched', False):
		return
	orig_as_bytes = mime_multipart_cls.as_bytes

	def as_bytes_without_from_mangling(self: Any, unixfrom: bool = False, policy: Any = None) -> bytes:
		base_policy = self.policy if policy is None else policy
		if hasattr(base_policy, 'clone'):
			base_policy = base_policy.clone(mangle_from_=False)
		return orig_as_bytes(self, unixfrom=unixfrom, policy=base_policy)

	mime_multipart_cls.as_bytes = as_bytes_without_from_mangling
	mime_multipart_cls._no_from_mangle_patched = True


def configure_upload_token_recovery(site: Any, request_class: type, api_error_class: type[Exception]) -> None:
	"""Use current CSRF tokens for this site's uploads, with one refresh per request.

	Pywikibot's chunk uploader retains its initial token for later chunks, polls,
	and a precreated final request. Refresh at submission so a recovered token
	reaches all of them. Its private request factory is adapted only for upload
	requests; queries and cached requests keep their original classes.
	"""
	original_factory = site._request_class
	if getattr(original_factory, '_nlb_upload_token_recovery', False):
		return

	class UploadRequest(request_class):
		"""Keep chunk and polling tokens current without unbounded token recovery."""

		def submit(self) -> dict[str, Any]:
			"""Read the current wallet token immediately before submitting an upload."""
			self._nlb_token_refreshed = False
			self['token'] = self.site.tokens['csrf']
			return super().submit()

		def _bad_token(self, code: str) -> bool:
			"""Allow Pywikibot to refresh once, even when successive error details differ."""
			if code == 'badtoken':
				if self._nlb_token_refreshed:
					raise api_error_class('badtoken', 'Commons rejected a refreshed CSRF token; upload stopped.')
				self._nlb_token_refreshed = True
			return super()._bad_token(code)

	def request_factory(kwargs: dict[str, Any]) -> type:
		"""Select the guarded request class only for uncached upload API calls."""
		base_class = original_factory(kwargs)
		parameters = kwargs.get('parameters') or kwargs
		if base_class is request_class and parameters.get('action') == 'upload':
			return UploadRequest
		return base_class

	request_factory._nlb_upload_token_recovery = True
	site._request_class = request_factory


class UploadRetriesExhausted(RuntimeError):
	"""A transient Commons failure exhausted the scan's shared upload retry budget."""

	def __init__(self, last_error: Exception, attempts: int) -> None:
		"""Retain diagnostics for a pending upload that can be retried on a later run."""
		self.last_error = last_error
		self.attempts = attempts
		super().__init__(f'Upload failed after {attempts} attempts ({type(last_error).__name__}): {last_error}')


def make_stash_retrying_uploader(uploader_class: type, api_error_class: type[Exception]) -> type:
	'''Adapt Pywikibot's publication phase to retry locks using the assembled stash.

	A failed asynchronous status poll can repeat a stored failure indefinitely.
	Retry the original publication request instead, keeping its file key, text,
	summary and warning policy. Chunk transfers retain Pywikibot's own handling.
	Support both the legacy submit hook and the newer publication state machine.
	https://www.mediawiki.org/wiki/API:Upload#Example_3:_Upload_file_in_chunks
	'''
	class StashRetryingUploader(uploader_class):
		'''Reuse one completed stash with the caller's shared bounded retry policy.'''

		def __init__(self, *args: Any, retry: Any, **kwargs: Any) -> None:
			'''Keep retry accounting in the outer uploader, shared with chunk failures.'''
			super().__init__(*args, **kwargs)
			self.retry = retry
			self.already_exists = False

		def _retry_publication(self, error: Exception, file_key: str | None) -> bool:
			'''Retry only locks on an assembled stash, sharing the outer upload budget.'''
			message = getattr(error, 'info', '') or str(error)
			if not file_key or getattr(error, 'code', '') != 'stashfailed' or (
				'could not acquire lock' not in message.lower()
			):
				raise error
			if not self.retry(error, 'Commons publication'):
				self.already_exists = True
				return False
			return True

		def _finalize_upload(self, state: Any) -> bool | None:
			'''Restore the publication request after newer Pywikibot replaces it with a poll.'''
			if not state.request.get('checkstatus'):
				self._publication_request = state.request
				self._publication_stage = state.stage
			while True:
				try:
					return super()._finalize_upload(state)
				except api_error_class as error:
					if not self._retry_publication(error, state.file_key):
						return True
					state.request = self._publication_request
					state.stage = self._publication_stage
					state.response = None

		def submit(self, request: Any, result: Any, data_result: Any, ignore_warnings: Any,
			ignore_all_warnings: bool, report_success: Any, file_key: str | None) -> bool:
			'''Restart legacy publication after a lock error, including errors from status polls.'''
			while True:
				try:
					return super().submit(request, result, data_result, ignore_warnings,
						ignore_all_warnings, report_success, file_key)
				except api_error_class as error:
					if not self._retry_publication(error, file_key):
						return True
					result = None

	return StashRetryingUploader


class CommonsUploader:
	'''Wrapper around Pywikibot to upload files to Wikimedia Commons with retries.'''

	def __init__(self, pywikibot_dir: str = DEFAULT_PYWIKIBOT_DIR) -> None:
		"""Configure PYWIKIBOT_DIR and initialize the Commons site connection."""
		os.environ.setdefault('PYWIKIBOT_DIR', pywikibot_dir)
		import pywikibot
		from pywikibot import config as pwb_config
		import pywikibot.data.api
		from pywikibot.site._upload import Uploader

		pwb_config.socket_timeout = 200
		# Keep Pywikibot/operator throttling and maxlag defaults for Wikimedia writes.
		patch_mime_multipart_from_mangling(pywikibot.data.api.CTEBinaryMIMEMultipart)
		self.pywikibot = pywikibot
		self._upload_driver_class = make_stash_retrying_uploader(Uploader, pywikibot.exceptions.APIError)
		self.site = pywikibot.Site('commons', 'commons')
		configure_upload_token_recovery(self.site, pywikibot.data.api.Request, pywikibot.exceptions.APIError)

	def title_exists(self, commons_filename: str) -> bool:
		"""Return True if File:<commons_filename> already exists on Commons."""
		file_page = self.pywikibot.FilePage(self.site, f'File:{commons_filename}')
		return bool(file_page.exists())

	def file_info(self, title: str) -> dict[str, Any]:
		'''Read complete Commons binary/page identity, allowing for publication visibility.'''
		return read_file_info(self.site, title)

	def upload(self, file_path: Path, commons_filename: str, wikitext: str, summary: str) -> dict[str, Any]:
		'''Upload with one retry budget, reusing the stash when publication is locked.'''
		self.site.login()
		if not self.site.has_right('upload'):
			raise self.pywikibot.exceptions.Error('The Commons account does not have upload permission.')
		if self.site.is_uploaddisabled():
			raise self.pywikibot.exceptions.Error('Commons uploads are currently disabled.')
		file_page = self.pywikibot.FilePage(self.site, f'File:{commons_filename}')
		if file_page.exists():
			return {'status': 'existing_title', 'commons_title': f'File:{commons_filename}'}

		failures = 0

		def retry(error: Exception, phase: str) -> bool:
			"""Refresh title existence before another attempt or final deferral."""
			nonlocal failures
			failures += 1
			if failures < UPLOAD_MAX_RETRIES:
				err_label = getattr(error, 'code', None) or type(error).__name__
				log_progress(f'{phase} attempt {failures}/{UPLOAD_MAX_RETRIES} failed with {err_label}. Retrying in 60s...')
				time.sleep(60)
			# exists() alone reuses a cached negative result. This refresh keeps text intact.
			self.site.loadpageinfo(file_page)
			if file_page.exists():
				return False
			if failures >= UPLOAD_MAX_RETRIES:
				raise UploadRetriesExhausted(error, failures) from error
			return True

		while True:
			try:
				driver = self._upload_driver_class(
					self.site, file_page, source_filename=str(file_path), text=wikitext,
					chunk_size=UPLOAD_CHUNK_SIZE,
					asynchronous=True,
					comment=summary,
					ignore_warnings=False,
					retry=retry,
				)
				if not driver.upload():
					raise RuntimeError('Commons upload did not complete.')
				status = 'existing_title' if driver.already_exists else 'uploaded'
				return {'status': status, 'commons_title': f'File:{commons_filename}'}
			except Exception as error:
				error_code = getattr(error, 'code', None)
				if isinstance(error, self.pywikibot.exceptions.UploadError) and error_code == 'exists':
					return {'status': 'existing_title', 'commons_title': f'File:{commons_filename}'}
				if isinstance(error, self.pywikibot.exceptions.UploadError) and error_code == 'duplicate':
					duplicates = extract_duplicate_filenames(error)
					return {
						'status': 'duplicate_sha1',
						'commons_title': f'File:{commons_filename}',
						'duplicates': duplicates,
					}
				if not is_retryable_upload_error(error):
					raise
				if not retry(error, 'Commons upload'):
					return {'status': 'existing_title', 'commons_title': f'File:{commons_filename}'}


def log_progress(message: str) -> None:
	"""Print an unbuffered progress message showing the current URL and action."""
	print(message, flush=True)


def format_elapsed_time(elapsed_seconds: float | int) -> str:
	"""Format a duration in seconds as 'Spent X days Y hours Z minutes W seconds'."""
	total_seconds = max(0, int(round(elapsed_seconds)))
	days, remainder = divmod(total_seconds, 86400)
	hours, remainder = divmod(remainder, 3600)
	minutes, seconds = divmod(remainder, 60)

	parts: list[str] = []
	for value, singular, plural in (
		(days, 'day', 'days'),
		(hours, 'hour', 'hours'),
		(minutes, 'minute', 'minutes'),
		(seconds, 'second', 'seconds'),
	):
		if value > 0 or (not parts and singular == 'second'):
			unit = singular if value == 1 else plural
			parts.append(f'{value} {unit}')

	return 'Spent ' + ' '.join(parts)


def process_single_scan(
	client: NlbClient, uploader: Any, state: dict[str, Any], state_path: Path,
	staging_dir: Path, marc_id: str, file_id: str, part_label: str,
	record_meta: dict[str, Any], marc_meta: dict[str, Any],
	category: str = DEFAULT_CATEGORY, license_template: str = DEFAULT_LICENSE,
	dry_run: bool = False, progress_label: str = '', *,
	explicit_license: bool = False, metadata_only: bool = False,
	file_entry: dict[str, Any] | None = None,
) -> str:
	'''Archive each scan's metadata before deduplication; verify bytes before completion.

	Dry runs use an isolated in-memory state. Failed metadata, rights decisions,
	downloads and verification stay pending at the earliest affected search page.
	'''
	if dry_run or metadata_only:
		state = copy.deepcopy(state)
		state['_read_only'] = True
	key = item_state_key(marc_id, file_id)
	prefix = f'{progress_label} ' if progress_label else ''
	archive = getattr(client, 'archive', None)
	previous = dict(state.get('items', {}).get(key, {}))
	state['current_record_id'], state['current_item_key'] = marc_id, key
	save_state(state_path, state)
	decision = evaluate_publication(record_meta, marc_meta, license_template, explicit_license=explicit_license)
	commons_filename = build_commons_filename(record_meta, marc_meta, marc_id, file_id, part_label)
	identity = assess_scan_identity(file_entry or {'file_id': file_id, 'part_label': part_label}, record_meta, marc_meta)
	base = {'marc_id': marc_id, 'file_id': file_id, 'rights': decision, 'scan_identity': identity}
	viewer_url = f'{VIEWER_BASE_URL}/viewer?markID={marc_id}&fileID={file_id}'

	def finish(status: str, *, pending: bool = False, **details: Any) -> str:
		'''Persist an outcome and its archive relationship without losing pending work.'''
		entry = {**base, **details, 'status': status, 'updated_at': datetime.now(timezone.utc).isoformat()}
		state['items'][key] = entry
		if pending:
			page = max(1, int(state.get('current_page') or 1))
			state['retry_from_page'] = min(page, int(state.get('retry_from_page') or page))
			log_progress(f'{prefix}[{key}] Pending ({status}): {details.get("error", "Review required")}')
		if archive is not None:
			if marc_id not in archive.snapshots:
				archive.save_record(marc_id, record_meta, marc_meta)
			archive.record_result(marc_id, file_id, entry)
		save_state(state_path, state)
		return status

	def preserve_description(metadata: dict[str, Any]) -> str:
		'''Keep the complete Commons description even for an already uploaded binary.'''
		text = build_commons_description(metadata, marc_meta, marc_id, file_id, part_label, category, decision['license'])
		if archive is not None:
			if marc_id not in archive.snapshots:
				archive.save_record(marc_id, record_meta, marc_meta)
			archive.save_scan(marc_id, file_id, text, f'File:{commons_filename}', rights=decision, part_label=part_label, scan_identity=identity)
		return text

	if not identity['eligible']:
		# Keep the historical binary association for repair, without generating a
		# new authoritative description from metadata known to be out of scope.
		base = {**previous, **base}
		return finish('metadata_review', pending=True, error=identity['reason'], previous_status=previous.get('previous_status', previous.get('status')))
	wikitext = preserve_description(record_meta)
	if hasattr(client, 'fetch_scan_manifest'):
		try:
			manifest = client.fetch_scan_manifest(marc_id, file_id)
			wikitext = preserve_description(with_viewer_metadata(record_meta, manifest))
		except (CatalogRetriesExhausted, ViewerAPIUnavailable, CatalogResourceMissing) as error:
			# A fresh metadata outage does not erase an earlier publication or its
			# binary identity. Keep it available for later verification and refresh.
			base = {**previous, **base}
			status = finish('metadata_failed', pending=True, error=str(error), attempts=getattr(error, 'attempts', 1),
				previous_status=previous.get('previous_status', previous.get('status')))
			if isinstance(error, ViewerAPIUnavailable):
				raise
			return status
	if metadata_only:
		if archive is not None:
			archive.record_result(marc_id, file_id, {**previous, **base} if previous else {**base, 'status': 'metadata_ready'})
		return 'metadata_ready'
	publication_status = previous.get('previous_status') if previous.get('status') in ('metadata_failed', 'metadata_review') else previous.get('status')
	if publication_status in TERMINAL_STATUSES and previous.get('verification'):
		state['items'][key] = {**previous, **base, 'status': publication_status}
		for field in ('error', 'error_type', 'attempts', 'previous_status'):
			state['items'][key].pop(field, None)
		if archive is not None:
			archive.record_result(marc_id, file_id, state['items'][key])
		save_state(state_path, state)
		return 'completed'
	if (not dry_run and publication_status == 'verification_failed'
		and re.fullmatch(r'[0-9a-fA-F]{40}', str(previous.get('sha1', '')))
		and type(previous.get('size_bytes')) is int and previous['size_bytes'] > 0
		and str(previous.get('commons_title', '')).startswith('File:')):
		# Publication succeeded or may have succeeded; recheck the saved identity
		# before any download or upload. This read does not grant publication rights;
		# missing or mismatched files remain pending without falling through to upload.
		base = {**{name: value for name, value in previous.items() if name not in ('error', 'attempts', 'error_type', 'previous_status')}, **base}
		try:
			info = uploader.file_info(previous['commons_title'])
			if info['sha1'].lower() != previous['sha1'].lower() or info['size_bytes'] != previous['size_bytes']:
				raise ValueError('Commons stored hash or size differs from the saved source identity')
		except Exception as error:
			return finish('verification_failed', pending=True, error=str(error))
		log_progress(f'{prefix}[{key}] Recovered Commons verification without downloading the PDF.')
		return finish('existing_title', commons_title=info.get('commons_title', previous['commons_title']), verification=info)

	if not decision['eligible']:
		log_progress(f'{prefix}[{key}] Deferring for rights review: {decision["reason"]}')
		return finish('rights_review', pending=True, error=decision['reason'])

	title_exists = False
	if not dry_run:
		log_progress(f'{prefix}[{key}] Checking title on Commons:\nFile:{commons_filename}')
		title_exists = uploader.title_exists(commons_filename)
	staging_dir.mkdir(parents=True, exist_ok=True)
	# Source identifiers never become path components; workers get distinct files.
	import tempfile
	with tempfile.NamedTemporaryFile(dir=staging_dir, prefix='nlb-', suffix='.pdf', delete=False) as temporary:
		temp_pdf = Path(temporary.name)
	retain_staging_pdf = False
	download_completed = False
	try:
		try:
			size_bytes = client.download_pdf_scan(marc_id, file_id, temp_pdf)
			download_completed = True
		except ViewerSessionExpired as error:
			finish('download_failed', pending=True, error=str(error), error_type=type(error).__name__)
			raise
		except DownloadRetriesExhausted as error:
			log_progress(f'{prefix}[{key}] Deferring download after {error.attempts} attempts: {error}')
			if getattr(error.last_error, 'rejected_pdf', None) is not None:
				base['rejected_pdf'] = error.last_error.rejected_pdf
			return finish('download_failed', pending=True, error=str(error), attempts=error.attempts, error_type=type(error.last_error).__name__)
		except requests.HTTPError as error:
			if error.response is None or error.response.status_code != 400:
				raise
			log_progress(f'{prefix}[{key}] Skipping download (HTTP 400): {viewer_url}\n{error}')
			return finish('download_rejected', pending=True, error=str(error), http_status=400)
		except (InvalidPDFError, ValueError) as error:
			if not isinstance(error, InvalidPDFError) and 'not a valid PDF' not in str(error):
				raise
			return finish('download_failed', pending=True, error=str(error), error_type=type(error).__name__)
		with temp_pdf.open('rb') as source:
			sha256 = hashlib.file_digest(source, 'sha256').hexdigest()
		base.update(sha256=sha256, size_bytes=size_bytes)
		validation = getattr(client, 'pdf_validation', {}).get((marc_id, file_id))
		if validation is not None:
			base['pdf_validation'] = validation
		if archive is not None:
			archive.preserve_pdf(marc_id, file_id, temp_pdf)
		log_progress(f'{prefix}[{key}] Checking SHA-1 hash on Commons: {viewer_url}')
		try:
			sha1, existing_titles = check_commons_by_hash(temp_pdf, session=client.session)
		except (requests.RequestException, ValueError) as error:
			if isinstance(error, requests.exceptions.SSLError):
				raise
			if isinstance(error, requests.HTTPError) and (error.response is None or error.response.status_code not in {*DOWNLOAD_TRANSIENT_STATUSES, 429}):
				raise
			return finish('verification_failed', pending=True, error=str(error))
		base['sha1'] = sha1
		if existing_titles:
			log_progress(f'{prefix}[{key}] Skipping (duplicate SHA-1 on Commons: {existing_titles}): {viewer_url}')
			status = 'existing_title' if f'File:{commons_filename}' in existing_titles else 'duplicate_sha1'
			return finish(status, duplicates=existing_titles, commons_title=existing_titles[0], verification={'method': 'Commons allimages SHA-1', 'sha1': sha1})
		if title_exists:
			return finish('title_conflict', pending=True, commons_title=f'File:{commons_filename}', error='Existing title has different bytes; file was not overwritten')
		if dry_run:
			log_progress(f'{prefix}[{key}] Dry-run ready ({size_bytes / (1024 * 1024):.2f} MiB, SHA-1 {sha1}): File:{commons_filename}')
			if archive is not None:
				archive.record_result(marc_id, file_id, {**base, 'status': 'dry_run_ready'})
			return 'dry_run_ready'

		log_progress(f'{prefix}[{key}] Uploading to Wikimedia Commons:\nFile:{commons_filename}')
		try:
			result = uploader.upload(temp_pdf, commons_filename, wikitext, f'From {ELIB_BASE_URL}/elib/Record/{marc_id} (fileID {file_id})')
		except UploadRetriesExhausted as error:
			return finish('upload_failed', pending=True, commons_title=f'File:{commons_filename}', error=str(error), attempts=error.attempts, error_type=type(error.last_error).__name__, error_code=getattr(error.last_error, 'code', None))
		title = result.get('commons_title', f'File:{commons_filename}')
		duplicates = [
			name if name.startswith('File:') else f'File:{name}'
			for name in result.get('duplicates') or []
		]
		if result['status'] == 'duplicate_sha1':
			if not duplicates:
				return finish('verification_failed', pending=True, error='Commons duplicate warning omitted the actual file title')
			title = duplicates[0]
		try:
			info = uploader.file_info(title)
			if info['sha1'].lower() != sha1.lower() or info['size_bytes'] != size_bytes:
				raise ValueError('Commons stored hash or size differs from the downloaded original')
		except Exception as error:
			# The upload may already exist; leave verification pending before another run.
			return finish('verification_failed', pending=True, commons_title=title, error=str(error))
		canonical_title = info.get('commons_title', title)
		duplicates = [canonical_title if name == title else name for name in duplicates]
		title = canonical_title
		log_progress(f'{prefix}[{key}] Uploaded and verified:\nhttps://commons.wikimedia.org/wiki/{quote(title.replace(" ", "_"))}\n')
		return finish(result['status'], commons_title=title, duplicates=duplicates, verification=info)
	except OSError as error:
		# Preserve completed originals after any local archive/state failure, not
		# only quarantine failures raised during the download itself.
		if isinstance(error, ArchiveWriteError) or (download_completed and not isinstance(error, requests.RequestException)):
			retain_staging_pdf = temp_pdf.exists()
			if retain_staging_pdf:
				log_progress(f'[{key}] Preservation write failed; staging original retained at {temp_pdf}')
				from preservation import atomic_json
				try:
					atomic_json(temp_pdf.with_suffix('.recovery.json'), {
						**base, 'proposed_commons_title': f'File:{commons_filename}',
						'source_url': viewer_url, 'pdf_path': str(temp_pdf.resolve()), 'error': str(error),
					})
				except OSError:
					# The staging filesystem may also be full; the source key and
					# exact retained path are still available in the progress log.
					pass
		raise
	finally:
		if not retain_staging_pdf:
			temp_pdf.unlink(missing_ok=True)


def with_viewer_metadata(record_meta: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
	'''Include useful IIIF descriptive fields in the readable Commons record.'''
	from viewer_outline import outline_entries

	result = copy.deepcopy(record_meta)
	fields = result.setdefault('fields', [])
	def plain(value: Any) -> str:
		'''Flatten language maps and structured values without interpreting wiki markup.'''
		if isinstance(value, str):
			if '<' not in value:
				return value
			soup = BeautifulSoup(value, 'html.parser')
			for link in soup.find_all('a', href=True):
				url = link['href']
				if urlsplit(url).scheme in ('http', 'https'):
					link.append(f' ({url})')
			return soup.get_text(' ', strip=True)
		if isinstance(value, list):
			return '; '.join(plain(item) for item in value)
		if isinstance(value, dict):
			return '; '.join(f'{key}: {plain(item)}' for key, item in value.items())
		return str(value)
	for item in manifest.get('metadata') or []:
		if isinstance(item, dict) and 'value' in item:
			fields.append({'name': f'IIIF — {plain(item.get("label", "Metadata"))}', 'values': [plain(item['value'])]})
	for key in ('label', 'description', 'summary', 'attribution', 'requiredStatement', 'rights', 'license', 'provider', 'seeAlso', 'homepage', 'partOf', 'within'):
		if manifest.get(key):
			fields.append({'name': f'IIIF — {key}', 'values': [plain(manifest[key])]})
	outline = outline_entries(manifest, plain)
	if outline:
		fields.append({'name': 'Структура документа по IIIF / Viewer outline', 'values': outline})
	return result


def iterate_and_upload(
	client: NlbClient,
	uploader: Any,
	state_path: Path,
	staging_dir: Path,
	until_year: int | None = DEFAULT_UNTIL_YEAR,
	from_year: int | None = None,
	search_url: str | None = None,
	category: str = DEFAULT_CATEGORY,
	license_template: str = DEFAULT_LICENSE,
	max_uploads: int = 0,
	dry_run: bool = False,
	explicit_license: bool = False,
	metadata_only: bool = False,
	refresh_metadata: bool = False,
	continue_run: bool = False,
) -> dict[str, int]:
	'''Scan from page one by default; explicit continuation resumes pending progress.'''
	if search_url:
		url_from, url_to = extract_year_bounds_from_search_url(search_url)
		if from_year is None:
			from_year = url_from
		if until_year is None:
			until_year = url_to

	state = load_state(
		state_path,
		until_year=until_year,
		from_year=from_year,
		search_url=search_url,
	)
	if dry_run or metadata_only:
		state['_read_only'] = True
	if not continue_run or metadata_only:
		state['current_page'] = 1
	page = max(1, int(state.get('current_page') or 1))
	if state.get('retry_from_page') is not None:
		page = max(1, min(page, int(state['retry_from_page'])))
		# A dry run cannot resolve pending uploads, even when downloads now succeed.
		if not dry_run:
			state.pop('retry_from_page')
		# Save the rewind before fetching: even an immediate interruption can resume.
		state['current_page'] = page
		save_state(state_path, state)
	counts: dict[str, int] = {
		'uploaded': 0,
		'download_rejected': 0,
		'download_failed': 0,
		'upload_failed': 0,
		'metadata_failed': 0,
		'metadata_review': 0,
		'search_failed': 0,
		'duplicate_sha1': 0,
		'existing_title': 0,
		'restricted': 0,
		'not_pdf': 0,
		'dry_run_ready': 0,
		'metadata_ready': 0,
		'rights_review': 0,
		'verification_failed': 0,
		'title_conflict': 0,
		'completed': 0,
	}
	uploaded_bytes = 0
	shutdown_requested = False
	start_time = time.monotonic()

	def clear_record_failure(marc_id: str) -> None:
		"""Remove resolved catalog diagnostics while keeping dry runs nonterminal."""
		if not dry_run and state.get('record_failures', {}).pop(marc_id, None) is not None:
			save_state(state_path, state)

	def _handle_sigint(signum: int, frame: Any) -> None:
		"""Request a graceful stop once; a second interrupt aborts a stalled operation."""
		nonlocal shutdown_requested
		if shutdown_requested:
			log_progress('\nSecond Ctrl-C received: aborting current item...')
			raise KeyboardInterrupt
		shutdown_requested = True
		log_progress('\nCtrl-C received: finishing current item before shutting down... Press Ctrl-C again to abort.')

	previous_sigint_handler = signal.signal(signal.SIGINT, _handle_sigint)
	try:
		while not shutdown_requested:
			archive = getattr(client, 'archive', None)
			if archive is not None:
				archive.current_record = None
			page_url = build_search_page_url(
				page=page,
				from_year=from_year,
				to_year=until_year,
				search_url=search_url,
			)
			log_progress(f'[page {page}] Fetching search results: {page_url}')
			state['current_page'] = page
			save_state(state_path, state)
			try:
				search_page = client.fetch_search_page(
					page=page,
					from_year=from_year,
					to_year=until_year,
					search_url=search_url,
				)
			except CatalogRetriesExhausted as error:
				log_progress(f'[page {page}] Deferring search until the next run: {error}')
				counts['search_failed'] += 1
				break
			records = search_page['records']
			if not records:
				break

			state['current_page'] = page
			save_state(state_path, state)

			total_records = int(search_page.get('total_results') or 0)
			total_pages = (total_records + SEARCH_PAGE_SIZE - 1) // SEARCH_PAGE_SIZE
			for record_index, search_entry in enumerate(records, 1):
				if shutdown_requested:
					break

				marc_id = search_entry['marc_id']
				position = (page - 1) * SEARCH_PAGE_SIZE + record_index
				record_counter = f'{position}/{total_records}' if total_records else str(position)
				page_counter = f'{page}/{total_pages}' if total_pages else str(page)
				progress_label = f'[record {record_counter}; page {page_counter}]'
				record_url = f'{ELIB_BASE_URL}/elib/Record/{marc_id}'
				direct_files = search_entry.get('file_ids') or []
				direct_mets = search_entry.get('mets_ids') or []
				state['current_record_id'] = marc_id
				save_state(state_path, state)
				if archive is not None:
					archive.begin_record(marc_id)
				try:
					log_progress(f'{progress_label} [{marc_id}] Fetching metadata: {record_url}')
					record_meta, marc_meta = client.fetch_record_metadata(marc_id)
					if archive is not None:
						archive.save_record(marc_id, record_meta, marc_meta)
					log_progress(f'{progress_label} [{marc_id}] Checking access: {record_url}')
					access = client.check_doc_access(marc_id)
					if not access.get('publicAccess'):
						log_progress(f'{progress_label} [{marc_id}] Skipping (restricted access): {record_url}')
						for fid in direct_files or ['0']:
							key = item_state_key(marc_id, fid)
							previous = state['items'].get(key, {})
							state['items'][key] = {
								**previous,
								'status': 'restricted',
								'previous_status': previous.get('previous_status', previous.get('status')),
								'marc_id': marc_id,
								'file_id': fid,
								'updated_at': datetime.now(timezone.utc).isoformat(),
							}
							if archive is not None:
								archive.record_result(marc_id, fid, state['items'][key])
						save_state(state_path, state)
						counts['restricted'] += 1
						clear_record_failure(marc_id)
						continue

					pub_year = resolve_publication_year(record_meta, marc_meta)
					if from_year is not None and pub_year is not None and pub_year < from_year:
						clear_record_failure(marc_id)
						log_progress(f'{progress_label} [{marc_id}] Skipping (year {pub_year} < {from_year}): {record_url}')
						continue
					if until_year is not None and pub_year is not None and pub_year > until_year:
						clear_record_failure(marc_id)
						log_progress(f'{progress_label} [{marc_id}] Skipping (year {pub_year} > {until_year}): {record_url}')
						continue

					mets_ids = [*direct_mets, *record_meta.get('mets_ids', [])]
					if mets_ids:
						log_progress(
							f'{progress_label} [{marc_id}] Resolving multi-part METS files: '
							f'{VIEWER_BASE_URL}/viewer/html?metsID={mets_ids[0]}&markID={marc_id}'
						)
					file_entries = client.resolve_record_files(search_entry, record_meta)
					if not file_entries:
						raise CatalogRetriesExhausted(InvalidCatalogResponse('Open digitized record has no downloadable file entries'), 1)
				except (CatalogRetriesExhausted, ViewerAPIUnavailable, CatalogResourceMissing) as error:
					last_error = getattr(error, 'last_error', error)
					last_response = getattr(last_error, 'response', None)
					state.setdefault('record_failures', {})[marc_id] = {
						'status': 'metadata_failed', 'attempts': getattr(error, 'attempts', 1),
						'error_type': type(last_error).__name__, 'error': str(error),
						'http_status': last_response.status_code if last_response is not None else None,
						'updated_at': datetime.now(timezone.utc).isoformat(),
					}
					state['retry_from_page'] = min(page, int(state.get('retry_from_page') or page))
					save_state(state_path, state)
					counts['metadata_failed'] += 1
					log_progress(f'{progress_label} [{marc_id}] Deferring record until the next run: {error}')
					if isinstance(error, ViewerAPIUnavailable):
						raise
					continue
				clear_record_failure(marc_id)
				for file_entry in file_entries:
					file_id = file_entry['file_id']
					status = process_single_scan(
						client=client,
						uploader=uploader,
						state=state,
						state_path=state_path,
						staging_dir=staging_dir,
						marc_id=marc_id,
						file_id=file_id,
						part_label=file_entry.get('part_label', ''),
						record_meta=record_meta,
						marc_meta=marc_meta,
						category=category,
						license_template=license_template,
						dry_run=dry_run,
						progress_label=progress_label,
						explicit_license=explicit_license,
						metadata_only=metadata_only,
						file_entry=file_entry,
					)
					counts[status] = counts.get(status, 0) + 1
					if status == 'uploaded':
						item_entry = state.get('items', {}).get(item_state_key(marc_id, file_id), {})
						uploaded_bytes += int(item_entry.get('size_bytes') or 0)

					if shutdown_requested:
						break

					if max_uploads > 0 and (counts['uploaded'] + counts['dry_run_ready'] + counts['metadata_ready']) >= max_uploads:
						log_progress(format_elapsed_time(time.monotonic() - start_time))
						return counts

				if shutdown_requested:
					break

			if shutdown_requested:
				break

			total_results = search_page.get('total_results') or 0
			if total_results and page * SEARCH_PAGE_SIZE >= total_results:
				break
			page += 1
			state['current_page'] = page
			save_state(state_path, state)
	finally:
		signal.signal(signal.SIGINT, previous_sigint_handler)

	if shutdown_requested:
		uploaded_mb = uploaded_bytes / (1024 * 1024)
		log_progress(f"Graceful shutdown complete. Uploaded: {counts['uploaded']} file(s), {uploaded_mb:.2f} MB")

	log_progress(format_elapsed_time(time.monotonic() - start_time))
	return counts


def refresh_uploaded_metadata(
	client: NlbClient, state_path: Path, staging_dir: Path, *,
	category: str = DEFAULT_CATEGORY, license_template: str = DEFAULT_LICENSE,
	explicit_license: bool = False, continue_run: bool = False,
) -> dict[str, int]:
	'''Refresh state-known Commons associations without search, PDF transfer or state writes.

	Every selected file receives a fresh result in this run's archive snapshot.
	Missing or failed source reads cannot leave an older description eligible for
	automatic publication. Historical title/hash evidence remains available.
	Progress describes local source collection, before any Commons edit phase.
	'''
	state = json.loads(state_path.read_text(encoding='utf-8'))
	archive = client.archive
	counts = dict.fromkeys(('metadata_ready', 'metadata_review', 'metadata_failed', 'source_missing', 'source_changed', 'restricted', 'skipped'), 0)
	groups: dict[str, dict[str, dict]] = {}
	published = {'uploaded', 'existing_title', 'duplicate_sha1', 'verification_failed'}
	for key, item in state.get('items', {}).items():
		titles = [item.get('commons_title'), *item.get('duplicates', [])]
		if (item.get('status') not in published and item.get('previous_status') not in published
			or not re.fullmatch(r'[0-9a-fA-F]{40}', str(item.get('sha1', '')))
			or type(item.get('size_bytes')) is not int or item['size_bytes'] <= 0
			or not any(isinstance(title, str) and title.startswith('File:') for title in titles)):
			counts['skipped'] += 1
			continue
		marc_id, file_id = key.split(':', 1)
		groups.setdefault(marc_id, {})[file_id] = item
	from refresh_checkpoint import RefreshCheckpoint
	checkpoint = RefreshCheckpoint(archive, groups, {
		'category': category, 'license_template': license_template, 'explicit_license': explicit_license,
	}, resume=continue_run, log=log_progress)
	client.refresh_checkpoint = checkpoint

	def checkpoint_record(marc_id: str, before: dict[str, int]) -> None:
		'''Record one completed source boundary, including its deferred scan counts.'''
		checkpoint.save_record(marc_id, {key: value - before[key] for key, value in counts.items()})

	def failure(marc_id: str, file_id: str, previous: dict, status: str, reason: str, **details: Any) -> None:
		'''Retain historical binary identity while explicitly disqualifying a failed refresh.'''
		counts[status] += 1
		archive.record_result(marc_id, file_id, {
			**previous, 'status': status, 'previous_status': previous.get('previous_status', previous.get('status')),
			'error': reason, 'scan_identity': {'eligible': False, 'reason': reason},
			'metadata_refresh': {'status': status, 'checked_at': datetime.now(timezone.utc).isoformat(), **details},
		})
		log_progress(f'[{marc_id}:{file_id}] Metadata refresh deferred ({status}): {reason}')

	log_progress(f'Collecting source metadata for {len(groups)} records into the local archive.')
	for position, (marc_id, items) in enumerate(groups.items(), 1):
		saved = checkpoint.restore(marc_id)
		if saved is not None:
			for status, count in saved.items():
				counts[status] += count
			log_progress(f'[metadata record {position}/{len(groups)}] Reusing verified source collection: {marc_id}')
			continue
		before = counts.copy()
		log_progress(f'[metadata record {position}/{len(groups)}] Collecting source metadata: {marc_id}')
		archive.begin_record(marc_id)
		try:
			record_meta, marc_meta = client.fetch_record_metadata(marc_id)
			archive.save_record(marc_id, record_meta, marc_meta)
			if not client.check_doc_access(marc_id).get('publicAccess'):
				for fid, previous in items.items():
					failure(marc_id, fid, previous, 'restricted', 'Source record is no longer publicly accessible')
				checkpoint_record(marc_id, before)
				continue
			files = {item['file_id']: item for item in client.resolve_record_files({}, record_meta)}
		except (CatalogRetriesExhausted, ViewerAPIUnavailable) as error:
			for fid, previous in items.items():
				failure(marc_id, fid, previous, 'metadata_failed', str(error))
			checkpoint_record(marc_id, before)
			if isinstance(error, ViewerAPIUnavailable):
				raise
			continue
		except requests.HTTPError as error:
			status = 'source_missing' if isinstance(error, CatalogRecordMissing) else 'metadata_failed'
			for fid, previous in items.items():
				failure(marc_id, fid, previous, status, str(error))
			checkpoint_record(marc_id, before)
			continue
		file_items = list(items.items())
		for file_index, (fid, previous) in enumerate(file_items):
			if fid not in files:
				failure(marc_id, fid, previous, 'source_missing', 'Previously uploaded file ID is absent from the current record', current_file_ids=list(files))
				continue
			entry = files[fid]
			old_location = previous.get('scan_identity', {}).get('context', {}).get('file', {}).get('file_location')
			if old_location and entry.get('file_location') and old_location != entry['file_location']:
				failure(marc_id, fid, previous, 'source_changed', f'Source path changed from {old_location} to {entry["file_location"]}')
				continue
			isolated = default_state()
			isolated['items'][item_state_key(marc_id, fid)] = previous
			try:
				status = process_single_scan(client, None, isolated, state_path, staging_dir,
					marc_id, fid, entry.get('part_label', ''), record_meta, marc_meta,
					category, license_template, metadata_only=True, explicit_license=explicit_license, file_entry=entry)
			except ViewerAPIUnavailable as error:
				# Finish the record boundary without letting unvisited scans appear ready.
				for pending_id, pending_item in file_items[file_index:]:
					failure(marc_id, pending_id, pending_item, 'metadata_failed', str(error))
				checkpoint_record(marc_id, before)
				raise
			except requests.HTTPError as error:
				failure(marc_id, fid, previous, 'metadata_failed', str(error))
				continue
			if status != 'metadata_ready':
				result = archive.manifests[marc_id]['files'][fid]
				failure(marc_id, fid, previous, status, result.get('error', 'Incomplete metadata refresh'))
				continue
			counts[status] += 1
			archive.record_result(marc_id, fid, {'metadata_refresh': {'status': status, 'checked_at': datetime.now(timezone.utc).isoformat()}})
		checkpoint_record(marc_id, before)
	deferred = sum(count for status, count in counts.items() if status not in ('metadata_ready', 'skipped'))
	log_progress(f'Source metadata collection complete: {counts["metadata_ready"]} scan associations ready, {deferred} deferred.')
	return counts


def build_arg_parser() -> argparse.ArgumentParser:
	'''Create the command-line argument parser.'''
	parser = argparse.ArgumentParser(
		description='Iterate over open-access scans on elib.nlb.by and upload them to Wikimedia Commons.'
	)
	parser.add_argument(
		'url',
		nargs='?',
		default=None,
		help='Optional elib.nlb.by Search/Results or Record URL to process.',
	)
	parser.add_argument(
		'--url',
		dest='search_url',
		default=None,
		help='Optional elib.nlb.by Search/Results or Record URL to process.',
	)
	parser.add_argument(
		'--from-year',
		type=int,
		default=None,
		help='Upload works published from this year onwards (inclusive).',
	)
	parser.add_argument(
		'--to-year',
		'--until-year',
		dest='until_year',
		type=int,
		default=None,
		help='Upload works published up to this year (default: 1900, or taken from URL if provided).',
	)
	parser.add_argument(
		'--category',
		default=DEFAULT_CATEGORY,
		help='Single Wikimedia Commons category without brackets (default: elib.nlb.by).',
	)
	parser.add_argument(
		'--license',
		default=None,
		help='Explicitly attest a Commons license template without braces; omitted selects a license from catalog evidence.',
	)
	parser.add_argument(
		'--state-file',
		type=Path,
		default=Path(__file__).resolve().parent / 'state.json',
		help='Path to the resumable JSON state file.',
	)
	parser.add_argument(
		'--staging-dir',
		type=Path,
		default=Path('/tmp/nlb-commons-staging'),
		help='Temporary directory for downloaded PDFs prior to removal.',
	)
	parser.add_argument(
		'--archive-dir', type=Path, default=None,
		help='Permanent source metadata archive (default: archive/ beside the state file).',
	)
	parser.add_argument('--keep-pdfs', action='store_true', help='Also retain original PDFs in the archive after verification.')
	parser.add_argument('--metadata-only', action='store_true', help='Revisit all selected records and archive Commons metadata without uploading or changing upload progress.')
	parser.add_argument('--refresh-metadata', action='store_true', help='Archive updated metadata for completed files during the upload run; starts at page 1 unless --continue is given.')
	parser.add_argument('--refresh-uploaded-metadata', action='store_true', help='Refresh saved Commons associations and automatically update their bibliographic metadata; no PDF uploads. Combine with --dry-run for an optional preview.')
	parser.add_argument('--continue', dest='continue_run', action='store_true', help='Resume saved catalog progress or an interrupted metadata collection; otherwise start from the beginning while retaining upload history.')
	parser.add_argument('--report', type=Path, help='Write a machine-readable run summary to this JSON file.')
	parser.add_argument(
		'--viewer-cookies',
		type=Path,
		default=None,
		help='Netscape cookie file exported from a signed-in NLB viewer session.',
	)
	parser.add_argument(
		'--pywikibot-dir',
		default=DEFAULT_PYWIKIBOT_DIR,
		help='Directory containing user-config.py and Pywikibot session files.',
	)
	parser.add_argument(
		'--max-uploads',
		type=int,
		default=0,
		help='Stop after uploading this many files (0 = unlimited).',
	)
	parser.add_argument(
		'--dry-run',
		action='store_true',
		help='Preview without Commons writes; ordinary upload previews download/hash-check PDFs, metadata refresh previews do not.',
	)
	return parser


def main(argv: list[str] | None = None) -> int:
	'''Entry point for CLI execution.'''
	parser = build_arg_parser()
	args = parser.parse_args(argv)
	if args.report and args.report.resolve() == args.state_file.resolve():
		parser.error('--report must not overwrite the upload state file')
	if args.continue_run and args.metadata_only:
		parser.error('--metadata-only does not save catalog progress; use it without --continue')
	if args.refresh_uploaded_metadata:
		if args.metadata_only or args.refresh_metadata or args.url or args.search_url or args.from_year is not None or args.until_year is not None or args.max_uploads:
			parser.error('--refresh-uploaded-metadata uses saved associations; search filters, upload limits and other refresh modes do not apply')

	target_url = args.search_url or args.url
	until_year = args.until_year
	if until_year is None:
		until_year = extract_year_bounds_from_search_url(target_url)[1]
		if until_year is None:
			until_year = DEFAULT_UNTIL_YEAR

	archive = ArchiveStore(args.archive_dir or args.state_file.parent / 'archive', keep_pdfs=args.keep_pdfs)
	if args.refresh_uploaded_metadata and args.state_file.resolve() in {
		(archive.root / 'metadata-refresh' / name).resolve() for name in ('collection.json', 'archive-issues.json')
	}:
		parser.error('Metadata collection and archive diagnostics must not overwrite the upload state file')
	if args.report:
		output = args.report.resolve()
		protected = [archive.root / name for name in ('objects', 'records', 'quarantine', 'metadata-refresh/pages')]
		if any(output.is_relative_to(path.resolve()) for path in protected) or (
			output.parent == (archive.root / 'metadata-refresh').resolve() and output.name.startswith('collection')
		):
			parser.error('--report must not overwrite archived sources, receipts or collection checkpoints')
	if args.refresh_uploaded_metadata and (args.report or archive.root / 'metadata-refresh' / 'report.json').resolve() == args.state_file.resolve():
		parser.error('The metadata refresh report must not overwrite the upload state file')
	client = NlbClient(viewer_cookies=args.viewer_cookies, archive=archive,
		metadata_reads_only=args.metadata_only or args.refresh_uploaded_metadata)
	client.archive = archive
	uploader = None if args.dry_run or args.metadata_only or args.refresh_uploaded_metadata else CommonsUploader(pywikibot_dir=args.pywikibot_dir)

	try:
		if args.refresh_uploaded_metadata:
			from metadata_refresh import apply_uploaded_metadata, inspect_refresh_archive
			issues = inspect_refresh_archive(archive.root)
			if issues:
				from preservation import atomic_json
				atomic_json(archive.root / 'metadata-refresh' / 'archive-issues.json', issues)
				log_progress(f'Archive preflight found {len(issues)} issues; affected Commons pages will be deferred. See archive-issues.json.')
			counts = refresh_uploaded_metadata(client, args.state_file, args.staging_dir,
				category=args.category, license_template=args.license or DEFAULT_LICENSE, explicit_license=args.license is not None,
				continue_run=args.continue_run)
			report = apply_uploaded_metadata(archive.root, archive.snapshots.values(), dry_run=args.dry_run,
				report_path=args.report, pywikibot_dir=args.pywikibot_dir)
			counts.update(report['counts'])
			from preservation import atomic_json
			atomic_json(args.report or archive.root / 'metadata-refresh' / 'report.json', {**report, 'counts': counts})
			# Preview runs keep their verified collection available for the real apply.
			# An interrupted/failed edit also keeps it for a bounded retry next run.
			if not args.dry_run and not counts.get('metadata_edit_failed') and hasattr(client, 'refresh_checkpoint'):
				client.refresh_checkpoint.complete()
			print(json.dumps(counts, ensure_ascii=False))
			return 2 if any(counts.get(key) for key in ('metadata_review', 'metadata_failed', 'source_missing',
				'source_changed', 'restricted', 'metadata_conflict', 'metadata_edit_failed', 'metadata_source_skipped')) else 0
		counts = iterate_and_upload(
			client=client,
			uploader=uploader,
			state_path=args.state_file,
			staging_dir=args.staging_dir,
			until_year=until_year,
			from_year=args.from_year,
			search_url=target_url,
			category=args.category,
			license_template=args.license or DEFAULT_LICENSE,
			max_uploads=args.max_uploads,
			dry_run=args.dry_run,
			explicit_license=args.license is not None,
			metadata_only=args.metadata_only,
			refresh_metadata=args.refresh_metadata,
			continue_run=args.continue_run,
		)
		print(json.dumps(counts, ensure_ascii=False))
		if args.report:
			from preservation import atomic_json
			atomic_json(args.report, {'counts': counts, 'finished_at': datetime.now(timezone.utc).isoformat()})
		return 2 if any(counts.get(key) for key in (
			'search_failed', 'metadata_failed', 'download_failed', 'download_rejected',
			'upload_failed', 'verification_failed', 'title_conflict', 'rights_review', 'metadata_review',
		)) else 0
	except ViewerAPIUnavailable as error:
		print(f'Stopped: {error}')
		if args.refresh_uploaded_metadata:
			print('No Commons edits were attempted in this refresh. Source collection remains incomplete; '
				'after the library API recovers, rerun with --refresh-uploaded-metadata --continue.')
		return 2
	except ViewerSessionExpired as error:
		print(f'Stopped: {error}')
		return 2
	except KeyboardInterrupt:
		print('Interrupted by user; state saved for resumption.')
		return 130


if __name__ == '__main__':
	raise SystemExit(main())
