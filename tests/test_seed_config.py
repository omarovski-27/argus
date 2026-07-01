"""Unit tests for ingestion.seed_config's re-seed guard (_plan_seed — pure, no DB).

The hazard under test (CLAUDE.md / L6): a full seed against a LIVE config silently
reverts drift (an advanced phase, a derived sleeve_shares) to bootstrap defaults.
"""

from ingestion.seed_config import _plan_seed

SEED_KEYS = ["sleeve_pct", "phase", "watchlist"]


def test_bootstrap_on_empty_table_writes_everything():
    to_write, refusal = _plan_seed(set(), SEED_KEYS, missing_only=False)
    assert to_write == SEED_KEYS
    assert refusal is None


def test_full_reseed_against_live_rows_is_refused():
    to_write, refusal = _plan_seed({"phase"}, SEED_KEYS, missing_only=False)
    assert to_write == []
    assert refusal is not None
    assert "phase" in refusal  # names the live keys a re-seed would revert


def test_missing_only_writes_only_absent_keys():
    to_write, refusal = _plan_seed({"phase", "sleeve_pct"}, SEED_KEYS, missing_only=True)
    assert to_write == ["watchlist"]
    assert refusal is None


def test_missing_only_with_nothing_missing_is_a_noop():
    to_write, refusal = _plan_seed(set(SEED_KEYS), SEED_KEYS, missing_only=True)
    assert to_write == []
    assert refusal is None


def test_refusal_even_when_live_keys_are_not_seed_keys():
    # Any live row means the table is live — refuse the full path regardless.
    to_write, refusal = _plan_seed({"some_runtime_key"}, SEED_KEYS, missing_only=False)
    assert to_write == []
    assert refusal is not None
