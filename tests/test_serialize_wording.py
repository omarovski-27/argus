"""§7 wording tests (digest serializer + shared event-filter phrase).

Two wording rules, both Law-2-shaped:
  1. A forward-dated arming event says the filter "will trigger" — "IN EFFECT" is
     reserved for events inside the 24h window of the digest date (a future state
     voiced as a present fact is misinformation).
  2. Config-sourced figures are voiced as configuration ("(config)"), never in a way
     a reader/model could mistake for retrieved portfolio state.
"""

from digest.serialize import _book_block, _calendar_block
from shared.event_filter import (
    EVENT_FILTER_RULE_ACTIVE,
    EVENT_FILTER_RULE_FORWARD,
    event_filter_phrase,
)


# --------------------------------------------------------------------------- #
# event_filter_phrase — tense by proximity, weaker claim on uncertainty
# --------------------------------------------------------------------------- #
def test_event_on_digest_date_is_in_effect():
    assert event_filter_phrase("2026-07-06", "2026-07-06") == EVENT_FILTER_RULE_ACTIVE


def test_event_tomorrow_is_in_effect():
    assert event_filter_phrase("2026-07-07", "2026-07-06") == EVENT_FILTER_RULE_ACTIVE


def test_event_next_week_will_trigger():
    assert event_filter_phrase("2026-07-13", "2026-07-06") == EVENT_FILTER_RULE_FORWARD


def test_unparseable_or_missing_dates_take_the_weaker_claim():
    assert event_filter_phrase(None, "2026-07-06") == EVENT_FILTER_RULE_FORWARD
    assert event_filter_phrase("2026-07-06", None) == EVENT_FILTER_RULE_FORWARD
    assert event_filter_phrase("soon", "2026-07-06") == EVENT_FILTER_RULE_FORWARD


# --------------------------------------------------------------------------- #
# _calendar_block — the rendered tag
# --------------------------------------------------------------------------- #
def _event(d: str, typ: str = "fomc") -> dict:
    return {"date": d, "type": typ, "symbol": None, "materiality": "high"}


def test_forward_event_renders_will_trigger_not_active():
    block = _calendar_block([_event("2026-07-13")], "2026-07-06")
    assert "will trigger the §8 event filter" in block
    assert "IN EFFECT" not in block


def test_imminent_event_renders_in_effect():
    block = _calendar_block([_event("2026-07-07")], "2026-07-06")
    assert "IN EFFECT" in block


def test_non_arming_event_gets_no_tag():
    block = _calendar_block([_event("2026-07-07", typ="holiday")], "2026-07-06")
    assert "event filter" not in block


# --------------------------------------------------------------------------- #
# _book_block — config figures voiced as configuration
# --------------------------------------------------------------------------- #
def _bundle(sleeve_shares=None) -> dict:
    cfg = {
        "sleeve_pct": 0.2,
        "weekly_trade_cap": 2,
        "phase": "A",
        "kill_criteria": {
            "early_warning": {"trade": 10},
            "checkpoint": {"trade": 20},
            "verdict": {"trade": 50},
        },
    }
    if sleeve_shares is not None:
        cfg["sleeve_shares"] = sleeve_shares
    return {
        "generated_for": "2026-07-06",
        "config": cfg,
        "positions": {"date": "2026-07-03", "rows": [{"symbol": "TSLA", "qty": 2.0}]},
        "round_trips": {"recent_30d": [], "cumulative_delta_shares": None},
        "source_health": {},
    }


def test_config_figures_are_voiced_as_config():
    block = _book_block(_bundle())
    assert "sleeve_pct 20% (config)" in block
    assert "(weekly cap, config)" in block
    assert "phase A (config)" in block
    assert "Pre-registered gates (config):" in block


def test_registered_sleeve_unit_is_still_config_voiced():
    block = _book_block(_bundle(sleeve_shares=17))
    assert "17 shares, registered unit; sleeve_pct 20% (config)" in block


def test_positions_line_stays_voiced_as_retrieved_snapshot():
    block = _book_block(_bundle())
    assert "Positions (snapshot 2026-07-03): TSLA qty 2.0" in block
    # The retrieved line carries no config marker — the distinction IS the point.
    assert "(config)" not in block.split("\n")[1]
