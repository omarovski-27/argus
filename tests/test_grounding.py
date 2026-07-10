"""Unit tests for digest.grounding — the Law-2 numeric grounding validator.

``validate_text`` is a pure function of (synthesized text, serialized block), so
every case here is two strings — no DB, no bundle. The live regression probe is
``python -m digest.grounding`` (validates a stored digest against its own frozen
bundle_json; all four current-era stored digests pass with zero flags).
"""

import pytest

from digest.grounding import GroundingError, enforce_grounding, validate_text

BLOCK = """=== ARGUS DIGEST INPUT ===
GENERATED_FOR: 2026-06-29 (Sunday)   RUN_TYPE: weekly

PRICES (last close per ticker; Tiingo EOD)
  TSLA  429.30 on 2026-06-26  (prev 424.51, Δ +4.79 / +1.1%)
  SPY   761.90 on 2026-06-26

INDICATORS (latest local pandas_ta values)
  TSLA  (as_of 2026-06-26): SMA50 322.97 | SMA200 348.80 | RSI14 72.1 | MACD -3.31 (signal -5.60, hist 2.29)

MACRO (latest observation per FRED series)
  VIX (implied volatility index): 17.63  (2026-06-26, age 3d); trailing 252-session range 13.98-52.33 (latest at ~62th pctile)

HEADLINES (24 total; 3 off-watchlist news item(s) dropped). [direction magnitude 0-1] source — title
  [bullish 0.62] Alpha Vantage — Tesla raises $16.5B in new capital

BOOK (core untouchable; sleeve-only metrics)
  Round trips this week: 0 / 2 (weekly cap).
"""


def _tokens(violations):
    return [v["token"] for v in violations]


def test_clean_text_passes():
    text = (
        "TSLA closed at 429.30 on 2026-06-26, up +1.1% from 424.51. "
        "RSI14 at 72.1 sits high on its 0-100 range. VIX printed 17.63, "
        "near the 62nd percentile of its trailing 252-session range 13.98-52.33. "
        "Round trips this week: 0 of 2."
    )
    assert validate_text(text, BLOCK) == []


def test_hallucinated_number_is_flagged():
    violations = validate_text("TSLA volume was 93.99 million shares.", BLOCK)
    assert _tokens(violations) == ["93.99"]
    assert "million shares" in violations[0]["context"]


def test_model_computed_spread_is_flagged():
    # The digest-3 real-world catch: a derived "80 bps" appearing nowhere in the block.
    violations = validate_text("a positive term premium of roughly 80 bps by the data", BLOCK)
    assert _tokens(violations) == ["80"]


def test_rounding_to_displayed_precision_passes_misquote_fails():
    assert validate_text("VIX at 17.6 is mid-range.", BLOCK) == []  # 17.63 -> "17.6"
    assert validate_text("VIX at 18 is mid-range.", BLOCK) == []    # 17.63 -> "18"
    assert _tokens(validate_text("VIX at 17.9.", BLOCK)) == ["17.9"]  # not a rounding


def test_magnitude_suffix_forms_pass():
    assert validate_text("Tesla raised $16.5B in new capital.", BLOCK) == []
    assert validate_text("Tesla raised 16.5 billion dollars.", BLOCK) == []
    # 3.2B appears nowhere — flagged even in suffixed form.
    assert _tokens(validate_text("a $3.2B raise", BLOCK)) == ["3.2"]


def test_iso_date_membership():
    assert validate_text("Data as of 2026-06-26.", BLOCK) == []
    assert _tokens(validate_text("Data as of 2026-06-25.", BLOCK)) == ["2026-06-25"]


def test_prose_date_grounds_against_block_iso():
    assert validate_text("TSLA closed at 429.30 on June 26.", BLOCK) == []
    assert validate_text("as of June 26th, 2026", BLOCK) == []
    assert validate_text("on the 26th of June", BLOCK) == []
    # June 27 matches no block date -> its day falls through to numeric matching -> flagged.
    assert _tokens(validate_text("a June 27 session", BLOCK)) == ["27"]


def test_year_mention_grounds_from_block_dates():
    assert validate_text("So far in 2026 the trend holds.", BLOCK) == []


def test_unicode_minus_and_sign_are_normalized():
    assert validate_text("MACD at −3.31 with histogram 2.29.", BLOCK) == []


def test_label_embedded_digits_ground_but_are_not_claims():
    # Block-side RSI14/SMA50/SMA200 ground "14-day", "50-day", "200-day" phrasing.
    assert validate_text("the 14-day RSI and the 50- and 200-day averages", BLOCK) == []


def test_bounded_scale_whitelist():
    # Clauses 1 & 4 permit locating a bounded value on its intrinsic scale.
    assert validate_text("RSI 72.1 — high on its 0-100 range.", BLOCK) == []
    assert validate_text("sentiment magnitude 0.62 on a 0-1 scale", BLOCK) == []


def test_comma_grouped_numbers_normalize():
    assert validate_text("capital of $16,500,000,000", BLOCK) == []  # == 16.5B expanded


def test_enforce_raises_with_offending_tokens():
    with pytest.raises(GroundingError) as err:
        enforce_grounding("An invented 93.99 million share print.", {})
    assert "93.99" in str(err.value)
    assert err.value.violations[0]["token"] == "93.99"


def test_empty_bundle_serializes_and_still_validates():
    # serialize_bundle({}) renders the all-sections-missing block; grounded text passes.
    assert enforce_grounding("Positions: none on record.", {}) is None


def test_in_thousands_suffix_form_grounds_against_full_dollar_block():
    """Filing-table phrasing: 'X in thousands' == the block's full-dollar figure
    (the PLTR re-denomination class — same equivalence as 'X thousand')."""
    block = "operating cash flow 2,100,591,000; total assets 8,900,392,000"
    assert validate_text("OCF was 2,100,591 in thousands.", block) == []
    assert validate_text("Total assets of 8,900,392 in thousands.", block) == []


def test_in_millions_suffix_form_grounds_too():
    block = "revenue 82,056,000,000"
    assert validate_text("Segment revenue was 82,056 in millions.", block) == []


def test_in_thousands_form_still_flags_when_nothing_matches():
    violations = validate_text("SBC was 999,999 in thousands.", "revenue 100")
    assert [v["token"] for v in violations] == ["999,999"]
