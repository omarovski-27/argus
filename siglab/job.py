"""Argus Signal Lab — the DB wrapper: fetch stored series → engine → ledger (report/write).

Read-only by default (``--report``): fetch TSLA OHLC + indicators + VIX + arming events,
run the pure engine, print the backfilled record. This is the FIRST honest read on the
rule — it runs today, before ``signal_ledger`` even exists, because the report needs no
table. ``--write`` inserts only the ledger dates not already present (insert-only-missing,
so a re-run never overwrites a day scored live — protecting the live event-filter state).
``--nightly`` is the same write path invoked by ``daily.yml``: it appends the just-closed
day and fail-louds (``signal:inputs_missing``) if a mature day could not be scored.

Run:  python -m siglab.job              (read-only backfill report — safe, no table needed)
      python -m siglab.job --write      (insert missing ledger rows — needs the migration)
      python -m siglab.job --nightly    (daily.yml step: append the new day, fail-loud)
"""

from __future__ import annotations

if __name__ == "__main__" and __package__ in (None, ""):
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import time
import uuid

from postgrest.exceptions import APIError

from shared.event_filter import FILTERED_EVENT_TYPES, triggers_event_filter
from siglab.engine import build_ledger_rows
from siglab.ledger import compute_stats
from siglab.registry import SIGNAL_VERSION, load_signal, signal_params
from siglab.render import render_signal_full

_PAGE = 1000


def _to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _paged(query_factory) -> list[dict]:
    """Collect all rows from a PostgREST query, paging past the 1000-row cap."""
    rows: list[dict] = []
    start = 0
    while True:
        batch = query_factory(start, start + _PAGE - 1).execute().data or []
        rows.extend(batch)
        if len(batch) < _PAGE:
            break
        start += _PAGE
    return rows


def _fetch_series(client, params: dict) -> tuple[list, dict, list, set]:
    """(prices_asc, indicators_by_date, vix_asc, arming_dates) for the signal symbol."""
    symbol = params.get("symbol", "TSLA")
    prices = _paged(lambda a, b: (
        client.table("prices_eod").select("date,open,high,low,close")
        .eq("symbol", symbol).order("date").range(a, b)
    ))
    for r in prices:
        for k in ("open", "high", "low", "close"):
            r[k] = _to_float(r.get(k))

    ind_rows = _paged(lambda a, b: (
        client.table("indicators").select("date,name,value")
        .eq("symbol", symbol).in_("name", ["sma50", "macd_hist"]).order("date").range(a, b)
    ))
    indicators_by_date: dict[str, dict] = {}
    for r in ind_rows:
        indicators_by_date.setdefault(str(r["date"]), {})[r["name"]] = _to_float(r.get("value"))

    vix = _paged(lambda a, b: (
        client.table("macro_series").select("date,value")
        .eq("series_id", "VIXCLS").order("date").range(a, b)
    ))
    for r in vix:
        r["value"] = _to_float(r.get("value"))

    cal = (
        client.table("calendar_events").select("date,type,symbol")
        .in_("type", list(FILTERED_EVENT_TYPES)).execute().data or []
    )
    arming_dates = {str(r["date"]) for r in cal if triggers_event_filter(r)}
    return prices, indicators_by_date, vix, arming_dates


def compute_backfill(client) -> tuple[list[dict], int, dict]:
    """(ledger_rows, warmup_skipped, stats) from stored history — pure of any write."""
    blob = load_signal(client)
    params = signal_params(blob)
    prices, ind_by_date, vix, arming = _fetch_series(client, params)
    rows, warmup = build_ledger_rows(prices, ind_by_date, vix, arming, params)
    stats = compute_stats(rows, blob)
    return rows, warmup, stats


def read_ledger(client) -> list[dict]:
    """All ledger rows for the active signal version (ascending). Read-only.

    The live render surfaces (/today, /signal, the digest) read the PERSISTED ledger —
    fast and indexed — rather than recomputing from source each time. Raises if the
    table is absent (pre-migration); callers degrade gracefully to a 'pending' line."""
    return _paged(lambda a, b: (
        client.table("signal_ledger")
        .select("date,signal_state,outcome,shadow_pnl")
        .eq("signal_version", SIGNAL_VERSION).order("date").range(a, b)
    ))


# PostgREST's code for "relation not found in the schema cache" — the ledger table before
# its migration is applied. A KNOWN pending state (the /today card renders 'backfill
# pending'), NOT a data-integrity failure, so the nightly job skips it rather than red-alerts.
_MISSING_TABLE_CODE = "PGRST205"


def ledger_table_exists(client) -> bool:
    """True iff ``signal_ledger`` is queryable; False on the pre-migration PGRST205.

    Any other API error propagates (Law 7): only the specific 'table absent' condition is
    the benign pre-migration state — a permissions or connectivity error must still surface."""
    try:
        client.table("signal_ledger").select("date").limit(1).execute()
        return True
    except APIError as exc:
        if getattr(exc, "code", None) == _MISSING_TABLE_CODE:
            return False
        raise


def _existing_dates(client) -> set[str]:
    rows = _paged(lambda a, b: (
        client.table("signal_ledger").select("date")
        .eq("signal_version", SIGNAL_VERSION).order("date").range(a, b)
    ))
    return {str(r["date"]) for r in rows}


def write_missing(client, rows: list[dict]) -> int:
    """Insert only ledger dates not already present (insert-only-missing; never overwrite)."""
    existing = _existing_dates(client)
    fresh = [r for r in rows if r["date"] not in existing]
    for r in fresh:
        client.table("signal_ledger").insert({
            "signal_version": SIGNAL_VERSION,
            "date": r["date"],
            "signal_state": r["signal_state"],
            "outcome": r["outcome"],
            "shadow_pnl": r["shadow_pnl"],
            "inputs_json": r["inputs_json"],
        }).execute()
    return len(fresh)


def run_nightly(client, run_id: str | None = None) -> dict:
    """daily.yml step: compute + append the just-closed day (insert-only-missing), fail-loud."""
    from shared.fetch_logger import write_fetch_log

    run_id = run_id or f"signal-{uuid.uuid4().hex[:12]}"
    start = time.monotonic()
    try:
        rows, warmup, stats = compute_backfill(client)
        # Pre-migration guard: if the ledger DDL isn't applied yet, this is a known pending
        # state — skip GREEN (no red alert) so wiring the nightly step is safe before Omar
        # applies the migration. The first run AFTER it lands backfills the full history
        # (insert-only-missing) and the /today card lights up with no further action.
        if not ledger_table_exists(client):
            write_fetch_log(
                "signal", run_id, "success", int((time.monotonic() - start) * 1000),
                "signal_ledger absent (pre-migration) — nightly skipped, no rows written",
            )
            print("[signal] nightly: signal_ledger table absent (pre-migration) — skipped, no write.")
            return stats
        written = write_missing(client, rows)
        # Fail-loud (Law 7): a mature series should always yield a row for the latest
        # priced day. If the newest price date produced no ledger row, an input is
        # missing where it should not be — surface it, don't skip silently.
        prices, *_ = _fetch_series(client, signal_params(load_signal(client)))
        latest_price_date = str(prices[-1]["date"]) if prices else None
        latest_row_date = rows[-1]["date"] if rows else None
        if latest_price_date and latest_row_date != latest_price_date:
            write_fetch_log(
                "signal:inputs_missing", run_id, "failure",
                int((time.monotonic() - start) * 1000),
                f"latest priced day {latest_price_date} has no signal row (inputs missing)",
            )
        write_fetch_log("signal", run_id, "success", int((time.monotonic() - start) * 1000))
        print(f"[signal] nightly: {written} new row(s); status {stats['status']}; "
              f"day {stats['n_days']}; record {stats['wins']}-{stats['losses']}.")
        return stats
    except Exception as exc:  # noqa: BLE001 — surface, log, never swallow (Law 7)
        write_fetch_log("signal", run_id, "failure", int((time.monotonic() - start) * 1000), str(exc))
        print(f"[signal] nightly FAILED — {exc}")
        raise


def _print_report(rows, warmup, stats) -> None:
    print(f"[signal] backfill: {len(rows)} computable day(s), {warmup} warmup day(s) skipped.")
    triggered = stats["n_triggered"]
    print(f"[signal] FAVORABLE-triggered: {triggered} "
          f"({stats['wins']} win / {stats['losses']} loss), "
          f"no_trigger {stats['no_trigger']}, unknown {stats['unknown']}.")
    wr = stats["winrate"]
    print(f"[signal] win rate: {'n/a' if wr is None else f'{wr*100:.1f}%'}; "
          f"cumulative shadow P&L: {stats['cum_pnl']:+,.2f}.")
    print(f"[signal] derived status: {stats['status']}  ({stats['evidence_label']})")
    print("\n--- /signal render ---")
    print(render_signal_full(stats))


if __name__ == "__main__":
    import sys

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
    from shared.db import get_client

    client = get_client()
    if "--nightly" in sys.argv:
        run_nightly(client)
    else:
        rows, warmup, stats = compute_backfill(client)
        _print_report(rows, warmup, stats)
        if "--write" in sys.argv:
            n = write_missing(client, rows)
            print(f"\n[signal] wrote {n} new ledger row(s).")
