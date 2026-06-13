"""Argus ingestion — FRED macro-series fetcher (blueprint §4 / §6).

Pulls the six macro series Argus tracks for regime context (digest §7) and upserts
them into ``macro_series``:

    DFF       — effective federal funds rate
    CPIAUCSL  — CPI, all urban consumers (headline inflation level)
    UNRATE    — unemployment rate
    DGS10     — 10-year Treasury constant-maturity yield
    T10Y2Y    — 10y-minus-2y spread (curve / recession signal)
    VIXCLS    — CBOE VIX close (regime / volatility)

Endpoint (FRED observations):
    GET https://api.stlouisfed.org/fred/series/observations
        ?series_id=...&api_key=...&file_type=json[&observation_start=YYYY-MM-DD]
    Response: {"observations": [{"date": "YYYY-MM-DD", "value": "1.55"}, ...]}.
    FRED encodes a missing observation as the string ``"."``.

Only retrieved values are stored (Law 2). A ``"."`` is the *absence* of a fact, not
a fact, so those rows are skipped. The api_key rides in the query string (FRED has
no header auth) but is redacted from any error the shared fetcher logs/raises (§13).
All HTTP via :func:`shared.fetcher_base.fetch_with_retry`; one ``fetch_log`` row
per series per run on the happy path.

Run:  python -m ingestion.fred   (or: python ingestion/fred.py)
"""

from __future__ import annotations

if __name__ == "__main__" and __package__ in (None, ""):
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import os

from shared.db import get_client
from shared.exceptions import FetchError
from shared.fetcher_base import fetch_with_retry

FRED_OBSERVATIONS_URL = "https://api.stlouisfed.org/fred/series/observations"

# The 6 series, matching the macro_series CHECK constraint in the applied migration.
FRED_SERIES: tuple[str, ...] = ("DFF", "CPIAUCSL", "UNRATE", "DGS10", "T10Y2Y", "VIXCLS")

# Default window: ~a decade is plenty for VIX percentiles / regime context (§7)
# while keeping each upsert bounded (DFF is daily back to 1954). Pass
# observation_start=None to fetch a series' full history.
_DEFAULT_OBSERVATION_START = "2015-01-01"

# FRED's sentinel for a missing observation.
_MISSING_VALUES = (None, ".", "")


def fetch_macro(
    run_id: str,
    *,
    observation_start: str | None = _DEFAULT_OBSERVATION_START,
) -> None:
    """Fetch the six FRED macro series and upsert them into ``macro_series`` (§6).

    Args:
        run_id: Run identifier, logged to ``fetch_log`` to group this run's fetches.
        observation_start: ISO ``YYYY-MM-DD`` lower bound (defaults to a ~decade
            window). Pass ``None`` to fetch each series' full available history.

    Missing observations (FRED ``"."``) are skipped rather than stored (Law 2). A
    per-series failure is surfaced (already in ``fetch_log``, Law 7) and skipped so
    one bad series does not abort the others.
    """
    api_key = os.environ.get("FRED_API_KEY")
    if not api_key:
        raise RuntimeError("Missing FRED_API_KEY (see .env.example).")

    client = get_client()

    for series_id in FRED_SERIES:
        params = {"series_id": series_id, "api_key": api_key, "file_type": "json"}
        if observation_start:
            params["observation_start"] = observation_start

        try:
            payload = fetch_with_retry(
                FRED_OBSERVATIONS_URL, {}, params, f"fred:{series_id}", run_id
            )
        except FetchError as exc:
            print(f"[fred] {series_id}: unavailable — {exc}")
            continue

        rows = [
            {"series_id": series_id, "date": obs["date"], "value": float(obs["value"])}
            for obs in payload.get("observations", [])
            if obs.get("value") not in _MISSING_VALUES
        ]
        if rows:
            client.table("macro_series").upsert(
                rows, on_conflict="series_id,date"
            ).execute()
        print(f"[fred] {series_id}: upserted {len(rows)} observation(s).")


if __name__ == "__main__":
    import uuid

    manual_run_id = f"manual-fred-{uuid.uuid4().hex[:12]}"
    fetch_macro(manual_run_id)
