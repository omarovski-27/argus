"""Sleeve-entry writer tests (journal/sleeve_open.py) — the §8 derivation + refusal matrix.

Pure core only (no DB, no terminal): the derivation math, the row-derived cash with
its missing-deposit caveat, and every preflight blocker. The interactive confirm and
the single-key write are thin glue over these, exercised live by the operator.
"""

from datetime import date

import pytest

from journal.sleeve_open import derive_cash, derive_sleeve_shares, preflight_blockers


# --------------------------------------------------------------------------- #
# derive_sleeve_shares — floor(sleeve_pct × PV ÷ price)
# --------------------------------------------------------------------------- #
def test_derivation_floors_never_rounds_up():
    # 0.20 × 35,950 ÷ 393.45 = 18.27… -> 18 (§8: floor, so the unit is affordable)
    assert derive_sleeve_shares(0.20, 35_950.0, 393.45) == 18
    # Exactly at a boundary stays exact.
    assert derive_sleeve_shares(0.5, 100.0, 10.0) == 5
    # Just under the next integer floors down.
    assert derive_sleeve_shares(0.5, 99.9, 10.0) == 4


@pytest.mark.parametrize(
    ("pct", "pv", "price"),
    [(0.0, 100.0, 10.0), (1.1, 100.0, 10.0), (0.2, 0.0, 10.0), (0.2, -5.0, 10.0), (0.2, 100.0, 0.0)],
)
def test_derivation_rejects_non_positive_inputs(pct, pv, price):
    with pytest.raises(ValueError):
        derive_sleeve_shares(pct, pv, price)


# --------------------------------------------------------------------------- #
# derive_cash — deposits − buys − fees + sells, from stored rows only
# --------------------------------------------------------------------------- #
def _buy(qty, price, fees, exec_time="2026-06-26T13:31:00+00:00"):
    return {"exec_time": exec_time, "created_at": exec_time, "side": "buy",
            "qty": qty, "price": price, "fees": fees}


def test_cash_derivation_math():
    contributions = [{"date": "2026-06-25", "amount": 1000.0}, {"date": "2026-07-09", "amount": 35000.0}]
    transactions = [
        _buy(2.0, 374.105, 1.000006),
        _buy(1.0, 152.88, 1.000003),
        {"exec_time": "2026-07-01T14:00:00+00:00", "created_at": "2026-07-01T14:00:00+00:00",
         "side": "sell", "qty": 1.0, "price": 160.0, "fees": 1.0},
    ]
    out = derive_cash(contributions, transactions)
    assert out["deposits"] == 36000.0
    assert out["buy_cost"] == pytest.approx(901.09)
    assert out["sell_proceeds"] == 160.0
    assert out["fees"] == pytest.approx(3.000009)
    assert out["cash"] == pytest.approx(36000.0 - 901.09 - 3.000009 + 160.0)
    assert out["caveats"] == []  # earliest contribution (06-25) predates the fills


def test_fills_before_any_contribution_flag_the_missing_deposit():
    # The live pre-widen shape: 06-26 fills, but the only stored deposit is 07-09.
    contributions = [{"date": "2026-07-09", "amount": 35000.0}]
    transactions = [_buy(2.0, 374.105, 1.0)]
    assert "missing_deposit" in derive_cash(contributions, transactions)["caveats"]


def test_fills_with_no_contributions_at_all_flag_the_missing_deposit():
    assert "missing_deposit" in derive_cash([], [_buy(1.0, 152.88, 1.0)])["caveats"]


def test_null_exec_time_falls_back_to_created_at_for_the_caveat():
    # ids 3/4's live shape: exec_time None, created_at 2026-06-26 — still detectably
    # earlier than a 2026-07-09 first contribution.
    txn = {"exec_time": None, "created_at": "2026-06-26T21:04:06+00:00",
           "side": "buy", "qty": 1.0, "price": 152.88, "fees": 1.0}
    out = derive_cash([{"date": "2026-07-09", "amount": 35000.0}], [txn])
    assert "missing_deposit" in out["caveats"]


def test_no_transactions_is_cleanly_derivable():
    out = derive_cash([{"date": "2026-07-09", "amount": 35000.0}], [])
    assert out["cash"] == 35000.0
    assert out["caveats"] == []


# --------------------------------------------------------------------------- #
# preflight_blockers — the refusal matrix (nothing is ever written on a refusal)
# --------------------------------------------------------------------------- #
_TODAY = date(2026, 7, 10)


def _clear(**overrides):
    kwargs = dict(
        sleeve_shares_row=None,
        sleeve_pct=0.2,
        min_floor=10,
        positions_date="2026-07-09",
        price_date="2026-07-09",
        cash_caveats=[],
        today=_TODAY,
    )
    kwargs.update(overrides)
    return preflight_blockers(**kwargs)


def test_clear_preflight_has_no_blockers():
    assert _clear() == []


def test_already_registered_sleeve_blocks():
    blockers = _clear(sleeve_shares_row=17)
    assert any("already registered" in b for b in blockers)


def test_missing_floor_blocks_and_names_the_seeder():
    blockers = _clear(min_floor=None)
    assert any("seed_sleeve_floor" in b for b in blockers)


def test_invalid_sleeve_pct_blocks():
    assert any("sleeve_pct" in b for b in _clear(sleeve_pct=None))
    assert any("sleeve_pct" in b for b in _clear(sleeve_pct=1.5))


def test_stale_inputs_block_but_weekend_age_passes():
    # Friday snapshot read on Monday (3 calendar days) passes the 4-day envelope…
    assert _clear(positions_date="2026-07-03", today=date(2026, 7, 6)) == []
    # …but anything staler refuses, for either input.
    assert any("positions_snapshot" in b for b in _clear(positions_date="2026-07-03"))
    assert any("sleeve price" in b for b in _clear(price_date="2026-07-01"))


def test_missing_rows_block():
    assert any("positions_snapshot" in b for b in _clear(positions_date=None))
    assert any("sleeve price" in b for b in _clear(price_date=None))


def test_missing_deposit_caveat_blocks_registration():
    blockers = _clear(cash_caveats=["missing_deposit"])
    assert any("backfill" in b for b in blockers)
