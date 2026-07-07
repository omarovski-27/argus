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
    assert _draft_problems(clean, block) == []
    dirty = "Revenue was 137. You should buy before the print."
    problems = _draft_problems(dirty, block)
    assert any("ungrounded figure '137'" in p for p in problems)
    assert any("instruction-shaped language [advice-verb construction]" in p for p in problems)
    assert any("missing closing line" in p for p in problems)


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
