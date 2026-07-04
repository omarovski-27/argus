"""Unit tests for ingestion.ibkr_flex date parsing — the pure format core (no DB).

Pins the dd/MM/yyyy branch added 2026-07-05: the live Flex query emits day-first
slash dates (probed raw statement: toDate='03/07/2026', whenGenerated=
'04/07/2026;174639' on a LastBusinessDay statement — only day-first reads sane).
Before this branch existed every date in every section resolved to None, so
positions_snapshot dropped ALL rows for 9 straight days (2026-06-26..07-04), the
first two real fills stored with NULL exec_time (blinding round-trip pairing,
Law 6), and the funding deposit never reached contributions — all under green
fetch_log rows (the Law 7 gap fixed alongside).

MM/dd is deliberately unsupported: on day<=12 values both conventions "succeed"
and a silently transposed date is a corrupt journal row — the tests assert the
day-first reading so any future format change fails here, loudly.
"""

from ingestion.ibkr_flex import _flex_date, _flex_datetime


def test_live_query_slash_date_is_day_first():
    # Regression memorial: '03/07/2026' is July 3rd, not March 7th.
    assert _flex_date("03/07/2026") == "2026-07-03"


def test_live_query_slash_datetime_with_semicolon_time():
    assert _flex_datetime("04/07/2026;174639") == "2026-07-04T17:46:39"


def test_day_over_twelve_is_unambiguous():
    assert _flex_date("26/06/2026") == "2026-06-26"


def test_compact_formats_still_parse():
    assert _flex_date("20260703") == "2026-07-03"
    assert _flex_datetime("20260703;142500") == "2026-07-03T14:25:00"


def test_iso_formats_still_parse():
    assert _flex_date("2026-07-03") == "2026-07-03"
    assert _flex_date("2026-07-03 14:25:00") == "2026-07-03"
    assert _flex_datetime("2026-07-03T14:25:00Z") == "2026-07-03T14:25:00+00:00"


def test_garbage_and_empty_yield_none():
    assert _flex_date("") is None
    assert _flex_date(None) is None
    assert _flex_date("not-a-date") is None
    assert _flex_datetime("13/13/2026") is None  # no 13th month in either convention
