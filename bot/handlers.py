"""Argus bot — instant command handlers (blueprint §2 item 10 / §3 / §7 / §9).

The handlers behind the Telegram commands ``/book /journal /skip /health /override
/pulse /analyze``. Each is a pure function ``(message: dict) -> str``: it reads (or
writes) the Supabase spine directly and returns a reply string. ``api.webhook`` owns
the actual ``send_message`` call, so handlers never touch the network except ``/pulse``
and ``/analyze``, whose job IS an outbound trigger (a GitHub Actions
``workflow_dispatch``).

Design notes tied to the live schema (schema is truth — the §4 migration):
  • Facts are retrieved, never generated (Law 2): every number rendered here comes
    from a stored row; nothing is computed from assumptions the DB doesn't hold.
  • Information, never instruction (Law 1): these views report state — allocation,
    Δshares, checkpoint distance — and never say buy / sell / safe-to-trade.
  • ``round_trips`` is the sleeve unit (a sell→rebuy *pair*); it has ``date`` (not
    ``exec_time``) and no per-row ``side`` — the journal renders ``symbol`` instead.
  • Identifiers that contain Markdown specials (``override_type`` / ``skip`` reason /
    ``fetch_log`` source names all carry ``_``) are wrapped in a backtick code span so
    Telegram's legacy Markdown parser does not choke on an unbalanced underscore.
"""

from __future__ import annotations

import os
import re
from datetime import date, datetime, timezone
from typing import Any, NamedTuple

import httpx
from dotenv import load_dotenv
from postgrest.exceptions import APIError

from shared.db import get_client
from shared.event_filter import (
    EVENT_FILTER_RULE_ACTIVE,
    EVENT_FILTER_RULE_FORWARD,
    FILTERED_EVENT_TYPES,
    event_filter_phrase,
    triggers_event_filter,
)
from shared.sources import is_non_data_source
from siglab.engine import vix_percentile_asof
from siglab.job import read_ledger
from siglab.ledger import compute_stats
from siglab.registry import load_signal
from siglab.render import (
    render_signal_full,
    render_signal_full_pending,
    render_signal_line,
    render_signal_today,
    render_signal_today_pending,
)

# --- config-driven constants, with documented Phase-0 fallbacks ----------------- #
# The $100K goal (§0 / §13). Read from config.target_usd when present so it stays a
# tunable JSONB row (§2 item 3), not a hardcoded constant; default to the blueprint
# figure while config is unseeded — a fixed goal a default stands in for safely (unlike
# sleeve_shares, whose absence now means "no active sleeve", not a default-able value).
_DEFAULT_TARGET_USD = 100_000.0
# Pre-registered journal gates (§9). Extracted from config.kill_criteria when seeded.
_DEFAULT_CHECKPOINTS: tuple[int, ...] = (10, 20, 50)
# Weekly round-trip cap (§8) fallback when config.weekly_trade_cap is unseeded — matches
# ingestion/seed_config.py and bot/event_filter_check.py so the /today count never drifts.
_DEFAULT_WEEKLY_CAP = 2
# The indicator names /today reads (canonical set from ingestion/indicators.py).
_TODAY_INDICATORS: tuple[str, ...] = ("sma50", "sma200", "rsi14", "macd", "macd_signal")

# /skip reasons — must match the skip_log.reason CHECK constraint (§4 table 14).
_SKIP_REASONS: tuple[str, ...] = ("event_filter", "discretion", "other")
# The sleeve trades a single ticker (§8); /felt stamps it onto the pending annotation. The
# symbol is a config row (config.sleeve_symbol), resolved at runtime by _sleeve_symbol below
# — never a hardcoded constant, and never defaulted (a guessed ticker is a corrupt note, L6).
# /felt vocabulary fallbacks (§8 Step 4) — used when config.annotation_* is unseeded; the live
# lists are config rows (grow by edit, not migration), mirroring the _DEFAULT_* convention. Kept
# in sync with ingestion/seed_config.py so an unseeded fallback never shows stale words.
_DEFAULT_REASONS: tuple[str, ...] = ("momentum", "setup", "catalyst", "reversion", "gut feel")
_DEFAULT_FEELINGS: tuple[str, ...] = ("calm", "confident", "anxious", "scared", "fomo", "greedy")
# /override types — must match the transactions.override_type CHECK (§4 table 10).
_OVERRIDE_TYPES: tuple[str, ...] = (
    "round_trip_sell",
    "round_trip_rebuy",
    "dca_buy",
    "dca_sell",
    "unclassified",
)

# /pulse → workflow_dispatch on the digest workflow (blueprint §3 / §11).
_GITHUB_API_BASE = "https://api.github.com"
_PULSE_WORKFLOW = "digest.yml"
_HTTP_TIMEOUT_SECONDS = 30.0

# /analyze → workflow_dispatch on the Phase-5 dossier workflow (module spec §4).
_ANALYZE_WORKFLOW = "analyze.yml"
# Ticker SHAPE check only (1-5 alphanumerics, optional .X/-X class suffix, must start
# with a letter). Existence is deliberately NOT checked here — that is the pipeline's
# job, where an unknown symbol degrades to a reduced-depth dossier that names its
# gaps (Law 2) instead of a webhook-side guess. Shape-gating still keeps garbage
# (and anything shell-hostile) out of the dispatch payload.
_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9]{0,4}(?:[.-][A-Z0-9]{1,2})?$")

# /health scan window: fetch_log grows by ~15-30 rows/day, so the latest row for
# every source comfortably falls inside the most recent 1000 rows (weeks of runs).
_FETCH_LOG_SCAN = 1000
_STATUS_MARK = {"success": "✓", "failure": "✗", "timeout": "⌛", "unavailable": "∅"}


# --------------------------------------------------------------------------- #
# Reply contract
# --------------------------------------------------------------------------- #
class Reply(NamedTuple):
    """A handler result that carries an inline keyboard alongside its text.

    Handlers may return a plain ``str`` (text-only, every command but /felt) OR a ``Reply``
    (text + ``reply_markup``, the /felt button flow). ``api.webhook`` normalises both: for a
    typed command it ``sendMessage``s, for a button tap it ``editMessageText``s in place. A
    plain ``str`` is exactly the old behaviour — existing handlers are unchanged.
    """

    text: str
    reply_markup: dict | None = None


# --------------------------------------------------------------------------- #
# Small shared helpers
# --------------------------------------------------------------------------- #
def _utc_today() -> date:
    """Today's date in UTC (all Argus date logic is UTC; §3 / §12)."""
    return datetime.now(timezone.utc).date()


def _to_float(value: object) -> float | None:
    """Coerce a numeric cell (PostgREST may return numeric as str) to float, else None."""
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _money(value: float | None) -> str:
    """Format a USD amount as ``$1,234.56``; None → ``n/a`` (Law 2: absence is shown)."""
    return "n/a" if value is None else f"${value:,.2f}"


def _signed(value: float | None, places: int = 2) -> str:
    """Format a signed number as ``+0.06`` / ``-0.06``; None → ``n/a``."""
    return "n/a" if value is None else f"{value:+.{places}f}"


def _pct(part: float, whole: float) -> str:
    """Format ``part/whole`` as a percentage; undefined (whole == 0) → ``n/a``."""
    return "n/a" if not whole else f"{100.0 * part / whole:.1f}%"


def _code(text: object) -> str:
    """Wrap an identifier in a backtick code span so Markdown specials don't parse.

    Telegram legacy Markdown treats a lone ``_`` / ``*`` as an entity delimiter; an
    unbalanced one returns HTTP 400. Identifiers like ``round_trip_sell`` or
    ``ibkr_flex:trades`` carry underscores, so they are rendered as inline code.
    """
    return f"`{text}`"


def _short_ts(value: str | None) -> str:
    """Render an ISO timestamptz as ``YYYY-MM-DD HH:MM``; None → ``n/a``."""
    if not value:
        return "n/a"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value[:16]
    return parsed.strftime("%Y-%m-%d %H:%M")


def _month_bounds(today: date) -> tuple[str, str]:
    """Return ``(first_of_month, first_of_next_month)`` ISO dates for a half-open range."""
    first = today.replace(day=1)
    next_first = (
        first.replace(year=first.year + 1, month=1)
        if first.month == 12
        else first.replace(month=first.month + 1)
    )
    return first.isoformat(), next_first.isoformat()


def _load_config() -> dict[str, Any]:
    """Load all ``config`` rows into a ``{key: value}`` dict (values are native JSONB)."""
    resp = get_client().table("config").select("key,value").execute()
    return {row["key"]: row["value"] for row in (resp.data or [])}


def _sleeve_symbol(config: dict[str, Any]) -> str | None:
    """The single sleeve ticker from ``config.sleeve_symbol`` (§8), or None if unset/invalid.

    There is deliberately NO default: the caller (``handle_felt``) must fail loud on None and
    refuse to stage a note, never fall back to a guessed ticker — a wrong symbol is a corrupt
    journal row (L6), strictly worse than the explicit constant this replaced. (``sleeve_shares``
    is likewise undefaulted now, but its absence is the benign "no active sleeve" state; a
    missing symbol is never benign.)
    """
    symbol = config.get("sleeve_symbol")
    return symbol if isinstance(symbol, str) and symbol.strip() else None


def _checkpoints(config: dict[str, Any]) -> list[int]:
    """Pre-registered gate trade-counts from ``config.kill_criteria`` (§9); else default.

    kill_criteria is ``{"early_warning":{"trade":10,...},"checkpoint":{"trade":20,...},
    "verdict":{"trade":50,...}}`` — the trade numbers are the checkpoints (§4 / §9).
    """
    kill = config.get("kill_criteria") or {}
    points = sorted(
        int(rule["trade"])
        for rule in kill.values()
        if isinstance(rule, dict) and isinstance(rule.get("trade"), (int, float))
    )
    return points or list(_DEFAULT_CHECKPOINTS)


def _next_checkpoint(n_trades: int, checkpoints: list[int]) -> int | None:
    """First checkpoint strictly after ``n_trades``; None once all are passed."""
    return next((point for point in checkpoints if point > n_trades), None)


# --------------------------------------------------------------------------- #
# /book — allocation, distance to target, sleeve status, DCA, concentration
# --------------------------------------------------------------------------- #
def handle_book(message: dict) -> str:
    """Render the 'Your Book' view (blueprint §7 §4): allocation, target gap, sleeve.

    Reads the latest ``positions_snapshot`` (max date), this month's ``contributions``,
    all ``round_trips`` (cumulative sleeve Δshares + checkpoint distance), and ``config``
    (phase, kill_criteria, target). Information only (Law 1); every figure is a stored
    fact (Law 2). ``message`` is unused — ``/book`` takes no arguments.
    """
    client = get_client()
    config = _load_config()

    latest = (
        client.table("positions_snapshot")
        .select("date")
        .order("date", desc=True)
        .limit(1)
        .execute()
    )
    if not latest.data:
        return "*Your Book*\n\nNo positions snapshot yet — awaiting the first IBKR Flex pull."

    snap_date = latest.data[0]["date"]
    positions = (
        client.table("positions_snapshot")
        .select("symbol,qty,market_value,cost_basis")
        .eq("date", snap_date)
        .execute()
        .data
    )
    by_symbol = {row["symbol"]: row for row in positions}
    total_mv = sum((_to_float(row.get("market_value")) or 0.0) for row in positions)

    target = _to_float(config.get("target_usd")) or _DEFAULT_TARGET_USD
    distance = target - total_mv

    month_start, month_end = _month_bounds(_utc_today())
    contribs = (
        client.table("contributions")
        .select("amount")
        .gte("date", month_start)
        .lt("date", month_end)
        .execute()
        .data
    )
    contrib_total = sum((_to_float(row.get("amount")) or 0.0) for row in contribs)

    trips = client.table("round_trips").select("delta_shares").execute().data
    n_trades = len(trips)
    cum_delta = sum((_to_float(row.get("delta_shares")) or 0.0) for row in trips)
    phase = config.get("phase", "?")
    next_cp = _next_checkpoint(n_trades, _checkpoints(config))

    musk_mv = sum((_to_float(by_symbol.get(s, {}).get("market_value")) or 0.0) for s in ("TSLA", "SPCX"))

    lines = [f"*Your Book* — snapshot {snap_date}", "", "*Allocation*"]
    for symbol in sorted(by_symbol):
        mv = _to_float(by_symbol[symbol].get("market_value")) or 0.0
        lines.append(f"• {symbol}: {_money(mv)}  ({_pct(mv, total_mv)})")
    lines.append(f"• *Total*: {_money(total_mv)}")
    lines.append("")
    lines.append(f"*To target:* {_money(distance)} to go  (target {_money(target)})")
    lines.append("")
    lines.append(f"*Sleeve* — phase {phase}")
    lines.append(f"• Cumulative Δshares: {_signed(cum_delta)}  over {n_trades} round trip(s)")
    if next_cp is not None:
        lines.append(f"• Next checkpoint at trade {next_cp} — {next_cp - n_trades} to go")
    else:
        lines.append("• All pre-registered checkpoints passed")
    lines.append("")
    lines.append(f"*DCA this month:* {_money(contrib_total)}  ({len(contribs)} deposit(s))")
    lines.append("")
    lines.append(f"Concentration: TSLA+SPCX ≈ {_pct(musk_mv, total_mv)} of book — one Musk factor.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# /journal — cumulative Δshares, last 10 round trips, checkpoint proximity
# --------------------------------------------------------------------------- #
def handle_journal(message: dict) -> str:
    """Render the journal view (blueprint §9): the verdict is sleeve-only Δshares.

    Reads all ``round_trips`` (ordered by date — the schema has no ``exec_time``),
    ``trade_annotations`` (reason / feeling / confidence for the last 10), and ``config``
    (phase, kill_criteria). ``message`` is unused — ``/journal`` takes no arguments.
    """
    client = get_client()
    config = _load_config()

    trips = (
        client.table("round_trips")
        .select("id,date,symbol,pnl_usd,delta_shares")
        .order("date", desc=False)
        .order("id", desc=False)
        .execute()
        .data
    )
    n_trades = len(trips)
    cum_delta = sum((_to_float(row.get("delta_shares")) or 0.0) for row in trips)
    phase = config.get("phase", "?")
    checkpoints = _checkpoints(config)
    next_cp = _next_checkpoint(n_trades, checkpoints)

    recent = trips[-10:]
    meta_by_trip: dict[int, dict] = {}
    if recent:
        annotations = (
            client.table("trade_annotations")
            .select("round_trip_id,confidence_1to5,reason,feeling")
            .in_("round_trip_id", [row["id"] for row in recent])
            .execute()
            .data
        )
        for row in annotations:
            meta_by_trip[row["round_trip_id"]] = row

    lines = [f"*Journal* — phase {phase}", ""]
    lines.append(
        f"*Cumulative sleeve Δshares:* {_signed(cum_delta)}  over {n_trades} round trip(s)"
    )
    lines.append("")
    if recent:
        lines.append("*Last 10 round trips*")
        for row in reversed(recent):  # most recent first
            meta = meta_by_trip.get(row["id"]) or {}
            # reason/feeling are free text — code-span them so a Markdown special can't 400 the send.
            tag = "/".join(_code(p) for p in (meta.get("reason"), meta.get("feeling")) if p)
            tag_str = f" · {tag}" if tag else ""
            conf = meta.get("confidence_1to5")
            conf_str = f" · conf {conf}/5" if conf is not None else ""
            lines.append(
                f"• {row['date']} {row['symbol']}: "
                f"P&L {_money(_to_float(row.get('pnl_usd')))} · Δ {_signed(_to_float(row.get('delta_shares')))}{tag_str}{conf_str}"
            )
    else:
        lines.append("No round trips recorded yet.")
    lines.append("")
    if next_cp is not None:
        lines.append(
            f"*Checkpoint:* trade {n_trades} of {next_cp} — "
            f"{next_cp - n_trades} to go. Sleeve Δshares: {_signed(cum_delta)}."
        )
    else:
        lines.append(f"*Checkpoint:* all passed (≥ {checkpoints[-1]} trades).")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# /today — deterministic trade-context card (blueprint §7 / §8; mapper-v2 sibling)
#
# A pure DB render — NO LLM, so it is grounding-exempt by construction (every figure is
# a stored row, Law 2, with nothing synthesized). Law 1 still binds hard: the card
# DESCRIBES state (trend, momentum, event-filter, cap, sleeve) and never advises — no
# good/bad-day, no should/could-trade, no buy/sell/enter/exit language. That exclusion
# is pinned by tests/test_today_handler.py, the same way the Law-1 lint guards the
# dossier. Plain-language vocabulary mirrors the dossier brief (terms glossed inline).
# --------------------------------------------------------------------------- #
def _week_bounds(today: date) -> tuple[str, str]:
    """(Monday, Sunday) ISO dates of the calendar week containing ``today`` (UTC).

    Mirrors ``bot.event_filter_check._week_bounds_utc`` so the /today weekly-cap count
    and the morning-warning cap count use the identical window (no drift).
    """
    from datetime import timedelta

    monday = today - timedelta(days=today.weekday())
    return monday.isoformat(), (monday + timedelta(days=6)).isoformat()


def _latest_close(client, symbol: str) -> tuple[str | None, float | None]:
    """(date, close) of a symbol's most recent ``prices_eod`` row, or (None, None)."""
    rows = (
        client.table("prices_eod").select("date,close")
        .eq("symbol", symbol).order("date", desc=True).limit(1).execute().data or []
    )
    if not rows:
        return None, None
    return rows[0].get("date"), _to_float(rows[0].get("close"))


def _latest_indicators(client, symbol: str) -> dict[str, float | None]:
    """Latest value per indicator name for a symbol (date-desc, first-seen wins).

    Young tickers are sparse by design (§4): a name simply absent from the map means
    'not enough history yet', which the render states rather than fabricating a value.
    """
    rows = (
        client.table("indicators").select("date,name,value")
        .eq("symbol", symbol).in_("name", list(_TODAY_INDICATORS))
        .order("date", desc=True).limit(60).execute().data or []
    )
    out: dict[str, float | None] = {}
    for row in rows:  # date-desc → the first row seen for a name is its latest value
        name = row.get("name")
        if name and name not in out:
            out[name] = _to_float(row.get("value"))
    return out


def _trend_phrase(close: float | None, sma50: float | None, sma200: float | None) -> str:
    """Price vs its 50-/200-day average price, in plain relational words (categorical).

    Purely descriptive (Law 1) — states where price sits relative to its averages, never
    'uptrend'/'bullish'/a call. A bounded, anchored comparison (memory: bounded-
    categorical is allowed; unbounded-magnitude is not)."""
    if close is None:
        return "price not available"
    above, below = [], []
    for label, ref in (("50-day", sma50), ("200-day", sma200)):
        if ref is None:
            continue
        (above if close >= ref else below).append(label)
    if not above and not below:
        return "no moving-average history yet (young listing)"
    if above and below:
        return (
            f"trading above its {' and '.join(above)} but below its "
            f"{' and '.join(below)} average price"
        )
    if above:
        return f"trading above its {' and '.join(above)} average price"
    return f"trading below its {' and '.join(below)} average price"


def _momentum_phrase(rsi: float | None, macd: float | None, signal: float | None) -> str:
    """RSI (glossed 'momentum score, 50 neutral') + MACD-vs-signal as improving/fading."""
    bits: list[str] = []
    if rsi is not None:
        bits.append(f"momentum score {rsi:.0f} (50 is neutral)")
    if macd is not None and signal is not None:
        direction = "improving" if macd >= signal else "fading"
        bits.append(f"trend momentum {direction} (MACD vs its signal line)")
    return "; ".join(bits) if bits else "momentum not yet available"


def _event_filter_line(client, today: date) -> str:
    """The §8 event-filter state: the nearest arming event + whether it is IN EFFECT.

    Reads the forward calendar and applies the SHARED arm decision
    (``shared.event_filter``), so /today, the digest and the morning push cannot
    disagree on what arms the 24h no-round-trip window."""
    rows = (
        client.table("calendar_events").select("date,type,symbol,materiality")
        .gte("date", today.isoformat()).in_("type", list(FILTERED_EVENT_TYPES))
        .order("date").limit(20).execute().data or []
    )
    arming = [row for row in rows if triggers_event_filter(row)]
    if not arming:
        return "not active — no arming event on the forward calendar."
    event = arming[0]
    label = str(event.get("type") or "").upper()
    sym = f" ({event['symbol']})" if event.get("symbol") else ""
    phrase = event_filter_phrase(event.get("date"), today.isoformat())
    if phrase == EVENT_FILTER_RULE_ACTIVE:
        return f"IN EFFECT — {label}{sym} within 24h; sleeve round trips blocked (§8)."
    return f"armed — {label}{sym} on {event.get('date')}; {EVENT_FILTER_RULE_FORWARD}."


def _sleeve_status_line(config: dict[str, Any]) -> str:
    """Sleeve registration status from ``config.sleeve_shares`` (absent = no active sleeve)."""
    shares = config.get("sleeve_shares")
    if isinstance(shares, (int, float)) and not isinstance(shares, bool):
        return f"*Sleeve:* registered — {int(shares)} shares (frozen unit)."
    return "*Sleeve:* not yet registered — no active sleeve."


def _signal_stats(client):
    """(stats, blob) from the persisted ledger, or (None, blob) when absent/pending.

    Degrades gracefully: a missing ``signal_ledger`` table (pre-migration) or an empty
    ledger (pre-backfill) returns None stats, and the caller renders a labelled
    'backfill pending' line — the 🧪 experiment label always shows."""
    blob = load_signal(client)
    try:
        rows = read_ledger(client)
    except Exception:  # noqa: BLE001 — table missing pre-migration; render pending, never crash
        rows = []
    return (compute_stats(rows, blob) if rows else None), blob


# --------------------------------------------------------------------------- #
# /today v2 — the default one-glance card (deterministic, no LLM)
#
# Six lines a human reads at a glance: overall Conditions (CALM/NORMAL/STORMY), the
# sleeve ticker's one-word direction, the market benchmarks collapsed to one line, the
# young listing, the next blocking event, and the labelled experimental signal. The
# detailed workings live behind `/today full` (`_render_full_card`). Law 1 still binds:
# every line DESCRIBES state and never advises — pinned by tests/test_today_handler.py.
# --------------------------------------------------------------------------- #

# The tech benchmark in the "Market overall" collapse; everything else on the market line
# is treated as the broad market (single-user watchlist is TSLA/SPCX/SPY/QQQ).
_TECH_INDEX = "QQQ"
_DIR_STRENGTH = {"UP": 2, "MIXED": 1, "DOWN": 0}

# Plain-language names for the arming event types (never the §8/materiality jargon; the
# card speaks in human terms). Keys mirror shared.event_filter.FILTERED_EVENT_TYPES.
_FRIENDLY_EVENT = {
    "fomc": "Fed decision",
    "cpi": "inflation report (CPI)",
    "nfp": "jobs report",
    "earnings": "earnings",
    "lockup": "lock-up expiry",
    "index": "index rebalance",
}


def _direction(
    close: float | None, sma50: float | None, sma200: float | None,
    macd: float | None, signal: float | None,
) -> tuple[str, str]:
    """(word, reason) — a deterministic one-word trend+push read (Law 1: describes only).

    UP iff price is above BOTH moving averages AND momentum is improving; DOWN iff below
    BOTH and momentum is fading; else MIXED. The reason is plain — no RSI number, no
    "MACD", no "momentum score" (those stay on `/today full`)."""
    refs = [r for r in (sma50, sma200) if r is not None]
    if not refs or close is None:
        trend_part, trend_up, trend_down = "no trend lines yet", False, False
    else:
        trend_up = all(close >= r for r in refs)
        trend_down = all(close < r for r in refs)
        trend_part = (
            "above its trend lines" if trend_up
            else "below its trend lines" if trend_down
            else "between its trend lines"
        )
    if macd is not None and signal is not None:
        push_up: bool | None = macd >= signal
        push_part = "push strengthening" if push_up else "push weakening"
    else:
        push_up, push_part = None, "push unclear"

    if trend_up and push_up:
        return "UP", f"{trend_part}, {push_part}"
    if trend_down and push_up is False:
        return "DOWN", f"{trend_part}, {push_part}"
    return "MIXED", f"{trend_part}, {push_part}"


def _direction_line(symbol: str, close: float | None, ind: dict) -> str:
    """The sleeve ticker's one-glance line: ``*TSLA*: pointing DOWN (…)`` / mixed picture."""
    word, reason = _direction(
        close, ind.get("sma50"), ind.get("sma200"), ind.get("macd"), ind.get("macd_signal")
    )
    if word == "MIXED":
        return f"*{symbol}*: a mixed picture ({reason})."
    return f"*{symbol}*: pointing {word} ({reason})."


def _market_line(dirs: dict[str, str]) -> str | None:
    """Collapse the market benchmarks (SPY/QQQ) into ONE line; None if none present.

    Both same → ``pointing UP`` / ``pointing DOWN`` / ``mixed``. Diverging → the broad
    market leads and the tech leg is qualified (``pointing up, tech wobbling``)."""
    if not dirs:
        return None
    if len(set(dirs.values())) == 1:
        word = next(iter(dirs.values()))
        return "*Market overall*: mixed." if word == "MIXED" else f"*Market overall*: pointing {word}."
    broad = dirs.get("SPY")
    tech = dirs.get(_TECH_INDEX)
    if broad is None or tech is None:  # no clean broad/tech split — state each plainly
        parts = ", ".join(f"{s} {w.lower()}" for s, w in sorted(dirs.items()))
        return f"*Market overall*: {parts}."
    broad_plain = {"UP": "pointing up", "DOWN": "pointing down", "MIXED": "mixed"}[broad]
    if _DIR_STRENGTH[tech] < _DIR_STRENGTH[broad]:
        qualifier = "tech wobbling"
    elif _DIR_STRENGTH[tech] > _DIR_STRENGTH[broad]:
        qualifier = "tech out in front"
    else:
        qualifier = "tech in step"
    return f"*Market overall*: {broad_plain}, {qualifier}."


def _young_line(symbol: str, ind: dict) -> str:
    """A young listing's plain line — no trend lines yet, just the recent push (Law 1)."""
    macd, signal, rsi = ind.get("macd"), ind.get("macd_signal"), ind.get("rsi14")
    if macd is not None and signal is not None:
        push = "strong" if macd >= signal else "soft"
    elif rsi is not None:
        push = "strong" if rsi >= 50 else "soft"
    else:
        return f"*{symbol}*: too young for trend lines; still building history."
    return f"*{symbol}*: too young for trend lines; recent push {push}."


def _nearest_arming_event(client, today: date) -> dict | None:
    """The nearest forward calendar event that arms the sleeve block, or None."""
    rows = (
        client.table("calendar_events").select("date,type,symbol,materiality")
        .gte("date", today.isoformat()).in_("type", list(FILTERED_EVENT_TYPES))
        .order("date").limit(20).execute().data or []
    )
    arming = [row for row in rows if triggers_event_filter(row)]
    return arming[0] if arming else None


def _friendly_event(event: dict) -> str:
    """A human name for an arming event (never '§8'/'materiality')."""
    kind = str(event.get("type") or "").lower()
    if kind == "earnings" and event.get("symbol"):
        return f"{event['symbol']} earnings"
    return _FRIENDLY_EVENT.get(kind, kind.upper() or "event")


def _sessions_until(event_date: object, today: date) -> int | None:
    """Trading sessions (weekday proxy) from today to ``event_date``; None if past/unparseable.

    A weekday count is the same date-granularity approximation the §8 24h window uses
    (shared.event_filter documents it): close enough for a 'within 2 sessions' threshold,
    and honest about not modelling market holidays."""
    from datetime import timedelta

    try:
        ed = date.fromisoformat(str(event_date)[:10])
    except (TypeError, ValueError):
        return None
    if ed < today:
        return None
    sessions, cursor = 0, today
    while cursor < ed:
        cursor += timedelta(days=1)
        if cursor.weekday() < 5:
            sessions += 1
    return sessions


def _when_phrase(event_date: object, today: date) -> str:
    """'today' / 'tomorrow' / 'in N days' for an event date (calendar days, for readability)."""
    try:
        days = (date.fromisoformat(str(event_date)[:10]) - today).days
    except (TypeError, ValueError):
        return "soon"
    if days <= 0:
        return "today"
    if days == 1:
        return "tomorrow"
    return f"in {days} days"


def _event_line_v2(event: dict | None, sessions: int | None, today: date) -> str:
    """The default card's event line: a ⛔ block warning within 2 sessions, else the next event.

    Speaks in human terms (Law 1 — describes the rule's effect, never '§8'/'armed'/
    'materiality'). Within 2 sessions the card states that the pre-registered event-filter
    rule blocks sleeve round trips today; otherwise it names the next event that matters."""
    if event is None:
        return "No blocking events on your calendar right now."
    friendly = _friendly_event(event)
    if sessions is not None and sessions <= 2:
        return f"⛔ {friendly} {_when_phrase(event.get('date'), today)} — your rules block sleeve round trips today."
    try:
        days = (date.fromisoformat(str(event.get("date"))[:10]) - today).days
        day_str = f" ({days} days)"
    except (TypeError, ValueError):
        day_str = ""
    return f"Next event that matters: {friendly}, {event.get('date')}{day_str}."


def _vix_percentile_today(client, today: date) -> float | None:
    """The fear gauge (VIX) percentile within its trailing ~1y window, as of today.

    Reuses ``siglab.engine.vix_percentile_asof`` so the /today conditions read and the
    Signal Lab rule share ONE percentile definition (no drift). None if VIX is unseeded."""
    rows = (
        client.table("macro_series").select("date,value")
        .eq("series_id", "VIXCLS").order("date", desc=True).limit(252).execute().data or []
    )
    asc = sorted(
        ({"date": r.get("date"), "value": _to_float(r.get("value"))} for r in rows),
        key=lambda r: str(r["date"]),
    )
    return vix_percentile_asof(asc, today.isoformat(), 252)


def _conditions_line(event_within_2: bool, vix_pct: float | None) -> str:
    """The top-of-card weather read: STORMY / CALM / NORMAL + a plain reason (Law 1).

    STORMY if a blocking event is within 2 sessions OR the fear gauge is high (≥ 80th
    pct); CALM if neither event nor elevated fear AND the gauge is low (≤ 40th pct);
    else NORMAL. Purely descriptive — the card names the weather, never a course of action."""
    high_fear = vix_pct is not None and vix_pct >= 80
    low_fear = vix_pct is not None and vix_pct <= 40
    if event_within_2 or high_fear:
        bits = []
        if event_within_2:
            bits.append("a blocking event is close")
        if high_fear:
            bits.append(f"the fear gauge is high (≈{vix_pct:.0f}th percentile of its year)")
        return f"*Conditions: STORMY* — {'; '.join(bits)}."
    if not event_within_2 and low_fear:
        return (
            f"*Conditions: CALM* — no blocking events near and the fear gauge is low "
            f"(≈{vix_pct:.0f}th percentile of its year)."
        )
    gauge = (
        f" — fear gauge mid-range (≈{vix_pct:.0f}th percentile of its year)"
        if vix_pct is not None else ""
    )
    return f"*Conditions: NORMAL* — nothing unusual{gauge}."


def _render_default_card(client, config: dict[str, Any], today: date) -> str:
    """The default one-glance card: conditions, sleeve direction, market, young, event, signal."""
    watchlist = [s for s in (config.get("watchlist") or []) if isinstance(s, str)]
    primary = _sleeve_symbol(config)

    data: dict[str, tuple[float | None, dict]] = {}
    for symbol in watchlist:
        _pdate, close = _latest_close(client, symbol)
        data[symbol] = (close, _latest_indicators(client, symbol))

    young = [s for s in watchlist if data[s][1].get("sma200") is None]  # no 200-day history yet
    event = _nearest_arming_event(client, today)
    sessions = _sessions_until(event.get("date"), today) if event else None
    event_within_2 = event is not None and sessions is not None and sessions <= 2
    vix_pct = _vix_percentile_today(client, today)

    lines = [f"*Today* — {today.isoformat()} (UTC)", "", _conditions_line(event_within_2, vix_pct), ""]

    if primary and primary in data:
        close, ind = data[primary]
        lines.append(_direction_line(primary, close, ind))

    market_syms = [s for s in watchlist if s != primary and s not in young]
    dirs = {
        s: _direction(
            data[s][0], data[s][1].get("sma50"), data[s][1].get("sma200"),
            data[s][1].get("macd"), data[s][1].get("macd_signal"),
        )[0]
        for s in market_syms
    }
    market_line = _market_line(dirs)
    if market_line:
        lines.append(market_line)

    for symbol in young:
        lines.append(_young_line(symbol, data[symbol][1]))

    lines.append("")
    lines.append(_event_line_v2(event, sessions, today))

    stats, blob = _signal_stats(client)
    lines.append("")
    lines.append(render_signal_today(stats) if stats else render_signal_today_pending(blob))
    return "\n".join(lines)


def _render_full_card(client, config: dict[str, Any], today: date) -> str:
    """The detailed card (`/today full`) — the workings: per-ticker trend+momentum, cap, sleeve."""
    watchlist = [s for s in (config.get("watchlist") or []) if isinstance(s, str)]

    lines = [f"*Today* — {today.isoformat()} (UTC)", "", "*Watchlist*"]
    if not watchlist:
        lines.append("• (no watchlist configured)")
    for symbol in watchlist:
        pdate, close = _latest_close(client, symbol)
        ind = _latest_indicators(client, symbol)
        trend = _trend_phrase(close, ind.get("sma50"), ind.get("sma200"))
        momentum = _momentum_phrase(ind.get("rsi14"), ind.get("macd"), ind.get("macd_signal"))
        asof = f" (as of {pdate})" if pdate else ""
        lines.append(f"• *{symbol}*{asof}: {trend}. {momentum}.")

    lines.append("")
    lines.append("*Event filter (§8)*")
    lines.append(_event_filter_line(client, today))

    monday, sunday = _week_bounds(today)
    trips = (
        client.table("round_trips").select("id")
        .gte("date", monday).lte("date", sunday).execute().data or []
    )
    cap_val = config.get("weekly_trade_cap")
    cap = int(cap_val) if isinstance(cap_val, (int, float)) and not isinstance(cap_val, bool) else _DEFAULT_WEEKLY_CAP
    lines.append("")
    lines.append(f"*This week:* {len(trips)}/{cap} round trips (weekly cap).")
    lines.append(_sleeve_status_line(config))

    # Signal Lab (Law 1 Amendment #2): the labelled experimental signal line.
    stats, blob = _signal_stats(client)
    lines.append("")
    lines.append(render_signal_line(stats) if stats else render_signal_today_pending(blob))
    return "\n".join(lines)


def handle_today(message: dict) -> str:
    """Render the 'Today' card. Default = the one-glance six-line card; ``/today full`` = detail.

    Both are deterministic DB renders (no LLM, grounding-exempt by construction) and both
    DESCRIBE state and never advise (Law 1, pinned by tests/test_today_handler.py). The
    default is the at-a-glance read; ``/today full`` shows the workings (per-ticker
    trend+momentum, the weekly cap, sleeve registration)."""
    client = get_client()
    config = _load_config()
    today = _utc_today()
    tokens = (message.get("text") or "").split()[1:]
    if tokens and tokens[0].lower() == "full":
        return _render_full_card(client, config, today)
    return _render_default_card(client, config, today)


# --------------------------------------------------------------------------- #
# /signal — full Signal Lab ledger stats on demand (Law 1 Amendment #2)
# --------------------------------------------------------------------------- #
def handle_signal(message: dict) -> str:
    """Render the full Signal Lab ledger (rule, record, gate progress). ``message`` unused.

    Reads the persisted ledger; a labelled 'backfill pending' line when it is empty. Pure
    information — the render describes the experiment's condition and record and never
    advises (the same hard no-advice rule as /today, tested in test_signal_render.py)."""
    client = get_client()
    stats, blob = _signal_stats(client)
    if not stats:
        return "*Signal Lab*\n\n" + render_signal_full_pending(blob)
    return render_signal_full(stats)


# --------------------------------------------------------------------------- #
# /skip — log a skipped trade with a reason (Law 6: skips are logged, never lost)
# --------------------------------------------------------------------------- #
def handle_skip(message: dict) -> str:
    """Log a skipped trade to ``skip_log`` (blueprint §8 / §9, Law 6).

    Parses the text after ``/skip``: a leading ``event_filter`` / ``discretion`` /
    ``other`` token sets the reason (default ``other``); any remaining words are kept
    as ``notes``. When the first token is not a known reason, the whole remainder is
    treated as free-text notes under reason ``other``.
    """
    tokens = (message.get("text") or "").split()[1:]  # drop the '/skip' word
    reason = "other"
    notes: str | None = None
    if tokens:
        if tokens[0].lower() in _SKIP_REASONS:
            reason = tokens[0].lower()
            notes = " ".join(tokens[1:]) or None
        else:
            notes = " ".join(tokens)

    get_client().table("skip_log").insert(
        {"date": _utc_today().isoformat(), "reason": reason, "notes": notes}
    ).execute()
    return f"Skip logged ✓ ({_code(reason)})"


# --------------------------------------------------------------------------- #
# /felt — stage an in-the-moment trade annotation (reason / feeling / confidence, §8 Step 4)
# --------------------------------------------------------------------------- #
def _annotation_vocab(config: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Allowed (reasons, feelings) from config; fall back to the drafts when unseeded (§8)."""
    reasons = config.get("annotation_reasons") or list(_DEFAULT_REASONS)
    feelings = config.get("annotation_feelings") or list(_DEFAULT_FEELINGS)
    return [str(r).lower() for r in reasons], [str(f).lower() for f in feelings]


def _annotation_summary(a: dict) -> str:
    """Render an annotation as ``setup · calm · conf 4/5`` (conf omitted when absent).

    reason / feeling are free text whose vocabulary grows by config edit; they are wrapped in a
    backtick code span (like every other identifier here) so a value carrying a Markdown special
    (``_`` / ``*``) can't produce an unbalanced entity and 400 the Telegram send (see ``_code``).
    """
    label = " · ".join(_code(p) for p in (a.get("reason"), a.get("feeling")) if p)
    conf = a.get("confidence_1to5")
    return f"{label} · conf {conf}/5" if conf is not None else label


# /felt is a TAP flow, not typed args: callback_data accumulates the choices as
# ``felt:f=<feeling>:r=<reason>:c=<n>`` (built one tap at a time, well under Telegram's 64-byte
# cap). It is USER-CONTROLLABLE, so it is never trusted on the round-trip — every field is
# re-validated against a fresh config-vocab read before the write (see _felt_cb_valid).
_FELT_CB_PREFIX = "felt:"


def _kb_rows(buttons: list[dict], width: int = 3) -> dict:
    """Wrap flat buttons into an ``inline_keyboard`` of ``width``-wide rows."""
    return {"inline_keyboard": [buttons[i : i + width] for i in range(0, len(buttons), width)]}


def _feeling_kb(feelings: list[str]) -> dict:
    """Stage-1 keyboard: one button per feeling, carrying ``felt:f=<feeling>``."""
    return _kb_rows([{"text": f, "callback_data": f"{_FELT_CB_PREFIX}f={f}"} for f in feelings])


def _reason_kb(feeling: str, reasons: list[str]) -> dict:
    """Stage-2 keyboard: carries the chosen feeling forward + this reason."""
    return _kb_rows(
        [{"text": r, "callback_data": f"{_FELT_CB_PREFIX}f={feeling}:r={r}"} for r in reasons]
    )


def _confidence_kb(feeling: str, reason: str) -> dict:
    """Stage-3 keyboard: 1–5, carrying feeling + reason + this confidence."""
    return _kb_rows(
        [
            {"text": str(n), "callback_data": f"{_FELT_CB_PREFIX}f={feeling}:r={reason}:c={n}"}
            for n in range(1, 6)
        ],
        width=5,
    )


def _parse_felt_cb(data: str) -> dict[str, str]:
    """Parse ``felt:f=…:r=…:c=…`` into a ``{f,r,c}`` subset (segments on ':', each on first '=')."""
    out: dict[str, str] = {}
    for segment in data[len(_FELT_CB_PREFIX) :].split(":"):
        if "=" not in segment:
            continue
        key, value = segment.split("=", 1)
        if key in ("f", "r", "c"):
            out[key] = value
    return out


def _felt_cb_valid(fields: dict[str, str], reasons: list[str], feelings: list[str]) -> bool:
    """True only if every PRESENT field is in the current vocab (f, r) / a 1–5 integer (c).

    The trust boundary for the round-trip: a tampered value, or a button tapped after the vocab
    changed mid-flow, fails here so the caller refuses to write — the wire value is never trusted
    (the config list is the gate, Law 3, exactly as the old typed parser was).
    """
    if "f" in fields and fields["f"] not in feelings:
        return False
    if "r" in fields and fields["r"] not in reasons:
        return False
    if "c" in fields:
        c = fields["c"]
        if not (c.isascii() and c.isdigit() and 1 <= int(c) <= 5):
            return False
    return True


# The Postgres unique_violation SQLSTATE — the (symbol, trade_date) index raising it is how a
# same-day /felt that lost the read→insert race is caught (see handle_felt).
_UNIQUE_VIOLATION = "23505"


def _todays_note(client: Any, symbol: str, today_iso: str) -> dict | None:
    """The sleeve's existing pending note for ``symbol`` on ``today_iso`` (consumed or not), or None.

    ANY note for the sleeve today locks the day. Pending rows persist after reconcile attaches
    them, so this matches on trade_date alone — NOT the unconsumed subset — enforcing one immutable
    note per UTC day even after attachment (a later same-day /felt would otherwise be accepted and
    then silently dropped at reconcile).
    """
    rows = (
        client.table("pending_annotations")
        .select("id,reason,feeling,confidence_1to5")
        .eq("symbol", symbol)
        .eq("trade_date", today_iso)
        .order("created_at")
        .limit(1)
        .execute()
        .data
    ) or []
    return rows[0] if rows else None


def _already_locked_reply(note: dict | None) -> str:
    """The friendly lock-first reply: today's note is immutable; show what's locked."""
    locked = f"{_annotation_summary(note)} is locked in. " if note else ""
    return f"Already logged today ✓ — {locked}One annotation per day; that one stands."


def handle_felt(message: dict) -> str | Reply:
    """Launch the in-the-moment annotation flow as tappable buttons (blueprint §8 Step 4, Law 2).

    /felt no longer parses typed args — it replies with an inline keyboard the user taps through
    (feeling → reason → confidence), which removes the whole parse-error class. LOCK-FIRST is
    checked UP FRONT: if today's note is already staged, reply 'already logged' WITHOUT showing
    buttons, so the user never taps through only to hit the lock. The write happens on the final
    (confidence) tap — see ``handle_felt_callback``. ``message`` text beyond '/felt' is ignored.
    Returns a ``Reply`` (buttons) on the happy path, else a plain ``str`` (refusal / already-logged).
    """
    config = _load_config()
    symbol = _sleeve_symbol(config)
    if symbol is None:
        # Fail loud (L7); never start a flow that would end in a guessed-ticker note (L6).
        print("[felt] config.sleeve_symbol missing/invalid — flow not started (no guessed ticker, L6).")
        return (
            "⚠️ Sleeve symbol not configured — can't log /felt. "
            "Seed `config.sleeve_symbol` first."
        )
    existing = _todays_note(get_client(), symbol, _utc_today().isoformat())
    if existing is not None:
        return _already_locked_reply(existing)  # locked up front — don't make them tap to find out
    _reasons, feelings = _annotation_vocab(config)
    return Reply("*How did you feel?*", _feeling_kb(feelings))


def _record_annotation(
    client: Any, symbol: str, today_iso: str, *, reason: str, feeling: str, confidence: int
) -> str:
    """Lock-first stage of the annotation — the WRITE, unchanged from the old typed path.

    Re-checks the lock and leans on the ``(symbol, trade_date)`` unique index as the backstop, so a
    stale or double final-tap is caught as 23505 → the friendly 'already logged' reply, never a
    duplicate row or an overwrite. Returns the reply text.
    """
    existing = _todays_note(client, symbol, today_iso)
    if existing is not None:
        return _already_locked_reply(existing)
    try:
        client.table("pending_annotations").insert(
            {
                "symbol": symbol,
                "trade_date": today_iso,
                "reason": reason,
                "feeling": feeling,
                "confidence_1to5": confidence,
            }
        ).execute()
    except APIError as exc:
        # The unique index is the concurrency backstop the read can't be: a final tap that LOST the
        # read→insert race trips 23505 → the SAME friendly lock reply, never a webhook 'internal
        # error'. Any other DB error must still surface (Law 7).
        if getattr(exc, "code", None) == _UNIQUE_VIOLATION:
            return _already_locked_reply(_todays_note(client, symbol, today_iso))
        raise
    summary = _annotation_summary(
        {"reason": reason, "feeling": feeling, "confidence_1to5": confidence}
    )
    return (
        f"Recorded ✓ — {summary}. "
        f"Attaches to today's {symbol} round trip at the next pairing run; "
        f"if you don't trade {symbol} today it stays unattached."
    )


def handle_felt_callback(callback_query: dict) -> Reply | None:
    """Advance the /felt button flow one tap; on the final tap, validate + write (§8 Step 4).

    Dispatch is by the highest field present in callback_data: ``{f}`` → ask reason, ``{f,r}`` →
    ask confidence, ``{f,r,c}`` → write. Every present field is re-validated against a FRESH config
    vocab read — the wire value is never trusted (L3/L6). Returns a ``Reply`` to morph the message
    in place, or ``None`` for a callback that isn't ours. Never raises on user input; only a genuine
    DB error propagates (Law 7), surfaced by the webhook.
    """
    data = callback_query.get("data") or ""
    if not data.startswith(_FELT_CB_PREFIX):
        return None  # not a /felt tap — the webhook still answers the spinner
    fields = _parse_felt_cb(data)
    config = _load_config()
    reasons, feelings = _annotation_vocab(config)
    if not fields or not _felt_cb_valid(fields, reasons, feelings):
        # tampered, malformed, or vocab changed mid-flow → refuse and drop the keyboard
        return Reply("⚠️ Vocabulary changed — send /felt again.")

    if "c" in fields:  # final tap: feeling + reason + confidence all present → write
        symbol = _sleeve_symbol(config)
        if symbol is None:
            print("[felt] config.sleeve_symbol missing/invalid — annotation NOT recorded (L6).")
            return Reply("⚠️ Sleeve symbol not configured — annotation not recorded.")
        text = _record_annotation(
            get_client(),
            symbol,
            _utc_today().isoformat(),
            reason=fields["r"],
            feeling=fields["f"],
            confidence=int(fields["c"]),
        )
        return Reply(text)  # keyboard dropped — flow complete
    if "r" in fields:  # feeling + reason chosen → ask confidence
        return Reply(
            f"{_code(fields['f'])} · {_code(fields['r'])} — *how confident?* (1–5)",
            _confidence_kb(fields["f"], fields["r"]),
        )
    # only feeling chosen → ask reason
    return Reply(f"{_code(fields['f'])} — *what was the reason?*", _reason_kb(fields["f"], reasons))


# --------------------------------------------------------------------------- #
# /health — per-source status, last digest, Flex token expiry (Law 7 surface)
# --------------------------------------------------------------------------- #
def handle_health(message: dict) -> str:
    """Render the Source Health view (blueprint §7 §5 / §12, Law 7).

    Reads the latest ``fetch_log`` row per source, the latest ``digests.sent_at``, and
    ``config.ibkr_token_expiry_date`` (days-to-expiry, else 'expiry unknown').
    ``message`` is unused — ``/health`` takes no arguments.
    """
    client = get_client()
    rows = (
        client.table("fetch_log")
        .select("source,status,error,created_at")
        .order("created_at", desc=True)
        .limit(_FETCH_LOG_SCAN)
        .execute()
        .data
    )
    latest: dict[str, dict] = {}
    for row in rows:
        source = row.get("source") or "(unknown)"
        # Exclude non-§5-data sources (pipeline:* steps, the telegram_webhook ear) — same verdict
        # taxonomy the digest uses (shared.sources), so /health can't redden on a non-data source.
        # Surviving sources still render exactly as before (raw label, no logical collapse).
        if is_non_data_source(source):
            continue
        if source not in latest:  # first seen in desc order == most recent
            latest[source] = row

    lines = ["*Source Health*", ""]
    if latest:
        for source in sorted(latest):
            row = latest[source]
            mark = _STATUS_MARK.get(row.get("status"), "?")
            lines.append(
                f"{mark} {_code(source)} — {row.get('status')} ({_short_ts(row.get('created_at'))})"
            )
    else:
        lines.append("No fetch-log rows yet.")

    digest = (
        client.table("digests")
        .select("run_type,sent_at")
        .order("sent_at", desc=True)
        .limit(1)
        .execute()
        .data
    )
    lines.append("")
    if digest and digest[0].get("sent_at"):
        lines.append(
            f"*Last digest:* {digest[0].get('run_type')} at {_short_ts(digest[0]['sent_at'])}"
        )
    else:
        lines.append("*Last digest:* none sent yet")

    lines.append(_flex_expiry_line(_load_config().get("ibkr_token_expiry_date")))
    return "\n".join(lines)


def _flex_expiry_line(expiry: Any) -> str:
    """Render the Flex-token days-to-expiry line (§13 — surfaced in /health)."""
    if not expiry:
        return "*Flex token:* expiry unknown"
    try:
        exp_date = date.fromisoformat(str(expiry)[:10])
    except ValueError:
        return "*Flex token:* expiry unknown"
    days = (exp_date - _utc_today()).days
    if days < 0:
        return f"*Flex token:* EXPIRED ({exp_date})"
    return f"*Flex token:* {days} day(s) to expiry ({exp_date})"


# --------------------------------------------------------------------------- #
# /override — manually set transactions.override_type (always wins, §4)
# --------------------------------------------------------------------------- #
def handle_override(message: dict) -> str:
    """Set ``transactions.override_type`` for one trade (blueprint §4 / §2 item 3).

    Usage: ``/override <transaction_id> <override_type>``. The override always wins
    over the auto-assigned ``trade_type`` (§4). On a bad id, an invalid type, or an
    unknown transaction, returns an error string (does not raise — the webhook would
    otherwise turn a user typo into an 'internal error' notice).
    """
    parts = (message.get("text") or "").split()
    if len(parts) < 3:
        return "Usage: /override <transaction_id> <override_type>"

    raw_id, raw_type = parts[1], parts[2].lower()
    try:
        transaction_id = int(raw_id)
    except ValueError:
        return f"Invalid transaction id {_code(raw_id)} — must be an integer."
    if raw_type not in _OVERRIDE_TYPES:
        allowed = ", ".join(_code(t) for t in _OVERRIDE_TYPES)
        return f"Invalid override_type {_code(raw_type)}. Allowed: {allowed}"

    resp = (
        get_client()
        .table("transactions")
        .update({"override_type": raw_type})
        .eq("id", transaction_id)
        .execute()
    )
    if not resp.data:
        return f"Trade {transaction_id} not found — no override set."
    return f"Override set ✓ — trade {transaction_id} → {_code(raw_type)}"


# --------------------------------------------------------------------------- #
# /pulse — fire a workflow_dispatch for a light pulse digest (blueprint §3 / §11)
# --------------------------------------------------------------------------- #
def handle_pulse(message: dict) -> str:
    """Trigger the digest workflow's ``workflow_dispatch`` with ``run_type='pulse'``.

    Posts to the GitHub Actions REST API. Uses the existing, purpose-built env vars
    ``GH_DISPATCH_PAT`` (the workflow-dispatch-scoped PAT) and ``GH_REPO`` (``owner/
    name``) — named ``GH_*`` because GitHub reserves the ``GITHUB_`` prefix for Actions
    secrets (see .env.example). On failure, surfaces the problem to the user (Law 7)
    without leaking the PAT (it rides in a header, not the URL). ``message`` is unused.
    """
    load_dotenv(override=True)
    repo = os.environ.get("GH_REPO")
    pat = os.environ.get("GH_DISPATCH_PAT")
    if not repo or not pat:
        # Backtick code spans, not bare names: these identifiers carry THREE
        # underscores between them, and the webhook sends replies Markdown-parsed —
        # unbalanced '_' 400s the send, turning this friendly refusal into
        # "Internal error" (live incident 2026-07-10, webhook-d4ad2a795a51).
        return "⚠️ Pulse unavailable — `GH_REPO` / `GH_DISPATCH_PAT` not configured."

    url = f"{_GITHUB_API_BASE}/repos/{repo}/actions/workflows/{_PULSE_WORKFLOW}/dispatches"
    headers = {
        "Authorization": f"Bearer {pat}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    body = {"ref": "main", "inputs": {"run_type": "pulse"}}
    try:
        response = httpx.post(url, headers=headers, json=body, timeout=_HTTP_TIMEOUT_SECONDS)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        # Surface, never swallow (Law 7); keep detail to the exception type (no PAT).
        return f"⚠️ Couldn't trigger pulse ({type(exc).__name__}). Check /health."
    return "Generating pulse digest ⏳ — arriving in ~1 min"


# --------------------------------------------------------------------------- #
# /analyze — fire a workflow_dispatch for a Phase-5 dossier run (module spec §4)
# --------------------------------------------------------------------------- #
def handle_analyze(message: dict) -> str:
    """Trigger the analyze workflow's ``workflow_dispatch`` with the requested ticker.

    Mirrors ``handle_pulse`` exactly: instant ack + dispatch, ZERO heavy work here
    (§3 — the ~3-5 min pack/valuation/synthesis run lives in GitHub Actions). The
    argument is shape-validated only (``_TICKER_RE``); a well-formed but unknown
    symbol is the pipeline's case to handle (reduced-depth dossier, named gaps).
    Failure surfaces to the user without leaking the PAT (Law 7 / §13).
    """
    parts = (message.get("text") or "").split()
    if len(parts) < 2:
        return "Usage: /analyze TICKER [full] (e.g. /analyze TSLA, or /analyze TSLA full)"
    ticker = parts[1].split("@")[0].upper()
    if not _TICKER_RE.match(ticker):
        # Deliberately does NOT echo the input: the reply goes through a
        # Markdown-parsed send, and a stray '_'/'*' in user text would 400 the
        # send and turn a friendly refusal into "Internal error".
        return "⚠️ That doesn't look like a ticker — 1-5 letters/digits, e.g. TSLA, GM, BRK.B."
    # Class shares: SEC's ticker map and yfinance both use the dash form (BRK-B),
    # so the dot form users naturally type is normalized before dispatch.
    ticker = ticker.replace(".", "-")
    # Optional delivery length: '/analyze TSLA full' delivers the full stored dossier;
    # anything else leaves it to config.dossier_length (brief by default, §3). Empty
    # string => the workflow's declared default => config resolves it in the job.
    length = parts[2].lower() if len(parts) > 2 and parts[2].lower() in ("full", "brief") else ""

    load_dotenv(override=True)
    repo = os.environ.get("GH_REPO")
    pat = os.environ.get("GH_DISPATCH_PAT")
    if not repo or not pat:
        # Backtick code spans — same Markdown-safety rule (and live incident) as
        # handle_pulse's twin message above.
        return "⚠️ Analyze unavailable — `GH_REPO` / `GH_DISPATCH_PAT` not configured."

    url = f"{_GITHUB_API_BASE}/repos/{repo}/actions/workflows/{_ANALYZE_WORKFLOW}/dispatches"
    headers = {
        "Authorization": f"Bearer {pat}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    body = {"ref": "main", "inputs": {"ticker": ticker, "length": length}}
    try:
        response = httpx.post(url, headers=headers, json=body, timeout=_HTTP_TIMEOUT_SECONDS)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        return f"⚠️ Couldn't trigger the dossier run ({type(exc).__name__}). Check /health."
    kind = "full dossier" if length == "full" else "dossier"
    return f"Building {kind} for {ticker}, ~5 min ⏳"
