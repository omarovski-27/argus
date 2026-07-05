"""Argus shared — fetch_log writer (Law 7: every fetch outcome is recorded)."""

from __future__ import annotations

import time

from shared.db import get_client


def elapsed_ms(start: float) -> int:
    """Whole milliseconds since a ``time.monotonic()`` reading (for latency_ms)."""
    return int((time.monotonic() - start) * 1000)

# Allowed status values — these MUST match the fetch_log CHECK constraint in the
# applied migration (supabase/migrations/20260612175007_init_spine.sql, table 15)
# and blueprint §12. The live DB rejects anything else, so 'ok'/'error' are
# intentionally NOT used: success -> 'success'; a failed attempt -> 'timeout' or
# 'failure'; a source given up on for the run -> 'unavailable'.
VALID_STATUSES: tuple[str, ...] = ("success", "failure", "timeout", "unavailable")


def write_fetch_log(
    source: str,
    run_id: str,
    status: str,
    latency_ms: int,
    error: str | None = None,
) -> None:
    """Write exactly one row to `fetch_log` (Law 7: silent failure is misinformation).

    Args:
        source:     data-source identifier (e.g. 'tiingo', 'fred').
        run_id:     the pipeline run this fetch belongs to (groups a run's attempts).
        status:     one of VALID_STATUSES; matches the DB CHECK constraint (§12).
        latency_ms: measured attempt latency in milliseconds (>= 0).
        error:      failure detail, or None on success.

    Raises:
        ValueError: if `status` is not one of VALID_STATUSES — caught before the DB
            round-trip so a bad status fails fast instead of as a CHECK violation.
        Exception:  if the DB insert itself fails, the underlying error propagates and
            is never swallowed, so a logging failure can never hide missing data.
    """
    if status not in VALID_STATUSES:
        raise ValueError(
            f"invalid fetch_log status {status!r}; expected one of {VALID_STATUSES}"
        )
    row = {
        "source": source,
        "run_id": run_id,
        "status": status,
        "latency_ms": latency_ms,
        "error": error,
    }
    get_client().table("fetch_log").insert(row).execute()
