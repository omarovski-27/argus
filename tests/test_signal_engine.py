"""Signal Lab engine tests (siglab/engine.py) — the pure computation over stored series.

The engine drives both backfill and the nightly job, so its warmup handling, the as-of VIX
percentile, and the per-day FAVORABLE/UNFAVORABLE + scoring are pinned here without a DB."""

import pytest

from siglab.engine import build_ledger_rows, vix_percentile_asof
from siglab.registry import signal_params

_P = signal_params()  # defaults: vix_max 80, bracket 1.50, shares 17, fee 2.00


# --------------------------------------------------------------------------- #
# As-of VIX percentile (matches digest.bundle._vix_trailing)
# --------------------------------------------------------------------------- #
def _vix(pairs):
    return [{"date": d, "value": v} for d, v in pairs]


def test_vix_percentile_asof_basic():
    vix = _vix([("2026-01-01", 90), ("2026-01-02", 80), ("2026-01-03", 70),
                ("2026-01-04", 60), ("2026-01-05", 50)])
    # asof 01-05: current 50 is the min → 1/5 = 20th percentile.
    assert vix_percentile_asof(vix, "2026-01-05", 252) == 20
    # asof 01-01: only one session, current 90 → 100th.
    assert vix_percentile_asof(vix, "2026-01-01", 252) == 100


def test_vix_percentile_uses_only_dates_at_or_before_asof():
    vix = _vix([("2026-01-01", 50), ("2026-01-02", 10), ("2026-01-03", 30)])
    # asof 01-03: window [50,10,30], current 30, count(<=30)={10,30}=2/3 → 67.
    assert vix_percentile_asof(vix, "2026-01-03", 252) == 67
    # asof 01-02 ignores the later 01-03 row: [50,10], current 10 → 1/2 = 50.
    assert vix_percentile_asof(vix, "2026-01-02", 252) == 50


def test_vix_percentile_none_without_history():
    assert vix_percentile_asof([], "2026-01-01", 252) is None


# --------------------------------------------------------------------------- #
# build_ledger_rows — warmup, FAVORABLE→scored, UNFAVORABLE→state-only
# --------------------------------------------------------------------------- #
def _fixture():
    prices = [
        {"date": "2026-03-02", "open": 97.0, "high": 99.0, "low": 96.0, "close": 98.0},
        {"date": "2026-03-03", "open": 99.0, "high": 101.0, "low": 98.0, "close": 100.0},
        {"date": "2026-03-04", "open": 200.0, "high": 203.0, "low": 199.0, "close": 105.0},
        {"date": "2026-03-05", "open": 300.0, "high": 301.0, "low": 299.0, "close": 108.0},
    ]
    indicators = {
        "2026-03-02": {"sma50": 90.0, "macd_hist": 1.0},
        "2026-03-03": {"sma50": 92.0, "macd_hist": 2.0},   # close 100>92, hist 2>1
        "2026-03-04": {"sma50": 95.0, "macd_hist": 1.5},   # hist 1.5 < prev 2.0 → not rising
    }
    # VIX descending so the latest close sits low in its window (percentile < 80).
    vix = _vix([("2026-02-27", 90), ("2026-02-28", 80), ("2026-03-01", 70),
                ("2026-03-02", 60), ("2026-03-03", 50)])
    return prices, indicators, vix


def test_warmup_skip_then_favorable_win_then_unfavorable():
    prices, indicators, vix = _fixture()
    rows, warmup = build_ledger_rows(prices, indicators, vix, set(), _P)

    assert warmup == 1  # scoring 03-03 needs D-2 macd_hist (none) → warmup skip
    assert [r["date"] for r in rows] == ["2026-03-04", "2026-03-05"]

    fav = rows[0]
    assert fav["signal_state"] == "FAVORABLE"
    assert fav["outcome"] == "win"                 # 03-04 high 203 >= open 200 + 1.50
    assert fav["shadow_pnl"] == pytest.approx(23.50)
    assert fav["inputs_json"]["conditions"]["close_above_sma50"] is True

    unfav = rows[1]
    assert unfav["signal_state"] == "UNFAVORABLE"  # 03-04 macd_hist 1.5 < 03-03's 2.0
    assert unfav["outcome"] == "no_trigger" and unfav["shadow_pnl"] == 0.0


def test_event_filter_arming_flips_favorable_to_unfavorable():
    prices, indicators, vix = _fixture()
    # Arm the filter on the FAVORABLE scored day 03-04 → event not clear → UNFAVORABLE.
    rows, _ = build_ledger_rows(prices, indicators, vix, {"2026-03-04"}, _P)
    row = next(r for r in rows if r["date"] == "2026-03-04")
    assert row["signal_state"] == "UNFAVORABLE"
    assert row["outcome"] == "no_trigger"


def test_favorable_day_missing_ohlc_is_unknown():
    prices, indicators, vix = _fixture()
    prices[2] = {**prices[2], "high": None}  # 03-04 favorable but no high → unscored
    rows, _ = build_ledger_rows(prices, indicators, vix, set(), _P)
    row = next(r for r in rows if r["date"] == "2026-03-04")
    assert row["signal_state"] == "FAVORABLE"
    assert row["outcome"] == "unknown" and row["shadow_pnl"] is None
