"""Claims-lint tests (analyst/claims.py) — grounded-but-wrong superlatives.

A superlative flags only when a superlative keyword, a concept keyword, and a
resolving value co-occur AND the value is not that concept's extremum. The canonical
target is the live TSLA slip "EPS peaked at 3.61 in FY 2022" (3.61 is EPS[2022]; the
peak is 4.30 at FY2023). Plain listings and unbound superlatives must pass.
"""

import pytest

from analyst.claims import enforce_claims, validate_claims, ClaimsError


def _pack() -> dict:
    def rows(field, pairs):
        return [{"period_end": f"{y}-12-31", field: v} for y, v in pairs]
    return {
        "series": {
            "revenue": rows("value", [(2009, 112e6), (2024, 97.69e9), (2025, 94.827e9)]),
            "capex": rows("value", [(2023, 8.9e9), (2024, 11.342e9), (2025, 8.527e9)]),
            "net_income": rows("value", [(2023, 14.997e9), (2024, 7.13e9), (2025, 3.79e9)]),
        },
        "metrics": {
            "eps_history": rows("eps", [(2022, 3.6132), (2023, 4.3033), (2024, 2.027), (2025, 1.0754)]),
            "fcf_proxy": rows("fcf", [(2022, 7.566e9), (2024, 3.581e9), (2025, 6.22e9)]),
            "margins": [
                {"period_end": "2011-12-31", "gross_margin": 0.302, "net_margin": 0.01},
                {"period_end": "2017-12-31", "gross_margin": 0.189, "net_margin": -0.09},
                {"period_end": "2022-12-31", "gross_margin": 0.256, "net_margin": 0.056},
                {"period_end": "2025-12-31", "gross_margin": 0.18, "net_margin": 0.04},
            ],
        },
    }


def _concepts(text):
    return {(v["concept"], v["direction"]) for v in validate_claims(text, _pack())}


# --------------------------------------------------------------------------- #
# The canonical grounded-but-wrong catches
# --------------------------------------------------------------------------- #
def test_eps_peaked_at_a_mid_series_value_flags():
    v = validate_claims("EPS peaked at 3.61 in FY 2022 and has since fallen to 1.08.", _pack())
    assert len(v) == 1
    assert v[0]["concept"] == "EPS" and v[0]["direction"] == "max"
    assert v[0]["actual_value"] == "4.30" and v[0]["actual_period"].startswith("2023")


def test_gross_margin_recency_anchored_peak_flags():
    assert ("gross margin", "max") in _concepts("Gross margin peaked at 25.6% in FY 2022.")
    assert ("gross margin", "max") in _concepts("Gross margin peaked at 25.6 percent in FY 2022.")


def test_fcf_highest_in_series_when_it_is_not_flags():
    assert ("free cash flow", "max") in _concepts(
        "FCF of 6,220,000,000 dollars — the highest in the series shown."
    )


def test_min_direction_lowest_at_a_non_min_flags():
    assert ("net margin", "min") in _concepts("Net margin hit its lowest at 4.0% in FY 2025.")


# --------------------------------------------------------------------------- #
# Correct superlatives PASS (the extremum is cited)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text", [
    "EPS peaked at 4.30 in FY 2023 before falling.",
    "Gross margin peaked at 30.2% in FY 2011 on nascent revenue.",
    "Net margin reached its lowest at negative 9.0% in FY 2017.",
    "FCF peaked at 7,566,000,000 dollars in FY 2022.",
])
def test_correct_superlatives_pass(text):
    assert validate_claims(text, _pack()) == []


# --------------------------------------------------------------------------- #
# Precision guards — unbound / non-superlative language must not flag
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text", [
    "EPS was 3.61 in FY 2022, 4.30 in FY 2023, 2.03 in FY 2024, and 1.08 in FY 2025.",
    "The biggest single mover in the sensitivity table is revenue CAGR.",
    "The filed record shows a 3-year CAGR of 5.2 percent.",
    "Management has a strong track record on delivery.",
    "Revenue peaked, then the market cooled.",
    "Gross margin has compressed from 25.6% in FY 2022 to 18.0% in FY 2025.",
])
def test_unbound_or_plain_language_passes(text):
    assert validate_claims(text, _pack()) == []


def test_enforce_raises_with_violations():
    with pytest.raises(ClaimsError) as err:
        enforce_claims("EPS peaked at 3.61 in FY 2022.", _pack())
    assert err.value.violations
    assert "EPS" in str(err.value)


def test_empty_pack_is_clean():
    assert validate_claims("EPS peaked at 3.61 in FY 2022.", {}) == []
