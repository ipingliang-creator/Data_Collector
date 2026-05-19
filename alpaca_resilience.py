"""Resilience wrappers for outbound Alpaca API calls.

`alpaca-py` makes its HTTP requests with **no timeout**, so a stalled
connection hangs the calling process indefinitely. It also retries
only HTTP 429 (rate limit) internally — not 5xx server errors,
timeouts, or dropped connections. These helpers close both gaps:

- `apply_session_timeout` — defaults a request timeout on a client's
  HTTP session, so no call can hang forever.
- `call_with_retry` — retries a call on transient failures (timeout,
  connection error, 5xx) with exponential backoff.

yfinance needs none of this — it sets its own timeouts and retries
transient errors internally.
"""
from __future__ import annotations

import logging
import time
from typing import Callable, TypeVar

import requests
from alpaca.common.exceptions import APIError

logger = logging.getLogger(__name__)

# Generous ceiling — normal Alpaca calls return in well under 5s, so a
# call still running at 30s has stalled and should be cut + retried.
DEFAULT_TIMEOUT = 30.0
DEFAULT_ATTEMPTS = 3        # 1 initial try + 2 retries
DEFAULT_BASE_DELAY = 2.0    # exponential backoff: 2s, then 4s

T = TypeVar("T")


def apply_session_timeout(client, seconds: float = DEFAULT_TIMEOUT):
    """Default a request `timeout` on an alpaca-py client's HTTP session.

    alpaca-py calls `requests` with no `timeout`, so a stalled
    connection blocks forever. We wrap the session's `request` to
    inject a default timeout — the SDK never passes one, so the
    default always takes effect.

    Returns `client` (so callers can wrap construction inline). No-op
    if the client exposes no patchable `_session` — defensive against
    an SDK-internals change.
    """
    session = getattr(client, "_session", None)
    if session is None or not hasattr(session, "request"):
        logger.warning(
            "Alpaca client has no patchable _session; calls run "
            "without a timeout"
        )
        return client

    original_request = session.request

    def request_with_timeout(*args, **kwargs):
        kwargs.setdefault("timeout", seconds)
        return original_request(*args, **kwargs)

    session.request = request_with_timeout
    return client


def _is_transient(exc: BaseException) -> bool:
    """True if `exc` is a transient failure worth retrying.

    Transient: connection errors, timeouts, and 5xx API errors.
    Permanent (not retried): 4xx API errors — the request itself is
    bad, so retrying won't help. An `APIError` with no readable status
    is treated as transient (retry — better than giving up blind).
    """
    if isinstance(
        exc, (requests.exceptions.Timeout, requests.exceptions.ConnectionError)
    ):
        return True
    if isinstance(exc, APIError):
        status = exc.status_code
        return status is None or status >= 500
    return False


def call_with_retry(
    fn: Callable[[], T],
    *,
    attempts: int = DEFAULT_ATTEMPTS,
    base_delay: float = DEFAULT_BASE_DELAY,
    what: str = "Alpaca call",
) -> T:
    """Call `fn()`, retrying transient failures with exponential backoff.

    Retries on connection errors, timeouts, and 5xx responses, up to
    `attempts` total tries with `base_delay * 2**n` second waits
    between them. Permanent errors (4xx, or anything non-transient)
    re-raise immediately. The last exception re-raises after the final
    attempt — so a caller's existing `try/except` still sees a clean
    failure instead of the job hanging.
    """
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:
            if not _is_transient(exc) or attempt == attempts:
                raise
            delay = base_delay * (2 ** (attempt - 1))
            print(
                f"  {what}: transient failure ({type(exc).__name__}); "
                f"retry {attempt}/{attempts - 1} in {delay:.0f}s",
                flush=True,
            )
            time.sleep(delay)
