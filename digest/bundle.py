"""Argus digest — assemble the frozen synthesis input bundle (blueprint §6 / §7, Law 2).

Law 2 in code: this module RETRIEVES facts; it never generates them. Every value in the
returned dict is read verbatim from a DB row. The few derived figures are aggregations
of stored rows, never fabrications: ``round_trips.cumulative_delta_shares`` (a sum of
stored ``delta_shares``, the core journal metric, §9) and the ``source_health`` summary,
counts and staleness ages (computed from stored ``fetch_log`` statuses and row dates).
Crucially, ``source_health`` is NOT derived from whether data happens to be present in
the bundle — a source that timed out and left stale rows would still be "present", and
reporting it OK is the silent-failure-as-misinformation Law 7 forbids (§12). Date
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
    source_health     latest fetch_log status per source + prices/Flex staleness +
                      Flex-token days-to-expiry (§7 line 5 / §12, Law 7)
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
_FETCH_LOG_SCAN = 1000          # latest-per-source scan depth (matches bot /health)
_PRICES_STALE_TRADING_DAYS = 2  # §12: prices > 2 trading days old -> warn
_FLEX_STALE_HOURS = 48          # §12: Flex > 48h since last success -> warn
_TOKEN_WARN_DAYS = 30           # §7: surface Flex-token days-to-expiry when < 30


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
    """Headlines from the last 48h, each with its joined sentiment rows (newest first).

    Law 7: the fetchers deliberately keep headlines whose date won't parse with
    ``published_at`` NULL, and those still get Haiku-scored — so a bare
    ``gte("published_at", …)`` would silently drop a fetched, scored headline (NULL >=
    cutoff is never true). Fall back to ``created_at`` for NULL-dated rows so any scored
    headline within the window reaches the bundle.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=_HEADLINE_LOOKBACK_HOURS)).isoformat()
    return (
        client.table("headlines")
        .select("*,sentiment(*)")
        .or_(f"published_at.gte.{cutoff},and(published_at.is.null,created_at.gte.{cutoff})")
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


def _parse_ts(value: object) -> datetime | None:
    """Parse a stored ISO-8601 timestamptz to an aware datetime (assume UTC if naive), else None."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _trading_days_elapsed(since: date, until: date) -> int:
    """Count weekdays in (since, until] — an approximation of trading sessions elapsed.

    Ignores market holidays, so near a holiday it may slightly OVER-count (flag stale a
    day early). Over-warning is the safe direction for a staleness flag — under-warning
    would hide stale data, the exact Law-7 failure mode. Returns 0 if until <= since.
    """
    if until <= since:
        return 0
    count, cursor = 0, since
    while cursor < until:
        cursor += timedelta(days=1)
        if cursor.weekday() < 5:  # Mon-Fri
            count += 1
    return count


def _latest_fetch_per_source(client) -> list[dict]:
    """Latest fetch_log row per source (mirrors the bot's /health), sorted by source.

    Scans the most recent ``_FETCH_LOG_SCAN`` rows newest-first and keeps the first row
    seen per source (== its most recent attempt). Each source's CURRENT known state is a
    retrieved fact (its stored status/error/created_at), never inferred from whether the
    source's data happens to be present in the bundle (Law 7).
    """
    rows = (
        client.table("fetch_log")
        .select("source,status,error,created_at")
        .order("created_at", desc=True)
        .limit(_FETCH_LOG_SCAN)
        .execute()
        .data
        or []
    )
    latest: dict[str, dict] = {}
    for row in rows:
        source = row.get("source") or "(unknown)"
        latest.setdefault(source, row)  # first seen in desc order == most recent
    return [
        {
            "source": src,
            "status": latest[src].get("status"),
            "error": latest[src].get("error"),
            "at": latest[src].get("created_at"),
        }
        for src in sorted(latest)
    ]


def _prices_staleness(prices: dict[str, list[dict]], today: date) -> dict:
    """Freshness of prices_eod vs §12's >2-trading-day threshold (from stored dates, not presence)."""
    per_symbol = {sym: (rows[-1]["date"] if rows else None) for sym, rows in prices.items()}
    dated = [d for d in per_symbol.values() if d]
    latest = max(dated) if dated else None
    elapsed = None
    if latest:
        try:
            elapsed = _trading_days_elapsed(date.fromisoformat(latest), today)
        except ValueError:
            elapsed = None
    return {
        "latest_date": latest,
        "per_symbol": per_symbol,
        "trading_days_old": elapsed,
        "threshold_trading_days": _PRICES_STALE_TRADING_DAYS,
        # No price data at all, or freshest price older than the threshold -> stale (warn).
        "stale": latest is None or (elapsed is not None and elapsed > _PRICES_STALE_TRADING_DAYS),
    }


# The Flex sections that actually STORE portfolio data. A successful transport call
# (ibkr_flex:send / :get) is NOT delivery — IBKR returns the "statement generation in
# progress" (1019) body as an HTTP 200, so :get can succeed while no statement is stored.
# Freshness must key off a section that wrote a row, else a blind journal reads as fresh.
_FLEX_STORE_SECTIONS = ("ibkr_flex:positions", "ibkr_flex:trades", "ibkr_flex:cash")


def _flex_staleness(client, positions: dict, now: datetime) -> dict:
    """Hours since Flex last STORED data vs §12's >48h threshold (a blind journal == stale).

    Keys off the section-store successes (``_FLEX_STORE_SECTIONS``), not the transport
    calls: ``ibkr_flex:get`` returning 200 with an "in progress" body is not delivery
    (§12 critical: Flex blind = journal blind). No store success -> stale (warn).
    """
    rows = (
        client.table("fetch_log")
        .select("created_at")
        .in_("source", list(_FLEX_STORE_SECTIONS))
        .eq("status", "success")
        .order("created_at", desc=True)
        .limit(1)
        .execute()
        .data
        or []
    )
    last_at = _parse_ts(rows[0]["created_at"]) if rows else None
    hours_old = round((now - last_at).total_seconds() / 3600.0, 1) if last_at else None
    return {
        "last_success_at": rows[0]["created_at"] if rows else None,
        "hours_old": hours_old,
        "threshold_hours": _FLEX_STALE_HOURS,
        "positions_snapshot_date": (positions or {}).get("date"),
        # Never succeeded, or last success older than the threshold -> stale (warn).
        "stale": last_at is None or (hours_old is not None and hours_old > _FLEX_STALE_HOURS),
    }


def _flex_token_health(config: dict, today: date) -> dict:
    """Flex-token days-to-expiry from ``config.ibkr_token_expiry_date`` (§7 surfaces when <30).

    Mirrors the bot's /health expiry logic (``_flex_expiry_line``) so the digest and
    /health never disagree. The key is not among the Phase-0 seeded config rows yet, so
    ``known`` is False until it is set — rendered honestly, never guessed.
    """
    expiry = config.get("ibkr_token_expiry_date")
    exp_date = None
    if expiry:
        try:
            exp_date = date.fromisoformat(str(expiry)[:10])
        except ValueError:
            exp_date = None
    if exp_date is None:
        return {
            "expiry_date": None, "days_to_expiry": None, "expired": False,
            "known": False, "warn_below_days": _TOKEN_WARN_DAYS, "warn": False,
        }
    days = (exp_date - today).days
    return {
        "expiry_date": exp_date.isoformat(),
        "days_to_expiry": days,
        "expired": days < 0,
        "known": True,
        "warn_below_days": _TOKEN_WARN_DAYS,
        "warn": days < _TOKEN_WARN_DAYS,
    }


def _logical_source(label: str) -> str:
    """Collapse a granular fetch_log label to its logical §5 source (text before ':').

    Fetchers log per-ticker / per-series / per-section labels (``tiingo:TSLA``,
    ``fred:DFF``, ``ibkr_flex:positions``); the §7 health verdict is per logical source
    (``tiingo``, ``fred``, ``ibkr_flex``). A bare label with no ':' is its own source.
    """
    return label.split(":", 1)[0] if ":" in label else label


def _aggregate_sources(fetches: list[dict]) -> list[dict]:
    """Collapse granular labels to logical §5 sources, most-recent row winning per source.

    Most-recent-wins is deliberate: it retires superseded sub-labels (a one-off ``av``
    aggregate row from before the fetcher switched to ``av:TSLA`` no longer outvotes the
    fresher per-ticker success) and reflects Flex's true state (the section-store failure
    at 19:12 outranks the transport ``:get`` success at 19:08). Excludes ``pipeline:*``
    rows — pipeline STEP outcomes, not §5 data sources: they log only on failure, dupe the
    underlying source's status, and ``pipeline:telegram`` is an outbound push, not an input.
    The full granular history stays in ``fetches``; this is the deduped view the verdict uses.
    """
    winner: dict[str, dict] = {}
    for f in fetches:
        logical = _logical_source(f.get("source") or "(unknown)")
        if logical == "pipeline":
            continue
        cur = winner.get(logical)
        if cur is None or (f.get("at") or "") > (cur.get("at") or ""):
            winner[logical] = f
    return [
        {
            "source": logical,
            "status": winner[logical].get("status"),
            "at": winner[logical].get("at"),
            "error": winner[logical].get("error"),
        }
        for logical in sorted(winner)
    ]


def _source_health(client, prices: dict, positions: dict, config: dict, today: date) -> dict:
    """Assemble the digest Source-Health facts (blueprint §7 line 5 / §12, Law 7).

    Components the §7 line requires — none derivable from mere data-presence in the bundle
    (deriving "OK" from presence is the silent-failure-as-misinformation Law 7 forbids):

      * sources    — latest status per LOGICAL §5 source (the §7 "7/8 OK; Reddit timed
                     out." verdict view); ``fetches`` keeps the full granular audit.
      * staleness  — prices (>2 trading days) and Flex (>48h since last STORE), from
                     stored row DATES/timestamps, not from whether rows exist.
      * flex_token — days-to-expiry from config.ibkr_token_expiry_date (a credential
                     property entirely absent from the data bundle).

    ``summary``/``ok``/``total``/``failing`` are computed over the deduped logical
    ``sources`` — an aggregation of retrieved statuses (like cumulative_delta_shares),
    never a fabricated verdict.
    """
    now = datetime.now(timezone.utc)
    fetches = _latest_fetch_per_source(client)       # granular, full audit trail
    sources = _aggregate_sources(fetches)            # logical §5 sources, the verdict view
    ok = [s for s in sources if s.get("status") == "success"]
    failing = [
        {"source": s["source"], "status": s.get("status")}
        for s in sources
        if s.get("status") != "success"
    ]
    tail = "; ".join(f"{s['source']} {s['status']}" for s in failing)
    summary = f"{len(ok)}/{len(sources)} OK" + (f"; {tail}" if tail else "")
    return {
        "as_of": now.isoformat(),
        "summary": summary,
        "ok": len(ok),
        "total": len(sources),
        "failing": failing,
        "sources": sources,
        "fetches": fetches,
        "staleness": {
            "prices": _prices_staleness(prices, today),
            "flex": _flex_staleness(client, positions, now),
        },
        "flex_token": _flex_token_health(config, today),
    }


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
    # Computed once and reused: source_health derives prices/Flex staleness from these
    # same retrieved rows (and the Flex-token line from config) — no second round-trip.
    prices = _recent_prices(client)
    positions = _latest_positions(client)
    config = _config(client)
    return {
        "run_type": run_type,
        "generated_for": today.isoformat(),
        "prices": prices,
        "indicators": _latest_indicators(client),
        "macro": _latest_macro(client),
        "calendar": _upcoming_calendar(client, today),
        "headlines": _recent_headlines(client),
        "positions": positions,
        "round_trips": _round_trips(client, today),
        "config": config,
        "source_health": _source_health(client, prices, positions, config, today),
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
