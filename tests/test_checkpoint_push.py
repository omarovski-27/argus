"""Tests for the checkpoint push (journal/checkpoint_push.py).

PURE only — no DB, no network, no Telegram. The crux under test is that a verdict push
reports the gate's OWN frozen as-of-gate basis (cumulative_pnl_at_gate, and for the
magnitude gate metric_delta_shares / price_used) and NEVER the live state. The catch-up
divergence: when trade_count has run past the gate, the message must still show the gate's
trade-N figures, not the current ones (Law 2 / Law 6). _verdict_text no longer even
receives the live state, so the leak is structurally impossible — these pin it.
"""

from __future__ import annotations

from journal.checkpoint import checkpoint_state
from journal.checkpoint_push import _verdict_text

# Mirrors the live config.kill_criteria row (§9); same as the engine tests.
KILL = {
    "early_warning": {"trade": 10, "delta_shares_lt": -1.0},
    "checkpoint": {"trade": 20, "delta_shares_lt": 0},
    "verdict": {"trade": 50, "delta_shares_lt": 0},
}


def _gate(state, role):
    return next(g for g in state["gates"] if g["role"] == role)


def _ledger(pnls, rebuy_px=405.0):
    return [
        {"id": i + 1, "date": f"2026-03-{i + 1:02d}", "pnl_usd": p, "rebuy_px": rebuy_px}
        for i, p in enumerate(pnls)
    ]


def test_magnitude_verdict_uses_fixed_price_basis():
    # 10 trips, first ten sum to -400.40, trip #10 rebuy_px 400 -> Δshares -1.001 < -1.0.
    trips = _ledger([0.0] * 9 + [-400.40])
    trips[9]["rebuy_px"] = 400.00
    state = checkpoint_state(trips, KILL, current_price=405.00)
    g = _gate(state, "early_warning")
    text = _verdict_text(g)
    assert "Gate 10 — early warning: pause & examine." in text
    assert "As of trade 10:" in text
    assert "-1.001" in text  # the fixed-price Δshares, not the live view
    assert "@ fixed 400.00" in text
    assert "$-400.40" in text


def test_sign_verdict_reports_cumulative_only():
    trips = _ledger([0.0] * 19 + [-0.01])  # first 20 sum to -0.01 < 0 -> halt
    state = checkpoint_state(trips, KILL, current_price=405.00)
    g = _gate(state, "checkpoint")
    text = _verdict_text(g)
    assert "Gate 20 — checkpoint: mandatory halt & review." in text
    assert "cumulative $-0.01 (sign gate)" in text
    assert "@ fixed" not in text  # no price ever entered a sign-gate verdict


def test_catchup_verdict_shows_gate10_basis_not_current_figures():
    """Count 12, gate 10 fires: the message must read trade-10 numbers, never trade-12's.

    Trips 1-10 sum to -400.40 (the gate-10 basis); trips 11-12 add +500 each, so the LIVE
    cumulative is +599.60 and the live count is 12. The verdict push must show none of that.
    """
    trips = _ledger([0.0] * 9 + [-400.40] + [500.00, 500.00])
    trips[9]["rebuy_px"] = 400.00
    state = checkpoint_state(trips, KILL, current_price=405.00)
    assert state["trade_count"] == 12
    assert state["cumulative_pnl_usd"] > 0  # live ledger is now net positive...

    g = _gate(state, "early_warning")
    assert g["reached"] and g["trade"] == 10
    text = _verdict_text(g)
    # ...but the verdict still reads the trade-10 basis: the loss and the fixed price.
    assert "As of trade 10:" in text
    assert "$-400.40" in text
    assert "-1.001" in text
    # And none of the live trade-12 figures bleed into the verdict line.
    assert "12" not in text
    assert "599" not in text
    assert "+500" not in text and "500.00" not in text
