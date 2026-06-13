"""Argus ingestion — one-time 200-trading-day historical price backfill (§5 / §6).

Seeds ``prices_eod`` with enough history that locally computed indicators
(pandas_ta, Phase 1) have something to work on — SMA200 needs 200 sessions, etc.
Reuses :func:`ingestion.tiingo.fetch_prices` (no duplicated HTTP / parse / upsert
logic); this module only computes the date window and clamps young tickers.

Idempotent: conflicting ``(symbol, date)`` rows are left untouched
(``ignore_duplicates=True``), so re-running never disturbs already-seeded prices.
SPCX is clamped to its ``first_trade_date`` (2026-06-12) — there is no price
history before a stock lists (§4), and Tiingo would have none to give.

Prereq: run ``seed_instruments`` first (``prices_eod.symbol`` FKs to instruments,
and the SPCX clamp reads ``instruments.first_trade_date``).

Run:  python -m ingestion.seed_prices   (or: python ingestion/seed_prices.py)
"""

from __future__ import annotations

if __name__ == "__main__" and __package__ in (None, ""):
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from datetime import date, datetime, timedelta, timezone

from ingestion.tiingo import DEFAULT_TICKERS, fetch_prices
from shared.db import get_client

# Calendar days per trading day (~252 trading / 365 calendar). Pad the window so
# `days` trading sessions actually land inside it.
_CALENDAR_DAYS_PER_TRADING_DAY = 1.45
_WINDOW_PADDING_DAYS = 5


def _new_run_id(prefix: str) -> str:
    """A timestamped run id for a manual/seed run (groups its fetch_log rows)."""
    return f"{prefix}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"


def seed_historical(tickers: list[str] | None = None, days: int = 200) -> None:
    """Backfill ~``days`` trading days of EOD prices per ticker via Tiingo.

    Args:
        tickers: Symbols to seed; defaults to the four tracked tickers.
        days: Target number of *trading* days (the calendar window is padded to
            cover weekends/holidays).
    """
    tickers = tickers or DEFAULT_TICKERS
    run_id = _new_run_id("seed-prices")
    client = get_client()

    # first_trade_date per ticker — SPCX must not be requested before it listed.
    instruments = client.table("instruments").select("symbol,first_trade_date").execute()
    first_trade = {row["symbol"]: row.get("first_trade_date") for row in instruments.data}

    calendar_lookback = int(days * _CALENDAR_DAYS_PER_TRADING_DAY) + _WINDOW_PADDING_DAYS
    window_start = (date.today() - timedelta(days=calendar_lookback)).isoformat()

    for ticker in tickers:
        start = window_start
        listed = first_trade.get(ticker)
        if listed and listed > start:  # ISO date strings compare lexicographically
            start = listed  # clamp (e.g. SPCX listed 2026-06-12)
        fetch_prices([ticker], run_id, start_date=start, ignore_duplicates=True)
        print(f"[seed_prices] {ticker}: seeded from {start} (target {days} trading days).")


if __name__ == "__main__":
    seed_historical()
