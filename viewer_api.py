'''Stop repeated viewer outages while leaving individual missing sources retryable.'''

import requests


UNAVAILABLE_HTTP_STATUSES = frozenset({404, 410, 500, 502, 503, 504})


class ViewerAPIUnavailable(RuntimeError):
	'''A viewer operation has failed for several distinct records without recovery.'''

	def __init__(self, operation: str, record_count: int, error: BaseException) -> None:
		'''Describe the outage without including response bodies, cookies or request URLs.'''
		self.operation = operation
		self.record_count = record_count
		self.last_error = error
		response = error.response if isinstance(error, requests.HTTPError) else None
		detail = f'HTTP {response.status_code}' if response is not None else type(error).__name__
		super().__init__(
			f'NLB viewer API unavailable: {operation} failed for {record_count} distinct '
			f'sources without recovery ({detail}). Stopping before further Commons edits. '
			'Affected work remains retryable; use --continue after the viewer API recovers.'
		)


class ViewerAPIHealth:
	'''Bound repeated unavailable responses independently for each viewer operation.

	Call failed with the underlying request error after its request-level retries
	finish. Repeated attempts or scans of one record count only once. A successful
	response or an unrelated error ends that operation's outage streak.
	'''

	def __init__(self, threshold: int = 3) -> None:
		'''Require a positive number of distinct failed records before stopping work.'''
		if type(threshold) is not int or threshold < 1:
			raise ValueError('Viewer outage threshold must be a positive integer')
		self.threshold = threshold
		self._failed_records: dict[str, set[str]] = {}

	def succeeded(self, operation: str) -> None:
		'''Clear only the operation whose fresh response was successfully validated.'''
		self._failed_records.pop(operation, None)

	def failed(self, operation: str, record_id: str, error: BaseException) -> None:
		'''Count an unavailable operation once per record and stop at the threshold.

	Authentication, certificate, schema and other unclassified failures retain
	their own handling and clear any older outage evidence for this operation.
	The caller must pass fixed operation names, never URLs or authentication data.
	'''
		unavailable = isinstance(error, (requests.Timeout, requests.ConnectionError)) and not isinstance(
			error, requests.exceptions.SSLError,
		)
		if isinstance(error, requests.HTTPError):
			unavailable = error.response is not None and error.response.status_code in UNAVAILABLE_HTTP_STATUSES
		if not unavailable:
			self.succeeded(operation)
			return
		records = self._failed_records.setdefault(operation, set())
		records.add(record_id)
		if len(records) >= self.threshold:
			raise ViewerAPIUnavailable(operation, len(records), error) from error
