"""Argus ingestion — static seed of the four tracked instruments (blueprint §4, §6).

Seeds the ``instruments`` table with TSLA, SPCX, SPY, QQQ. No external API: this is
fixed reference data. ``first_trade_date`` drives young-ticker indicator suppression
— SPCX listed 2026-06-12, so it has no SMA50 until 50 sessions exist (§4). For the
three established tickers the date is reference-only (their full history makes
suppression moot); only SPCX's is functionally load-bearing.

Schema note: the applied migration keys ``instruments`` by ``symbol`` (text PK) and
holds only (symbol, name, first_trade_date) — there is no ``ticker`` or
``asset_type`` column (blueprint §4 / 20260612175007_init_spine.sql). Run this
before any seed that references ``instruments`` (prices, calendar, Flex), since
those tables FK to ``instruments(symbol)``.

Run:  python -m ingestion.seed_instruments   (or: python ingestion/seed_instruments.py)
"""

from __future__ import annotations

# Allow both `python -m ingestion.seed_instruments` and direct
# `python ingestion/seed_instruments.py`: put the repo root on sys.path so the
# `shared` package imports cleanly when run as a loose script.
if __name__ == "__main__" and __package__ in (None, ""):
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from shared.db import get_client

# The tracked universe (blueprint §2 item 5 / §6). first_trade_date is an ISO date
# string; SPCX = 2026-06-12 is the load-bearing one (indicator suppression, §4).
INSTRUMENTS: list[dict[str, str]] = [
    {"symbol": "TSLA", "name": "Tesla, Inc.", "first_trade_date": "2010-06-29"},
    {"symbol": "SPCX", "name": "SpaceX (Space Exploration Technologies)", "first_trade_date": "2026-06-12"},
    {"symbol": "SPY", "name": "SPDR S&P 500 ETF Trust", "first_trade_date": "1993-01-22"},
    {"symbol": "QQQ", "name": "Invesco QQQ Trust", "first_trade_date": "1999-03-10"},
]


def seed_instruments() -> None:
    """Upsert the four tracked instruments, idempotent on the ``symbol`` primary key.

    Re-running is safe: existing rows are updated in place (name / first_trade_date
    refreshed), none are duplicated. Static data only — no external API call.
    """
    client = get_client()
    client.table("instruments").upsert(INSTRUMENTS, on_conflict="symbol").execute()
    symbols = ", ".join(row["symbol"] for row in INSTRUMENTS)
    print(f"[seed_instruments] upserted {len(INSTRUMENTS)} instruments: {symbols}.")


if __name__ == "__main__":
    seed_instruments()
