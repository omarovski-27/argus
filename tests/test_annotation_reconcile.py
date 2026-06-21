"""Tests for the /felt annotation pipeline (pure logic only — no DB / network / Telegram).

Three pure surfaces under test:
  • ``parse_felt`` (bot/handlers.py): /felt arg parsing + vocab validation.
  • ``match_annotations`` (journal/annotation_reconcile.py): pairing staged notes to round trips
    on (symbol, UTC date), and the idempotency/self-heal property that comes from NOT excluding
    already-annotated trips.
  • ``stale_unmatched`` (journal/annotation_reconcile.py): the Law-7 audit — unconsumed notes past
    their trade_date with no trip (a today note is normal and not flagged).
"""

from __future__ import annotations

from bot.handlers import parse_felt
from journal.annotation_reconcile import match_annotations, stale_unmatched

REASONS = ["momentum", "setup", "catalyst", "reversion", "discretionary"]
FEELINGS = ["calm", "fomo", "anxious", "revenge", "bored"]


# --------------------------------------------------------------------------- #
# parse_felt — validation + parsing
# --------------------------------------------------------------------------- #
def test_parse_felt_reason_feeling_only():
    out = parse_felt(["setup", "calm"], REASONS, FEELINGS)
    assert out == {"reason": "setup", "feeling": "calm", "confidence_1to5": None}


def test_parse_felt_with_confidence():
    out = parse_felt(["reversion", "anxious", "3"], REASONS, FEELINGS)
    assert out == {"reason": "reversion", "feeling": "anxious", "confidence_1to5": 3}


def test_parse_felt_is_case_insensitive():
    out = parse_felt(["SETUP", "Calm"], REASONS, FEELINGS)
    assert out == {"reason": "setup", "feeling": "calm", "confidence_1to5": None}


def test_parse_felt_too_few_tokens_returns_usage():
    out = parse_felt(["setup"], REASONS, FEELINGS)
    assert isinstance(out, str) and out.startswith("Usage:")


def test_parse_felt_bad_reason():
    out = parse_felt(["scalp", "calm"], REASONS, FEELINGS)
    assert isinstance(out, str) and "reason" in out.lower()


def test_parse_felt_bad_feeling():
    out = parse_felt(["setup", "elated"], REASONS, FEELINGS)
    assert isinstance(out, str) and "feeling" in out.lower()


def test_parse_felt_confidence_non_integer():
    out = parse_felt(["setup", "calm", "high"], REASONS, FEELINGS)
    assert isinstance(out, str) and "integer" in out.lower()


def test_parse_felt_confidence_out_of_range():
    out = parse_felt(["setup", "calm", "6"], REASONS, FEELINGS)
    assert isinstance(out, str) and "1-5" in out


def test_parse_felt_confidence_non_ascii_digit_rejected():
    # int('٣') == 3 silently; the ASCII-digit gate must reject it to the documented contract.
    out = parse_felt(["setup", "calm", "٣"], REASONS, FEELINGS)
    assert isinstance(out, str) and "integer" in out.lower()


def test_parse_felt_confidence_plus_sign_rejected():
    out = parse_felt(["setup", "calm", "+3"], REASONS, FEELINGS)
    assert isinstance(out, str) and "integer" in out.lower()


def test_parse_felt_confidence_underscore_grouping_rejected():
    # int('3_0') == 30 in Python; must not be silently coerced past the gate.
    out = parse_felt(["setup", "calm", "3_0"], REASONS, FEELINGS)
    assert isinstance(out, str) and "integer" in out.lower()


def test_parse_felt_confidence_surrounding_space_is_split_away():
    # ' 3 ' can't reach as one token (split() drops the spaces), but a tab-joined oddity would;
    # assert the canonical clean integer still parses so the gate didn't over-reject.
    assert parse_felt(["setup", "calm", "3"], REASONS, FEELINGS)["confidence_1to5"] == 3


# --------------------------------------------------------------------------- #
# match_annotations — (symbol, UTC date) pairing
# --------------------------------------------------------------------------- #
def _note(id_, created_at, reason="setup", feeling="calm", conf=4, symbol="TSLA", trade_date=None):
    # trade_date is the explicit UTC calendar day the handler stamps; default to the created_at
    # date so the existing same-day cases keep matching, override it for the cross-date case.
    return {
        "id": id_,
        "created_at": created_at,
        "trade_date": trade_date or created_at[:10],
        "symbol": symbol,
        "reason": reason,
        "feeling": feeling,
        "confidence_1to5": conf,
    }


def _trip(id_, date_, symbol="TSLA"):
    return {"id": id_, "date": date_, "symbol": symbol}


def test_one_note_one_same_day_trip_attaches_and_consumes():
    trips = [_trip(100, "2026-03-02")]
    pending = [_note(1, "2026-03-02T20:14:33+00:00")]
    rows, consumed = match_annotations(trips, pending)
    assert rows == [
        {
            "round_trip_id": 100,
            "reason": "setup",
            "feeling": "calm",
            "confidence_1to5": 4,
            "captured_at": "2026-03-02T20:14:33+00:00",  # the honest in-moment time, carried
        }
    ]
    assert consumed == [(1, 100)]


def test_note_with_no_trip_that_day_is_not_consumed():
    # Stale-note proof: a /felt with no trade that day finds no trip bucket → never attaches.
    trips = [_trip(100, "2026-03-05")]
    pending = [_note(1, "2026-03-02T20:00:00+00:00")]
    rows, consumed = match_annotations(trips, pending)
    assert rows == [] and consumed == []


def test_different_utc_date_does_not_match():
    # Window-bug guard: exact-date only — a Friday note must not attach to a Monday trip.
    trips = [_trip(100, "2026-03-02")]  # Monday
    pending = [_note(1, "2026-02-27T21:00:00+00:00")]  # prior Friday
    rows, consumed = match_annotations(trips, pending)
    assert rows == [] and consumed == []


def test_wrong_symbol_does_not_match():
    trips = [_trip(100, "2026-03-02", symbol="TSLA")]
    pending = [_note(1, "2026-03-02T20:00:00+00:00", symbol="SPCX")]
    rows, consumed = match_annotations(trips, pending)
    assert rows == [] and consumed == []


def test_two_trips_one_note_annotates_earliest_trip():
    # FIFO by (date, id): the single note attaches to the lower-id (earlier) same-day trip.
    trips = [_trip(101, "2026-03-02"), _trip(100, "2026-03-02")]
    pending = [_note(1, "2026-03-02T20:00:00+00:00")]
    rows, consumed = match_annotations(trips, pending)
    assert len(rows) == 1
    assert rows[0]["round_trip_id"] == 100  # earliest trip, not insertion order
    assert consumed == [(1, 100)]


def test_self_heal_reemits_pair_for_already_annotated_trip():
    # Correction-1 property: an already-annotated trip whose note is STILL unconsumed (a crash
    # orphan) is re-matched — the matcher doesn't exclude annotated trips. The runner's
    # UPDATE-on-conflict upsert then rewrites identical values and the note is finally marked
    # consumed. So the matcher must re-emit the (note→trip) pair, not skip it.
    trips = [_trip(100, "2026-03-02")]
    pending = [_note(1, "2026-03-02T20:00:00+00:00")]  # unconsumed (crash left it so)
    rows, consumed = match_annotations(trips, pending)
    assert rows and rows[0]["round_trip_id"] == 100
    assert consumed == [(1, 100)]


# --------------------------------------------------------------------------- #
# stale_unmatched — the Law-7 audit (a past-date note with no trip is stranded)
# --------------------------------------------------------------------------- #
def test_stale_unmatched_flags_past_note_with_no_trip():
    # A note from a prior day that matched no trip (matched_ids empty) is stranded → surfaced.
    pending = [_note(1, "2026-03-02T20:00:00+00:00", trade_date="2026-03-02")]
    stale = stale_unmatched(pending, matched_ids=set(), today="2026-03-05")
    assert [n["id"] for n in stale] == [1]


def test_stale_unmatched_ignores_today_note():
    # A note dated today that didn't match is NORMAL (may not have traded yet) — not flagged.
    pending = [_note(1, "2026-03-05T20:00:00+00:00", trade_date="2026-03-05")]
    assert stale_unmatched(pending, matched_ids=set(), today="2026-03-05") == []


def test_stale_unmatched_skips_matched_notes():
    # A past note that DID match a trip this run is consumed, not stranded.
    pending = [_note(1, "2026-03-02T20:00:00+00:00", trade_date="2026-03-02")]
    assert stale_unmatched(pending, matched_ids={1}, today="2026-03-05") == []


def test_stale_unmatched_ignores_note_without_trade_date():
    # A note with no trade_date can't be judged stale (and never matches) — never flagged.
    # Built directly: the _note helper's ``trade_date or created_at[:10]`` can't yield a null.
    pending = [{"id": 1, "trade_date": None, "symbol": "TSLA"}]
    assert stale_unmatched(pending, matched_ids=set(), today="2026-03-05") == []
