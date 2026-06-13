"""Argus ingestion — Tiingo EOD price fetcher (blueprint §5 / §6).

Tiingo is the primary price source (free tier, ~50 symbols/hr; Argus needs 4:
TSLA, SPCX, SPY, QQQ). This pulls daily OHLCV and upserts it into ``prices_eod``.

Endpoint (Tiingo daily prices):
    GET https://api.tiingo.com/tiingo/daily/{ticker}/prices?startDate=YYYY-MM-DD[&endDate=...]
    Auth: ``Authorization: Token <TIINGO_API_KEY>`` header (key never in the URL/logs).
    Response: JSON array of bars, each with date/open/high/low/close/volume (+ adj*).

Only the raw retrieved fields are stored — no adjusted/computed numbers (Law 2);
indicators are derived later, locally, via pandas_ta (§6). The applied schema keys
``prices_eod`` by (symbol, date) with a ``source`` column ('tiingo'|'yfinance') —
there is no instrument_id and no adj_close column, so neither is written.

All HTTP goes through :func:`shared.fetcher_base.fetch_with_retry` (the §12
reliability contract + fetch_log logging); no bare httpx calls. One ``fetch_log``
row is written per ticker per run on the happy path (the shared fetcher also logs
each retried attempt).

Run:  python -m ingestion.tiingo   (or: python ingestion/tiingo.py)
"""

from __future__ import annotations

if __name__ == "__main__" and __package__ in (None, ""):
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import os
from datetime import date, timedelta

from shared.db import get_client
from shared.exceptions import FetchError
from shared.fetcher_base import fetch_with_retry

TIINGO_BASE_URL = "https://api.tiingo.com/tiingo/daily"
DEFAULT_TICKERS: list[str] = ["TSLA", "SPCX", "SPY", "QQQ"]

# Daily mode (no explicit start_date): re-pull a short trailing window so any
# missed sessions backfill; the upsert merges, so re-pulling settled days is cheap.
_DAILY_LOOKBACK_DAYS = 7


def _int_or_none(value: object) -> int | None:
    """Coerce a raw volume value to int (prices_eod.volume is bigint); None passes through."""
    if value is None:
        return None
    return int(value)


def fetch_prices(
    tickers: list[str],
    run_id: str,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    ignore_duplicates: bool = False,
) -> None:
    """Fetch EOD OHLCV from Tiingo and upsert it into ``prices_eod`` (§5 / §6).

    Args:
        tickers: Symbols to fetch. They must already exist in ``instruments``
            (``prices_eod.symbol`` FKs to ``instruments(symbol)``).
        run_id: Run identifier, logged to ``fetch_log`` to group this run's fetches.
        start_date: ISO ``YYYY-MM-DD`` lower bound. Defaults to a short trailing
            window for daily +1-row updates.
        end_date: ISO ``YYYY-MM-DD`` upper bound. Defaults to Tiingo's latest bar.
        ignore_duplicates: When True, conflicting ``(symbol, date)`` rows are left
            untouched — used by the historical seed (insert-once). When False
            (default daily mode) conflicting rows are updated.

    A per-ticker fetch failure is surfaced (already in ``fetch_log`` via the shared
    fetcher, Law 7) and skipped so one bad symbol does not abort the others.
    """
    api_key = os.environ.get("TIINGO_API_KEY")
    if not api_key:
        raise RuntimeError("Missing TIINGO_API_KEY (see .env.example).")

    if start_date is None:
        start_date = (date.today() - timedelta(days=_DAILY_LOOKBACK_DAYS)).isoformat()

    client = get_client()
    headers = {"Content-Type": "application/json", "Authorization": f"Token {api_key}"}

    for ticker in tickers:
        params = {"startDate": start_date, "format": "json"}
        if end_date:
            params["endDate"] = end_date

        try:
            bars = fetch_with_retry(
                f"{TIINGO_BASE_URL}/{ticker}/prices",
                headers,
                params,
                f"tiingo:{ticker}",
                run_id,
            )
        except FetchError as exc:
            # Already logged to fetch_log by the shared fetcher; surface and move on.
            print(f"[tiingo] {ticker}: unavailable — {exc}")
            continue

        rows = [
            {
                "symbol": ticker,
                "date": bar["date"][:10],  # 'YYYY-MM-DDT00:00:00.000Z' -> 'YYYY-MM-DD'
                "open": bar.get("open"),
                "high": bar.get("high"),
                "low": bar.get("low"),
                "close": bar.get("close"),
                "volume": _int_or_none(bar.get("volume")),
                "source": "tiingo",
            }
            for bar in bars
        ]
        if rows:
            client.table("prices_eod").upsert(
                rows, on_conflict="symbol,date", ignore_duplicates=ignore_duplicates
            ).execute()
        print(f"[tiingo] {ticker}: upserted {len(rows)} price row(s) from {start_date}.")


if __name__ == "__main__":
    import uuid

    manual_run_id = f"manual-tiingo-{uuid.uuid4().hex[:12]}"
    fetch_prices(DEFAULT_TICKERS, manual_run_id)
