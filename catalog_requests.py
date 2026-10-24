"""Bound transient NLB catalog GET failures without changing shared-session retries."""

from collections.abc import Callable
import time
from typing import Any

import requests


CATALOG_MAX_ATTEMPTS = 5
CATALOG_RETRY_DELAY = 5
CATALOG_TRANSIENT_STATUSES = frozenset({500, 502, 503, 504})


class CatalogRetriesExhausted(RuntimeError):
	"""A catalog read remains pending after its transient retry budget is exhausted."""

	def __init__(self, last_error: requests.RequestException, attempts: int) -> None:
		"""Preserve the final failure and attempt count for resumable batch diagnostics."""
		self.last_error = last_error
		self.attempts = attempts
		super().__init__(f'Catalog request failed after {attempts} attempts ({type(last_error).__name__}: {last_error})')


def get_catalog_response(
	session: requests.Session,
	url: str,
	*,
	timeout: float,
	log: Callable[[str], None] | None = None,
	**kwargs: Any,
) -> requests.Response:
	"""GET an NLB catalog resource with five attempts and 5/10/20/40-second delays.

	Retry read/connection failures and HTTP 500/502/503/504. Certificate errors,
	permanent HTTP failures, and interrupts retain their original exception type.
	The caller owns the successful response; failed responses are always closed.
	Use this for catalog reads only, leaving PDF and Commons retries independent.

	An application-level loop also catches failures while Requests consumes a
	response body, which an HTTPAdapter/urllib3 transport retry cannot replay.
	See https://requests.readthedocs.io/en/latest/user/advanced/#example-automatic-retries
	"""
	for attempt in range(1, CATALOG_MAX_ATTEMPTS + 1):
		response = None
		try:
			response = session.get(url, timeout=timeout, **kwargs)
			response.raise_for_status()
			return response
		except requests.RequestException as error:
			if response is None:
				response = error.response
			if response is not None:
				response.close()
			retryable = (
				isinstance(error, (requests.Timeout, requests.ConnectionError))
				and not isinstance(error, requests.exceptions.SSLError)
			) or (
				isinstance(error, requests.HTTPError)
				and response is not None
				and response.status_code in CATALOG_TRANSIENT_STATUSES
			)
			if not retryable:
				raise
			if attempt == CATALOG_MAX_ATTEMPTS:
				raise CatalogRetriesExhausted(error, attempt) from error
			delay = CATALOG_RETRY_DELAY * (2 ** (attempt - 1))
			if log is not None:
				log(
					f'Catalog request attempt {attempt}/{CATALOG_MAX_ATTEMPTS} '
					f'failed ({type(error).__name__}: {error}); retrying in {delay} seconds'
				)
			time.sleep(delay)
