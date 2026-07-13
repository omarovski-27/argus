"""Signal Lab rule + shadow-scorer tests (siglab/rule.py, siglab/shadow.py).

The rule is the pre-registered hypothesis; the scorer is the conservative shadow. Both are
pure and fail loud on missing inputs (never default a missing input to False/win)."""

import pytest

from siglab.rule import MissingInputError, SignalInputs, evaluate_signal
from siglab.shadow import LOSS, NO_TRIGGER, WIN, score_bracket

_P = {"bracket": 1.50, "shares": 17, "fee_per_round_trip": 2.00}


# --------------------------------------------------------------------------- #
# Rule
# --------------------------------------------------------------------------- #
def _fav(**over) -> SignalInputs:
    base = dict(close=110.0, sma50=100.0, macd_hist=2.0, macd_hist_prev=1.0,
                event_clear=True, vix_percentile=50.0)
    base.update(over)
    return SignalInputs(**base)


def test_all_conditions_met_is_favorable():
    r = evaluate_signal(_fav())
    assert r["state"] == "FAVORABLE"
    assert all(r["conditions"].values())


@pytest.mark.parametrize("over", [
    dict(close=90.0),                         # not above sma50
    dict(macd_hist=1.0, macd_hist_prev=1.0),  # hist not rising (equal)
    dict(macd_hist=0.5, macd_hist_prev=1.0),  # hist falling
    dict(event_clear=False),                  # event filter armed next session
    dict(vix_percentile=80.0),                # not < 80 (boundary)
    dict(vix_percentile=95.0),                # extreme fear
])
def test_any_failing_leg_is_unfavorable(over):
    assert evaluate_signal(_fav(**over))["state"] == "UNFAVORABLE"


def test_vix_boundary_is_strict_less_than():
    assert evaluate_signal(_fav(vix_percentile=79.9))["state"] == "FAVORABLE"
    assert evaluate_signal(_fav(vix_percentile=80.0))["state"] == "UNFAVORABLE"


def test_missing_input_raises_and_lists_names():
    with pytest.raises(MissingInputError):
        evaluate_signal(_fav(sma50=None))
    inp = SignalInputs(None, 100.0, 2.0, 1.0, True, None)
    assert set(inp.missing()) == {"close", "vix_percentile"}


# --------------------------------------------------------------------------- #
# Shadow scorer — conservative bracket
# --------------------------------------------------------------------------- #
def test_win_when_only_target_touched():
    outcome, pnl = score_bracket(100.0, 102.0, 99.5, **_P)  # high>=101.5, low>98.5
    assert outcome == WIN
    assert pnl == pytest.approx(1.50 * 17 - 2.00)   # +23.50


def test_loss_when_only_stop_touched():
    outcome, pnl = score_bracket(100.0, 101.0, 98.0, **_P)  # low<=98.5, high<101.5
    assert outcome == LOSS
    assert pnl == pytest.approx(-1.50 * 17 - 2.00)  # -27.50


def test_both_bands_touched_is_conservative_loss():
    outcome, pnl = score_bracket(100.0, 102.0, 98.0, **_P)  # both bands
    assert outcome == LOSS and pnl == pytest.approx(-27.50)


def test_neither_band_is_no_trigger_zero_pnl():
    outcome, pnl = score_bracket(100.0, 101.0, 99.0, **_P)  # neither
    assert outcome == NO_TRIGGER and pnl == 0.0


def test_unit_economics_match_the_blueprint():
    # Blueprint §8: net win +$23.50, net loss -$27.50 at 17 sh × $1.50 ∓ $2.00 fee.
    assert score_bracket(100.0, 102.0, 99.5, **_P)[1] == pytest.approx(23.50)
    assert score_bracket(100.0, 101.0, 98.0, **_P)[1] == pytest.approx(-27.50)


def test_missing_ohlc_raises():
    with pytest.raises(MissingInputError):
        score_bracket(100.0, None, 99.0, **_P)
