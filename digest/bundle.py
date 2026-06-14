"""Argus digest — assemble the frozen synthesis input bundle (blueprint §6 / §7, Law 2).

Law 2 in code: this module RETRIEVES facts; it never generates them. Every value in the
returned dict is read verbatim from a DB row. The only derived figure is
``round_trips.cumulative_delta_shares`` — a plain sum of stored ``delta_shares`` (the
core journal metric, §9), an aggregation of stored rows, not a fabricated number. Date
windows (last 30 days, next 14 days, last 48h) are query bounds, not invented facts.

The returned dict becomes ``digests.bundle_json`` — the exact, frozen input the Sonnet
synthesis runs on, persisted so every digest is reproducible forever (§6).

Pulled (blueprint §7 sections map to these keys):
    prices            last 30 trading days per tracked ticker (ascending)
    indicators        latest values per symbol (the most recent indicator date)
    macro             latest value per FRED series
    calendar          next 14 days of calendar_events (ordered)
    headlines         last 48h, each with its joined sentiment rows
    positions         the latest positions_snapshot date's rows
    round_trips       last 30 days + cumulative sleeve Delta-shares
    config            all config rows (key -> JSONB value)
    last_digest_sent_at   prior digest timestamp (for the /pulse delta)

Run:  python -m digest.bundle   (or: python digest/bundle.py)
"""

from __future__ import annotations

if __name__ == "__main__" and __package__ in (None, ""):
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from datetime import date, datetime, timedelta, timezone

from shared.db import get_client

_TRACKED: tuple[str, ...] = ("TSLA", "SPCX", "SPY", "QQQ")
_MACRO_SERIES: tuple[str, ...] = ("DFF", "CPIAUCSL", "UNRATE", "DGS10", "T10Y2Y", "VIXCLS")
_PRICE_WINDOW = 30          # trading days
_HEADLINE_LOOKBACK_HOURS = 48
_CALENDAR_AHEAD_DAYS = 14
_ROUND_TRIP_WINDOW_DAYS = 30


def _to_float(value: object) -> float | None:
    """Coerce a numeric cell (PostgREST may return numeric as str) to float, else None."""
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _recent_prices(client) -> dict[str, list[dict]]:
    """Last 30 trading days per tracked ticker, returned ascending by date."""
    out: dict[str, list[dict]] = {}
    for symbol in _TRACKED:
        rows = (
            client.table("prices_eod")
            .select("*")
            .eq("symbol", symbol)
            .order("date", desc=True)
            .limit(_PRICE_WINDOW)
            .execute()
            .data
            or []
        )
        out[symbol] = list(reversed(rows))  # ascending for readability
    return out


def _latest_indicators(client) -> dict[str, dict]:
    """Latest indicator values per symbol.

    Indicators are dense daily, so every currently-active indicator name shares the
    symbol's most-recent indicator date; we read that date's rows. A suppressed
    indicator simply has no rows (and no date) — it is absent here, never null-valued.
    """
    out: dict[str, dict] = {}
    for symbol in _TRACKED:
        latest = (
            client.table("indicators")
            .select("date")
            .eq("symbol", symbol)
            .order("date", desc=True)
            .limit(1)
            .execute()
            .data
            or []
        )
        if not latest:
            out[symbol] = {}
            continue
        as_of = latest[0]["date"]
        rows = (
            client.table("indicators")
            .select("name,value")
            .eq("symbol", symbol)
            .eq("date", as_of)
            .execute()
            .data
            or []
        )
        out[symbol] = {"as_of": as_of, "values": {r["name"]: r["value"] for r in rows}}
    return out


def _latest_macro(client) -> dict[str, dict | None]:
    """Latest observation per FRED series (value + its date), or None if unseeded."""
    out: dict[str, dict | None] = {}
    for series_id in _MACRO_SERIES:
        rows = (
            client.table("macro_series")
            .select("date,value")
            .eq("series_id", series_id)
            .order("date", desc=True)
            .limit(1)
            .execute()
            .data
            or []
        )
        out[series_id] = rows[0] if rows else None
    return out


def _upcoming_calendar(client, today: date) -> list[dict]:
    """calendar_events from today through +14 days, ordered by date (rendered, never invented)."""
    end = (today + timedelta(days=_CALENDAR_AHEAD_DAYS)).isoformat()
    return (
        client.table("calendar_events")
        .select("*")
        .gte("date", today.isoformat())
        .lte("date", end)
        .order("date")
        .execute()
        .data
        or []
    )


def _recent_headlines(client) -> list[dict]:
    """Headlines from the last 48h, each with its joined sentiment rows (newest first)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=_HEADLINE_LOOKBACK_HOURS)).isoformat()
    return (
        client.table("headlines")
        .select("*, sentiment(*)")
        .gte("published_at", cutoff)
        .order("published_at", desc=True)
        .execute()
        .data
        or []
    )


def _latest_positions(client) -> dict:
    """The most recent positions_snapshot date and its rows (Law 5: as stored)."""
    latest = (
        client.table("positions_snapshot")
        .select("date")
        .order("date", desc=True)
        .limit(1)
        .execute()
        .data
        or []
    )
    if not latest:
        return {"date": None, "rows": []}
    as_of = latest[0]["date"]
    rows = client.table("positions_snapshot").select("*").eq("date", as_of).execute().data or []
    return {"date": as_of, "rows": rows}


def _round_trips(client, today: date) -> dict:
    """Last-30-day round trips + cumulative sleeve Delta-shares (sum of all delta_shares).

    The cumulative figure is the core journal metric (§9): cumulative from trade #1, so
    it sums ``delta_shares`` across ALL round trips — an aggregation of stored rows, the
    one derived value in the bundle (Law 2's intent: every component traces to a row).
    """
    all_rows = client.table("round_trips").select("*").order("date").execute().data or []
    cumulative = 0.0
    for row in all_rows:
        delta = _to_float(row.get("delta_shares"))
        if delta is not None:
            cumulative += delta
    cutoff = (today - timedelta(days=_ROUND_TRIP_WINDOW_DAYS)).isoformat()
    recent = [row for row in all_rows if (row.get("date") or "") >= cutoff]
    return {"recent_30d": recent, "cumulative_delta_shares": round(cumulative, 6)}


def _config(client) -> dict:
    """All config rows as a {key: JSONB value} map (sleeve_shares, phase, gates, …)."""
    rows = client.table("config").select("key,value").execute().data or []
    return {row["key"]: row["value"] for row in rows}


def _last_digest_sent_at(client) -> str | None:
    """The most recent digest's sent_at (for the /pulse delta), or None if none sent."""
    rows = (
        client.table("digests")
        .select("sent_at")
        .order("sent_at", desc=True)
        .limit(1)
        .execute()
        .data
        or []
    )
    return rows[0].get("sent_at") if rows else None


def assemble_bundle(run_type: str) -> dict:
    """Assemble the frozen synthesis input bundle from the DB (Law 2: retrieve, never generate).

    Args:
        run_type: The run that requested the bundle ('monday'/'full'/'pulse'); recorded
            in the bundle for the synthesizer's context. The shape is identical across
            run types — a /pulse run simply assembles from whatever the DB holds without
            having refreshed news/indicators first.

    Returns:
        A JSON-serializable dict (every value sourced from a DB row) destined for
        ``digests.bundle_json``.
    """
    client = get_client()
    today = date.today()
    return {
        "run_type": run_type,
        "generated_for": today.isoformat(),
        "prices": _recent_prices(client),
        "indicators": _latest_indicators(client),
        "macro": _latest_macro(client),
        "calendar": _upcoming_calendar(client, today),
        "headlines": _recent_headlines(client),
        "positions": _latest_positions(client),
        "round_trips": _round_trips(client, today),
        "config": _config(client),
        "last_digest_sent_at": _last_digest_sent_at(client),
    }


if __name__ == "__main__":
    import json

    bundle = assemble_bundle("pulse")
    summary = {
        "run_type": bundle["run_type"],
        "generated_for": bundle["generated_for"],
        "prices_symbols": {s: len(rows) for s, rows in bundle["prices"].items()},
        "headlines": len(bundle["headlines"]),
        "calendar_events": len(bundle["calendar"]),
        "round_trips_cumulative_delta_shares": bundle["round_trips"]["cumulative_delta_shares"],
        "config_keys": sorted(bundle["config"]),
    }
    print(json.dumps(summary, indent=2, default=str))
