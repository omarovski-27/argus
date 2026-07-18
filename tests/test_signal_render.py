"""Signal Lab render tests (siglab/render.py) — the hard Law-1 no-advice rule + finalization.

The load-bearing guarantee of Amendment #2: the signal states its CONDITION and its RECORD
and nothing else. v1 is FINALIZED INCONCLUSIVE (2026-07-18): its surfaces show an 'under
test / no verdict' line with today's state — NEVER a W–L or shadow P&L (those were the
invalid daily-bar measurement). ``test_finalized_render_never_advises`` scans every finalized
surface for banned instruction words; the 🧪 label is mandatory everywhere. The scored-record
path is kept for a hypothetical instrument-scaled signal_v2 and is tested via a testing blob.
"""

import re

import pytest

from siglab.ledger import compute_stats
from siglab.registry import SIGNAL_V1_DEFAULT
from siglab.render import (
    EXPERIMENT_LABEL,
    render_signal_full,
    render_signal_full_pending,
    render_signal_line,
    render_signal_today,
    render_signal_today_pending,
)

# The hard exclusion (Amendment #2): describes, never advises.
_BANNED = [
    r"\bgood day\b", r"\bbad day\b", r"\bshould\b", r"\bcould\b",
    r"\bbuy\b", r"\bsell\b", r"\benter\b", r"\bexit\b", r"\bsafe to\b",
]

# v1's blob is finalized INCONCLUSIVE; the scored path needs a non-finalized (testing) blob.
_TESTING_BLOB = {**SIGNAL_V1_DEFAULT, "status": "testing"}


def _mk(seq: str) -> list[dict]:
    """A scored win/loss ledger (for the future-v2 scored path only)."""
    return [
        {"date": f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}", "signal_state": "FAVORABLE",
         "outcome": "win" if c == "w" else "loss", "shadow_pnl": 23.50 if c == "w" else -27.50}
        for i, c in enumerate(seq)
    ]


def _state_rows(states: list[str]) -> list[dict]:
    """The real forward-only ledger: state rows, outcome always unknown, P&L NULL."""
    return [
        {"date": f"2026-07-{10 + i:02d}", "signal_state": s, "outcome": "unknown", "shadow_pnl": None}
        for i, s in enumerate(states)
    ]


_FINAL = compute_stats(_state_rows(["UNFAVORABLE", "FAVORABLE"]), SIGNAL_V1_DEFAULT)


# --------------------------------------------------------------------------- #
# Finalized (v1) surfaces — under test, no verdict, no advice, 🧪 mandatory
# --------------------------------------------------------------------------- #
_FINALIZED_SURFACES = [
    render_signal_today(_FINAL),
    render_signal_line(_FINAL),
    render_signal_full(_FINAL),
    render_signal_today_pending(SIGNAL_V1_DEFAULT),
    render_signal_full_pending(SIGNAL_V1_DEFAULT),
]


@pytest.mark.parametrize("text", _FINALIZED_SURFACES)
def test_finalized_render_never_advises(text):
    low = text.lower()
    for pattern in _BANNED:
        assert not re.search(pattern, low), f"render advised — matched {pattern!r}"


@pytest.mark.parametrize("text", _FINALIZED_SURFACES)
def test_label_is_mandatory(text):
    assert EXPERIMENT_LABEL in text


@pytest.mark.parametrize("text", _FINALIZED_SURFACES)
def test_no_broken_record_leaks(text):
    # The whole point: no W–L, no shadow P&L, no win rate on any finalized surface.
    low = text.lower()
    assert "shadow p&l" not in low
    assert "win rate" not in low
    assert "record so far" not in low
    assert "won" not in low  # "days like today won X%" must not appear either


def test_today_line_is_under_test_with_state():
    line = render_signal_today(_FINAL)
    assert line.startswith(EXPERIMENT_LABEL)
    assert "(under test): FAVORABLE." in line          # today's state (last row)
    assert "No verdict — daily data can't score this bracket; awaiting live round-trips." in line


def test_pending_today_line_awaits_first_state():
    line = render_signal_today_pending(SIGNAL_V1_DEFAULT)
    assert "(under test): awaiting first state log." in line
    assert "No verdict" in line


def test_full_carries_rule_reason_and_promotion():
    full = render_signal_full(_FINAL)
    assert "FAVORABLE iff TSLA close > SMA50" in full          # the registered rule
    assert "Status: INCONCLUSIVE" in full
    assert "unmeasurable at daily resolution" in full          # the finalization reason
    assert "actual journal round-trips" in full                # the promotion path
    assert "Omar's alone" in full
    assert "Record so far" not in full and "win rate" not in full.lower()


def test_full_pending_shows_finalized_framing():
    full = render_signal_full_pending(SIGNAL_V1_DEFAULT)
    assert "Status: INCONCLUSIVE" in full
    assert "unmeasurable at daily resolution" in full
    assert "Days of state logged: 0" in full


# --------------------------------------------------------------------------- #
# Scored path — preserved for a hypothetical instrument-scaled signal_v2
# --------------------------------------------------------------------------- #
def test_scored_path_still_renders_record_for_future_v2():
    stats = compute_stats(_mk("w" * 12 + "l" * 8), _TESTING_BLOB)   # 12-8, +$62.00
    line = render_signal_line(stats)
    assert "Record so far: 12–8" in line
    assert "shadow P&L +$62.00" in line
    today = render_signal_today(stats)
    assert "days like today won 60% historically (n=20, shadow +$62.00)." in today
    for text in (line, today, render_signal_full(stats)):
        for pattern in _BANNED:
            assert not re.search(pattern, text.lower())
