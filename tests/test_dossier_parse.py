"""Verdict-block parsing tests (analyst/dossier.py) — the §2 controlled vocabulary.

``parse_verdicts`` splits the machine line off the delivered text and enforces the
locked vocabulary EXACTLY (module spec §2; the vocab is pinned here so an edit to
either side breaks a test, not a live dossier). Off-vocabulary verdicts are a
Law-1 boundary violation: the run fails, nothing is stored.
"""

import json

import pytest

from analyst.dossier import VERDICT_VOCAB, VerdictParseError, parse_verdicts

_GOOD = {
    "graham": {"verdict": "EXPENSIVE", "margin_of_safety_pct": None},
    "buffett": {"business": "Good", "price": "Premium"},
    "taleb": {"verdict": "ROBUST", "ruin_list": ["demand collapse", "key-man risk"]},
}


def _dossier(verdicts: dict | str) -> str:
    payload = verdicts if isinstance(verdicts, str) else json.dumps(verdicts)
    return (
        "STAGE 1 - BUSINESS\nProse.\n\n"
        "Framework verdicts rendered. Timing and sizing are yours.\n"
        f"VERDICTS_JSON: {payload}"
    )


def test_vocabulary_is_pinned_exactly():
    assert VERDICT_VOCAB == {
        "graham": {"CHEAP", "FAIR", "EXPENSIVE"},
        "buffett_business": {"Wonderful", "Good", "Mediocre"},
        "buffett_price": {"Discount", "Fair", "Premium"},
        "taleb": {"FRAGILE", "ROBUST", "ANTIFRAGILE"},
    }


def test_happy_path_strips_machine_line_and_returns_verdicts():
    text, verdicts = parse_verdicts(_dossier(_GOOD))
    assert verdicts == _GOOD
    assert "VERDICTS_JSON" not in text
    assert text.endswith("Framework verdicts rendered. Timing and sizing are yours.")


def test_numeric_margin_of_safety_is_accepted():
    good = json.loads(json.dumps(_GOOD))
    good["graham"] = {"verdict": "CHEAP", "margin_of_safety_pct": 32.5}
    _, verdicts = parse_verdicts(_dossier(good))
    assert verdicts["graham"]["margin_of_safety_pct"] == 32.5


def test_missing_machine_line_raises():
    with pytest.raises(VerdictParseError, match="no VERDICTS_JSON"):
        parse_verdicts("A dossier with no machine line.")


def test_malformed_json_raises():
    with pytest.raises(VerdictParseError, match="not valid JSON"):
        parse_verdicts(_dossier('{"graham": }'))


def test_draft_problems_composes_both_gates():
    from analyst.dossier import _draft_problems

    block = "revenue 100; margin 40.0%"
    clean = (
        "Revenue was 100 with a 40.0% margin.\n"
        "Framework verdicts rendered. Timing and sizing are yours."
    )
    assert _draft_problems(clean, block, {}) == []
    dirty = "Revenue was 137. You should buy before the print."
    problems = _draft_problems(dirty, block, {})
    assert any("ungrounded figure '137'" in p for p in problems)
    assert any("instruction-shaped language [advice-verb construction]" in p for p in problems)
    assert any("missing closing line" in p for p in problems)


def _pack_with_section(section_text: str) -> dict:
    return {"filings": {"10k": {"accn": "a", "filed": "2026-02-18",
                                "sections": {"mdna": {"text": section_text}}}}}


def test_unit_normalized_filings_figures_ground():
    """A thousands-table figure cited in full-dollar form grounds via the expansion
    whitelist (the PLTR failure class) — but ONLY figures the section actually prints."""
    from analyst.dossier import validate_dossier_grounding
    from digest.grounding import validate_text

    section = ("The following table sets forth stock-based compensation "
               "(in thousands, except percentages):\n total 684,033 and 691,638 ")
    pack = _pack_with_section(section)
    block = "FILINGS TEXT\n" + section
    text = "Stock-based compensation was 684,033,000 dollars."
    # The raw shared gate flags it; the dossier chokepoint accepts the normalization.
    assert [v["token"] for v in validate_text(text, block)] == ["684,033,000"]
    assert validate_dossier_grounding(text, block, pack) == []
    # A full-dollar figure the section does NOT print still fails.
    assert validate_dossier_grounding("SBC was 999,999,000 dollars.", block, pack) != []


def test_millions_tables_normalize_too_and_undeclared_sections_do_not():
    from analyst.dossier import validate_dossier_grounding

    millions = _pack_with_section("Revenue by segment (in millions): total 82,056 ")
    block_m = "FILINGS TEXT\nRevenue by segment (in millions): total 82,056 "
    assert validate_dossier_grounding(
        "Segment revenue was 82,056,000,000 dollars.", block_m, millions
    ) == []
    undeclared = _pack_with_section("total 684,033 with no unit header anywhere")
    block_u = "FILINGS TEXT\ntotal 684,033 with no unit header anywhere"
    assert validate_dossier_grounding(
        "SBC was 684,033,000 dollars.", block_u, undeclared
    ) != []


@pytest.mark.parametrize(
    ("mutate", "fragment"),
    [
        (lambda v: v["graham"].update(verdict="CHEAPISH"), "graham.verdict"),
        (lambda v: v["buffett"].update(business="Great"), "buffett.business"),
        (lambda v: v["buffett"].update(price="Bargain"), "buffett.price"),
        (lambda v: v["taleb"].update(verdict="SOLID"), "taleb.verdict"),
        (lambda v: v["taleb"].update(ruin_list=[]), "ruin_list"),
        (lambda v: v["graham"].update(margin_of_safety_pct="32%"), "margin_of_safety_pct"),
    ],
)
def test_off_vocabulary_raises_naming_the_field(mutate, fragment):
    bad = json.loads(json.dumps(_GOOD))
    mutate(bad)
    with pytest.raises(VerdictParseError, match="off-vocabulary") as err:
        parse_verdicts(_dossier(bad))
    assert fragment in str(err.value)


def test_stage_references_are_masked_before_grounding():
    """Stage headers/cross-refs are contract STRUCTURE, not data claims: on a sparse
    pack their digits ground to nothing and deadlock the repair pass against the
    fixed-structure clause (NTDOY probe). Only 'stage [1-8]' is masked — real
    figures still flag."""
    from analyst.dossier import _mask_structural
    from digest.grounding import validate_text

    sparse_block = "TARGET\nsymbol ZZZ; SEC CIK unresolved"
    text = "STAGE 8 — MR. MARKET\nNothing here (see Stage 3). Not available."
    assert validate_text(_mask_structural(text), sparse_block) == []
    # A genuine ungrounded figure survives the mask.
    dirty = _mask_structural("STAGE 2 — FINANCIALS\nRevenue was 137.")
    assert [v["token"] for v in validate_text(dirty, sparse_block)] == ["137"]


def test_verdict_problems_cross_check_the_valuation():
    from analyst.dossier import _verdict_problems

    ok = {"graham": {"verdict": "CHEAP", "margin_of_safety_pct": 69.4},
          "taleb": {"verdict": "FRAGILE", "ruin_list": ["recession demand collapse"]}}
    val = {"renderable": True, "margin_of_safety_pct": 0.6944}
    assert _verdict_problems(ok, val) == []

    # n/m valuation (deep-negative MoS) demands null.
    nm_val = {"renderable": True, "margin_of_safety_pct": -10.2}
    bad = {"graham": {"verdict": "EXPENSIVE", "margin_of_safety_pct": -1020.0},
           "taleb": {"verdict": "FRAGILE", "ruin_list": ["x"]}}
    assert any("must be null" in p for p in _verdict_problems(bad, nm_val))

    # A meaningful MoS must be echoed, and echoed correctly.
    missing = {"graham": {"verdict": "CHEAP", "margin_of_safety_pct": None},
               "taleb": {"verdict": "FRAGILE", "ruin_list": ["x"]}}
    assert any("is null but the DATA renders" in p for p in _verdict_problems(missing, val))
    wrong = {"graham": {"verdict": "CHEAP", "margin_of_safety_pct": 42.0},
             "taleb": {"verdict": "FRAGILE", "ruin_list": ["x"]}}
    assert any("not the DATA's margin of safety" in p for p in _verdict_problems(wrong, val))

    # Instruction-shaped ruin-list items are flagged (they bypass the prose lint).
    nudge = {"graham": {"verdict": "CHEAP", "margin_of_safety_pct": 69.4},
             "taleb": {"verdict": "FRAGILE", "ruin_list": ["you should sell before covenant breach"]}}
    assert any("instruction-shaped" in p for p in _verdict_problems(nudge, val))


def test_form_names_are_masked_as_nomenclature():
    from analyst.dossier import _mask_structural
    from digest.grounding import validate_text

    sparse_block = "TARGET\nsymbol ZZZ; SEC CIK unresolved"
    text = "No Form 20-F on record; no 10-K, no DEF 14A. Not available."
    assert validate_text(_mask_structural(text), sparse_block) == []
