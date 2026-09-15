"""Retrying HTTP client shared by every ingest module.

Two classes of failure, deliberately kept apart:

  RetryableHTTPError  429, 5xx, transport errors — back off and try again.
  FatalHTTPError      other 4xx — a malformed SoQL query or a bad field name
                      will never succeed on retry, so failing fast is correct.

Socrata exposes no rate-limit headers at all (checked 2026-09-04: no
X-RateLimit-*, no Retry-After), so there is no budget to observe. Back off on
429 regardless and treat the absence of a signal as "assume nothing".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import requests
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

log = logging.getLogger(__name__)

# TxDOT date-string predicates legitimately take 10-12s (measured 2026-09-04),
# so a short timeout would turn slow-but-working into spurious retries.
DEFAULT_TIMEOUT = 90.0
MAX_ATTEMPTS = 6


class RetryableHTTPError(Exception):
    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class FatalHTTPError(Exception):
    """A 4xx that retrying cannot fix."""


@dataclass(frozen=True)
class Response:
    body: bytes
    status: int
    url: str
    headers: dict[str, str] = field(default_factory=dict)


def build_session(app_token: str = "") -> requests.Session:
    session = requests.Session()
    session.headers["User-Agent"] = "crash-to-contact/0.1 (take-home; contact via repo)"
    if app_token:
        # Socrata: lifts the shared anonymous per-IP pool onto a per-token budget.
        session.headers["X-App-Token"] = app_token
    return session


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None  # HTTP-date form; not worth parsing, backoff covers it


def _wait(retry_state) -> float:
    """Honour Retry-After when the server sends one, else exponential + jitter."""
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    retry_after = getattr(exc, "retry_after", None)
    if retry_after is not None:
        return min(retry_after, 120.0)
    return wait_exponential_jitter(initial=1, max=60)(retry_state)


@retry(
    retry=retry_if_exception_type((RetryableHTTPError, requests.exceptions.RequestException)),
    wait=_wait,
    stop=stop_after_attempt(MAX_ATTEMPTS),
    before_sleep=before_sleep_log(log, logging.WARNING),
    reraise=True,
)
def get(
    url: str,
    *,
    params: dict | None = None,
    session: requests.Session | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> Response:
    """GET with retry. Returns raw bytes — bronze needs the payload unparsed."""
    sess = session or build_session()
    resp = sess.get(url, params=params, timeout=timeout)

    if resp.status_code == 429 or resp.status_code >= 500:
        raise RetryableHTTPError(
            f"HTTP {resp.status_code} for {resp.url}: {resp.text[:200]}",
            retry_after=_parse_retry_after(resp.headers.get("Retry-After")),
        )
    if resp.status_code >= 400:
        # Socrata puts an errorCode in the body; ArcGIS puts error.details.
        raise FatalHTTPError(f"HTTP {resp.status_code} for {resp.url}: {resp.text[:500]}")

    return Response(
        body=resp.content,
        status=resp.status_code,
        url=resp.url,
        headers=dict(resp.headers),
    )
