"""Argus journal — round-trip pairing engine (Phase 2; blueprint §7 / §8).

Reads classified fills from ``transactions`` and writes paired sleeve round trips to
``round_trips``. Purely additive: it touches no existing code and never mutates
``transactions``. Classification already happened upstream in ``ingestion.ibkr_flex``
(quantity-proximity → trade_type, plus any manual override_type), so this engine needs
no ``sleeve_shares`` — it only consumes the labels.

The pairing rule (the sleeve unit of work, §7/§8):
  • Effective type per leg = ``override_type`` if set, else ``trade_type`` (/override
    always wins, §4). Only ``round_trip_sell`` / ``round_trip_rebuy`` legs are eligible;
    ``dca_*`` and ``unclassified`` are skipped.
  • Group eligible legs by (symbol, trade date), then pair sell→rebuy in exec_time order.
    Multiple of each on a day pair off in order; a sell with no same-day rebuy yields no
    row (and no error).
  • One ``round_trips`` row per pair, with the metric:
        pnl_usd = (sell_px − rebuy_px) × qty − fees      (fees = both legs summed)

delta_shares is deliberately written NULL — see the guard comment at its assignment.

Idempotency (Law 6): the engine re-reads ALL transactions and re-derives every round
trip on each run, then upserts on the sell leg's ext_id (``round_trips.sell_ext_id``,
UNIQUE) with ``ignore_duplicates=True`` — append-only, never a destructive update. So a
daily re-run over an overlapping window is a no-op for trips already recorded.

Reliability (Law 7): the run is wrapped; success and failure both write one ``fetch_log``
row under source ``journal:pairing``, and a failure is re-raised so the scheduled job
fails loud rather than silently leaving the journal un-paired.

Run:  python -m journal.pairing   (or: python journal/pairing.py)
"""

from __future__ import annotations

if __name__ == "__main__" and __package__ in (None, ""):
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import time
import uuid
from datetime import date, datetime

from shared.db import get_client
from shared.fetch_logger import write_fetch_log

# Effective-type values that make a leg part of a sleeve round trip (§4 / §8). Everything
# else (dca_buy, dca_sell, unclassified) is ignored by the pairing engine.
_SELL_TYPE = "round_trip_sell"
_REBUY_TYPE = "round_trip_rebuy"


# --------------------------------------------------------------------------- #
# Pure pairing logic (no DB / no network — unit-tested in tests/test_pairing.py)
# --------------------------------------------------------------------------- #
def effective_type(txn: dict) -> str | None:
    """Return a leg's effective classification: override_type if set, else trade_type.

    /override always wins (§4). Returns None when neither is present.
    """
    return txn.get("override_type") or txn.get("trade_type")


def _as_datetime(value) -> datetime | None:
    """Coerce an exec_time (ISO string or datetime) to a datetime; None if unparseable."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _trade_date(exec_time) -> date | None:
    """The calendar date a fill belongs to, from its exec_time.

    Uses the date component as-is. For regular-hours US trades the UTC date equals the
    ET trading date (a session never crosses midnight UTC: 16:00 ET = 20:00/21:00 UTC),
    so no timezone conversion is needed for same-day grouping — boring beats clever (Law 8).
    """
    dt = _as_datetime(exec_time)
    return dt.date() if dt else None


def _sum_fees(*legs: dict) -> float:
    """Sum the legs' fees, treating a missing/None fee as 0 (stored as a positive cost)."""
    return float(sum((leg.get("fees") or 0.0) for leg in legs))


def _resolve_digest_id(trade_date: date, digests: list[dict]) -> int | None:
    """Latest digest with sent_at ≤ the trade date (the digest in effect when it traded).

    digests: rows with ``id`` and ``sent_at`` (ISO/datetime). A digest sent on the trade
    date itself counts. None when no digest precedes the trade (daily detection routinely
    runs before the first Monday digest exists — round_trips.digest_id is NULLABLE, §4).
    """
    best_id, best_dt = None, None
    for d in digests:
        sent = _as_datetime(d.get("sent_at"))
        if sent is None or sent.date() > trade_date:
            continue
        if best_dt is None or sent > best_dt:
            best_dt, best_id = sent, d.get("id")
    return best_id


def pair_round_trips(transactions: list[dict], digests: list[dict] | None = None) -> list[dict]:
    """Pair eligible sell→rebuy legs into round_trips rows (pure; no I/O).

    Args:
        transactions: ``transactions`` rows (ext_id, exec_time, symbol, side, qty, price,
            fees, trade_type, override_type). Reads ALL of them so the result is the
            complete set of round trips — re-running is idempotent at the DB layer.
        digests: ``digests`` rows (id, sent_at) for digest_id resolution; default none.

    Returns:
        A list of round_trips row dicts ready to upsert, ordered by (date, sell exec_time).
        sell_ext_id is the idempotency key.
    """
    digests = digests or []

    # Eligible legs only, bucketed by (symbol, date); skip dca_* / unclassified, and any
    # leg we can't date or price (can't compute pnl) — skipped, never crashing the batch.
    buckets: dict[tuple[str, date], dict[str, list[dict]]] = {}
    for txn in transactions:
        etype = effective_type(txn)
        if etype not in (_SELL_TYPE, _REBUY_TYPE):
            continue
        tdate = _trade_date(txn.get("exec_time"))
        if tdate is None or txn.get("price") is None:
            continue
        key = (txn["symbol"], tdate)
        side = buckets.setdefault(key, {"sells": [], "rebuys": []})
        (side["sells"] if etype == _SELL_TYPE else side["rebuys"]).append(txn)

    rows: list[dict] = []
    for (symbol, tdate), side in buckets.items():
        # Pair in exec_time order: the i-th sell with the i-th rebuy. Extra legs on
        # either side (a dangling sell, or a surplus rebuy) simply go unpaired — no row.
        sells = sorted(side["sells"], key=lambda t: _as_datetime(t.get("exec_time")) or datetime.min)
        rebuys = sorted(side["rebuys"], key=lambda t: _as_datetime(t.get("exec_time")) or datetime.min)
        for sell, rebuy in zip(sells, rebuys):
            qty = sell.get("qty")
            sell_px = sell.get("price")
            rebuy_px = rebuy.get("price")
            fees = _sum_fees(sell, rebuy)
            # The core sleeve metric: more shares = winning is derived LATER from this.
            pnl_usd = (sell_px - rebuy_px) * qty - fees
            rows.append(
                {
                    "sell_ext_id": sell.get("ext_id"),
                    "date": tdate.isoformat(),
                    "symbol": symbol,
                    "qty": qty,
                    "sell_px": sell_px,
                    "rebuy_px": rebuy_px,
                    "fees": fees,
                    "pnl_usd": pnl_usd,
                    # delta_shares is NEVER stored per row. The sleeve share view is
                    # derived at READ time as (cumulative pnl_usd ÷ current price): prices
                    # vary trade to trade, so a per-row share figure would be meaningless
                    # and — fatally — summable into a wrong total. Leaving it NULL is the
                    # guard that no caller can accidentally sum per-row shares (§7, Law 2).
                    "delta_shares": None,
                    "digest_id": _resolve_digest_id(tdate, digests),
                    # day_trades_in_window filled in below (needs the full week's pairs).
                    "_sort": _as_datetime(sell.get("exec_time")) or datetime.min,
                }
            )

    # Order all trips by (date, sell exec_time), then stamp day_trades_in_window as the
    # 1-based ordinal of each trip within its ISO calendar week ("the Nth round trip of
    # the week"). Ordinal — not the week's total — because the engine upserts append-only:
    # a later same-week trip must not retro-change an already-stored row's value, and an
    # ordinal assigned in time order is stable across re-runs (the week total would not be).
    rows.sort(key=lambda r: (r["date"], r["_sort"]))
    week_seen: dict[tuple[int, int], int] = {}
    for r in rows:
        iso_year, iso_week, _ = date.fromisoformat(r["date"]).isocalendar()
        week_seen[(iso_year, iso_week)] = week_seen.get((iso_year, iso_week), 0) + 1
        r["day_trades_in_window"] = week_seen[(iso_year, iso_week)]
        del r["_sort"]
    return rows


# --------------------------------------------------------------------------- #
# DB runner (wrapped + logged; reuses ingestion.ibkr_flex conventions)
# --------------------------------------------------------------------------- #
def _elapsed_ms(start: float) -> int:
    """Whole milliseconds since a ``time.monotonic()`` reading (for fetch_log)."""
    return int((time.monotonic() - start) * 1000)


def run_pairing(run_id: str) -> int:
    """Re-derive all round trips from ``transactions`` and upsert them into ``round_trips``.

    Wrapped + logged (Law 7): writes one ``fetch_log`` row under ``journal:pairing`` and,
    on failure, re-raises so the scheduled job fails loud. Idempotent (Law 6): upserts on
    the UNIQUE sell_ext_id with ignore_duplicates, so re-runs never duplicate a trip.

    Returns the number of round_trips rows derived this run.
    """
    start = time.monotonic()
    try:
        client = get_client()
        transactions = (
            client.table("transactions")
            .select("ext_id,exec_time,symbol,side,qty,price,fees,trade_type,override_type")
            .execute()
            .data
        ) or []
        digests = (
            client.table("digests").select("id,sent_at").execute().data
        ) or []

        rows = pair_round_trips(transactions, digests)
        if rows:
            client.table("round_trips").upsert(
                rows, on_conflict="sell_ext_id", ignore_duplicates=True
            ).execute()

        write_fetch_log("journal:pairing", run_id, "success", _elapsed_ms(start))
        print(f"[pairing] derived {len(rows)} round trip(s) from {len(transactions)} fill(s).")
        return len(rows)
    except Exception as exc:  # noqa: BLE001 — surface, never swallow (Law 7)
        write_fetch_log("journal:pairing", run_id, "failure", _elapsed_ms(start), str(exc))
        print(f"[pairing] FAILED — {exc}")
        raise


if __name__ == "__main__":
    run_pairing(f"manual-pairing-{uuid.uuid4().hex[:12]}")
