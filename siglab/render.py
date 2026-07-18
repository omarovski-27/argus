"""Argus Signal Lab — the ONE renderer (pure). Mandatory label; describes, never advises.

Every surface (/today, /signal, the Monday digest) renders through here, so the label and
the no-advice discipline cannot drift between them (the ``shared.event_filter`` pattern).
The hard render rule (Law 1, Amendment #2), pinned by tests/test_signal_render.py: the
output states the signal's CONDITION and its RECORD and nothing else — the words good-day,
should, buy, sell, enter, exit never appear. The ``🧪`` experiment label is mandatory on
every rendering.

FINALIZED v1 (2026-07-18, INCONCLUSIVE): the shadow scorer could not test the rule at daily
resolution, so v1 has NO win/loss verdict — the renders show an 'under test / no verdict'
line (state today only), never a W–L or shadow P&L (those were the broken measurement). The
scored-record path below is kept for a future instrument-scaled signal_v2 and is selected by
``stats['status']`` being a non-finalized (gate-derived) value.
"""

from __future__ import annotations

from siglab.registry import FINALIZED_STATUSES

# The label that MUST prefix every signal rendering (Amendment #2). Its presence is tested.
EXPERIMENT_LABEL = "🧪"

# The single sentence every finalized-signal surface states (no W–L, no P&L — those were the
# invalid measurement). Kept in one place so /today, /signal and the digest cannot drift.
_NO_VERDICT = "No verdict — daily data can't score this bracket; awaiting live round-trips."


def _is_finalized(stats: dict) -> bool:
    return stats.get("status") in FINALIZED_STATUSES


def _money_signed(value: float | None) -> str:
    """'+$123.50' / '-$45.00' / '+$0.00' — a signed shadow-dollar figure."""
    v = float(value or 0.0)
    sign = "-" if v < 0 else "+"
    return f"{sign}${abs(v):,.2f}"


def _under_test_line(version: str, state: str | None) -> str:
    """The finalized one-liner: today's state + no verdict + the promotion pointer (no record)."""
    shown = state or "awaiting first state log"
    return f"{EXPERIMENT_LABEL} Signal {version} (under test): {shown}. {_NO_VERDICT}"


def render_signal_line(stats: dict) -> str:
    """The one-line /today-full + digest rendering (label mandatory, no advice)."""
    if _is_finalized(stats):
        return _under_test_line(stats.get("version", "v1"), stats.get("today_state"))
    state = stats.get("today_state") or "UNFAVORABLE"
    return (
        f"{EXPERIMENT_LABEL} Signal {stats.get('version', 'v1')} (experiment, "
        f"day {stats.get('n_days', 0)}): {state}. "
        f"Record so far: {stats.get('wins', 0)}–{stats.get('losses', 0)}, "
        f"shadow P&L {_money_signed(stats.get('cum_pnl'))}. "
        f"{stats.get('evidence_label', 'No evidence of edge yet')}."
    )


def render_signal_today(stats: dict) -> str:
    """The compact one-glance /today line. Finalized → the under-test line (no W–L/P&L)."""
    if _is_finalized(stats):
        return _under_test_line(stats.get("version", "v1"), stats.get("today_state"))
    # Scored path (a future instrument-scaled signal_v2): today's state + measured frequency.
    state = stats.get("today_state") or "UNFAVORABLE"
    winrate = stats.get("winrate")
    winrate_str = "n/a" if winrate is None else f"{winrate * 100:.0f}%"
    return (
        f"{EXPERIMENT_LABEL} Signal: {state} — days like today won {winrate_str} "
        f"historically (n={stats.get('n_triggered', 0)}, "
        f"shadow {_money_signed(stats.get('cum_pnl'))})."
    )


def render_signal_today_pending(blob: dict) -> str:
    """The compact /today line before any ledger row exists (label mandatory, no advice).

    v1 is finalized under-test, so pending still renders the under-test framing; a future
    non-finalized signal renders the old 'registered … backfill pending' line."""
    if blob.get("status") in FINALIZED_STATUSES:
        return _under_test_line(blob.get("version", "v1"), None)
    return (
        f"{EXPERIMENT_LABEL} Signal {blob.get('version', 'v1')}: registered "
        f"{blob.get('registered_at')} — no track record yet (backfill pending)."
    )


def _render_full_finalized(stats: dict) -> str:
    """The /signal full view for a finalized (INCONCLUSIVE) signal — rule, reason, promotion."""
    lines = [
        _under_test_line(stats.get("version", "v1"), stats.get("today_state")),
        "",
        f"Rule (registered {stats.get('registered_at')}): {stats.get('rule')}",
        "",
        f"Status: {stats.get('status', 'INCONCLUSIVE')}"
        + (f" (finalized {stats.get('finalized_at')})" if stats.get("finalized_at") else "")
        + f" — {stats.get('status_reason')}",
        "",
        f"Promotion path: {stats.get('promotion_path')}",
        "",
        f"Days of state logged: {stats.get('n_days', 0)}",
        "",
        "Experiment — shadow only, no real orders, not advice. Promotion is Omar's alone.",
    ]
    return "\n".join(lines)


def render_signal_full(stats: dict) -> str:
    """The /signal command — finalized view, or (future v2) full ledger stats + gate progress."""
    if _is_finalized(stats):
        return _render_full_finalized(stats)
    winrate = stats.get("winrate")
    winrate_str = "n/a (no triggers yet)" if winrate is None else f"{winrate * 100:.0f}%"
    lines = [
        render_signal_line(stats),
        "",
        f"Rule (registered {stats.get('registered_at')}): {stats.get('rule')}",
        "",
        f"Observation days: {stats.get('n_days', 0)}",
        f"Triggered (win/loss): {stats.get('n_triggered', 0)} "
        f"— {stats.get('wins', 0)} win / {stats.get('losses', 0)} loss "
        f"(win rate {winrate_str})",
        f"No-trigger days: {stats.get('no_trigger', 0)}; unscored (missing data): {stats.get('unknown', 0)}",
        f"Cumulative shadow P&L: {_money_signed(stats.get('cum_pnl'))}",
        f"Status: {stats.get('status', 'testing')}",
    ]
    nxt = stats.get("next_gate")
    if nxt:
        lines.append(
            f"Next gate: {nxt['remaining']} more triggered day(s) to N={nxt['n']}."
        )
    lines.append("")
    lines.append(
        "Experiment — shadow only, no real orders, not advice. A PASS verdict unlocks a "
        "promotion decision that is Omar's alone."
    )
    return "\n".join(lines)


def render_signal_full_pending(blob: dict) -> str:
    """The /signal view before any ledger row exists. Finalized → the under-test framing.

    Builds a minimal stats dict from the blob so the finalized full view (rule + reason +
    promotion path) renders even with zero logged days; a non-finalized signal keeps the
    old 'registered … backfill pending' one-liner."""
    if blob.get("status") in FINALIZED_STATUSES:
        return _render_full_finalized({
            "version": blob.get("version", "v1"),
            "today_state": None,
            "registered_at": blob.get("registered_at"),
            "rule": blob.get("rule"),
            "status": blob.get("status"),
            "status_reason": blob.get("status_reason"),
            "promotion_path": blob.get("promotion_path"),
            "finalized_at": blob.get("finalized_at"),
            "n_days": 0,
        })
    return render_signal_today_pending(blob)
