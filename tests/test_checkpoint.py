"""Tests for the checkpoint engine (journal/checkpoint.py).

These exercise the PURE logic only — no DB, no network. The crux under test is the
metric→gate mapping (§9): sign gates (20/50) must give the SAME verdict no matter what
price is plugged in, and the magnitude gate (10) must evaluate at a FIXED price (trip
#10's own rebuy_px) so its verdict can't drift with the live market (Law 6).
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from journal.checkpoint import _load_sleeve_symbol, checkpoint_state

# The pre-registered gates, mirroring the live config.kill_criteria row (§9).
KILL = {
    "early_warning": {"trade": 10, "delta_shares_lt": -1.0},
    "checkpoint": {"trade": 20, "delta_shares_lt": 0},
    "verdict": {"trade": 50, "delta_shares_lt": 0},
}

_BASE = date(2026, 3, 2)  # a fixed Monday; one trip per day keeps (date, id) order trivial


def ledger(pnls, rebuy_px=405.0):
    """Build N synthetic round_trips with sequential dates/ids and a known pnl each."""
    return [
        {
            "id": i + 1,
            "date": (_BASE + timedelta(days=i)).isoformat(),
            "pnl_usd": p,
            "rebuy_px": rebuy_px,
        }
        for i, p in enumerate(pnls)
    ]


def _gate(state, role):
    return next(g for g in state["gates"] if g["role"] == role)


# --------------------------------------------------------------------------- #
# Gate 10 — magnitude gate, just below / just above the −1.0 threshold
# --------------------------------------------------------------------------- #
def test_gate_10_pause_just_below_threshold():
    # First 10 trips sum to −400.40; trip #10's rebuy_px is 400 → Δshares = −1.001 < −1.0.
    trips = ledger([0.0] * 9 + [-400.40])
    trips[9]["rebuy_px"] = 400.00
    state = checkpoint_state(trips, KILL, current_price=405.00)
    g = _gate(state, "early_warning")
    assert g["reached"] and g["breached"] is True
    assert g["verdict"] == "pause" and g["action"] == "pause & examine"
    assert g["price_used"] == 400.00  # the FIXED price, not current_price (405)
    assert g["metric_delta_shares"] == pytest.approx(-1.001)
    # Landing exactly on the gate surfaces it as the just-arrived verdict.
    assert state["at_gate"]["role"] == "early_warning"


def test_gate_10_continue_just_above_threshold():
    # First 10 sum to −399.60; /400 = −0.999, NOT below −1.0 → continue.
    trips = ledger([0.0] * 9 + [-399.60])
    trips[9]["rebuy_px"] = 400.00
    state = checkpoint_state(trips, KILL, current_price=405.00)
    g = _gate(state, "early_warning")
    assert g["breached"] is False
    assert g["verdict"] == "continue"
    assert g["metric_delta_shares"] == pytest.approx(-0.999)


def test_gate_10_uses_fixed_price_not_current_price():
    # Breached at the fixed price (400). Sweep current_price across two orders of magnitude;
    # the gate-10 verdict must NOT move — only the live top-level Δshares does.
    trips = ledger([0.0] * 9 + [-400.40])
    trips[9]["rebuy_px"] = 400.00
    verdicts, live_views = set(), set()
    for px in (1.00, 50.00, 405.00, 100_000.00):
        state = checkpoint_state(trips, KILL, current_price=px)
        g = _gate(state, "early_warning")
        verdicts.add(g["verdict"])
        assert g["price_used"] == 400.00  # always the fixed trip-#10 rebuy price
        live_views.add(round(state["delta_shares"], 6))
    assert verdicts == {"pause"}  # immutable verdict across every current price
    assert len(live_views) == 4  # the live display DID move with price (proves it's separate)


# --------------------------------------------------------------------------- #
# Gate 20 — sign gate, price-independent, just below / just above 0
# --------------------------------------------------------------------------- #
def test_gate_20_halt_when_cumulative_negative():
    trips = ledger([0.0] * 19 + [-0.01])  # first 20 sum to −0.01 < 0
    state = checkpoint_state(trips, KILL, current_price=405.00)
    g = _gate(state, "checkpoint")
    assert g["reached"] and g["breached"] is True
    assert g["verdict"] == "halt" and "Phase B" not in g["action"]
    assert g["price_independent"] is True
    assert g["price_used"] is None  # no price entered the verdict at all


def test_gate_20_pass_unlocks_phase_b_when_cumulative_nonnegative():
    trips = ledger([0.0] * 19 + [0.01])  # first 20 sum to +0.01 ≥ 0
    state = checkpoint_state(trips, KILL, current_price=405.00)
    g = _gate(state, "checkpoint")
    assert g["breached"] is False
    assert g["verdict"] == "pass" and "Phase B" in g["action"]


def test_gate_20_verdict_is_price_invariant():
    # Same ledger, wildly different prices → identical sign-gate verdict every time.
    for pnls, expected in (([0.0] * 19 + [-0.01], "halt"), ([0.0] * 19 + [0.01], "pass")):
        verdicts = {
            _gate(checkpoint_state(ledger(pnls), KILL, current_price=px), "checkpoint")["verdict"]
            for px in (0.01, 1.00, 405.00, 1_000_000.00)
        }
        assert verdicts == {expected}


# --------------------------------------------------------------------------- #
# Gate 50 — sign gate, same stability as gate 20
# --------------------------------------------------------------------------- #
def test_gate_50_stop_vs_pass_and_price_invariance():
    losing = ledger([0.0] * 49 + [-0.01])  # first 50 sum < 0 → permanent stop
    winning = ledger([0.0] * 49 + [0.01])  # first 50 sum ≥ 0 → Phase C discussion
    for trips, expected, phrase in (
        (losing, "stop", "rejoins core"),
        (winning, "pass", "Phase C"),
    ):
        verdicts = set()
        for px in (0.01, 405.00, 1_000_000.00):
            g = _gate(checkpoint_state(trips, KILL, current_price=px), "verdict")
            verdicts.add(g["verdict"])
            assert phrase in g["action"]
        assert verdicts == {expected}  # price never flips a sign gate


# --------------------------------------------------------------------------- #
# Sign gates hold even with NO price at all (the strongest price-independence proof)
# --------------------------------------------------------------------------- #
def test_sign_gates_evaluate_with_no_current_price():
    trips = ledger([0.0] * 19 + [-0.01])
    state = checkpoint_state(trips, KILL, current_price=None)
    assert state["delta_shares"] is None  # live view can't render without a price...
    assert _gate(state, "checkpoint")["breached"] is True  # ...but the verdict still lands


# --------------------------------------------------------------------------- #
# Proximity math: "trade 7 of 10, 3 to go" / "Trade 18 of 20 — checkpoint in 2."
# --------------------------------------------------------------------------- #
def test_proximity_seven_of_ten():
    state = checkpoint_state(ledger([0.0] * 7), KILL, current_price=405.00)
    ng = state["next_gate"]
    assert ng["trade"] == 10 and ng["trades_to_go"] == 3 and ng["role"] == "early_warning"
    assert state["at_gate"] is None  # between gates → no verdict just landed
    assert state["proximity_text"].startswith("Trade 7 of 10 — early warning in 3.")


def test_proximity_eighteen_of_twenty_with_live_delta():
    # Cumulative +124.32 over 18 trips @ 405 → +0.31 sleeve shares (matches the §9 example).
    trips = ledger([124.32 / 18] * 18)
    state = checkpoint_state(trips, KILL, current_price=405.00)
    ng = state["next_gate"]
    assert ng["trade"] == 20 and ng["trades_to_go"] == 2 and ng["name"] == "checkpoint"
    assert state["delta_shares"] == pytest.approx(124.32 / 18 * 18 / 405.00)
    assert state["proximity_text"] == "Trade 18 of 20 — checkpoint in 2. Sleeve Δshares: +0.31."


def test_next_gate_is_none_past_the_final_gate():
    state = checkpoint_state(ledger([0.0] * 51), KILL, current_price=405.00)
    assert state["next_gate"] is None
    assert "past the final gate" in state["proximity_text"]


# --------------------------------------------------------------------------- #
# Reached gates stay evaluated once passed (immutability across the ledger growing)
# --------------------------------------------------------------------------- #
def test_earlier_gates_remain_evaluated_after_count_moves_on():
    # 25 winning trips: gates 10 and 20 are history (both pass), 50 not yet reached.
    trips = ledger([1.00] * 25)  # cum10 = +10 → +0.025 sh (continue); cum20 = +20 (pass)
    trips[9]["rebuy_px"] = 400.00  # gate-10 fixed price
    state = checkpoint_state(trips, KILL, current_price=405.00)
    assert _gate(state, "early_warning")["reached"] and _gate(state, "early_warning")["verdict"] == "continue"
    assert _gate(state, "checkpoint")["reached"] and _gate(state, "checkpoint")["verdict"] == "pass"
    assert _gate(state, "verdict")["reached"] is False
    assert state["at_gate"] is None  # 25 is not a gate count → no fresh verdict


# --------------------------------------------------------------------------- #
# Statistical-honesty line (§9)
# --------------------------------------------------------------------------- #
def test_statistical_note_early_signal_before_significance():
    state = checkpoint_state(ledger([0.0] * 10), KILL, current_price=405.00)
    assert "early signal only" in state["statistical_note"]
    assert state["statistical_note"].startswith("10 trades")


def test_statistical_note_drops_early_signal_near_fifty():
    state = checkpoint_state(ledger([0.0] * 50), KILL, current_price=405.00)
    assert "early signal only" not in state["statistical_note"]


# --------------------------------------------------------------------------- #
# Cumulative metric + live share view (one conversion at read time, §7)
# --------------------------------------------------------------------------- #
def test_cumulative_and_live_share_view():
    # +23.50 winner then −27.50 loser → −4.00 cumulative; /400 → −0.01 sleeve shares.
    state = checkpoint_state(ledger([23.50, -27.50]), KILL, current_price=400.00)
    assert state["cumulative_pnl_usd"] == pytest.approx(-4.00)
    assert state["delta_shares"] == pytest.approx(-0.01)
    assert state["trade_count"] == 2


# --------------------------------------------------------------------------- #
# _load_sleeve_symbol — fail loud on a missing/invalid config row (L6/L7)
# --------------------------------------------------------------------------- #
class _ConfigClient:
    """Minimal fake: ``config.select('value').eq('key','sleeve_symbol')`` → ``rows``."""

    def __init__(self, rows):
        self._rows = rows

    def table(self, name):
        assert name == "config"
        return self

    def select(self, *a, **k):
        return self

    def eq(self, *a, **k):
        return self

    def execute(self):
        return type("R", (), {"data": self._rows})()


def test_load_sleeve_symbol_returns_seeded_value():
    assert _load_sleeve_symbol(_ConfigClient([{"value": "TSLA"}])) == "TSLA"


@pytest.mark.parametrize("rows", [[], [{"value": None}], [{"value": "  "}], [{"value": 17}]])
def test_load_sleeve_symbol_fails_loud_when_missing_or_invalid(rows):
    # Missing row, null, blank, or non-string → raise (never guess a ticker, L6). The wrapped
    # caller (run_checkpoint / check_and_push) turns this into a surfaced fetch_log failure (L7).
    with pytest.raises(RuntimeError, match="sleeve_symbol"):
        _load_sleeve_symbol(_ConfigClient(rows))
