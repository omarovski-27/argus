"""Tests for the round-trip pairing engine (journal/pairing.py).

These exercise the PURE pairing logic only — no DB, no network. Each fixture has a
known hand-computed answer, checked directly against the spec. Fees are 1.00 per leg
(2.00 per pair) throughout, which is what makes the winner/loser pnl land on the
round numbers in the spec (+23.50 / −27.50).
"""

from __future__ import annotations

import pytest

from journal.pairing import effective_type, pair_round_trips

SYM = "TSLA"
FEE = 1.00  # per leg → 2.00 summed per round trip


def leg(ext_id, exec_time, side, qty, price, trade_type, override_type=None):
    """Build a synthetic ``transactions`` row."""
    return {
        "ext_id": ext_id,
        "exec_time": exec_time,
        "symbol": SYM,
        "side": side,
        "qty": qty,
        "price": price,
        "fees": FEE,
        "trade_type": trade_type,
        "override_type": override_type,
    }


def sell(ext_id, price, t="2026-06-15T14:30:00+00:00", qty=17, **kw):
    return leg(ext_id, t, "sell", qty, price, "round_trip_sell", **kw)


def rebuy(ext_id, price, t="2026-06-15T15:30:00+00:00", qty=17, **kw):
    return leg(ext_id, t, "buy", qty, price, "round_trip_rebuy", **kw)


# --------------------------------------------------------------------------- #
# pnl metric
# --------------------------------------------------------------------------- #
def test_winner_pnl_is_plus_23_50():
    rows = pair_round_trips([sell("s1", 405.00), rebuy("r1", 403.50)])
    assert len(rows) == 1
    row = rows[0]
    assert row["pnl_usd"] == pytest.approx(23.50)  # (405 − 403.50)·17 − 2.00
    assert row["qty"] == 17
    assert row["sell_px"] == 405.00
    assert row["rebuy_px"] == 403.50
    assert row["fees"] == pytest.approx(2.00)
    assert row["sell_ext_id"] == "s1"
    assert row["delta_shares"] is None  # never stored per row


def test_loser_pnl_is_minus_27_50():
    rows = pair_round_trips([sell("s1", 405.00), rebuy("r1", 406.50)])
    assert len(rows) == 1
    assert rows[0]["pnl_usd"] == pytest.approx(-27.50)  # (405 − 406.50)·17 − 2.00


# --------------------------------------------------------------------------- #
# idempotency
# --------------------------------------------------------------------------- #
def test_pairing_twice_same_fills_dedups_to_one_row():
    fills = [sell("s1", 405.00), rebuy("r1", 403.50)]
    first = pair_round_trips(fills)
    second = pair_round_trips(fills)
    # Deterministic: same input → same single row, same idempotency key both times.
    assert len(first) == 1 and len(second) == 1
    assert first[0]["sell_ext_id"] == second[0]["sell_ext_id"] == "s1"
    # Simulate the UNIQUE(sell_ext_id) upsert: applying both runs leaves exactly one row.
    by_key = {r["sell_ext_id"]: r for r in (first + second)}
    assert len(by_key) == 1


# --------------------------------------------------------------------------- #
# classification: override wins; unclassified skipped
# --------------------------------------------------------------------------- #
def test_override_makes_a_dca_buy_pair_as_a_rebuy():
    # trade_type says dca_buy, but /override says round_trip_rebuy → effective type wins.
    overridden = leg("r1", "2026-06-15T15:30:00+00:00", "buy", 17, 403.50,
                     "dca_buy", override_type="round_trip_rebuy")
    assert effective_type(overridden) == "round_trip_rebuy"
    rows = pair_round_trips([sell("s1", 405.00), overridden])
    assert len(rows) == 1
    assert rows[0]["pnl_usd"] == pytest.approx(23.50)


def test_unclassified_leg_is_skipped():
    noise = leg("x1", "2026-06-15T15:31:00+00:00", "buy", 9, 404.00, "unclassified")
    rows = pair_round_trips([sell("s1", 405.00), rebuy("r1", 403.50), noise])
    # The unclassified leg neither pairs nor adds a row — exactly the one real pair remains.
    assert len(rows) == 1
    assert rows[0]["sell_ext_id"] == "s1"


# --------------------------------------------------------------------------- #
# multiple pairs same day → exec_time order
# --------------------------------------------------------------------------- #
def test_two_pairs_same_day_ordered_by_exec_time():
    fills = [
        rebuy("r2", 410.00, t="2026-06-15T13:30:00+00:00"),  # second trip's rebuy
        sell("s2", 411.00, t="2026-06-15T13:00:00+00:00"),   # second trip's sell
        rebuy("r1", 403.50, t="2026-06-15T10:00:00+00:00"),  # first trip's rebuy
        sell("s1", 405.00, t="2026-06-15T09:30:00+00:00"),   # first trip's sell
    ]
    rows = pair_round_trips(fills)
    assert [r["sell_ext_id"] for r in rows] == ["s1", "s2"]   # earliest sell first
    assert [r["day_trades_in_window"] for r in rows] == [1, 2]  # ordinal within the week
    assert rows[0]["rebuy_px"] == 403.50 and rows[1]["rebuy_px"] == 410.00


# --------------------------------------------------------------------------- #
# dangling sell
# --------------------------------------------------------------------------- #
def test_dangling_sell_yields_no_row_and_no_crash():
    rows = pair_round_trips([sell("s1", 405.00)])  # no rebuy
    assert rows == []


def test_surplus_rebuy_is_left_unpaired():
    # Two rebuys, one sell, same day → exactly one pair (earliest rebuy), surplus dropped.
    fills = [
        sell("s1", 405.00, t="2026-06-15T09:30:00+00:00"),
        rebuy("r1", 403.50, t="2026-06-15T10:00:00+00:00"),
        rebuy("r2", 402.00, t="2026-06-15T11:00:00+00:00"),
    ]
    rows = pair_round_trips(fills)
    assert len(rows) == 1
    assert rows[0]["rebuy_px"] == 403.50


# --------------------------------------------------------------------------- #
# cumulative metric + share view (computed once, never per-row)
# --------------------------------------------------------------------------- #
def test_cumulative_pnl_and_share_view_computed_once():
    fills = [
        # week 1 winner: +23.50
        sell("s1", 405.00, t="2026-06-15T14:30:00+00:00"),
        rebuy("r1", 403.50, t="2026-06-15T15:30:00+00:00"),
        # week 2 loser: −27.50
        sell("s2", 405.00, t="2026-06-22T14:30:00+00:00"),
        rebuy("r2", 406.50, t="2026-06-22T15:30:00+00:00"),
    ]
    rows = pair_round_trips(fills)
    assert len(rows) == 2

    # The metric is summed from the stored pnl_usd, never from per-row shares.
    assert all(r["delta_shares"] is None for r in rows)
    cumulative_pnl = sum(r["pnl_usd"] for r in rows)
    assert cumulative_pnl == pytest.approx(-4.00)  # 23.50 − 27.50

    # Share view derived ONCE from the cumulative total at the current price (§7).
    current_price = 400.00
    share_view = cumulative_pnl / current_price
    assert share_view == pytest.approx(-0.01)

    # Different ISO weeks → each trip is the first of its own week.
    assert [r["day_trades_in_window"] for r in rows] == [1, 1]


# --------------------------------------------------------------------------- #
# digest_id resolution (latest digest with sent_at ≤ trade date)
# --------------------------------------------------------------------------- #
def test_digest_id_is_latest_sent_on_or_before_trade_date():
    digests = [
        {"id": 1, "sent_at": "2026-06-08T11:00:00+00:00"},  # prior week
        {"id": 2, "sent_at": "2026-06-15T11:00:00+00:00"},  # trade day → in effect
        {"id": 3, "sent_at": "2026-06-22T11:00:00+00:00"},  # after → excluded
    ]
    rows = pair_round_trips([sell("s1", 405.00), rebuy("r1", 403.50)], digests)
    assert rows[0]["digest_id"] == 2


def test_digest_id_is_none_when_no_digest_precedes_trade():
    digests = [{"id": 3, "sent_at": "2026-06-22T11:00:00+00:00"}]  # all after the trade
    rows = pair_round_trips([sell("s1", 405.00), rebuy("r1", 403.50)], digests)
    assert rows[0]["digest_id"] is None
