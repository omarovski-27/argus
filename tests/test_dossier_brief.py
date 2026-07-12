"""Brief-mode + length-resolution tests (analyst/dossier.py, module spec §3).

The full dossier is always stored; the brief is a deterministic transform of the gated
full text plus code-rendered numeric compacts. These tests pin: the fail-loud length
resolver, the brief's structure (what it keeps, what it collapses), the length bound,
the graceful fallback, and — the Law-2 point — that the code-rendered numbers and
valuation ground against the frozen pack.
"""

import pytest

from analyst.dossier import (
    _brief_numbers,
    _brief_valuation,
    render_brief,
    resolve_dossier_length,
    validate_dossier_grounding,
)
from analyst.law1 import CLOSING_LINE
from analyst.serialize import serialize_analysis
from quant.valuation import run_valuation

# Reuse the serializer test's hand-checkable pack conventions (rev 100, OE 10, 10 shares).
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
    return {"period_end": pe, "value": value, "accn": f"a-{pe}", "filed": "2026-01-29"}


def _pack() -> dict:
    return {
        "symbol": "X",
        "cik": "0000000001",
        "price": {"close": 15.0, "date": "2026-07-02", "source": "prices_eod"},
        "series": {
            "revenue": [_row("2023-12-31", 80.0), _row("2024-12-31", 90.0), _row("2025-12-31", 100.0)],
            "gross_profit": [_row("2025-12-31", 40.0)],
            "operating_income": [_row("2025-12-31", 12.0)],
            "net_income": [_row("2025-12-31", 8.0)],
            "operating_cash_flow": [_row("2025-12-31", 14.0)],
            "capex": [_row("2025-12-31", 3.0)],
            "depreciation_amortization": [_row("2025-12-31", 5.0)],
            "total_assets": [_row("2025-12-31", 120.0)],
            "total_liabilities": [_row("2025-12-31", 60.0)],
            "shares_diluted": [_row("2023-12-31", 9.0), _row("2025-12-31", 10.0)],
        },
        "metrics": {
            "margins": [
                {"period_end": "2024-12-31", "gross_margin": 0.45, "operating_margin": 0.13, "net_margin": 0.09},
                {"period_end": "2025-12-31", "gross_margin": 0.40, "operating_margin": 0.12, "net_margin": 0.08},
            ],
            "revenue_cagr": {"3": {"value": 0.111, "reason": None}},
            "earnings_consistency": {"years_covered": 3, "first_period_end": "2023-12-31",
                                     "last_period_end": "2025-12-31", "loss_years": 0, "profit_years": 3},
            "fcf_proxy": [{"period_end": "2025-12-31", "fcf": 11.0, "basis": "ocf_minus_capex"}],
            "eps_history": [{"period_end": "2024-12-31", "eps": 0.90}, {"period_end": "2025-12-31", "eps": 0.80}],
        },
        "peers": {"symbol": "X", "peers": [], "source": "config", "table": [], "missing_fundamentals": []},
        "estimates": {},
        "news": {"window_days": 14, "headlines": []},
        "source_health": {},
        "filings": {"10k": {"note": "n/a"}, "def14a": {"note": "n/a"}},
    }


_VERDICTS = {
    "graham": {"verdict": "CHEAP", "margin_of_safety_pct": 30.0},
    "buffett": {"business": "Good", "price": "Discount"},
    "taleb": {"verdict": "FRAGILE", "ruin_list": ["a demand collapse", "a key supplier failing"]},
}

# Real dossiers run long (~2,800 words), with substantial Stage 2-8 prose the brief
# replaces/collapses. The stub mirrors that: the dropped/replaced stages carry filler
# so the transform is a genuine trim, not a stub artifact.
_PAD = ("The analysis continues with further detail, context and qualification across "
        "several sentences of narrative prose that the brief does not carry. ") * 12

_FULL = (
    "Bottom line: the frameworks disagree on X at $15.00 — the price looks cheap, but "
    "Taleb flags it as fragile (ruin-exposed). No clean call; the disagreement itself is "
    "the finding.\n\n"
    "STAGE 1 — BUSINESS\nThe company sells widgets to repeat industrial buyers. "
    "The repeat purchase holds because switching is costly.\n\n"
    "STAGE 2 — FINANCIALS\nRevenue trajectory is up. Margins compressed modestly. " + _PAD + "\n\n"
    "STAGE 3 — MOAT & PEERS\nUNIQUE_MOAT_MARKER peers have thinner margins. " + _PAD + "\n\n"
    "STAGE 4 — MANAGEMENT & BOARD\nUNIQUE_MGMT_MARKER founder owns a large stake. " + _PAD + "\n\n"
    "STAGE 5 — STRATEGY & GUIDANCE POSTURE\nUNIQUE_STRAT_MARKER capex is rising. " + _PAD + "\n\n"
    "STAGE 6 — FRAGILITY AUDIT\nLeverage is moderate. " + _PAD + "\nWhat kills this company:\n"
    "- a demand collapse\n- a key supplier failing\n\n"
    "STAGE 7 — VALUATION\nThe bear-weighted estimate sits below the price. " + _PAD + "\n\n"
    "STAGE 8 — MR. MARKET\nUNIQUE_MRMARKET_MARKER the crowd leans bullish. " + _PAD + "\n\n"
    "VERDICT BLOCK\n"
    "Graham: CHEAP — a margin of safety of 30.0%.\n"
    "Buffett: Good business at a Discount price.\n"
    "Taleb: FRAGILE — demand collapse; supplier risk.\n\n"
    "What would change this verdict:\nA margin recovery.\n\n"
    "Open questions for Omar:\nWhat is the real maintenance capex?\n\n"
    f"{CLOSING_LINE}"
)


# --------------------------------------------------------------------------- #
# resolve_dossier_length — fail-loud (Law 7)
# --------------------------------------------------------------------------- #
class _FakeClient:
    def __init__(self, rows):
        self._rows = rows

    def table(self, _name):
        return self

    def select(self, *_a):
        return self

    def eq(self, *_a):
        return self

    def limit(self, *_a):
        return self

    def execute(self):
        return type("R", (), {"data": self._rows})()


def test_override_wins_without_touching_config():
    # A poisoned client (would blow up if read) proves the override never reads config.
    assert resolve_dossier_length(None, "full") == "full"
    assert resolve_dossier_length(None, "brief") == "brief"


def test_bad_override_raises():
    with pytest.raises(ValueError):
        resolve_dossier_length(_FakeClient([]), "medium")


def test_config_value_is_read_when_no_override():
    assert resolve_dossier_length(_FakeClient([{"value": "brief"}]), None) == "brief"
    assert resolve_dossier_length(_FakeClient([{"value": "full"}]), None) == "full"


def test_missing_or_garbage_config_fails_loud():
    with pytest.raises(RuntimeError):
        resolve_dossier_length(_FakeClient([]), None)
    with pytest.raises(RuntimeError):
        resolve_dossier_length(_FakeClient([{"value": "verbose"}]), None)
    with pytest.raises(RuntimeError):
        resolve_dossier_length(_FakeClient([{"value": None}]), None)


# --------------------------------------------------------------------------- #
# The brief's structure
# --------------------------------------------------------------------------- #
def test_brief_keeps_essentials_and_collapses_the_rest():
    pack = _pack()
    valuation = run_valuation(pack, GRID)
    brief = render_brief(_FULL, _VERDICTS, pack, valuation, "X")

    # Bottom line first; closing line last.
    assert brief.startswith("Bottom line: the frameworks disagree on X")
    assert brief.rstrip().endswith(CLOSING_LINE)
    # Kept: verdict lines, business, ruin list, open questions.
    assert "Graham: CHEAP" in brief and "Taleb: FRAGILE" in brief
    assert "sells widgets" in brief
    assert "WHAT KILLS THIS COMPANY:" in brief and "a demand collapse" in brief
    assert "Open questions for Omar:" in brief
    # Collapsed: Stages 3/4/5/8 unique prose is gone, replaced by one pointer line.
    for marker in ("UNIQUE_MOAT_MARKER", "UNIQUE_MGMT_MARKER", "UNIQUE_STRAT_MARKER",
                   "UNIQUE_MRMARKET_MARKER"):
        assert marker not in brief
    assert "ask for any section with /analyze X full" in brief
    # The code-rendered numbers block is present and human-worded.
    assert "THE NUMBERS THAT MATTER" in brief
    assert "Revenue: $100" in brief


def test_brief_is_shorter_than_full():
    pack = _pack()
    valuation = run_valuation(pack, GRID)
    brief = render_brief(_FULL, _VERDICTS, pack, valuation, "X")
    assert len(brief.split()) < len(_FULL.split())


def test_brief_falls_back_to_full_when_structure_is_missing():
    # No stage headers / no bottom line => deliver the full (superset) text unchanged.
    junk = "Just some text with no structure and no bottom line."
    assert render_brief(junk, _VERDICTS, _pack(), {}, "X") == junk


# --------------------------------------------------------------------------- #
# Law 2: the code-rendered compacts ground against the frozen pack
# --------------------------------------------------------------------------- #
def test_brief_numbers_and_valuation_ground_against_the_block():
    pack = _pack()
    valuation = run_valuation(pack, GRID)
    block = serialize_analysis(pack, valuation)
    assert validate_dossier_grounding(_brief_numbers(pack), block, pack) == []
    assert validate_dossier_grounding(_brief_valuation(valuation), block, pack) == []


def test_brief_numbers_render_peak_to_now_from_the_full_series():
    # Gross margin peak is the EARLY year (45.0% FY 2024), not the latest (40.0%) —
    # the recency-anchoring trap the extrema block also guards. EPS likewise.
    nums = _brief_numbers(_pack())
    assert "Gross margin: 40.0% now (FY 2025), against a peak of 45.0% in FY 2024" in nums
    assert "peak of 0.90 in FY 2024" in nums
    assert "Diluted share count grew from 9 (FY 2023) to 10 (FY 2025)" in nums


# --------------------------------------------------------------------------- #
# Plain language (Phase 4): serializer glosses + the 6th synthesis clause
# --------------------------------------------------------------------------- #
def test_serializer_glosses_terms_of_art():
    pack = _pack()
    block = serialize_analysis(pack, run_valuation(pack, GRID))
    assert "the cash the business generates after maintaining itself" in block  # owner earnings
    if "reverse-DCF" in block and "not solvable" not in block:
        assert "working backwards from today's price to the growth it assumes" in block


def test_system_prompt_carries_the_plain_language_clause():
    from analyst.dossier import _SYSTEM

    assert "6. Plain language" in _SYSTEM
    assert "percentage points" in _SYSTEM        # the replacement for basis points
    assert "bps" in _SYSTEM                       # named in the outright ban
    assert "average yearly growth" in _SYSTEM     # the CAGR gloss
    assert "the cash the business generates after maintaining itself" in _SYSTEM
