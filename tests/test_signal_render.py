"""Signal Lab render tests (siglab/render.py) — the hard Law-1 no-advice rule.

The load-bearing guarantee of Amendment #2: the signal states its CONDITION and its
RECORD and nothing else. ``test_render_never_advises`` scans every render surface across
every status for banned instruction words; the 🧪 experiment label is mandatory on every
rendering; a RETIRED verdict renders permanently."""

import re

import pytest

from siglab.ledger import compute_stats
from siglab.registry import SIGNAL_V1_DEFAULT
from siglab.render import EXPERIMENT_LABEL, render_signal_full, render_signal_line

# The hard exclusion (Amendment #2): describes, never advises.
_BANNED = [
    r"\bgood day\b", r"\bbad day\b", r"\bshould\b", r"\bcould\b",
    r"\bbuy\b", r"\bsell\b", r"\benter\b", r"\bexit\b", r"\bsafe to\b",
]


def _mk(seq: str) -> list[dict]:
    return [
        {"date": f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}", "signal_state": "FAVORABLE",
         "outcome": "win" if c == "w" else "loss", "shadow_pnl": 23.50 if c == "w" else -27.50}
        for i, c in enumerate(seq)
    ]


_LEDGERS = {
    "empty": [],
    "testing_above": _mk("w" * 12 + "l" * 8),
    "testing_below": _mk("w" * 8 + "l" * 12),
    "retired": _mk("w" * 16 + "l" * 14),
    "passed": _mk("w" * 40 + "l" * 20),
}


@pytest.mark.parametrize("name", list(_LEDGERS))
def test_render_never_advises(name):
    stats = compute_stats(_LEDGERS[name], SIGNAL_V1_DEFAULT)
    for text in (render_signal_line(stats), render_signal_full(stats)):
        low = text.lower()
        for pattern in _BANNED:
            assert not re.search(pattern, low), f"{name}: render advised — matched {pattern!r}"


@pytest.mark.parametrize("name", list(_LEDGERS))
def test_label_is_mandatory(name):
    stats = compute_stats(_LEDGERS[name], SIGNAL_V1_DEFAULT)
    assert render_signal_line(stats).startswith(EXPERIMENT_LABEL)
    assert EXPERIMENT_LABEL in render_signal_full(stats)


def test_line_states_condition_and_record():
    stats = compute_stats(_mk("w" * 12 + "l" * 8), SIGNAL_V1_DEFAULT)
    line = render_signal_line(stats)
    assert "experiment" in line
    assert "FAVORABLE" in line or "UNFAVORABLE" in line
    assert "Record so far: 12–8" in line
    assert "shadow P&L +$62.00" in line
    assert "Tracking above coin-flip" in line


def test_retired_renders_permanently():
    # Retired at 30 then a long win streak — the record still renders RETIRED.
    stats = compute_stats(_mk("w" * 16 + "l" * 14 + "w" * 40), SIGNAL_V1_DEFAULT)
    assert stats["status"] == "RETIRED"
    assert "RETIRED — failed its gate" in render_signal_line(stats)


def test_full_render_carries_rule_and_disclaimer():
    stats = compute_stats(_mk("w" * 5 + "l" * 3), SIGNAL_V1_DEFAULT)
    full = render_signal_full(stats)
    assert "FAVORABLE iff TSLA close > SMA50" in full        # the registered rule
    assert "shadow only" in full and "not advice" in full     # the disclaimer
    assert "Omar's alone" in full                              # promotion is Omar's
