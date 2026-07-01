"""Unit tests for quant.metrics — pure derivations on injected records (no DB).

Every test injects the ``data`` dict (concept -> records) so nothing touches
fundamentals_latest; the DB read path is exercised by the ``python -m quant.metrics``
live probe instead.
"""

import pytest

from quant.metrics import (
    earnings_consistency,
    eps_history,
    fcf_proxy,
    margins,
    revenue_cagr,
)


def _rec(period_end: str, value, **extra) -> dict:
    row = {
        "period_end": period_end,
        "value": value,
        "accn": f"accn-{period_end}",
        "filed": "2026-01-29",
    }
    row.update(extra)
    return row


def _data(**concepts) -> dict:
    """Build the full input dict; unnamed concepts default to empty series."""
    base = {
        c: []
        for c in (
            "revenue",
            "cost_of_revenue",
            "gross_profit",
            "operating_income",
            "net_income",
            "operating_cash_flow",
            "capex",
            "shares_diluted",
        )
    }
    base.update(concepts)
    return base


def test_margins_compute_and_carry_provenance():
    data = _data(
        revenue=[_rec("2024-12-31", 100.0)],
        cost_of_revenue=[_rec("2024-12-31", 80.0)],
        gross_profit=[_rec("2024-12-31", 20.0)],
        operating_income=[_rec("2024-12-31", 10.0)],
        net_income=[_rec("2024-12-31", 5.0)],
    )
    (m,) = margins("X", data)
    assert m["gross_margin"] == pytest.approx(0.20)
    assert m["operating_margin"] == pytest.approx(0.10)
    assert m["net_margin"] == pytest.approx(0.05)
    assert m["gross_profit_stored"] == m["gross_profit_computed"] == 20.0
    assert m["inputs"]["revenue"]["accn"] == "accn-2024-12-31"


def test_margins_missing_component_stays_visible():
    data = _data(
        revenue=[_rec("2024-12-31", 100.0)],
        net_income=[_rec("2024-12-31", 5.0)],
    )
    (m,) = margins("X", data)
    assert m["gross_margin"] is None  # no cost_of_revenue -> no invented margin
    assert m["operating_margin"] is None
    assert m["net_margin"] == pytest.approx(0.05)


def test_revenue_cagr_matches_horizon_years():
    data = _data(
        revenue=[
            _rec("2022-12-31", 100.0),
            _rec("2023-12-31", 120.0),
            _rec("2024-12-31", 150.0),
            _rec("2025-12-31", 200.0),
        ]
    )
    out = revenue_cagr("X", data, horizons=(3,))
    assert out[3]["value"] == pytest.approx((200.0 / 100.0) ** (1 / 3) - 1)
    assert out[3]["from"]["period_end"] == "2022-12-31"
    assert out[3]["to"]["period_end"] == "2025-12-31"


def test_revenue_cagr_missing_or_nonpositive_base_is_named():
    data = _data(revenue=[_rec("2024-12-31", -5.0), _rec("2025-12-31", 200.0)])
    out = revenue_cagr("X", data, horizons=(1, 5))
    assert out[1]["value"] is None and "<= 0" in out[1]["reason"]
    assert out[5]["value"] is None and "FY2020" in out[5]["reason"]


def test_earnings_consistency_counts_losses():
    data = _data(
        net_income=[
            _rec("2022-12-31", -10.0),
            _rec("2023-12-31", 5.0),
            _rec("2024-12-31", -1.0),
        ]
    )
    ec = earnings_consistency("X", data)
    assert ec["years_covered"] == 3
    assert ec["loss_years"] == 2
    assert ec["profit_years"] == 1
    assert [l["period_end"] for l in ec["losses"]] == ["2022-12-31", "2024-12-31"]


def test_fcf_proxy_subtracts_capex_when_present():
    data = _data(
        operating_cash_flow=[_rec("2024-12-31", 100.0)],
        capex=[_rec("2024-12-31", 30.0)],
    )
    (f,) = fcf_proxy("X", data)
    assert f["fcf"] == pytest.approx(70.0)
    assert f["basis"] == "ocf_minus_capex"
    assert f["capex_available"] is True


def test_fcf_proxy_flags_missing_capex_never_silent():
    data = _data(operating_cash_flow=[_rec("2024-12-31", 100.0)])
    (f,) = fcf_proxy("X", data)
    assert f["fcf"] == pytest.approx(100.0)  # degrades to OCF...
    assert f["basis"] == "ocf_only_capex_unavailable"  # ...but says so explicitly
    assert f["capex_available"] is False


def test_eps_uses_adjusted_shares_and_marks_missing():
    data = _data(
        net_income=[_rec("2024-12-31", 15.0), _rec("2025-12-31", 30.0)],
        shares_diluted=[_rec("2025-12-31", 10.0, split_factor=15.0)],
    )
    by_year = {e["period_end"]: e for e in eps_history("X", data)}
    assert by_year["2025-12-31"]["eps"] == pytest.approx(3.0)
    assert by_year["2025-12-31"]["shares_split_factor"] == 15.0
    assert by_year["2024-12-31"]["eps"] is None  # no shares row -> no invented EPS
