"""Law-1 lint tests (analyst/law1.py) — instruction shapes fail, analysis passes.

The lint's contract (module docstring): patterns match INSTRUCTION SHAPES, not
bare trade words — the analytical vocabulary legitimately contains "share
buybacks", "sell-side consensus", "exit multiple", "customers enter into
contracts", consensus price targets and hold ratings. Every banned pattern gets
a firing example here; every legitimate phrase gets a counter-example proving it
still passes. A new pattern added to BANNED_PATTERNS needs both.
"""

import pytest

from analyst.law1 import CLOSING_LINE, Law1Error, enforce_law1, validate_law1


def _with_close(text: str) -> str:
    """Append the mandatory closing line so only the pattern under test can fail."""
    return f"{text}\n{CLOSING_LINE}"


def _rules(text: str) -> list[str]:
    return [v["rule"] for v in validate_law1(_with_close(text))]


# --------------------------------------------------------------------------- #
# Instruction shapes — each rule fires
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("text", "rule"),
    [
        ("Given the setup, you should buy before the print.", "advice-verb construction"),
        ("It may be worth accumulating on weakness.", "advice-verb construction"),
        ("We recommend selling into strength.", "advice-verb construction"),
        ("Consider trimming after the run-up.", "advice-verb construction"),
        ("It is time to exit the name.", "advice-verb construction"),
        ("Consider adding to the position here.", "advice-verb construction"),
        ("Buy now.", "imperative trade instruction"),
        ("The chart looks weak. Sell before earnings.", "imperative trade instruction"),
        ("It looks safe to buy at these levels.", "safe-to-trade language"),
        ("Now is a good time to start a position.", "timing call"),
        ("This is an attractive entry for patient capital.", "timing call"),
        ("Wait for a pullback to get involved.", "timing call"),
        ("The 200-day average marks a good entry point.", "timing call"),
        ("Position sizing matters more than timing here.", "sizing instruction"),
        ("Allocate 30% of the portfolio to this name.", "sizing instruction"),
        ("Put 20 % into the stock and forget it.", "sizing instruction"),
        ("A 5% position is appropriate.", "sizing instruction"),
        ("Size your position according to conviction.", "sizing instruction"),
        ("Set a stop-loss at 250 to protect capital.", "bracket/level instruction"),
        ("Take profits above the prior high.", "bracket/level instruction"),
        ("Enter at $250 and exit at $320.", "bracket/level instruction"),
        ("Buy below $200; the math works there.", "bracket/level instruction"),
        ("Back up the truck on any dip.", "colloquial trade nudge"),
        ("Get in now before the crowd does.", "colloquial trade nudge"),
    ],
)
def test_instruction_shape_fires(text, rule):
    assert rule in _rules(text), f"expected rule {rule!r} to fire on {text!r}"


# --------------------------------------------------------------------------- #
# Legitimate analytical vocabulary — must PASS (the precision contract)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text",
    [
        # Stage-4 capital-allocation discussion: "buyback" is not "buy".
        "Share buybacks retired 4 percent of the float; the buyback price averaged well above book.",
        "Buybacks accelerated in the second half.",
        # Sell-side vocabulary.
        "Sell-side consensus sits well above the filed growth record.",
        "The consensus rating is a hold, with a mean target far above the close.",
        # Valuation vocabulary: exit multiple is an assumption, not an instruction.
        "The bull scenario assumes an exit multiple of 25x; the exit multiple is the biggest mover.",
        # Business prose: customers enter contracts, companies enter markets.
        "Customers typically enter into multi-year lease agreements.",
        "The company plans to enter the European market next year.",
        # Consensus price targets are DATA (Stage 8), not instructions.
        "Analyst price targets: mean 350.00, low 120.00, high 500.00.",
        # Additive facts are not "adding to a position".
        "Management is adding capacity at two plants.",
        # Waiting as description, not timing advice.
        "The market appears to be waiting for the next delivery report.",
        # The mandatory closing line itself must never trip the lint.
        "",
    ],
)
def test_legitimate_analysis_passes(text):
    violations = validate_law1(_with_close(text))
    assert violations == [], f"false positive on {text!r}: {violations}"


# --------------------------------------------------------------------------- #
# Structure: the closing line is mandatory; enforce_law1 raises with context
# --------------------------------------------------------------------------- #
def test_missing_closing_line_is_a_violation():
    violations = validate_law1("A clean analytical paragraph with no instructions.")
    assert [v["rule"] for v in violations] == ["missing closing line"]


def test_enforce_raises_law1error_with_violations():
    with pytest.raises(Law1Error) as err:
        enforce_law1(_with_close("You should buy before the print."))
    assert err.value.violations, "Law1Error must carry the violation list"
    assert "advice-verb construction" in str(err.value)


def test_clean_dossier_passes():
    text = _with_close(
        "STAGE 7 - VALUATION\n"
        "The bear-weighted estimate sits far below the current price; the reverse-DCF "
        "implies growth well above the filed record. The exit multiple is the biggest "
        "mover in the sensitivity table.\n\n"
        "Graham: EXPENSIVE - the current price sits far above the bear-weighted estimate.\n"
        "Buffett: Good business at a Premium price.\n"
        "Taleb: ROBUST - ruin list: demand collapse; key-man concentration.\n"
    )
    assert enforce_law1(text) is None
