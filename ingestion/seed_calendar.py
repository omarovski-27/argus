"""Argus ingestion — static calendar seed: SPCX schedule + 2026 macro events (§14/§15).

No external API at runtime (Law 8): the dates are embedded constants. The forward
calendar is first-class and rendered from this table, never hallucinated (Law 4),
and it drives the event filter — no round trips within 24h of FOMC / CPI / NFP /
SPCX lockup-or-index dates (§8).

Provenance (Law 2 — facts retrieved, never generated from memory):
  • SPCX rows: blueprint §15 (staggered lockup per the S-1).
  • FOMC rows: the 2026 statement (second-meeting-day) dates from
    federalreserve.gov/monetarypolicy/fomccalendars.htm.
  • CPI / NFP rows: the 2026 BLS release schedule (Consumer Price Index and the
    Employment Situation), the primary sources named in §6, retrieved at build time.

Scope: macro events are seeded *forward* from 2026-06-13 only. The calendar is
forward-looking (§7) and the event filter gates only future trades (§8); seeding
earlier-2026 BLS dates adds no value and they were shifted by the 2025-26
appropriations lapse (revision ambiguity). Annual re-seeding for later years is the
spec'd ``seed-calendar --year YYYY`` CLI flow (§6), not this one-time seeder.

Idempotent: upsert on the ``(type, date, symbol)`` dedup key, doing nothing on
conflict. The constraint is ``UNIQUE NULLS NOT DISTINCT``, so macro rows (NULL
symbol) dedupe correctly too.

Prereq: run ``seed_instruments`` first — the SPCX rows FK to ``instruments(symbol)``.

NOT seeded (no fixed date — Law 2 / Law 4 forbid inventing one):
  • SPCX Q2 / Q3 earnings (TBD; auto-resolve via Finnhub once the 8-K is filed, §15).
    When that resolver lands, seed ONLY book symbols (config watchlist) — Finnhub's
    earnings_calendar returns every company, and shared.event_filter arms on any
    ``earnings`` row by type alone; a broad pull would over-block the §8 filter on
    unrelated earnings. Book-scoped keeps type-membership ≡ §8 (else add the
    ``event['symbol'] in book`` gate in triggers_event_filter).
  • SPCX +10% conditional unlock (close >= $175.50 on >=5 of 10 sessions
    post-Q2-earnings): undated until the Q2 anchor exists. Its ``conditional_rule``
    (see the calendar_events DDL comment) attaches to the Q2 earnings row when known;
    until then Argus monitors the trigger directly from ``prices_eod`` (§14 / §15).

Run:  python -m ingestion.seed_calendar   (or: python ingestion/seed_calendar.py)
"""

from __future__ import annotations

if __name__ == "__main__" and __package__ in (None, ""):
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from shared.db import get_client

# --- SPCX lockup / index / research schedule (blueprint §15) ----------------- #
# Only rows with a concrete date are seeded (calendar_events.date is NOT NULL).
SPCX_EVENTS: list[dict[str, str | None]] = [
    {"date": "2026-06-22", "type": "research", "symbol": "SPCX", "materiality": "low"},     # analyst-initiations window opens (quiet-period end)
    {"date": "2026-07-02", "type": "index", "symbol": "SPCX", "materiality": "medium"},     # Nasdaq-100 fast-entry eligibility (~)
    {"date": "2026-08-21", "type": "lockup", "symbol": "SPCX", "materiality": "medium"},    # Day 70 — 7% tranche
    {"date": "2026-09-10", "type": "lockup", "symbol": "SPCX", "materiality": "medium"},    # Day 90 — 7%
    {"date": "2026-09-25", "type": "lockup", "symbol": "SPCX", "materiality": "medium"},    # Day 105 — 7%
    {"date": "2026-10-10", "type": "lockup", "symbol": "SPCX", "materiality": "medium"},    # Day 120 — 7%
    {"date": "2026-10-25", "type": "lockup", "symbol": "SPCX", "materiality": "medium"},    # Day 135 — 7%
    {"date": "2026-12-09", "type": "lockup", "symbol": "SPCX", "materiality": "high"},      # Day 180 — full lockup expiry (major supply event)
    {"date": "2027-06-13", "type": "lockup", "symbol": "SPCX", "materiality": "high"},      # Day 366 — Musk + major backers eligible (~)
]

# --- 2026 macro events, forward from 2026-06-13 (all high materiality, §8) ---- #
# FOMC statement (second meeting) days — federalreserve.gov.
FOMC_2026 = ["2026-06-17", "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09"]
# CPI release dates — BLS Consumer Price Index schedule.
CPI_2026 = ["2026-07-14", "2026-08-12", "2026-09-11", "2026-10-14", "2026-11-10", "2026-12-10"]
# Employment Situation (nonfarm payrolls) release dates — BLS schedule.
NFP_2026 = ["2026-07-02", "2026-08-07", "2026-09-04", "2026-10-02", "2026-11-06", "2026-12-04"]


def _macro_rows() -> list[dict[str, str | None]]:
    """Build the forward 2026 FOMC/CPI/NFP rows (symbol NULL = macro event)."""
    rows: list[dict[str, str | None]] = []
    for event_type, dates in (("fomc", FOMC_2026), ("cpi", CPI_2026), ("nfp", NFP_2026)):
        for event_date in dates:
            rows.append(
                {"date": event_date, "type": event_type, "symbol": None, "materiality": "high"}
            )
    return rows


def seed_spcx_calendar() -> None:
    """Seed ``calendar_events`` with the SPCX schedule (§15) + forward 2026 macro events.

    Idempotent: upserts on the ``(type, date, symbol)`` dedup key, doing nothing on
    conflict. Static data only — no external API (Law 8).
    """
    rows = SPCX_EVENTS + _macro_rows()
    get_client().table("calendar_events").upsert(
        rows, on_conflict="type,date,symbol", ignore_duplicates=True
    ).execute()
    macro_n = len(rows) - len(SPCX_EVENTS)
    print(
        f"[seed_calendar] upserted {len(rows)} event(s): "
        f"{len(SPCX_EVENTS)} SPCX + {macro_n} macro (FOMC/CPI/NFP)."
    )


if __name__ == "__main__":
    seed_spcx_calendar()
