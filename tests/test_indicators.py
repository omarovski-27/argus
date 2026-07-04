"""Unit tests for ingestion.indicators — the young-ticker suppression guards (no DB).

Everything runs through the pure ``_indicator_rows`` on synthetic frames. Two contracts:

1. ``_MIN_PERIODS`` sits at/above pandas_ta's internal floors. Below its floor pandas_ta
   returns None — not a NaN series — and a guard that admits fewer sessions hands None
   to the row loop. That was the 2026-07-03 Daily Data #14 crash: SPCX's 14th session
   passed the old ``>= 14`` rsi14 guard, but ``ta.rsi`` needs length+1 = 15 closes, so
   the job died on ``zip(frame["date"], None)`` and SPY/QQQ were never computed. The
   boundary test is parametrized over every indicator: at floor-1 sessions the name is
   absent WITHOUT raising; at the floor it yields at least one row. A pandas-ta upgrade
   that moves a floor turns this red in CI (push) instead of in the 20:30 UTC job.
2. A None series is suppression-by-omission (§4), never a crash — even if a floor
   drifts under our guard again, the loop skips it (Law 7: it prints, it never aborts).
"""

import pandas as pd
import pytest

from ingestion.indicators import _MIN_PERIODS, _indicator_rows


def _frame(sessions: int) -> pd.DataFrame:
    """A cleaned (date, close) frame shaped like compute_indicators builds it:
    ISO date strings, float closes with enough wiggle that nothing degenerates."""
    dates = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2025-01-02", periods=sessions)]
    closes = [100.0 + (i % 7) - (i % 3) + i * 0.05 for i in range(sessions)]
    return pd.DataFrame({"date": dates, "close": closes})


def _names(rows: list[dict]) -> set[str]:
    return {r["name"] for r in rows}


@pytest.mark.parametrize("name,floor", sorted(_MIN_PERIODS.items()))
def test_boundary_matches_pandas_ta_floor(name: str, floor: int):
    below = _indicator_rows("TEST", _frame(floor - 1))  # must not raise
    assert name not in _names(below)
    at_floor = _indicator_rows("TEST", _frame(floor))
    assert name in _names(at_floor)


def test_fourteen_sessions_yields_nothing_and_does_not_crash():
    # Regression memorial for Daily Data #14 (2026-07-03): SPCX at exactly 14 sessions
    # must produce zero rows — not a TypeError that aborts the whole job mid-loop.
    assert _indicator_rows("SPCX", _frame(14)) == []


def test_row_shape_and_values_are_real():
    rows = _indicator_rows("TEST", _frame(60))  # sma50 + rsi14 + macd family; no sma200
    assert _names(rows) == {"sma50", "rsi14", "macd", "macd_signal", "macd_hist"}
    for row in rows:
        assert set(row) == {"symbol", "date", "name", "value"}
        assert row["symbol"] == "TEST"
        assert isinstance(row["value"], float) and not pd.isna(row["value"])


def test_none_series_is_suppressed_not_iterated(monkeypatch):
    # Structural half of the fix, independent of any particular pandas_ta version: a
    # None where a series was expected is suppression, and the symbol's OTHER
    # indicators still compute (on 2026-07-03 the crash also starved SPY/QQQ).
    import ingestion.indicators as mod

    monkeypatch.setattr(mod.ta, "rsi", lambda *args, **kwargs: None)
    rows = _indicator_rows("TEST", _frame(60))
    assert "rsi14" not in _names(rows)
    assert "sma50" in _names(rows)
