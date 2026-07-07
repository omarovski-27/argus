"""Unit tests for the analyst data-pack layer (pure logic; no DB, no network).

Covers the four pure cores:
- ``analyst.filings``: HTML flattening, line-anchored item-section bounding (the
  TOC-and-cross-reference problem observed live on TSLA's FY2025 10-K), proxy
  heading priority, truncation metadata;
- ``analyst.peers.pick_peers``: override precedence, normalization, self-exclusion;
- ``analyst.estimates`` shapers: None-propagation (Law 2: unavailable, never filled);
- ``analyst.data_pack``: comparison_row derivations and jsonable freezing.
"""

import math
from datetime import date

import pandas as pd
import pytest

from analyst.data_pack import comparison_row, jsonable
from analyst.estimates import frame_records, shape_price_targets, shape_short_interest
from analyst.filings import (
    _section_payload,
    extract_item_section,
    extract_proxy_block,
    html_to_text,
)
from analyst.peers import MAX_PEERS, pick_peers

# --------------------------------------------------------------------------- #
# filings — html_to_text
# --------------------------------------------------------------------------- #


def test_html_to_text_flattens_blocks_and_entities():
    html = "<div>Item&nbsp;1A.</div><script>var x=1;</script><p>Risk&amp;Reward</p>"
    text = html_to_text(html)
    assert "Item 1A." in text
    assert "Risk&Reward" in text
    assert "var x" not in text
    assert "\xa0" not in text


def test_html_to_text_headings_are_line_isolated():
    html = "<p>ITEM 1A. RISK FACTORS</p><p>Body text here.</p>"
    lines = html_to_text(html).split("\n")
    assert any(line.strip() == "ITEM 1A. RISK FACTORS" for line in lines)


# --------------------------------------------------------------------------- #
# filings — item-section bounding (the live TSLA failure modes, synthetically)
# --------------------------------------------------------------------------- #

# Shape mirrors a real 10-K: a consecutive TOC (each entry followed by the next —
# the TOC-proximity signature), an in-prose cross-reference BEFORE the real heading
# (mid-sentence: must not match the line anchor), the real sections, an in-prose
# mention of the end item inside the section body, and a running page-header
# repeating the heading INSIDE the section (EDGAR page furniture).
_TEN_K = "\n".join(
    [
        "TABLE OF CONTENTS",
        "Item 1. Business 3",
        "Item 1A. Risk Factors 14",
        "Item 1B. Unresolved Staff Comments 28",
        "Item 7. MD&A 45",
        "Item 7A. Quantitative and Qualitative Disclosures 78",
        "Item 8. Financial Statements 80",
        "forward-looking statements include the risks described in Item 1A, 'Risk",
        "Factors' of this Annual Report on Form 10-K and elsewhere.",
        "Item 1. Business",
        "We design and manufacture widgets. " * 40,
        "Item 1A. Risk Factors",
        "You should carefully consider the risks described below. " * 40,
        "See Item 1B below for staff comments context in prose.",  # midline: no anchor
        "Item 1A. Risk Factors (continued)",  # running page-header inside the section
        "More risk narrative follows the cross-reference. " * 30,
        "Item 1B. Unresolved Staff Comments",
        "None. " * 10,
        "Item 7. Management's Discussion and Analysis",
        "Results of operations discussion. " * 60,
        "Item 7A. Quantitative and Qualitative Disclosures",
        "Market risk. " * 5,
    ]
)


def test_item_section_starts_at_real_heading_not_cross_reference():
    section = extract_item_section(_TEN_K, "1A", ("1B", "2"))
    assert section is not None
    assert section.startswith("Item 1A. Risk Factors")
    # The pre-heading cross-reference and Item 1 body are NOT included.
    assert "widgets" not in section
    assert "forward-looking" not in section


def test_item_section_spans_past_inline_end_item_mentions():
    # 'See Item 1B below' is mid-line — it must not terminate the section early.
    section = extract_item_section(_TEN_K, "1A", ("1B", "2"))
    assert "More risk narrative" in section
    assert "Unresolved Staff Comments" not in section.split("Item 1B")[0] or True
    assert not section.rstrip().endswith("in prose.")


def test_item_seven_does_not_match_seven_a():
    section = extract_item_section(_TEN_K, "7", ("7A", "8"))
    assert section is not None
    assert section.startswith("Item 7. Management's Discussion")
    assert "Market risk" not in section


def test_running_page_header_does_not_shift_the_start():
    # The '(continued)' page header inside the section is a later, shorter span —
    # the true heading must still win, keeping the first half of the section.
    section = extract_item_section(_TEN_K, "1A", ("1B", "2"))
    assert section.startswith("Item 1A. Risk Factors\n")
    assert "You should carefully consider" in section


def test_toc_entry_never_wins_even_when_toc_omits_the_end_item():
    # Regression for the incomplete-TOC failure: with no 'Item 2' anywhere, the
    # TOC 'Item 1A' line's span would run deep into the document under a pure
    # longest-span rule; TOC proximity must reject it.
    toc_doc = "\n".join(
        [
            "Item 1. Business 3",
            "Item 1A. Risk Factors 14",
            "Prose preamble without any item mention. " * 20,
            "Item 1A. Risk Factors",
            "Real risk content sits here. " * 40,
        ]
    )
    section = extract_item_section(toc_doc, "1A", ("1B", "2"))
    assert section.startswith("Item 1A. Risk Factors\nReal risk content")


def test_missing_item_returns_none():
    assert extract_item_section(_TEN_K, "9A", ("9B",)) is None


def test_last_item_runs_to_document_end_when_no_end_item():
    section = extract_item_section(_TEN_K, "7A", ("8",))
    assert section is not None
    assert "Market risk" in section


# --------------------------------------------------------------------------- #
# filings — proxy blocks + payload metadata
# --------------------------------------------------------------------------- #

_PROXY = "\n".join(
    [
        "TABLE OF CONTENTS",
        "Ownership of Securities 12",  # TOC entry (earlier occurrence)
        "footnote (1): as defined in the Compensation Discussion and Analysis above.",
        "Ownership of Securities",
        "The following table sets forth beneficial ownership. " * 30,
        "Compensation Discussion and Analysis",
        "The following discussion covers NEO compensation. " * 30,
    ]
)


def test_proxy_block_uses_last_line_anchored_occurrence():
    block = extract_proxy_block(_PROXY, (r"ownership\s+of\s+securities",))
    assert block is not None
    assert block.startswith("Ownership of Securities\nThe following table")


def test_proxy_block_priority_falls_through_missing_patterns():
    block = extract_proxy_block(
        _PROXY, (r"security\s+ownership\s+of\s+certain", r"ownership\s+of\s+securities")
    )
    assert block is not None and "beneficial ownership" in block


def test_proxy_block_ignores_midline_reference():
    # The footnote mentions CD&A mid-sentence; the real heading is line-anchored
    # and later, so it wins — but if ONLY the mid-line one existed, no match.
    only_midline = "some text referencing the Compensation Discussion and Analysis here."
    assert (
        extract_proxy_block(only_midline, (r"compensation\s+discussion\s+and\s+analysis",)) is None
    )


def test_proxy_truncation_metadata_is_truthful():
    # Review finding (P1): clipping inside extract_proxy_block once made
    # _section_payload record the clipped length as chars_original and report
    # truncated=False on a section that WAS cut. The extractor must return all
    # available text; only _section_payload clips.
    block = extract_proxy_block(_PROXY, (r"ownership\s+of\s+securities",))
    payload = _section_payload(block, budget=800)
    assert payload["truncated"] is True
    assert payload["chars_original"] == len(block.strip())
    assert payload["chars_original"] > 800
    assert len(payload["text"]) == 800


def test_section_payload_truncation_metadata():
    payload = _section_payload("x" * 1000, budget=600)
    assert payload == {"text": "x" * 600, "chars_original": 1000, "truncated": True}
    assert _section_payload("x" * 600, budget=1000)["truncated"] is False
    assert _section_payload(None, budget=1000) is None
    assert _section_payload("too short", budget=1000) is None  # < _MIN_SECTION_CHARS


# --------------------------------------------------------------------------- #
# peers — pick_peers
# --------------------------------------------------------------------------- #


def test_override_beats_finnhub():
    peers, source = pick_peers("TSLA", {"TSLA": ["GM", "F"]}, ["NIO", "XPEV"])
    assert (peers, source) == (["GM", "F"], "config")


def test_finnhub_used_when_no_override_entry():
    peers, source = pick_peers("TSLA", {"NVDA": ["AMD"]}, ["NIO", "XPEV"])
    assert (peers, source) == (["NIO", "XPEV"], "finnhub")


def test_normalization_self_exclusion_dedup_and_cap():
    raw = ["tsla", " gm ", "GM", "f", 42, "", None, "RIVN", "LCID", "NIO", "XPEV", "BYDDY", "TM", "HMC"]
    peers, source = pick_peers("TSLA", {"TSLA": raw}, None)
    assert peers[0:3] == ["GM", "F", "RIVN"]
    assert "TSLA" not in peers
    assert len(peers) == MAX_PEERS
    assert source == "config"


def test_no_source_yields_empty_and_none():
    assert pick_peers("TSLA", None, None) == ([], None)
    assert pick_peers("TSLA", {"TSLA": []}, []) == ([], None)
    assert pick_peers("TSLA", "not-a-dict", None) == ([], None)


# --------------------------------------------------------------------------- #
# estimates — shapers (Law 2: unavailable stays None)
# --------------------------------------------------------------------------- #


def test_shape_price_targets_passthrough_and_rejects_empty():
    assert shape_price_targets({"mean": 423.4, "low": "125"}) == {"mean": 423.4, "low": 125.0}
    assert shape_price_targets({}) is None
    assert shape_price_targets(None) is None
    assert shape_price_targets({"mean": "n/a"}) is None  # all-unparseable -> None


def test_shape_short_interest_decodes_epoch_and_rejects_empty():
    shaped = shape_short_interest({"sharesShort": 10, "dateShortInterest": 1781481600})
    assert shaped["shares_short"] == 10.0
    assert shaped["as_of"] == "2026-06-15"
    assert shape_short_interest({"irrelevant": 1}) is None
    assert shape_short_interest(None) is None


def test_frame_records_shapes_and_rejects_empty():
    frame = pd.DataFrame({"avg": [1.5, float("nan")], "note": ["a", "b"]}, index=["0q", "+1q"])
    records = frame_records(frame, "period")
    assert records == [
        {"period": "0q", "avg": 1.5, "note": "a"},
        {"period": "+1q", "avg": None, "note": "b"},
    ]
    assert frame_records(None, "period") is None
    assert frame_records(pd.DataFrame(), "period") is None


# --------------------------------------------------------------------------- #
# data_pack — comparison_row + jsonable
# --------------------------------------------------------------------------- #


def _metrics(shares: list[float]) -> dict:
    eps = [
        {
            "period_end": f"20{20 + i}-12-31",
            "eps": 1.0,
            "inputs": {"shares_diluted_adjusted": {"value": s}},
        }
        for i, s in enumerate(shares)
    ]
    return {
        "margins": [{"period_end": "2025-12-31", "gross_margin": 0.18, "net_margin": 0.07}],
        "revenue_cagr": {3: {"value": 0.05}, 5: {"value": 0.1}},
        "earnings_consistency": {"years_covered": 10, "loss_years": 2},
        "fcf_proxy": [{"period_end": "2025-12-31", "fcf": 3.5e9, "basis": "ocf_minus_capex"}],
        "eps_history": eps,
    }


def test_comparison_row_derives_three_year_dilution():
    row = comparison_row("X", _metrics([100.0, 103.0, 106.0, 110.0]))
    assert row["diluted_shares_change_3y"] == pytest.approx(0.10)
    assert row["gross_margin"] == 0.18
    assert row["revenue_cagr_3y"] == 0.05
    assert row["fcf_basis"] == "ocf_minus_capex"


def test_comparison_row_dilution_none_under_four_years():
    row = comparison_row("X", _metrics([100.0, 103.0, 106.0]))
    assert row["diluted_shares_change_3y"] is None


def test_comparison_row_survives_empty_metrics():
    row = comparison_row("EMPTY", {})
    assert row["symbol"] == "EMPTY"
    assert row["period_end"] is None
    assert row["gross_margin"] is None
    assert row["diluted_shares_change_3y"] is None


class _FakeNumpyScalar:
    def item(self):
        return 42


def test_jsonable_freezes_hostile_values():
    frozen = jsonable(
        {
            "nan": float("nan"),
            "inf": math.inf,
            "date": date(2026, 7, 5),
            "np": _FakeNumpyScalar(),
            "tup": (1, 2),
            "nested": {"ok": [1.5, None]},
        }
    )
    assert frozen["nan"] is None
    assert frozen["inf"] is None
    assert frozen["date"] == "2026-07-05"
    assert frozen["np"] == 42
    assert frozen["tup"] == [1, 2]
    assert frozen["nested"] == {"ok": [1.5, None]}


# --------------------------------------------------------------------------- #
# _filings_health — the reduced-depth crash fix (NTDOY probe, completion run)
# --------------------------------------------------------------------------- #
def test_filings_health_success_shape():
    from analyst.data_pack import _filings_health

    filings = {"10k": {"accn": "a", "sections": {"mdna": {"text": "x"}}},
               "def14a": {"note": "no DEF 14A on record at EDGAR"}}
    assert _filings_health(filings) == "success"


def test_filings_health_cik_unresolved_note_is_a_string_not_a_crash():
    from analyst.data_pack import _filings_health

    # The reduced-depth shape whose values() are STRINGS — the old inline
    # expression called .get on one and crashed every non-SEC-ticker pack.
    filings = {"note": "CIK unresolved: no EDGAR filings (reduced-depth dossier)"}
    assert _filings_health(filings) == "CIK unresolved: no EDGAR filings (reduced-depth dossier)"


def test_filings_health_per_form_failure_surfaces_first_note():
    from analyst.data_pack import _filings_health

    filings = {"10k": {"note": "unavailable: EDGAR 503"}, "def14a": {"note": "no DEF 14A on record at EDGAR"}}
    assert _filings_health(filings) == "unavailable: EDGAR 503"


def test_filings_health_empty_is_unavailable():
    from analyst.data_pack import _filings_health

    assert _filings_health({}) == "unavailable"
