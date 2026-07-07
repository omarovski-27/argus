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
from shared.sources import is_non_data_source

# --- config-driven constants, with documented Phase-0 fallbacks ----------------- #
# The $100K goal (§0 / §13). Read from config.target_usd when present so it stays a
# tunable JSONB row (§2 item 3), not a hardcoded constant; default to the blueprint
# figure while config is unseeded — a fixed goal a default stands in for safely (unlike
# sleeve_shares, whose absence now means "no active sleeve", not a default-able value).
_DEFAULT_TARGET_USD = 100_000.0
# Pre-registered journal gates (§9). Extracted from config.kill_criteria when seeded.
_DEFAULT_CHECKPOINTS: tuple[int, ...] = (10, 20, 50)

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
        return "Usage: /analyze TICKER (e.g. /analyze TSLA)"
    ticker = parts[1].split("@")[0].upper()
    if not _TICKER_RE.match(ticker):
        return f"⚠️ {parts[1]!r} doesn't look like a ticker (e.g. TSLA, GM, BRK.B)."

    load_dotenv(override=True)
    repo = os.environ.get("GH_REPO")
    pat = os.environ.get("GH_DISPATCH_PAT")
    if not repo or not pat:
        return "⚠️ Analyze unavailable — GH_REPO / GH_DISPATCH_PAT not configured."

    url = f"{_GITHUB_API_BASE}/repos/{repo}/actions/workflows/{_ANALYZE_WORKFLOW}/dispatches"
    headers = {
        "Authorization": f"Bearer {pat}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    body = {"ref": "main", "inputs": {"ticker": ticker}}
    try:
        response = httpx.post(url, headers=headers, json=body, timeout=_HTTP_TIMEOUT_SECONDS)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        return f"⚠️ Couldn't trigger the dossier run ({type(exc).__name__}). Check /health."
    return f"Building dossier for {ticker}, ~5 min ⏳"
