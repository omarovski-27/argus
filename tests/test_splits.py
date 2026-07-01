"""Unit tests for quant.splits — the pure split-factor core (no DB / no network).

The factor rule under test: a filing reports share counts on the basis in effect at
its FILED date, so the factor is the product of ratios of every split whose
effective_date is STRICTLY AFTER the filed date. TSLA's two real splits are the
fixture because they are the seeded corporate_actions rows the live layer runs on.
"""

import pytest

from quant.splits import factor_from_splits

TSLA_SPLITS = [
    {"effective_date": "2020-08-31", "ratio": 5.0},
    {"effective_date": "2022-08-25", "ratio": 3.0},
]


def test_pre_both_splits_filing_gets_full_factor():
    # FY2015 10-K filed 2018-02-23 — before both splits: ×15.
    assert factor_from_splits(TSLA_SPLITS, "2018-02-23") == 15.0


def test_between_splits_filing_gets_later_split_only():
    # FY2020 10-K filed 2021-02-08 — after the 5:1, before the 3:1: ×3.
    assert factor_from_splits(TSLA_SPLITS, "2021-02-08") == 3.0


def test_post_both_splits_filing_is_unadjusted():
    # FY2022 10-K filed 2023-01-31 — after both: ×1.
    assert factor_from_splits(TSLA_SPLITS, "2023-01-31") == 1.0


def test_filed_on_effective_date_is_not_adjusted():
    # Strictly-after boundary: a filing made ON the effective date is already on the
    # new basis, so that split must not multiply it.
    assert factor_from_splits(TSLA_SPLITS, "2020-08-31") == 15.0 / 5.0
    assert factor_from_splits(TSLA_SPLITS, "2022-08-25") == 1.0


def test_no_splits_means_factor_one():
    assert factor_from_splits([], "2018-02-23") == 1.0


def test_invalid_ratio_fails_loud():
    # A silently skipped split would put every per-share figure on a wrong basis —
    # the layer must crash instead (Law 7).
    for bad in (None, 0, -3.0, "x"):
        with pytest.raises(ValueError):
            factor_from_splits([{"effective_date": "2020-08-31", "ratio": bad}], "2018-01-01")


def test_unparseable_filed_date_fails_loud():
    with pytest.raises(ValueError):
        factor_from_splits(TSLA_SPLITS, "not-a-date")
