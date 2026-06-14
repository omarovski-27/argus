"""Argus bot — instant command handlers (blueprint §2 item 10 / §3 / §7 / §9).

Six handlers behind the Telegram commands ``/book /journal /skip /health /override
/pulse``. Each is a pure function ``(message: dict) -> str``: it reads (or writes) the
Supabase spine directly and returns a reply string. ``api.webhook`` owns the actual
``send_message`` call, so handlers never touch the network except ``/pulse``, whose
job IS an outbound trigger (it fires a GitHub Actions ``workflow_dispatch``).

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
from datetime import date, datetime, timezone
from typing import Any

import httpx
from dotenv import load_dotenv

from shared.db import get_client

# --- config-driven constants, with documented Phase-0 fallbacks ----------------- #
# The $100K goal (§0 / §13). Read from config.target_usd when present so it stays a
# tunable JSONB row (§2 item 3), not a hardcoded constant; default to the blueprint
# figure while config is unseeded — mirrors ibkr_flex's sleeve_shares fallback.
_DEFAULT_TARGET_USD = 100_000.0
# Pre-registered journal gates (§9). Extracted from config.kill_criteria when seeded.
_DEFAULT_CHECKPOINTS: tuple[int, ...] = (10, 20, 50)

# /skip reasons — must match the skip_log.reason CHECK constraint (§4 table 14).
_SKIP_REASONS: tuple[str, ...] = ("event_filter", "discretion", "other")
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

# /health scan window: fetch_log grows by ~15-30 rows/day, so the latest row for
# every source comfortably falls inside the most recent 1000 rows (weeks of runs).
_FETCH_LOG_SCAN = 1000
_STATUS_MARK = {"success": "✓", "failure": "✗", "timeout": "⌛", "unavailable": "∅"}


# --------------------------------------------------------------------------- #
# Small shared helpers
# --------------------------------------------------------------------------- #
def _utc_today() -> date:
    """Today's date in UTC (all Argus date logic is UTC; §3 / §12)."""
    return datetime.now(timezone.utc).date()


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
    total_mv = sum((row.get("market_value") or 0.0) for row in positions)

    target = float(config.get("target_usd", _DEFAULT_TARGET_USD))
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
    contrib_total = sum((row.get("amount") or 0.0) for row in contribs)

    trips = client.table("round_trips").select("delta_shares").execute().data
    n_trades = len(trips)
    cum_delta = sum((row.get("delta_shares") or 0.0) for row in trips)
    phase = config.get("phase", "?")
    next_cp = _next_checkpoint(n_trades, _checkpoints(config))

    musk_mv = sum((by_symbol.get(s, {}).get("market_value") or 0.0) for s in ("TSLA", "SPCX"))

    lines = [f"*Your Book* — snapshot {snap_date}", "", "*Allocation*"]
    for symbol in sorted(by_symbol):
        mv = by_symbol[symbol].get("market_value") or 0.0
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
    ``trade_annotations`` (confidence for the last 10), and ``config`` (phase,
    kill_criteria). ``message`` is unused — ``/journal`` takes no arguments.
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
    cum_delta = sum((row.get("delta_shares") or 0.0) for row in trips)
    phase = config.get("phase", "?")
    checkpoints = _checkpoints(config)
    next_cp = _next_checkpoint(n_trades, checkpoints)

    recent = trips[-10:]
    conf_by_trip: dict[int, int] = {}
    if recent:
        annotations = (
            client.table("trade_annotations")
            .select("round_trip_id,confidence_1to5")
            .in_("round_trip_id", [row["id"] for row in recent])
            .execute()
            .data
        )
        for row in annotations:
            if row.get("confidence_1to5") is not None:
                conf_by_trip[row["round_trip_id"]] = row["confidence_1to5"]

    lines = [f"*Journal* — phase {phase}", ""]
    lines.append(
        f"*Cumulative sleeve Δshares:* {_signed(cum_delta)}  over {n_trades} round trip(s)"
    )
    lines.append("")
    if recent:
        lines.append("*Last 10 round trips*")
        for row in reversed(recent):  # most recent first
            conf = conf_by_trip.get(row["id"])
            conf_str = f" · conf {conf}/5" if conf is not None else ""
            lines.append(
                f"• {row['date']} {row['symbol']}: "
                f"P&L {_money(row.get('pnl_usd'))} · Δ {_signed(row.get('delta_shares'))}{conf_str}"
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
    load_dotenv()
    repo = os.environ.get("GH_REPO")
    pat = os.environ.get("GH_DISPATCH_PAT")
    if not repo or not pat:
        return "⚠️ Pulse unavailable — GH_REPO / GH_DISPATCH_PAT not configured."

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
