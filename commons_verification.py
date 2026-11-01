'''Validate Commons file identity, allowing bounded reads after upload publication.'''

from datetime import datetime, timezone
import re
import time
from typing import Any

import requests


VERIFICATION_MAX_ATTEMPTS = 5
VERIFICATION_RETRY_DELAY = 2
TRANSIENT_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504})
TRANSIENT_API_CODES = frozenset({
	'internal-error', 'maxlag', 'ratelimited', 'readonly',
	'internal_api_error_DBConnectionError', 'internal_api_error_DBQueryTimeoutError',
	'internal_api_error_DBUnexpectedError',
})


class InvalidVerificationResponse(ValueError):
	'''The response cannot yet establish a complete, usable Commons file identity.'''


class VerificationRetriesExhausted(ValueError):
	'''Keep the final verification failure and read count available for pending state.'''

	def __init__(self, last_error: Exception, attempts: int) -> None:
		'''Explain why verification remains pending without treating publication as failed.'''
		self.last_error = last_error
		self.attempts = attempts
		super().__init__(f'Commons verification failed after {attempts} attempts '
			f'({type(last_error).__name__}): {last_error}')


def parse_file_info(payload: Any) -> dict[str, Any]:
	'''Require a current image, positive page/revision IDs, SHA-1 and byte size.'''
	query = payload.get('query') if isinstance(payload, dict) else None
	pages = query.get('pages') if isinstance(query, dict) else None
	if not isinstance(pages, list) or len(pages) != 1 or not isinstance(pages[0], dict):
		raise InvalidVerificationResponse('Commons verification response has no single page object')
	page = pages[0]
	if 'missing' in page or 'invalid' in page:
		raise InvalidVerificationResponse('Commons verification page is missing or invalid')
	for field in ('pageid', 'lastrevid'):
		if type(page.get(field)) is not int or page[field] <= 0:
			raise InvalidVerificationResponse(f'Commons verification response has missing or invalid {field}')
	if not isinstance(page.get('title'), str) or not page['title'].strip():
		raise InvalidVerificationResponse('Commons verification response has missing or invalid title')
	if 'ns' in page and page['ns'] != 6:
		raise InvalidVerificationResponse('Commons verification response is outside the file namespace')
	images = page.get('imageinfo')
	if not isinstance(images, list) or not images or not isinstance(images[0], dict):
		raise InvalidVerificationResponse('Commons verification response has no file information')
	info = images[0]
	sha1 = info.get('sha1')
	if not isinstance(sha1, str) or not re.fullmatch(r'[0-9a-fA-F]{40}', sha1):
		raise InvalidVerificationResponse('Commons verification response has an invalid SHA-1 hash')
	if type(info.get('size')) is not int or info['size'] <= 0:
		raise InvalidVerificationResponse('Commons verification response has an invalid byte size')
	return {
		'commons_title': page['title'], 'page_id': page['pageid'],
		'revision_id': page['lastrevid'], 'sha1': sha1.lower(),
		'size_bytes': info['size'], 'timestamp': info.get('timestamp'),
		'url': info.get('url'), 'checked_at': datetime.now(timezone.utc).isoformat(),
	}


def is_retryable_verification_error(error: Exception) -> bool:
	'''Retry exposed transient failures while preserving TLS/authentication errors.

	Pywikibot already retries transport failures internally. Its exhausted timeout
	budget is deliberately not restarted by this additional publication-read loop.
	'''
	if isinstance(error, requests.exceptions.SSLError):
		return False
	if isinstance(error, InvalidVerificationResponse):
		return True
	if isinstance(error, requests.HTTPError):
		return error.response is not None and error.response.status_code in TRANSIENT_HTTP_STATUSES
	if isinstance(error, (requests.ConnectionError, requests.Timeout,
		requests.exceptions.ChunkedEncodingError, ConnectionError, TimeoutError)):
		return True
	if getattr(error, 'code', None) in TRANSIENT_API_CODES:
		return True
	if type(error).__module__.startswith('pywikibot.'):
		from pywikibot.exceptions import FatalServerError, ServerError
		return isinstance(error, ServerError) and not isinstance(error, FatalServerError)
	return False


def read_file_info(site: Any, title: str) -> dict[str, Any]:
	'''Make up to five fresh reads, leaving incomplete metadata unverified.

	A newly published image can be visible before its page identity and revision.
	Wait 2, 4, 8 and 16 seconds between reads, sharing this budget with transient
	failures exposed by Pywikibot. Authentication and certificate failures propagate.
	'''
	for attempt in range(1, VERIFICATION_MAX_ATTEMPTS + 1):
		try:
			payload = site.simple_request(
				action='query', titles=title, prop='imageinfo|info',
				iiprop='sha1|size|timestamp|url', formatversion=2,
			).submit()
			return parse_file_info(payload)
		except Exception as error:
			if not is_retryable_verification_error(error):
				raise
			if attempt == VERIFICATION_MAX_ATTEMPTS:
				raise VerificationRetriesExhausted(error, attempt) from error
			delay = VERIFICATION_RETRY_DELAY * 2 ** (attempt - 1)
			print(f'[{title}] Verification attempt {attempt}/{VERIFICATION_MAX_ATTEMPTS} '
				f'failed ({type(error).__name__}: {error}); retrying in {delay} seconds')
			time.sleep(delay)
	raise AssertionError('Commons verification retry loop ended without a result')
