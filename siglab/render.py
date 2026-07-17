"""Argus Signal Lab — the ONE renderer (pure). Mandatory label; describes, never advises.

Every surface (/today, /signal, the Monday digest) renders through here, so the label and
the no-advice discipline cannot drift between them (the ``shared.event_filter`` pattern).
The hard render rule (Law 1, Amendment #2), pinned by tests/test_signal_render.py: the
output states the signal's CONDITION and its RECORD and nothing else — the words good-day,
should, buy, sell, enter, exit never appear. The ``🧪`` experiment label is mandatory on
every rendering, and a RETIRED verdict renders permanently.
"""

from __future__ import annotations

# The label that MUST prefix every signal rendering (Amendment #2). Its presence is tested.
EXPERIMENT_LABEL = "🧪"


def _money_signed(value: float | None) -> str:
    """'+$123.50' / '-$45.00' / '+$0.00' — a signed shadow-dollar figure."""
    v = float(value or 0.0)
    sign = "-" if v < 0 else "+"
    return f"{sign}${abs(v):,.2f}"


def render_signal_line(stats: dict) -> str:
    """The one-line /today + digest rendering (label mandatory, record stated, no advice)."""
    state = stats.get("today_state") or "UNFAVORABLE"
    line = (
        f"{EXPERIMENT_LABEL} Signal {stats.get('version', 'v1')} (experiment, "
        f"day {stats.get('n_days', 0)}): {state}. "
        f"Record so far: {stats.get('wins', 0)}–{stats.get('losses', 0)}, "
        f"shadow P&L {_money_signed(stats.get('cum_pnl'))}. "
        f"{stats.get('evidence_label', 'No evidence of edge yet')}."
    )
    return line


def render_signal_today(stats: dict) -> str:
    """The compact one-glance /today line: today's state + the measured historical frequency.

    Format (Amendment #2): ``🧪 Signal: FAVORABLE — days like today won 11% historically
    (n=38, shadow -$841.00)``. The winrate/n/P&L are the ledger's triggered-day record
    (the only scored population); the 🧪 label is mandatory and the line states the record
    and nothing else (no advice — the hard render test guards it)."""
    state = stats.get("today_state") or "UNFAVORABLE"
    winrate = stats.get("winrate")
    winrate_str = "n/a" if winrate is None else f"{winrate * 100:.0f}%"
    return (
        f"{EXPERIMENT_LABEL} Signal: {state} — days like today won {winrate_str} "
        f"historically (n={stats.get('n_triggered', 0)}, "
        f"shadow {_money_signed(stats.get('cum_pnl'))})."
    )


def render_signal_today_pending(blob: dict) -> str:
    """The compact /today line before any ledger record exists (label mandatory, no advice)."""
    return (
        f"{EXPERIMENT_LABEL} Signal {blob.get('version', 'v1')}: registered "
        f"{blob.get('registered_at')} — no track record yet (backfill pending)."
    )


def render_signal_full(stats: dict) -> str:
    """The /signal command — full ledger stats + the registered rule + gate progress."""
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
