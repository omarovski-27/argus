"""Signal Lab ledger stats + verdict-gate tests (siglab/ledger.py).

The gates mirror the journal (Law 6): pre-registered, monotonic, terminal. N counts only
triggered (win/loss) days. RETIRED is decided on the fixed first-30 prefix so it can never
un-retire; PASS on the first-60 prefix."""

import pytest
from postgrest.exceptions import APIError

from siglab.job import ledger_table_exists
from siglab.ledger import compute_stats, derive_status
from siglab.registry import SIGNAL_V1_DEFAULT, signal_gates

_GATES = signal_gates()
_FEE = SIGNAL_V1_DEFAULT["params"]["fee_per_round_trip"]

# v1's blob is FINALIZED INCONCLUSIVE (authoritative over the gates). The gate + counting
# machinery is still exercised for a hypothetical scored signal_v2 via a NON-finalized blob.
_TESTING_BLOB = {**SIGNAL_V1_DEFAULT, "status": "testing"}


def _mk(seq: str) -> list[dict]:
    """A date-ascending triggered ledger from a 'w'/'l' string (e.g. 'wwl')."""
    rows = []
    for i, ch in enumerate(seq):
        rows.append({
            "date": f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}",
            "signal_state": "FAVORABLE",
            "outcome": "win" if ch == "w" else "loss",
            "shadow_pnl": 23.50 if ch == "w" else -27.50,
        })
    return rows


def _status(seq: str) -> str:
    return derive_status(_mk(seq), _GATES, _FEE)


# --------------------------------------------------------------------------- #
# N=30 retire gate
# --------------------------------------------------------------------------- #
def test_under_30_triggers_stays_testing():
    assert _status("w" * 20 + "l" * 9) == "testing"   # 29 triggered


def test_strong_record_survives_30():
    assert _status("w" * 20 + "l" * 10) == "testing"   # 0.667, pnl +195 → survives, not yet 60


def test_weak_record_retires_at_30():
    # 16w/14l: winrate 0.533 (<0.55) and pnl -9 (<=0) → RETIRED.
    assert _status("w" * 16 + "l" * 14) == "RETIRED"


def test_retire_is_monotonic_and_permanent():
    # First 30 fail; then 30 straight wins. The fixed first-30 prefix still fails, so it
    # stays RETIRED forever (the 'renders permanently' requirement).
    assert _status("w" * 16 + "l" * 14 + "w" * 30) == "RETIRED"


# --------------------------------------------------------------------------- #
# N=60 pass gate
# --------------------------------------------------------------------------- #
def test_clears_the_pass_gate_at_60():
    # first-30 = 30 wins (survives); 60 total 40w/20l: winrate 0.667>=0.58,
    # pnl 40*23.5 - 20*27.5 = 390 > fee*60*0.5 = 60 → PASS.
    assert _status("w" * 40 + "l" * 20) == "PASS"


def test_middling_60_stays_testing():
    # 34w/26l: winrate 0.567 — below the 0.58 pass bar but above the 0.55 retire bar,
    # pnl +84 > 0 → neither PASS nor RETIRED.
    assert _status("w" * 34 + "l" * 26) == "testing"


def test_decayed_record_retires_by_60():
    # first-30 = 20w/10l survives; then 30 losses → 20w/40l, winrate 0.333 → RETIRED.
    assert _status("w" * 20 + "l" * 10 + "l" * 30) == "RETIRED"


# --------------------------------------------------------------------------- #
# compute_stats — full shape, labels, non-triggered counting
# --------------------------------------------------------------------------- #
def _ledger_with_context() -> list[dict]:
    rows = _mk("w" * 12 + "l" * 8)                      # 20 triggered, 12-8
    rows.append({"date": "2026-06-01", "signal_state": "FAVORABLE",
                 "outcome": "no_trigger", "shadow_pnl": 0.0})
    rows.append({"date": "2026-06-02", "signal_state": "UNFAVORABLE",
                 "outcome": "no_trigger", "shadow_pnl": 0.0})
    rows.append({"date": "2026-06-03", "signal_state": "FAVORABLE",
                 "outcome": "unknown", "shadow_pnl": None})
    return rows


def test_compute_stats_counts_and_labels():
    stats = compute_stats(_ledger_with_context(), _TESTING_BLOB)
    assert stats["wins"] == 12 and stats["losses"] == 8
    assert stats["n_triggered"] == 20
    assert stats["winrate"] == pytest.approx(0.6)
    assert stats["no_trigger"] == 2 and stats["unknown"] == 1
    assert stats["n_days"] == 23              # 20 triggered + 3 context rows
    assert stats["cum_pnl"] == pytest.approx(12 * 23.50 - 8 * 27.50)  # +62.0
    assert stats["today_state"] == "FAVORABLE"       # last row by date
    assert stats["status"] == "testing"
    assert stats["evidence_label"] == "Tracking above coin-flip"   # winrate 0.6 > 0.5


def test_below_coinflip_label():
    stats = compute_stats(_mk("w" * 8 + "l" * 12), _TESTING_BLOB)  # 0.4
    assert stats["evidence_label"] == "No evidence of edge yet"


def test_empty_ledger_is_no_evidence():
    stats = compute_stats([], _TESTING_BLOB)
    assert stats["n_days"] == 0 and stats["winrate"] is None
    assert stats["status"] == "testing"
    assert stats["evidence_label"] == "No evidence of edge yet"
    assert stats["next_gate"]["n"] == 30 and stats["next_gate"]["remaining"] == 30


def test_retired_and_pass_labels():
    assert compute_stats(_mk("w" * 16 + "l" * 14), _TESTING_BLOB)["evidence_label"] \
        == "RETIRED — failed its gate"
    passed = compute_stats(_mk("w" * 40 + "l" * 20), _TESTING_BLOB)
    assert passed["status"] == "PASS"
    assert "promotion is Omar's alone" in passed["evidence_label"]
    assert passed["next_gate"] is None


# --------------------------------------------------------------------------- #
# FINALIZED status — the blob's INCONCLUSIVE verdict is authoritative & permanent
# --------------------------------------------------------------------------- #
def test_finalized_status_overrides_gate_verdict():
    # Even a 'winning' scored ledger cannot un-finalize v1: the blob says INCONCLUSIVE.
    stats = compute_stats(_mk("w" * 40 + "l" * 20), SIGNAL_V1_DEFAULT)
    assert stats["status"] == "INCONCLUSIVE"
    assert stats["evidence_label"] == "INCONCLUSIVE — daily data can't score this bracket"
    assert stats["next_gate"] is None
    assert "unmeasurable at daily resolution" in stats["status_reason"]
    assert "actual journal round-trips" in stats["promotion_path"]


def test_finalized_status_holds_for_state_only_ledger():
    # The real forward-only ledger: state rows, all outcome 'unknown' → still INCONCLUSIVE,
    # no wins/losses fabricated.
    rows = [{"date": "2026-07-15", "signal_state": "UNFAVORABLE", "outcome": "unknown",
             "shadow_pnl": None},
            {"date": "2026-07-16", "signal_state": "FAVORABLE", "outcome": "unknown",
             "shadow_pnl": None}]
    stats = compute_stats(rows, SIGNAL_V1_DEFAULT)
    assert stats["status"] == "INCONCLUSIVE"
    assert stats["wins"] == 0 and stats["losses"] == 0 and stats["n_triggered"] == 0
    assert stats["today_state"] == "FAVORABLE"   # last row by date


# --------------------------------------------------------------------------- #
# ledger_table_exists — the pre-migration graceful-skip guard (siglab/job.py)
# --------------------------------------------------------------------------- #
class _FakeTable:
    def __init__(self, exc=None):
        self._exc = exc

    def select(self, *_a, **_k):
        return self

    def limit(self, *_a):
        return self

    def execute(self):
        if self._exc is not None:
            raise self._exc
        return type("R", (), {"data": []})()


class _FakeClient:
    def __init__(self, exc=None):
        self._exc = exc

    def table(self, _name):
        return _FakeTable(self._exc)


def test_ledger_table_exists_true_when_queryable():
    assert ledger_table_exists(_FakeClient()) is True


def test_ledger_table_exists_false_on_missing_table():
    # PGRST205 = relation absent (pre-migration) → benign pending state, not a failure.
    exc = APIError({"message": "Could not find the table", "code": "PGRST205"})
    assert ledger_table_exists(_FakeClient(exc)) is False


def test_ledger_table_exists_reraises_other_api_errors():
    # Any non-missing-table error must surface (Law 7) — never swallowed as 'absent'.
    exc = APIError({"message": "permission denied", "code": "42501"})
    with pytest.raises(APIError):
        ledger_table_exists(_FakeClient(exc))
