"""Argus shared — the wrapped HTTP fetcher (Law 7: every call is wrapped, §12)."""

from __future__ import annotations

import json
import re
import time

import httpx

from shared.exceptions import FetchError
from shared.fetch_logger import write_fetch_log

# Reliability contract (blueprint §12 / §2 item 12): 30s timeout per attempt,
# 2 retries 30s apart, then fail loud. Total attempts = MAX_RETRIES + 1 = 3.
TIMEOUT_SECONDS: float = 30.0
MAX_RETRIES: int = 2
RETRY_BACKOFF_SECONDS: float = 30.0

# Credentials that must never reach fetch_log: FRED's api_key and the IBKR Flex
# token (`t`) travel in the query string (those APIs offer no header auth), and
# httpx error strings echo the full request URL. Redact them from any logged or
# raised error text (Law 13 / blueprint §13: secrets never leak, even to the DB).
_SECRET_PARAM_RE = re.compile(r"(?i)\b(api_key|token|t)=[^&\s]+")


def _redact(text: str) -> str:
    """Mask credential query params (api_key / token / t) in arbitrary text."""
    return _SECRET_PARAM_RE.sub(r"\1=***", text)


def _elapsed_ms(start: float) -> int:
    """Return whole milliseconds elapsed since a `time.monotonic()` reading."""
    return int((time.monotonic() - start) * 1000)


def fetch_with_retry(
    url: str,
    headers: dict,
    params: dict,
    source: str,
    run_id: str,
    *,
    parse: str = "json",
) -> dict | list | str:
    """GET `url` under the §12 reliability contract and return its parsed body.

    Each attempt uses a 30s timeout; on failure it retries up to MAX_RETRIES times,
    RETRY_BACKOFF_SECONDS apart. EVERY attempt — success or failure — is recorded to
    `fetch_log` with its measured latency (Law 7: no failure is swallowed). When all
    attempts are exhausted the function raises FetchError so the caller can surface
    the outage (Source Health line / staleness flags / alerts). Credentials carried
    in the query string are redacted from any logged/raised error (Law 13).

    Args:
        url:     the endpoint to GET.
        headers: request headers (may be empty).
        params:  query-string parameters (may be empty).
        source:  data-source identifier, logged to fetch_log (e.g. 'tiingo').
        run_id:  pipeline run id, logged to fetch_log to group this run's attempts.
        parse:   'json' (default) returns the decoded JSON body (dict or list);
                 'text' returns the raw response text — required for the IBKR Flex
                 Web Service, which is XML-only (the caller parses it with
                 xml.etree.ElementTree).

    Returns:
        The decoded JSON body (parse='json') or the raw response text (parse='text').

    Raises:
        FetchError: when every attempt (1 initial + MAX_RETRIES) has failed.
    """
    last_error: str | None = None

    for attempt in range(1, MAX_RETRIES + 2):
        start = time.monotonic()
        try:
            response = httpx.get(
                url, headers=headers, params=params, timeout=TIMEOUT_SECONDS
            )
            response.raise_for_status()
            data = response.json() if parse == "json" else response.text
        except httpx.TimeoutException as exc:
            status, err = "timeout", _redact(f"{type(exc).__name__}: {exc}")
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            status, err = "failure", _redact(f"{type(exc).__name__}: {exc}")
        else:
            write_fetch_log(source, run_id, "success", _elapsed_ms(start))
            return data

        # Shared failure path (timeout + failure): record the attempt, then back off
        # if any retries remain. `err` is rebound inside each except clause because
        # the `exc` name is cleared when the except block exits.
        last_error = err
        write_fetch_log(source, run_id, status, _elapsed_ms(start), err)
        if attempt <= MAX_RETRIES:
            time.sleep(RETRY_BACKOFF_SECONDS)

    raise FetchError(
        source,
        f"all {MAX_RETRIES + 1} attempts failed; last error: {last_error}",
    )
