"""Bottom-line rating tests (analyst/rating.py + the post-gate injection).

The rating is CODE, not the model (module spec §2, amended 2026-07-12): a pure map of
the four lens verdicts, injected AFTER the Law-1 lint. These tests pin the full
mapping table exhaustively, prove the injected sentence is recommendation-SHAPED (which
is exactly why it must bypass the lint), and prove the model writing "you should buy"
still blocks.
"""

import itertools

import pytest

from analyst.dossier import (
    VERDICT_VOCAB,
    _inject_bottom_line,
    finalize_dossier,
)
from analyst.law1 import CLOSING_LINE, validate_law1
from analyst.rating import (
    GRAHAM_VOCAB,
    PRICE_VOCAB,
    QUALITY_VOCAB,
    RATING_VOCAB,
    TALEB_VOCAB,
    Rating,
    derive_rating,
    rating_from_verdicts,
    render_bottom_line,
)


def _expected(graham: str, quality: str, taleb: str) -> str:
    """The §2 rules, restated independently — the mapper must match this."""
    if graham == "EXPENSIVE":
        return "UNATTRACTIVE"
    if graham == "CHEAP" and taleb != "FRAGILE" and quality in {"Wonderful", "Good"}:
        return "ATTRACTIVE"
    return "MIXED"


# --------------------------------------------------------------------------- #
# The vocabularies mirror the dossier's controlled verdict vocabulary
# --------------------------------------------------------------------------- #
def test_rating_vocab_mirrors_verdict_vocab():
    assert GRAHAM_VOCAB == VERDICT_VOCAB["graham"]
    assert QUALITY_VOCAB == VERDICT_VOCAB["buffett_business"]
    assert PRICE_VOCAB == VERDICT_VOCAB["buffett_price"]
    assert TALEB_VOCAB == VERDICT_VOCAB["taleb"]
    assert RATING_VOCAB == {"ATTRACTIVE", "MIXED", "UNATTRACTIVE"}


# --------------------------------------------------------------------------- #
# Full mapping table — all 81 lens combinations, exhaustively
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("graham", "quality", "price", "taleb"),
    list(itertools.product(sorted(GRAHAM_VOCAB), sorted(QUALITY_VOCAB),
                           sorted(PRICE_VOCAB), sorted(TALEB_VOCAB))),
)
def test_mapping_table_is_exhaustive_and_ordered(graham, quality, price, taleb):
    r = derive_rating(graham, quality, price, taleb)
    assert r.rating in RATING_VOCAB
    assert r.rating == _expected(graham, quality, taleb)
    assert r.basis == {
        "graham": graham, "buffett_quality": quality,
        "buffett_price": price, "taleb": taleb,
    }


def test_expensive_is_unattractive_regardless_of_quality():
    # PLTR class: EXPENSIVE + Wonderful business is still UNATTRACTIVE (price discipline).
    r = derive_rating("EXPENSIVE", "Wonderful", "Premium", "FRAGILE")
    assert r.rating == "UNATTRACTIVE"
    assert "expensive" in r.clause


def test_clean_attractive_requires_cheap_nonfragile_quality():
    assert derive_rating("CHEAP", "Wonderful", "Discount", "ROBUST").rating == "ATTRACTIVE"
    assert derive_rating("CHEAP", "Good", "Discount", "ANTIFRAGILE").rating == "ATTRACTIVE"
    # Any one failing leg -> MIXED.
    assert derive_rating("CHEAP", "Mediocre", "Discount", "ROBUST").rating == "MIXED"
    assert derive_rating("CHEAP", "Good", "Discount", "FRAGILE").rating == "MIXED"


def test_mixed_names_the_disagreeing_lens():
    # CHEAP + FRAGILE -> "cheap but ruin-exposed" (the spec's own example).
    r = derive_rating("CHEAP", "Good", "Discount", "FRAGILE")
    assert r.rating == "MIXED"
    assert "cheap" in r.clause and "fragile" in r.clause.lower()
    # CHEAP + Mediocre -> the quality lens is named.
    r2 = derive_rating("CHEAP", "Mediocre", "Discount", "ROBUST")
    assert "mediocre" in r2.clause.lower()
    # FAIR price with acceptable quality/fragility still has no clean call.
    r3 = derive_rating("FAIR", "Good", "Fair", "ROBUST")
    assert r3.rating == "MIXED" and "no margin of safety" in r3.clause


def test_off_vocabulary_input_raises():
    for bad in [("CHEAPISH", "Good", "Fair", "ROBUST"),
                ("CHEAP", "Great", "Fair", "ROBUST"),
                ("CHEAP", "Good", "Bargain", "ROBUST"),
                ("CHEAP", "Good", "Fair", "SOLID")]:
        with pytest.raises(ValueError):
            derive_rating(*bad)


# --------------------------------------------------------------------------- #
# The three live cases (module spec: TSLA/GM/PLTR)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("verdicts", "expected"),
    [
        ({"graham": {"verdict": "EXPENSIVE"}, "buffett": {"business": "Good", "price": "Premium"},
          "taleb": {"verdict": "FRAGILE"}}, "UNATTRACTIVE"),   # TSLA
        ({"graham": {"verdict": "CHEAP"}, "buffett": {"business": "Mediocre", "price": "Discount"},
          "taleb": {"verdict": "FRAGILE"}}, "MIXED"),          # GM
        ({"graham": {"verdict": "EXPENSIVE"}, "buffett": {"business": "Wonderful", "price": "Premium"},
          "taleb": {"verdict": "FRAGILE"}}, "UNATTRACTIVE"),   # PLTR
    ],
)
def test_live_cases(verdicts, expected):
    assert rating_from_verdicts(verdicts).rating == expected


# --------------------------------------------------------------------------- #
# Render templates — price present / absent
# --------------------------------------------------------------------------- #
def test_render_unattractive_carries_price_and_reason():
    r = derive_rating("EXPENSIVE", "Good", "Premium", "FRAGILE")
    line = render_bottom_line(r, "TSLA", 412.50)
    assert line.startswith("Bottom line: by these frameworks, TSLA is not worth buying")
    assert "$412.50" in line and "expensive" in line


def test_render_attractive_and_mixed():
    a = render_bottom_line(derive_rating("CHEAP", "Good", "Discount", "ROBUST"), "XYZ", 10.0)
    assert "XYZ is attractive at today's price of $10.00" in a
    m = render_bottom_line(derive_rating("CHEAP", "Good", "Discount", "FRAGILE"), "GM", 75.92)
    assert "disagree on GM at $75.92" in m and "No clean call" in m


def test_render_without_price_drops_the_dollar_figure():
    line = render_bottom_line(derive_rating("EXPENSIVE", "Good", "Premium", "FRAGILE"), "ZZZ", None)
    assert "today's price" in line and "$" not in line


# --------------------------------------------------------------------------- #
# The Law-1 boundary: the model's prose still blocks; the INJECTED line is
# recommendation-shaped (which is precisely why it is injected AFTER the lint)
# --------------------------------------------------------------------------- #
def test_model_written_recommendation_still_blocks():
    assert validate_law1(f"You should buy TSLA now.\n{CLOSING_LINE}"), \
        "the lint must still block model-written buy/sell prose"


def test_injected_bottom_line_is_recommendation_shaped_so_it_bypasses_the_lint():
    # The UNATTRACTIVE sentence trips the lint by design — proving it can only be
    # injected AFTER the lint pass, never fed through it.
    line = render_bottom_line(derive_rating("EXPENSIVE", "Good", "Premium", "FRAGILE"), "T", 9.0)
    assert validate_law1(f"{line}\n{CLOSING_LINE}"), \
        "the injected bottom line is recommendation-shaped; it must bypass the gate"


# --------------------------------------------------------------------------- #
# finalize_dossier — post-gate injection + verdict stamping
# --------------------------------------------------------------------------- #
_GATED = (
    "STAGE 1 — BUSINESS\nProse.\n\n"
    "VERDICT BLOCK\n"
    "Graham: EXPENSIVE — above the bear-weighted estimate.\n"
    "Buffett: Good business at a Premium price.\n"
    "Taleb: FRAGILE — key-man risk.\n\n"
    f"{CLOSING_LINE}"
)
_VERDICTS = {
    "graham": {"verdict": "EXPENSIVE", "margin_of_safety_pct": None},
    "buffett": {"business": "Good", "price": "Premium"},
    "taleb": {"verdict": "FRAGILE", "ruin_list": ["key-man risk"]},
}


def test_finalize_injects_top_and_verdict_block_and_stamps_verdicts():
    text, verdicts, rating = finalize_dossier(_GATED, _VERDICTS, "TSLA", 412.5)
    assert rating == "UNATTRACTIVE"
    # First line of the dossier.
    assert text.startswith("Bottom line: by these frameworks, TSLA is not worth buying")
    # Inside the verdict block too (two occurrences: top + before VERDICT BLOCK).
    assert text.count("Bottom line:") == 2
    # Still ends with the closing line (injection never disturbs it).
    assert text.rstrip().endswith(CLOSING_LINE)
    # Verdicts carry the rating + its basis for the stored row.
    assert verdicts["rating"] == "UNATTRACTIVE"
    assert verdicts["rating_basis"] == {
        "graham": "EXPENSIVE", "buffett_quality": "Good",
        "buffett_price": "Premium", "taleb": "FRAGILE",
    }
    # The original verdict fields are preserved.
    assert verdicts["taleb"]["ruin_list"] == ["key-man risk"]


def test_inject_falls_back_to_top_only_without_a_verdict_anchor():
    text = _inject_bottom_line("STAGE 1 — BUSINESS\nProse.\n\nEnd.", "Bottom line: X.")
    assert text.startswith("Bottom line: X.")
    assert text.count("Bottom line:") == 1
