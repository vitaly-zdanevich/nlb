#!/usr/bin/env python3
"""Iterate over open-access pre-1917 scans from elib.nlb.by and upload to Wikimedia Commons.

This script discovers digitized documents in the Electronic Library of the National
Library of Belarus (https://elib.nlb.by), resolves single-file records and multi-part
METS structures, fetches full HTML and MARCXML metadata, downloads each PDF scan to a
temporary file, checks Wikimedia Commons by SHA-1 hash before uploading, uploads new
files using Pywikibot credentials from /home/vitaly/.pywikibot, deletes the local scan
immediately afterwards, and persists atomic state to resume cleanly after interrupts.
"""

import argparse
import ast
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
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
import xml.etree.ElementTree as ET
import warnings

from bs4 import BeautifulSoup, Comment
import requests


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
HASH_CHECK_MAX_ATTEMPTS = 5
HASH_CHECK_RETRY_DELAY = 5

ILLEGAL_COMMONS_TITLE_CHARS_RE = re.compile(r'[\x00-\x1f\x7f#<>\[\]|{}]')
RECORD_ID_RE = re.compile(r'/elib/(?:Record|Collection)/([^/?#"\'<>\s]+)')
YEAR_RE = re.compile(r'(?<!\d)(1\d{3}|20\d{2})(?!\d)')

TERMINAL_STATUSES = frozenset({
	'uploaded',
	'duplicate_sha1',
	'existing_title',
	'restricted',
	'not_pdf',
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
	"""Return a fresh resumable state dictionary."""
	return {
		'version': 1,
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
	"""Load the persistent state file or initialize a new state structure."""
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
	return state


def save_state(path: Path, state: dict[str, Any]) -> None:
	"""Atomically write the state file so interrupts never corrupt progress."""
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


def extract_files_from_mets_structure(structure: dict[str, Any]) -> list[dict[str, str]]:
	"""Flatten a METS structure tree into ordered downloadable file descriptors."""
	files: list[dict[str, str]] = []
	seen_file_ids: set[str] = set()

	def walk(nodes: list[dict[str, Any]], parent_labels: list[str]) -> None:
		for node in nodes:
			if not isinstance(node, dict):
				continue
			data = node.get('data') or {}
			if not data.get('visible', True):
				continue
			link_name = (data.get('linkName') or '').strip()
			description = (data.get('description') or '').strip()
			label_parts = [part for part in (link_name, description) if part]
			current_label = ' — '.join(label_parts)
			path_labels = [*parent_labels, current_label] if current_label else list(parent_labels)

			node_type = data.get('nodeType')
			if node_type == 'FILE':
				if data.get('elibLocal'):
					continue
				file_info = data.get('file') or {}
				raw_file_id = file_info.get('id')
				if raw_file_id is not None:
					file_id = str(raw_file_id).strip()
					if file_id and file_id not in seen_file_ids:
						seen_file_ids.add(file_id)
						files.append({
							'file_id': file_id,
							'part_label': ' / '.join(path_labels),
							'file_location': str(file_info.get('fileLocation') or ''),
						})
			children = node.get('children') or []
			if isinstance(children, list) and children:
				walk(children, path_labels)

	top_nodes = structure.get('nodes') or []
	if isinstance(top_nodes, list):
		walk(top_nodes, [])
	return files


def extract_cell_values(cell: Any) -> list[str]:
	"""Extract individual line values from an HTML table cell, preserving <br> and block breaks."""
	fragment = BeautifulSoup(str(cell), 'html.parser')
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


def parse_record_html(html: str, marc_id: str) -> dict[str, Any]:
	"""Extract title, metadata table rows, and viewer links from a VuFind record page."""
	soup = BeautifulSoup(html, 'html.parser')
	title_node = soup.select_one('h1, h3[property="name"], h2[property="name"]')
	title = title_node.get_text(' ', strip=True) if title_node else ''

	fields: list[dict[str, Any]] = []
	for row in soup.select('table tr'):
		header = row.find('th')
		cell = row.find('td')
		if not header or not cell:
			continue
		key = re.sub(r'\s+', ' ', header.get_text(' ', strip=True)).rstrip(':').strip()
		values = extract_cell_values(cell)
		if not key or not values:
			continue
		if key.startswith('Всего') or key == 'Шифр':
			continue
		fields.append({
			'name': key,
			'value': '\n'.join(values),
			'values': values,
		})

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


def parse_marcxml(xml_text: str) -> dict[str, Any]:
	"""Parse UNIMARC/BELMARC metadata, retaining contributor roles and copy associations.

	Explicit $4 roles determine authorship. Without a role, only primary/equal
	responsibility fields (700/701/710/711) retain the author fallback.
	"""
	result: dict[str, Any] = {
		'control_number': '',
		'title': '',
		'subtitle': '',
		'responsibility': '',
		'responsibilities': [],
		'edition': '',
		'places': [],
		'publishers': [],
		'date': '',
		'year': '',
		'physical_description': '',
		'physical_descriptions': [],
		'series': [],
		'notes': [],
		'contents': [],
		'subjects': [],
		'genres': [],
		'geo_places': [],
		'alternate_genres': [],
		'alternate_geo_places': [],
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
	}
	if not xml_text.strip():
		return result

	try:
		root = ET.fromstring(xml_text)
	except ET.ParseError:
		return result

	record = root.find('.//marc:record', MARC_NS)
	if record is None:
		record = root.find('.//record')
	if record is None:
		return result

	for ctrl in record.findall('marc:controlfield', MARC_NS) + record.findall('controlfield'):
		tag = (ctrl.attrib.get('tag') or '').strip()
		val = (ctrl.text or '').strip()
		if tag == '001' and val:
			result['control_number'] = val
		if tag and val:
			result['raw_fields'].append({'tag': tag, 'value': val})

	parent_title = ''
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

		if tag == '100':
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
			for place in submap.get('a', []):
				if place not in result['places']:
					result['places'].append(place)
			for pub in submap.get('c', []):
				if pub not in result['publishers']:
					result['publishers'].append(pub)
			if submap.get('d') and not result['date']:
				result['date'] = ', '.join(submap['d'])
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
			if tag == '461' and submap.get('a') and not parent_title:
				parent_title = ' ; '.join(submap['a'])
			series_parts = submap.get('a', []) + submap.get('v', [])
			if series_parts:
				entry = ' ; '.join(series_parts)
				if entry not in result['series']:
					result['series'].append(entry)
		elif tag == '961':
			if submap.get('a') and not parent_title:
				parent_title = ' ; '.join(submap['a'])
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
		elif tag in {'600', '605', '606', '607', '690'}:
			subj_parts = (
				submap.get('a', [])
				+ submap.get('b', [])
				+ submap.get('d', [])
				+ submap.get('f', [])
				+ submap.get('x', [])
				+ submap.get('y', [])
				+ submap.get('z', [])
			)
			if subj_parts:
				subj = ' — '.join(subj_parts)
				if subj not in result['subjects']:
					result['subjects'].append(subj)
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
			surname = ' '.join(submap.get('a', []))
			given = ' '.join(submap.get('g', []) or submap.get('b', []))
			dates = ' '.join(submap.get('f', []))
			name = f'{surname}, {given}'.strip(', ') if given else surname
			if not name:
				continue
			if dates:
				name = f'{name} ({dates})'
			roles = list(dict.fromkeys(submap.get('4', [])))
			contributor = {
				'name': name,
				'tag': tag,
				'roles': roles,
				'copies': list(dict.fromkeys(submap.get('5', []))),
			}
			if contributor not in result['contributors']:
				result['contributors'].append(contributor)
			is_author = '070' in roles or (not roles and tag in {'700', '701', '710', '711'})
			if is_author and name not in result['authors']:
				result['authors'].append(name)

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
	"""Determine the earliest four-digit publication year from MARCXML or HTML fields."""
	candidates: list[str] = []
	if marc_meta.get('year'):
		candidates.append(str(marc_meta['year']))
	if marc_meta.get('date'):
		candidates.append(str(marc_meta['date']))
	for field in record_meta.get('fields', []):
		if field.get('name') in {'Опубликовано', 'Дата издания'}:
			candidates.append(str(field.get('value', '')))
	if record_meta.get('title'):
		candidates.append(str(record_meta['title']))

	for text in candidates:
		match = YEAR_RE.search(text)
		if match:
			return int(match.group(1))
	return None


def sanitize_commons_filename(filename: str) -> str:
	"""Sanitize a filename so it conforms to Wikimedia Commons naming rules."""
	sanitized = filename.replace('[', '(').replace(']', ')')
	sanitized = ILLEGAL_COMMONS_TITLE_CHARS_RE.sub('_', sanitized)
	sanitized = sanitized.replace("'", 'Ꞌ')
	sanitized = sanitized.replace('/', '-')
	sanitized = sanitized.replace('\\', '-')
	sanitized = sanitized.replace(':', ' -')
	sanitized = re.sub(r'[_\s]+([.,])', r'\1', sanitized)
	sanitized = re.sub(r'\s+', ' ', sanitized).strip()
	return sanitized


def build_commons_filename(
	record_meta: dict[str, Any],
	marc_meta: dict[str, Any],
	marc_id: str,
	file_id: str,
	part_label: str = '',
) -> str:
	"""Construct a unique, descriptive PDF filename within Commons' 240-byte UTF-8 limit."""
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
	clean_suffix = sanitize_commons_filename(suffix)

	max_base_bytes = 230 - len(clean_suffix.encode('utf-8'))
	while len(clean_base.encode('utf-8')) > max_base_bytes and len(clean_base) > 10:
		clean_base = clean_base[:-1].rstrip(' .,-_')

	return sanitize_commons_filename(f'{clean_base}{clean_suffix}')


def escape_wikitext(value: str) -> str:
	"""Escape pipe characters so they do not break MediaWiki template parameters."""
	return value.replace('|', '{{!}}')


def format_metadata_field(label: str, values: list[str]) -> str:
	"""Format a metadata field as a single line or nested multi-line bullet list."""
	cleaned = [val.strip() for val in values if val and val.strip()]
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


def build_commons_description(
	record_meta: dict[str, Any],
	marc_meta: dict[str, Any],
	marc_id: str,
	file_id: str,
	part_label: str = '',
	category: str = DEFAULT_CATEGORY,
	license_template: str = DEFAULT_LICENSE,
) -> str:
	"""Build Commons wikitext with subtitles and catalog-attributed contributor roles.

	Catalog copy references are preserved, but are not matched to the scanned copy.
	"""
	title = (record_meta.get('title') or marc_meta.get('title') or marc_id).strip()
	subtitle = (marc_meta.get('subtitle') or '').strip()
	if subtitle and subtitle.casefold() not in title.casefold():
		title = f'{title}: {subtitle}'
	if part_label:
		title = f'{title} ({part_label})'

	metadata_lines: list[str] = [f"'''{escape_wikitext(title)}'''"]
	if marc_meta.get('responsibility'):
		metadata_lines.append(format_metadata_field('Ответственность / Responsibility', [marc_meta['responsibility']]))
	if marc_meta.get('contributors'):
		metadata_lines.append(format_metadata_field(
			'Участники и роли по каталогу НББ (MARC 7xx$4)',
			[format_contributor(contributor) for contributor in marc_meta['contributors']],
		))
		if any('390' in contributor['roles'] for contributor in marc_meta['contributors']):
			metadata_lines.append(
				"''Сведения о прежних владельцах приведены по каталогу НББ; "
				"их связь с отсканированным экземпляром не установлена.''"
			)
	if part_label:
		metadata_lines.append(format_metadata_field('Часть / Выпуск / Part', [part_label]))

	seen_pairs: set[tuple[str, tuple[str, ...]]] = set()
	for field in record_meta.get('fields', []):
		name = field.get('name', '').strip()
		raw_values = field.get('values')
		if isinstance(raw_values, list):
			values = [str(v).strip() for v in raw_values if str(v).strip()]
		else:
			values = [v.strip() for v in str(field.get('value', '')).splitlines() if v.strip()]
		if not name or not values or name == 'Е-документ':
			continue
		pair = (name, tuple(values))
		if pair in seen_pairs:
			continue
		seen_pairs.add(pair)
		metadata_lines.append(format_metadata_field(name, values))

	if marc_meta.get('places'):
		metadata_lines.append(format_metadata_field('Место издания (MARC 210$a)', marc_meta['places']))
	if marc_meta.get('geo_places'):
		places_normalized = {p.strip().lower() for p in marc_meta.get('places', [])}
		new_geo = [g for g in marc_meta['geo_places'] if g.strip().lower() not in places_normalized]
		if new_geo:
			metadata_lines.append(format_metadata_field('Место издания (MARC 620)', new_geo))
	if marc_meta.get('alternate_geo_places'):
		metadata_lines.append(format_metadata_field('Варианты географических названий (MARC 926)', marc_meta['alternate_geo_places']))
	if marc_meta.get('publishers'):
		metadata_lines.append(format_metadata_field('Издатель (MARC 210$c)', marc_meta['publishers']))
	if marc_meta.get('edition'):
		metadata_lines.append(format_metadata_field('Издание (MARC 205)', [marc_meta['edition']]))
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
	contents = marc_meta.get('contents') or record_meta.get('contents') or []
	if contents:
		metadata_lines.append(format_metadata_field('Содержание (MARC 327)', contents))
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

	year = resolve_publication_year(record_meta, marc_meta)
	date_value = str(year) if year else escape_wikitext(marc_meta.get('date') or '')

	record_url = f'{ELIB_BASE_URL}/elib/Record/{marc_id}'
	marcxml_url = f'{ELIB_BASE_URL}/elib/Record/{marc_id}/Export?style=MARCXML'
	viewer_url = f'{VIEWER_BASE_URL}/viewer?markID={marc_id}&fileID={file_id}'
	source_value = (
		f'[{record_url} Электронная библиотека Национальной библиотеки Беларуси ({marc_id})]; '
		f'[{viewer_url} цифровой скан (fileID {file_id})]; '
		f'[{marcxml_url} MARCXML]'
	)

	normalized_category = category.strip()
	return (
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
	"""Hash the file once and query Commons directly, retrying transient failures.

	Timeouts, broken connections, invalid JSON and HTTP 500/502/503/504 receive
	bounded retries. An inconclusive lookup raises instead of allowing an upload.
	"""
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
				},
				headers={'User-Agent': COMMONS_USER_AGENT},
				timeout=30,
			) as response:
				response.raise_for_status()
				return digest, parse_commons_allimages_response(response.json(), digest)
		except (
			requests.exceptions.Timeout,
			requests.exceptions.ConnectionError,
			requests.exceptions.ChunkedEncodingError,
			requests.exceptions.JSONDecodeError,
			requests.exceptions.HTTPError,
		) as error:
			if isinstance(error, requests.exceptions.SSLError):
				raise
			if isinstance(error, requests.exceptions.HTTPError) and (
				error.response is None or error.response.status_code not in {500, 502, 503, 504}
			):
				raise
			if attempt >= HASH_CHECK_MAX_ATTEMPTS:
				raise
			delay = HASH_CHECK_RETRY_DELAY * 2 ** (attempt - 1)
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


class NlbClient:
	"""HTTP client for browsing elib.nlb.by metadata and downloading IIIF PDF scans."""

	def __init__(
		self,
		session: requests.Session | None = None,
		timeout: int = 60,
		viewer_cookies: Path | None = None,
	) -> None:
		"""Initialize the client, optionally reusing an exported signed-in viewer session."""
		self.session = session or requests.Session()
		self.session.headers.setdefault('User-Agent', DEFAULT_USER_AGENT)
		self.timeout = timeout
		self._viewer_session_initialized = False
		if viewer_cookies is not None:
			self.session.cookies.update(load_viewer_cookies(viewer_cookies))
			self._viewer_session_initialized = True

	def fetch_search_page(
		self,
		page: int,
		from_year: int | None = None,
		to_year: int | None = DEFAULT_UNTIL_YEAR,
		search_url: str | None = None,
	) -> dict[str, Any]:
		"""Fetch and parse a single page of digitized search results."""
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
		response = self.session.get(url, timeout=self.timeout)
		response.raise_for_status()
		return parse_search_results_html(response.text)

	def fetch_record_metadata(self, marc_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
		"""Fetch both the HTML record table and MARCXML export for a given marc_id."""
		record_url = f'{ELIB_BASE_URL}/elib/Record/{marc_id}'
		record_resp = self.session.get(record_url, timeout=self.timeout)
		record_resp.raise_for_status()
		record_meta = parse_record_html(record_resp.text, marc_id)

		marcxml_url = f'{ELIB_BASE_URL}/elib/Record/{marc_id}/Export?style=MARCXML'
		marc_resp = self.session.get(marcxml_url, timeout=self.timeout)
		marc_meta = parse_marcxml(marc_resp.text) if marc_resp.status_code == 200 else parse_marcxml('')
		if not marc_meta.get('contents'):
			toc_url = f'{ELIB_BASE_URL}/elib/Record/{marc_id}/TOC'
			try:
				toc_resp = self.session.get(toc_url, timeout=self.timeout)
				if toc_resp.status_code == 200:
					toc_soup = BeautifulSoup(toc_resp.text, 'html.parser')
					for li in toc_soup.select('.tab-pane.toc-tab li, ul.toc li'):
						txt = re.sub(r'\s+', ' ', li.get_text(' ', strip=True)).strip()
						if txt:
							for entry in extract_contents_entries(txt):
								if entry not in record_meta.setdefault('contents', []):
									record_meta['contents'].append(entry)
			except Exception:
				pass
		return record_meta, marc_meta

	def ensure_viewer_session(self, force: bool = False) -> None:
		"""Initialize the elib_session_id cookie on port 8070 if not already set."""
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

	def fetch_mets_files(self, mets_id: str) -> list[dict[str, str]]:
		"""Fetch the METS structure for a multi-part record and return its file entries."""
		self.ensure_viewer_session()
		resp = self.session.get(
			f'{VIEWER_BASE_URL}/mets//structure/state?id={mets_id}',
			headers={'Authorization': VIEWER_BASIC_AUTH},
			timeout=self.timeout,
		)
		resp.raise_for_status()
		return extract_files_from_mets_structure(resp.json())

	def check_doc_access(self, marc_id: str) -> dict[str, Any]:
		"""Query /iiif/access to determine whether a record is open-access."""
		self.ensure_viewer_session()
		resp = self.session.get(
			f'{VIEWER_BASE_URL}/iiif/access',
			params={'marcID': marc_id},
			headers={'Authorization': VIEWER_BASIC_AUTH},
			timeout=self.timeout,
		)
		resp.raise_for_status()
		return resp.json()

	def resolve_record_files(
		self,
		search_entry: dict[str, Any],
		record_meta: dict[str, Any],
	) -> list[dict[str, str]]:
		"""Return ordered [{'file_id': ..., 'part_label': ...}] items for a record."""
		files: list[dict[str, str]] = []
		seen: set[str] = set()

		for file_id in [*search_entry.get('file_ids', []), *record_meta.get('file_ids', [])]:
			if file_id not in seen:
				seen.add(file_id)
				files.append({'file_id': file_id, 'part_label': ''})

		for mets_id in [*search_entry.get('mets_ids', []), *record_meta.get('mets_ids', [])]:
			for item in self.fetch_mets_files(mets_id):
				fid = item['file_id']
				if fid not in seen:
					seen.add(fid)
					files.append({
						'file_id': fid,
						'part_label': item.get('part_label', ''),
					})

		return files

	def download_pdf_scan(self, marc_id: str, file_id: str, target_path: Path) -> int:
		"""Retry transient download failures with backoff, discarding partial PDFs.

		The viewer session POST runs once. Each GET/stream attempt starts from
		byte zero; exhausted retries propagate so the scan remains resumable.
		"""
		self.ensure_viewer_session()
		attempt = 1
		while True:
			try:
				return self._download_pdf_scan_once(marc_id, file_id, target_path)
			except (
				requests.exceptions.Timeout,
				requests.exceptions.ConnectionError,
				requests.exceptions.ChunkedEncodingError,
			) as error:
				if isinstance(error, requests.exceptions.SSLError):
					raise
				target_path.unlink(missing_ok=True)
				if attempt >= DOWNLOAD_MAX_ATTEMPTS:
					raise
				delay = DOWNLOAD_RETRY_DELAY * 2 ** (attempt - 1)
				log_progress(
					f'[{marc_id}:{file_id}] Download attempt {attempt}/{DOWNLOAD_MAX_ATTEMPTS} '
					f'failed ({type(error).__name__}: {error}); retrying in {delay} seconds'
				)
				time.sleep(delay)
				attempt += 1

	def _download_pdf_scan_once(self, marc_id: str, file_id: str, target_path: Path) -> int:
		"""Log the header's size before streaming; retain bounded HTTP error details.

		Use decimal MB for progress when Content-Length is valid. Unknown sizes
		are omitted, and valid lengths still guard against incomplete transfers.
		"""
		auth_headers = {'Authorization': VIEWER_BASIC_AUTH}

		manifest_resp = self.session.get(
			f'{VIEWER_BASE_URL}/iiif/manifest',
			params={'marcID': marc_id, 'fileId': file_id},
			headers=auth_headers,
			timeout=self.timeout,
		)
		manifest_resp.raise_for_status()
		manifest = manifest_resp.json()

		download_path = f'/iiif/file/{file_id}/bytes'
		for rendering in manifest.get('rendering') or []:
			if isinstance(rendering, dict) and rendering.get('id') and rendering.get('type') is None:
				download_path = str(rendering['id'])
				break

		self.session.get(
			f'{VIEWER_BASE_URL}/stats/event/PAGE_VIEWS',
			params={'marcID': marc_id, 'fileId': file_id},
			headers=auth_headers,
			timeout=self.timeout,
		).raise_for_status()

		self.session.get(
			f'{VIEWER_BASE_URL}/stats/event/IIIF_DOWNLOAD_ATTEMPT',
			params={'marcID': marc_id, 'fileId': file_id},
			headers=auth_headers,
			timeout=self.timeout,
		).raise_for_status()

		download_url = urljoin(VIEWER_BASE_URL, download_path)
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
			raise ValueError(f'Downloaded content for {marc_id}:{file_id} is not a valid PDF: {first_chunk!r}')
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


class CommonsUploader:
	"""Wrapper around Pywikibot to upload files to Wikimedia Commons with retries."""

	def __init__(self, pywikibot_dir: str = DEFAULT_PYWIKIBOT_DIR) -> None:
		"""Configure PYWIKIBOT_DIR and initialize the Commons site connection."""
		os.environ.setdefault('PYWIKIBOT_DIR', pywikibot_dir)
		import pywikibot
		from pywikibot import config as pwb_config
		import pywikibot.data.api

		pwb_config.socket_timeout = 200
		pwb_config.put_throttle = 0
		patch_mime_multipart_from_mangling(pywikibot.data.api.CTEBinaryMIMEMultipart)
		self.pywikibot = pywikibot
		self.site = pywikibot.Site('commons', 'commons')

	def title_exists(self, commons_filename: str) -> bool:
		"""Return True if File:<commons_filename> already exists on Commons."""
		file_page = self.pywikibot.FilePage(self.site, f'File:{commons_filename}')
		return bool(file_page.exists())

	def upload(self, file_path: Path, commons_filename: str, wikitext: str, summary: str) -> dict[str, Any]:
		"""Upload file_path to Commons as File:<commons_filename> with chunked retries."""
		self.site.login()
		file_page = self.pywikibot.FilePage(self.site, f'File:{commons_filename}')
		if file_page.exists():
			return {'status': 'existing_title', 'commons_title': f'File:{commons_filename}'}

		file_page.text = wikitext
		for attempt in range(1, UPLOAD_MAX_RETRIES + 1):
			if attempt > 1 and file_page.exists():
				return {'status': 'uploaded', 'commons_title': f'File:{commons_filename}'}
			try:
				file_page.upload(
					str(file_path),
					chunk_size=UPLOAD_CHUNK_SIZE,
					asynchronous=True,
					comment=summary,
					ignore_warnings=False,
				)
				return {'status': 'uploaded', 'commons_title': f'File:{commons_filename}'}
			except self.pywikibot.exceptions.UploadError as error:
				error_code = getattr(error, 'code', None)
				if error_code == 'exists':
					return {'status': 'existing_title', 'commons_title': f'File:{commons_filename}'}
				if error_code == 'duplicate':
					duplicates = extract_duplicate_filenames(error)
					return {
						'status': 'duplicate_sha1',
						'commons_title': f'File:{commons_filename}',
						'duplicates': duplicates,
					}
				if attempt >= UPLOAD_MAX_RETRIES or not is_retryable_upload_error(error):
					raise
				err_label = error_code or type(error).__name__
				log_progress(
					f'Attempt {attempt}/{UPLOAD_MAX_RETRIES} failed with {err_label}. Retrying in 60s...'
				)
				time.sleep(60)
			except Exception as error:
				if attempt >= UPLOAD_MAX_RETRIES or not is_retryable_upload_error(error):
					raise
				err_label = getattr(error, 'code', None) or type(error).__name__
				log_progress(
					f'Attempt {attempt}/{UPLOAD_MAX_RETRIES} failed with {err_label}. Retrying in 60s...'
				)
				time.sleep(60)

		raise RuntimeError(f'Upload failed after {UPLOAD_MAX_RETRIES} attempts: {commons_filename}')


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
	client: NlbClient,
	uploader: Any,
	state: dict[str, Any],
	state_path: Path,
	staging_dir: Path,
	marc_id: str,
	file_id: str,
	part_label: str,
	record_meta: dict[str, Any],
	marc_meta: dict[str, Any],
	category: str = DEFAULT_CATEGORY,
	license_template: str = DEFAULT_LICENSE,
	dry_run: bool = False,
) -> str:
	"""Process a scan, deferring HTTP 400 downloads until a later run.

	Record rejections without marking them completed. Save their earliest page
	so advancing through the remaining results cannot lose pending scans.
	"""
	key = item_state_key(marc_id, file_id)
	state['current_record_id'] = marc_id
	state['current_item_key'] = key
	save_state(state_path, state)

	viewer_url = f'{VIEWER_BASE_URL}/viewer?markID={marc_id}&fileID={file_id}'

	commons_filename = build_commons_filename(
		record_meta=record_meta,
		marc_meta=marc_meta,
		marc_id=marc_id,
		file_id=file_id,
		part_label=part_label,
	)

	if not dry_run:
		log_progress(f'[{marc_id}:{file_id}] Checking title on Commons:\nFile:{commons_filename}')
		if uploader.title_exists(commons_filename):
			log_progress(f'[{marc_id}:{file_id}] Skipping (title exists on Commons): File:{commons_filename}')
			state['items'][key] = {
				'status': 'existing_title',
				'marc_id': marc_id,
				'file_id': file_id,
				'commons_title': f'File:{commons_filename}',
				'updated_at': datetime.now(timezone.utc).isoformat(),
			}
			save_state(state_path, state)
			return 'existing_title'

	staging_dir.mkdir(parents=True, exist_ok=True)
	temp_pdf = staging_dir / f'{marc_id}_{file_id}.pdf'
	try:
		try:
			size_bytes = client.download_pdf_scan(marc_id, file_id, temp_pdf)
		except requests.exceptions.HTTPError as error:
			if error.response is None or error.response.status_code != 400:
				raise
			log_progress(f'[{marc_id}:{file_id}] Skipping download (HTTP 400): {viewer_url}\n{error}')
			state['items'][key] = {
				'status': 'download_rejected',
				'marc_id': marc_id,
				'file_id': file_id,
				'http_status': 400,
				'error': str(error),
				'updated_at': datetime.now(timezone.utc).isoformat(),
			}
			page = max(1, int(state.get('current_page') or 1))
			state['retry_from_page'] = min(page, int(state.get('retry_from_page') or page))
			save_state(state_path, state)
			return 'download_rejected'
		except ValueError as error:
			if 'not a valid PDF' in str(error):
				log_progress(f'[{marc_id}:{file_id}] Skipping (not a valid PDF): {viewer_url}')
				state['items'][key] = {
					'status': 'not_pdf',
					'marc_id': marc_id,
					'file_id': file_id,
					'error': str(error),
					'updated_at': datetime.now(timezone.utc).isoformat(),
				}
				save_state(state_path, state)
				return 'not_pdf'
			raise

		size_mib = size_bytes / (1024 * 1024)
		log_progress(f'[{marc_id}:{file_id}] Checking SHA-1 hash on Commons: {viewer_url}')
		sha1, existing_titles = check_commons_by_hash(
			temp_pdf,
			session=client.session,
		)
		if existing_titles:
			log_progress(f'[{marc_id}:{file_id}] Skipping (duplicate SHA-1 on Commons: {existing_titles}): {viewer_url}')
			state['items'][key] = {
				'status': 'duplicate_sha1',
				'marc_id': marc_id,
				'file_id': file_id,
				'sha1': sha1,
				'size_bytes': size_bytes,
				'duplicates': existing_titles,
				'updated_at': datetime.now(timezone.utc).isoformat(),
			}
			save_state(state_path, state)
			return 'duplicate_sha1'

		wikitext = build_commons_description(
			record_meta=record_meta,
			marc_meta=marc_meta,
			marc_id=marc_id,
			file_id=file_id,
			part_label=part_label,
			category=category,
			license_template=license_template,
		)

		if dry_run:
			log_progress(f'[{marc_id}:{file_id}] Dry-run ready ({size_mib:.2f} MiB, SHA-1 {sha1}): File:{commons_filename}')
			return 'dry_run_ready'

		log_progress(f'[{marc_id}:{file_id}] Uploading to Wikimedia Commons:\nFile:{commons_filename}')
		summary = f'From {ELIB_BASE_URL}/elib/Record/{marc_id} (fileID {file_id})'
		upload_result = uploader.upload(temp_pdf, commons_filename, wikitext, summary)
		status = upload_result['status']
		commons_title = upload_result.get('commons_title', f'File:{commons_filename}')
		commons_slug = commons_title.replace(' ', '_')
		commons_url = f'https://commons.wikimedia.org/wiki/{commons_slug}'
		if status == 'uploaded':
			log_progress(f'[{marc_id}:{file_id}] Uploaded:\n{commons_url}\n')
		else:
			log_progress(f'[{marc_id}:{file_id}] Skipping ({status}): {commons_url}\n')
		state['items'][key] = {
			'status': status,
			'marc_id': marc_id,
			'file_id': file_id,
			'sha1': sha1,
			'size_bytes': size_bytes,
			'commons_title': commons_title,
			'duplicates': upload_result.get('duplicates', []),
			'updated_at': datetime.now(timezone.utc).isoformat(),
		}
		save_state(state_path, state)
		return status
	finally:
		temp_pdf.unlink(missing_ok=True)


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
) -> dict[str, int]:
	"""Upload available scans, revisiting earlier HTTP 400 rejections on each run."""
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
		'duplicate_sha1': 0,
		'existing_title': 0,
		'restricted': 0,
		'not_pdf': 0,
		'dry_run_ready': 0,
	}
	uploaded_bytes = 0
	shutdown_requested = False
	start_time = time.monotonic()

	def _handle_sigint(signum: int, frame: Any) -> None:
		nonlocal shutdown_requested
		if not shutdown_requested:
			shutdown_requested = True
			log_progress('\nCtrl-C received: finishing current item before shutting down...')

	previous_sigint_handler = signal.signal(signal.SIGINT, _handle_sigint)
	try:
		while not shutdown_requested:
			page_url = build_search_page_url(
				page=page,
				from_year=from_year,
				to_year=until_year,
				search_url=search_url,
			)
			log_progress(f'[page {page}] Fetching search results: {page_url}')
			search_page = client.fetch_search_page(
				page=page,
				from_year=from_year,
				to_year=until_year,
				search_url=search_url,
			)
			records = search_page['records']
			if not records:
				break

			state['current_page'] = page
			save_state(state_path, state)

			for search_entry in records:
				if shutdown_requested:
					break

				marc_id = search_entry['marc_id']
				record_url = f'{ELIB_BASE_URL}/elib/Record/{marc_id}'
				direct_files = search_entry.get('file_ids') or []
				direct_mets = search_entry.get('mets_ids') or []
				if direct_files and not direct_mets and all(
					is_item_completed(state, marc_id, fid) for fid in direct_files
				):
					continue

				log_progress(f'[{marc_id}] Checking access: {record_url}')
				access = client.check_doc_access(marc_id)
				if not access.get('publicAccess'):
					log_progress(f'[{marc_id}] Skipping (restricted access): {record_url}')
					for fid in direct_files or ['0']:
						key = item_state_key(marc_id, fid)
						state['items'][key] = {
							'status': 'restricted',
							'marc_id': marc_id,
							'file_id': fid,
							'updated_at': datetime.now(timezone.utc).isoformat(),
						}
					save_state(state_path, state)
					counts['restricted'] += 1
					continue

				log_progress(f'[{marc_id}] Fetching metadata: {record_url}')
				record_meta, marc_meta = client.fetch_record_metadata(marc_id)
				pub_year = resolve_publication_year(record_meta, marc_meta)
				if from_year is not None and pub_year is not None and pub_year < from_year:
					log_progress(f'[{marc_id}] Skipping (year {pub_year} < {from_year}): {record_url}')
					continue
				if until_year is not None and pub_year is not None and pub_year > until_year:
					log_progress(f'[{marc_id}] Skipping (year {pub_year} > {until_year}): {record_url}')
					continue

				mets_ids = [*direct_mets, *record_meta.get('mets_ids', [])]
				if mets_ids:
					log_progress(
						f'[{marc_id}] Resolving multi-part METS files: '
						f'{VIEWER_BASE_URL}/viewer/html?metsID={mets_ids[0]}&markID={marc_id}'
					)
				file_entries = client.resolve_record_files(search_entry, record_meta)
				for file_entry in file_entries:
					file_id = file_entry['file_id']
					if is_item_completed(state, marc_id, file_id):
						if shutdown_requested:
							break
						continue

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
					)
					counts[status] = counts.get(status, 0) + 1
					if status == 'uploaded':
						item_entry = state.get('items', {}).get(item_state_key(marc_id, file_id), {})
						uploaded_bytes += int(item_entry.get('size_bytes') or 0)

					if shutdown_requested:
						break

					if max_uploads > 0 and (counts['uploaded'] + counts['dry_run_ready']) >= max_uploads:
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


def build_arg_parser() -> argparse.ArgumentParser:
	"""Create the command-line argument parser."""
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
		default=DEFAULT_LICENSE,
		help='Wikimedia Commons license template without braces (default: PD-scan|PD-old-assumed-expired).',
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
		help='Download and hash-check without uploading to Wikimedia Commons.',
	)
	return parser


def main(argv: list[str] | None = None) -> int:
	"""Entry point for CLI execution."""
	parser = build_arg_parser()
	args = parser.parse_args(argv)

	target_url = args.search_url or args.url
	until_year = args.until_year
	if until_year is None and not target_url:
		until_year = DEFAULT_UNTIL_YEAR

	client = NlbClient(viewer_cookies=args.viewer_cookies)
	uploader = None if args.dry_run else CommonsUploader(pywikibot_dir=args.pywikibot_dir)

	try:
		counts = iterate_and_upload(
			client=client,
			uploader=uploader,
			state_path=args.state_file,
			staging_dir=args.staging_dir,
			until_year=until_year,
			from_year=args.from_year,
			search_url=target_url,
			category=args.category,
			license_template=args.license,
			max_uploads=args.max_uploads,
			dry_run=args.dry_run,
		)
		print(json.dumps(counts, ensure_ascii=False))
		return 0
	except KeyboardInterrupt:
		print('Interrupted by user; state saved for resumption.')
		return 130


if __name__ == '__main__':
	raise SystemExit(main())
