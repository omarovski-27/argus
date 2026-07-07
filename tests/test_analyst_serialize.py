"""Serializer tests (analyst/serialize.py) — the DATA block IS the grounding whitelist.

Two properties matter (Law 2, both directions):
  1. Everything the dossier may cite is PRINTED in the block — so a sentence citing
     a serialized figure passes ``digest.grounding.validate_text`` against it.
  2. Absences render as explicit "not available" lines, never fabricated values.
Plus the margin-of-safety display cap: a deeply negative MoS renders as the
relationship with both anchors printed (the "-1020%" render fix), not as noise.
"""

import copy

from analyst.serialize import serialize_analysis
from digest.grounding import validate_text
from quant.valuation import run_valuation

# Mirrors tests/test_valuation.py's conventions: a minimal frozen pack whose
# valuation math is hand-checkable (rev 100, NI 8, D&A 5, capex 3 -> OE 10; 10 shares).
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


def _pack(price: float | None = 15.0) -> dict:
    return {
        "symbol": "X",
        "cik": "0000000001",
        "price": {"close": price, "date": "2026-07-02", "source": "prices_eod"},
        "series": {
            "revenue": [_row("2024-12-31", 90.0), _row("2025-12-31", 100.0)],
            "gross_profit": [_row("2025-12-31", 40.0)],
            "operating_income": [_row("2025-12-31", 12.0)],
            "net_income": [_row("2025-12-31", 8.0)],
            "operating_cash_flow": [_row("2025-12-31", 14.0)],
            "capex": [_row("2025-12-31", 3.0)],
            "depreciation_amortization": [_row("2025-12-31", 5.0)],
            "total_assets": [_row("2025-12-31", 120.0)],
            "total_liabilities": [_row("2025-12-31", 60.0)],
            "shares_diluted": [_row("2025-12-31", 10.0)],
        },
        "metrics": {
            "margins": [
                {"period_end": "2025-12-31", "gross_margin": 0.40, "operating_margin": 0.12, "net_margin": 0.08}
            ],
            "revenue_cagr": {"3": {"value": 0.111, "reason": None}},
            "earnings_consistency": {
                "years_covered": 2, "first_period_end": "2024-12-31",
                "last_period_end": "2025-12-31", "loss_years": 0, "profit_years": 2,
            },
            "fcf_proxy": [{"period_end": "2025-12-31", "fcf": 11.0, "basis": "ocf_minus_capex"}],
            "eps_history": [{"period_end": "2025-12-31", "eps": 0.80}],
        },
        "peers": {"symbol": "X", "peers": ["Y"], "source": "config",
                  "table": [], "missing_fundamentals": ["Y"]},
        "estimates": {"price_targets": {"mean": 18.0, "median": 17.5, "low": 9.0, "high": 30.0, "current": 15.0}},
        "news": {"window_days": 14, "headlines": []},
        "source_health": {"cik": "success", "fundamentals": "success"},
        "filings": {"10k": {"note": "no 10-K on record at EDGAR"},
                    "def14a": {"note": "no DEF 14A on record at EDGAR"}},
    }


def _block(price: float | None = 15.0) -> str:
    pack = _pack(price)
    return serialize_analysis(pack, run_valuation(pack, GRID))


# --------------------------------------------------------------------------- #
# Margin-of-safety display cap (the "-1020%" render fix)
# --------------------------------------------------------------------------- #
def test_deeply_negative_mos_renders_relationship_not_noise():
    block = _block(price=300.0)  # price far above any scenario value
    assert "not meaningful as a percentage" in block
    assert "sits far above the bear-weighted estimate" in block
    # Both anchors are printed, so both ground (Law 2 both directions).
    assert "300.00" in block
    # No absurd percentage anywhere in the MoS line.
    assert "margin of safety vs current price" not in block


def test_normal_mos_renders_as_percentage():
    block = _block(price=15.0)
    assert "margin of safety vs current price:" in block
    assert "not meaningful as a percentage" not in block


def test_missing_price_mos_not_computable():
    block = _block(price=None)
    assert "margin of safety: not computable (no current price)" in block


def test_non_renderable_valuation_states_reason():
    pack = _pack()
    pack["series"]["revenue"] = []
    block = serialize_analysis(pack, run_valuation(pack, GRID))
    assert "valuation: not renderable —" in block


# --------------------------------------------------------------------------- #
# Absences are explicit "not available" lines — never filled
# --------------------------------------------------------------------------- #
def test_sparse_pack_names_every_gap():
    pack = {
        "symbol": "ZZZ", "cik": None,
        "price": {"close": None, "date": None, "source": None},
        "series": {}, "metrics": {}, "peers": {},
        "estimates": {}, "news": {}, "source_health": {},
        "filings": {"note": "CIK unresolved: no EDGAR filings (reduced-depth dossier)"},
    }
    block = serialize_analysis(pack, {"renderable": False, "reason": "missing base inputs"})
    assert "SEC CIK unresolved" in block
    assert "(no filed annual fundamentals — not available)" in block
    assert "margins: not available" in block
    assert "consensus/sentiment context: not available" in block
    assert "valuation: not renderable — missing base inputs" in block


def test_filed_gaps_render_as_na_not_zero():
    pack = _pack()
    pack["estimates"] = {"price_targets": {"mean": None, "median": None, "low": None, "high": None, "current": None}}
    block = serialize_analysis(pack, run_valuation(pack, GRID))
    assert "mean n/a" in block


# --------------------------------------------------------------------------- #
# Derived-display rule: derivations the stages call for are computed IN the block
# (first TSLA run: the model's own equity subtraction and peer spreads failed the
# grounding gate — these lines are the fix, and they must ground)
# --------------------------------------------------------------------------- #
def test_equity_is_precomputed_per_fiscal_year():
    block = _block()
    assert "equity (assets minus liabilities) 60" in block
    assert validate_text("Equity stood at 60 at fiscal year end.", block) == []


def test_equity_line_absent_when_either_side_is_missing():
    pack = _pack()
    pack["series"]["total_liabilities"] = []
    block = serialize_analysis(pack, run_valuation(pack, GRID))
    assert "equity (assets minus liabilities)" not in block


def test_peer_gross_margin_spreads_are_precomputed():
    pack = _pack()
    pack["peers"] = {
        "symbol": "X", "peers": ["Y", "Z"], "source": "config",
        "table": [
            {"symbol": "X", "period_end": "2025-12-31", "gross_margin": 0.40},
            {"symbol": "Y", "period_end": "2025-12-31", "gross_margin": 0.288},
            {"symbol": "Z", "period_end": "2025-12-31", "gross_margin": None},
        ],
        "missing_fundamentals": ["Z"],
    }
    block = serialize_analysis(pack, run_valuation(pack, GRID))
    assert "gross-margin spread, X minus Y: +11.2 percentage points" in block
    assert "X minus Z" not in block  # no fabricated spread for a missing margin
    # The spread the first TSLA dossier invented now grounds when cited from the block.
    assert validate_text("The margin spread against Y is 11.2 percentage points.", block) == []


# --------------------------------------------------------------------------- #
# The block is the grounding whitelist: serialized figures pass validate_text
# --------------------------------------------------------------------------- #
def test_serialized_figures_ground_against_the_block():
    block = _block()
    cited = (
        "Revenue reached 100 in FY 2025-12-31 against 90 the prior year, a 40.0% gross "
        "margin; FCF was 11 on an OCF-minus-capex basis. The mean analyst target is 18.00 "
        "with the close at 15.00."
    )
    assert validate_text(cited, block) == []


def test_model_invented_figure_fails_against_the_block():
    block = _block()
    violations = validate_text("Deliveries grew 37% year over year.", block)
    assert [v["token"] for v in violations] == ["37"]
