'''Validate scan structure with pypdf without modifying the downloaded original.

The parser checks PDF objects and page contents. A matching IIIF canvas count is
checked when available; this cannot establish that the library scanned every leaf.
See https://pypdf.readthedocs.io/en/stable/user/robustness.html.
'''

from pathlib import Path
from typing import Any

from pypdf import PdfReader
from pypdf.errors import PyPdfError
import requests


class InvalidPDFError(requests.RequestException):
	'''An expected PDF is malformed, unreadable or inconsistent with its manifest.'''


def validate_pdf(path: Path, manifest: dict[str, Any] | None = None) -> dict[str, Any]:
	'''Parse pages and contents in strict mode; retain originals and reject repair needs.'''
	manifest = manifest or {}
	expected = None
	sequences = manifest.get('sequences')
	if isinstance(sequences, list) and sequences and isinstance(sequences[0], dict):
		canvases = sequences[0].get('canvases')
		if isinstance(canvases, list) and canvases:
			expected = len(canvases)
	items = manifest.get('items')
	if isinstance(items, list) and items and all(isinstance(item, dict) and item.get('type') == 'Canvas' for item in items):
		expected = len(items)
	try:
		with path.open('rb') as source:
			reader = PdfReader(source, strict=True)
			if reader.is_encrypted:
				raise InvalidPDFError('Encrypted PDF requires review')
			count = len(reader.pages)
			if count < 1:
				raise InvalidPDFError('PDF contains no pages')
			for page in reader.pages:
				contents = page.get_contents()
				if contents is not None:
					contents.get_data()
	except (PyPdfError, ValueError, KeyError, TypeError) as error:
		raise InvalidPDFError(f'Invalid PDF structure: {error}') from error
	if expected is not None and count != expected:
		raise InvalidPDFError(f'PDF has {count} pages; IIIF manifest has {expected} canvases')
	return {'pages': count, 'manifest_pages': expected, 'validator': 'pypdf strict', 'original_bytes_unchanged': True}
