'''Exercise structural PDF checks with real minimal PDFs and malformed responses.'''

import hashlib
import io
from pathlib import Path
import tempfile
import unittest

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject

from pdf_validation import InvalidPDFError, validate_pdf


def pdf_bytes(pages: int = 1, padding: int = 0) -> bytes:
	'''Build a valid deterministic PDF for offline transfer and integrity tests.'''
	writer = PdfWriter()
	for _ in range(pages):
		writer.add_blank_page(width=100, height=100)
	if padding:
		writer.add_metadata({'/Subject': 'x' * padding})
	stream = io.BytesIO()
	writer.write(stream)
	return stream.getvalue()


class TestPdfValidation(unittest.TestCase):
	'''Validation never rewrites bytes and refuses truncated or mismatched scans.'''

	def test_encrypted_and_empty_documents_require_review(self):
		'''A parseable container alone is insufficient without accessible scan pages.'''
		with tempfile.TemporaryDirectory() as directory:
			path = Path(directory) / 'scan.pdf'
			for encrypted in (False, True):
				with self.subTest(encrypted=encrypted):
					writer = PdfWriter()
					if encrypted:
						writer.add_blank_page(width=100, height=100)
						writer.encrypt('secret')
					writer.write(path)
					with self.assertRaises(InvalidPDFError):
						validate_pdf(path)

	def test_page_content_is_decoded_without_rewriting_source(self):
		'''Exercise real page content rather than only blank-page fixture containers.'''
		with tempfile.TemporaryDirectory() as directory:
			path = Path(directory) / 'scan.pdf'
			writer = PdfWriter()
			page = writer.add_blank_page(width=100, height=100)
			stream = DecodedStreamObject()
			stream.set_data(b'0 0 10 10 re S')
			page[NameObject('/Contents')] = writer._add_object(stream)
			writer.write(path)
			before = path.read_bytes()
			self.assertEqual(validate_pdf(path)['pages'], 1)
			self.assertEqual(path.read_bytes(), before)

	def test_valid_pdf_is_unchanged_and_pages_match_manifest(self):
		'''A read-only structural check preserves the exact original checksum.'''
		with tempfile.TemporaryDirectory() as directory:
			path = Path(directory) / 'scan.pdf'
			body = pdf_bytes(2)
			path.write_bytes(body)
			result = validate_pdf(path, {'items': [{'type': 'Canvas'}, {'type': 'Canvas'}]})
			self.assertEqual(result['pages'], 2)
			self.assertEqual(result['manifest_pages'], 2)
			self.assertEqual(hashlib.sha256(path.read_bytes()).digest(), hashlib.sha256(body).digest())

	def test_header_only_html_truncation_and_wrong_page_count_fail(self):
		'''Neither a PDF-looking prefix nor a successful HTTP status proves a full scan.'''
		with tempfile.TemporaryDirectory() as directory:
			path = Path(directory) / 'scan.pdf'
			for body, manifest in (
				(b'%PDF-1.4\n', {}), (b'<html>Login</html>', {}),
				(pdf_bytes()[:150], {}),
				(pdf_bytes(), {'sequences': [{'canvases': [{}, {}]}]}),
			):
				with self.subTest(size=len(body)):
					path.write_bytes(body)
					with self.assertRaises(InvalidPDFError):
						validate_pdf(path, manifest)
