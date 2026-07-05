"""Unit tests for quant.valuation — the Stage-7 scenario engine (pure; no DB).

The load-bearing checks:
- hand-checked scenario math on clean numbers (every intermediate verified);
- the reverse-DCF ROUND-TRIP: a price constructed from growth g must solve back
  to exactly g — the closed-form inversion is only trustworthy if it inverts;
- Law-4 posture: base-rate flags, bear-weighted MoS, assumptions echoed verbatim;
- Law-2/7 posture: missing inputs -> renderable=False with reasons, basis-labeled
  owner earnings, a malformed grid rejected loudly with every problem named.
"""

import pytest

from quant.valuation import (
    owner_earnings_series,
    reverse_dcf,
    run_valuation,
    scenario_value,
    valuation_inputs,
    validate_assumptions,
)

GRID = {
    "horizon_years": 5,
    "required_return": 0.10,
    "weights": {"bear": 0.50, "base": 0.35, "bull": 0.15},
    "base_rate_cagr_flag": 0.20,
    "scenarios": {
        "bear": {"revenue_cagr": 0.00, "terminal_margin": 0.05, "exit_multiple": 12.0, "annual_dilution": 0.03},
        "base": {"revenue_cagr": 0.10, "terminal_margin": 0.10, "exit_multiple": 20.0, "annual_dilution": 0.00},
        "bull": {"revenue_cagr": 0.20, "terminal_margin": 0.12, "exit_multiple": 25.0, "annual_dilution": 0.00},
    },
}


def _row(pe: str, value: float) -> dict:
    return {"period_end": pe, "value": value, "accn": f"accn-{pe}", "filed": "2026-01-29"}


def _pack(price: float | None = 30.0) -> dict:
    """A minimal frozen pack: rev 100, NI 8, D&A 5, capex 3 -> OE 10; 10 shares."""
    return {
        "symbol": "X",
        "price": {"close": price, "date": "2026-07-02", "source": "prices_eod"},
        "series": {
            "revenue": [_row("2024-12-31", 90.0), _row("2025-12-31", 100.0)],
            "net_income": [_row("2025-12-31", 8.0)],
            "depreciation_amortization": [_row("2025-12-31", 5.0)],
            "capex": [_row("2025-12-31", 3.0)],
            "operating_cash_flow": [_row("2025-12-31", 14.0)],
            "shares_diluted": [_row("2025-12-31", 10.0)],
        },
    }


# --------------------------------------------------------------------------- #
# scenario_value — hand-checked chain
# --------------------------------------------------------------------------- #


def test_scenario_math_hand_checked():
    # rev 100 @ 10%/5y = 161.051; x10% margin = 16.1051 earnings; x20 = 322.102
    # equity; / 10 shares (no dilution) = 32.2102 future; / 1.1^5 (=1.61051) = 20.00.
    s = scenario_value(100.0, 10.0, GRID["scenarios"]["base"], 5, 0.10)
    assert s["revenue_h"] == pytest.approx(161.051)
    assert s["earnings_h"] == pytest.approx(16.1051)
    assert s["equity_h"] == pytest.approx(322.102)
    assert s["shares_h"] == pytest.approx(10.0)
    assert s["per_share_future"] == pytest.approx(32.2102)
    assert s["per_share_pv"] == pytest.approx(20.0)
    assert s["assumptions"] == GRID["scenarios"]["base"]  # echoed verbatim (Law 4)


def test_dilution_compounds_against_per_share_value():
    diluting = {**GRID["scenarios"]["base"], "annual_dilution": 0.10}
    s = scenario_value(100.0, 10.0, diluting, 5, 0.10)
    assert s["shares_h"] == pytest.approx(10.0 * 1.1**5)
    assert s["per_share_pv"] == pytest.approx(20.0 / 1.1**5)


# --------------------------------------------------------------------------- #
# reverse_dcf — the round-trip is the whole point
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("g", [0.00, 0.07, 0.20, -0.05])
def test_reverse_dcf_round_trips(g):
    base = GRID["scenarios"]["base"]
    forward = scenario_value(100.0, 10.0, {**base, "revenue_cagr": g}, 5, 0.10)
    solved = reverse_dcf(forward["per_share_pv"], 100.0, 10.0, base, 5, 0.10)
    assert solved["implied_revenue_cagr"] == pytest.approx(g, abs=1e-12)


def test_reverse_dcf_unsolvable_is_explicit():
    assert reverse_dcf(0.0, 100.0, 10.0, GRID["scenarios"]["base"], 5, 0.10)[
        "implied_revenue_cagr"
    ] is None
    negative_margin = {**GRID["scenarios"]["base"], "terminal_margin": -0.05}
    out = reverse_dcf(30.0, 100.0, 10.0, negative_margin, 5, 0.10)
    assert out["implied_revenue_cagr"] is None and "unsolvable" in out["reason"]


# --------------------------------------------------------------------------- #
# owner earnings — basis labeling (Law 7 visibility)
# --------------------------------------------------------------------------- #


def test_owner_earnings_prefers_ni_da_capex_and_labels_fallback():
    series = _pack()["series"]
    (row,) = owner_earnings_series(series)
    assert row["owner_earnings"] == pytest.approx(10.0)  # 8 + 5 - 3
    assert row["basis"] == "ni_da_capex"

    del series["depreciation_amortization"]
    (fallback,) = owner_earnings_series(series)
    assert fallback["owner_earnings"] == pytest.approx(11.0)  # OCF 14 - capex 3
    assert fallback["basis"] == "ocf_minus_capex"


def test_owner_earnings_omits_unalignable_years():
    series = {"net_income": [_row("2025-12-31", 8.0)]}  # nothing to pair with
    assert owner_earnings_series(series) == []


# --------------------------------------------------------------------------- #
# run_valuation — the assembled output
# --------------------------------------------------------------------------- #


def test_run_valuation_full_output_shape_and_mos():
    v = run_valuation(_pack(price=30.0), GRID)
    assert v["renderable"] is True
    # bear: 100 * 0.05 * 12 / (10*1.03^5) / 1.1^5 = hand-checkable
    bear_pv = 100.0 * 0.05 * 12.0 / (10.0 * 1.03**5) / 1.1**5
    assert v["scenarios"]["bear"]["per_share_pv"] == pytest.approx(bear_pv)
    base_pv, bull_ps = v["scenarios"]["base"]["per_share_pv"], v["scenarios"]["bull"]["per_share_pv"]
    assert bear_pv < base_pv < bull_ps  # monotone grid -> monotone range
    weighted = 0.5 * bear_pv + 0.35 * base_pv + 0.15 * bull_ps
    assert v["weighted_value_per_share"] == pytest.approx(weighted)
    assert v["margin_of_safety_pct"] == pytest.approx(1 - 30.0 / weighted)
    assert v["assumption_grid"] == GRID  # the grid ships with its consequences
    assert v["inputs"]["owner_earnings_margin_0"] == pytest.approx(0.10)


def test_base_rate_flag_fires_only_above_threshold():
    v = run_valuation(_pack(), GRID)
    assert v["base_rate_flags"] == []  # bull is exactly 20%, not above
    hot = {**GRID, "scenarios": {**GRID["scenarios"], "bull": {**GRID["scenarios"]["bull"], "revenue_cagr": 0.25}}}
    v2 = run_valuation(_pack(), hot)
    assert len(v2["base_rate_flags"]) == 1 and "'bull'" in v2["base_rate_flags"][0]


def test_sensitivity_names_the_biggest_mover():
    v = run_valuation(_pack(), GRID)
    spreads = {var: s["spread"] for var, s in v["sensitivity"].items()}
    assert v["biggest_mover"] == max(spreads, key=spreads.get)
    assert all(s is not None for s in spreads.values())


def test_missing_revenue_is_not_renderable_with_reason():
    pack = _pack()
    pack["series"]["revenue"] = []
    v = run_valuation(pack, GRID)
    assert v["renderable"] is False
    assert "revenue" in v["reason"]
    assert "scenarios" not in v  # no numbers rendered off missing inputs (Law 2)


def test_missing_price_still_renders_range_but_no_mos_or_reverse_dcf():
    v = run_valuation(_pack(price=None), GRID)
    assert v["renderable"] is True  # the value RANGE needs no price...
    assert v["margin_of_safety_pct"] is None  # ...but price-relative outputs do
    assert v["reverse_dcf"]["implied_revenue_cagr"] is None


def test_mismatched_fiscal_years_are_noted():
    pack = _pack()
    pack["series"]["net_income"] = [_row("2024-12-31", 7.0)]
    pack["series"]["depreciation_amortization"] = [_row("2024-12-31", 5.0)]
    pack["series"]["capex"] = [_row("2024-12-31", 3.0)]
    pack["series"]["operating_cash_flow"] = []
    notes = valuation_inputs(pack)["notes"]
    assert any("different fiscal years" in n for n in notes)


# --------------------------------------------------------------------------- #
# grid validation — malformed config fails loud (Law 7)
# --------------------------------------------------------------------------- #


def test_validate_names_every_problem():
    broken = {"horizon_years": 5, "scenarios": {"bear": {"revenue_cagr": "high"}}}
    with pytest.raises(ValueError) as exc:
        validate_assumptions(broken)
    message = str(exc.value)
    for fragment in ("required_return", "weights", "base_rate_cagr_flag",
                     "scenario 'bear': 'revenue_cagr' must be finite",
                     "missing scenario 'base'"):
        assert fragment in message


def test_validate_accepts_the_seed_grid():
    from analyst.seed_valuation import VALUATION_ASSUMPTIONS

    validate_assumptions(VALUATION_ASSUMPTIONS)  # must not raise


# --------------------------------------------------------------------------- #
# range validation — the P2 review's confirmed cluster
# --------------------------------------------------------------------------- #


def _grid_with(path: tuple, value) -> dict:
    """A deep-copied GRID with one leaf replaced (path like ('scenarios','bear','x'))."""
    import copy

    grid = copy.deepcopy(GRID)
    node = grid
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    return grid


@pytest.mark.parametrize(
    "path,value",
    [
        (("scenarios", "bear", "annual_dilution"), -2.0),  # -> negative shares -> complex g
        (("scenarios", "base", "terminal_margin"), -0.05),
        (("scenarios", "base", "terminal_margin"), 0.0),
        (("scenarios", "bull", "exit_multiple"), -20.0),
        (("scenarios", "bull", "revenue_cagr"), -1.0),
        (("scenarios", "bull", "revenue_cagr"), float("inf")),
        (("required_return",), -1.0),  # -> ZeroDivisionError in discounting
        (("required_return",), -0.5),
        (("horizon_years",), 0),
        (("horizon_years",), -5),
        (("weights", "bear"), -0.1),
    ],
)
def test_out_of_range_grid_values_are_rejected(path, value):
    with pytest.raises(ValueError):
        validate_assumptions(_grid_with(path, value))


def test_weights_must_sum_to_one():
    with pytest.raises(ValueError, match="sum to 1"):
        validate_assumptions(_grid_with(("weights", "bear"), 0.9))


def test_non_monotone_scenarios_are_rejected_and_named():
    with pytest.raises(ValueError, match="revenue_cagr.*ascend"):
        validate_assumptions(_grid_with(("scenarios", "bear", "revenue_cagr"), 0.5))
    with pytest.raises(ValueError, match="annual_dilution.*descend"):
        validate_assumptions(_grid_with(("scenarios", "bull", "annual_dilution"), 0.4))


def test_reverse_dcf_guards_direct_callers_against_complex_growth():
    # Grid validation covers the config path; a hand-built base must not yield a
    # complex number either (dilution <= -1 with an odd horizon).
    hostile = {**GRID["scenarios"]["base"], "annual_dilution": -2.0}
    out = reverse_dcf(30.0, 100.0, 10.0, hostile, 5, 0.10)
    assert out["implied_revenue_cagr"] is None


def test_negative_revenue_is_not_renderable():
    pack = _pack()
    pack["series"]["revenue"] = [_row("2025-12-31", -5.0)]
    v = run_valuation(pack, GRID)
    assert v["renderable"] is False
    assert "non-positive" in v["reason"]


def test_none_period_end_rows_never_pair_into_fake_owner_earnings():
    series = {
        "net_income": [{"period_end": None, "value": 8.0}],
        "depreciation_amortization": [{"period_end": None, "value": 5.0}],
        "capex": [{"period_end": None, "value": 3.0}],
        "operating_cash_flow": [],
    }
    assert owner_earnings_series(series) == []
