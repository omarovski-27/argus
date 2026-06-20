"""Argus ingestion — bootstrap seed of the pre-registered ``config`` parameters (§4, §8).

Seeds the seven tunable parameters the digest's book section + the journal gates read
at runtime: sleeve_pct, sleeve_shares, bracket, phase, weekly_trade_cap, watchlist and
kill_criteria. No external API — these are fixed reference values, identical across
blueprint §8 / §1.5 and the migration's ``config.value`` column comment
(20260612175007_init_spine.sql). They are Omar's pre-registered risk limits; the values
below are the signed-off set.

Schema note: the applied migration keys ``config`` by ``key`` (text PK) with a ``jsonb``
``value`` and ``updated_at`` (default now()). On the bootstrap insert the default fills
``updated_at``; this script is a one-time bootstrap, not the runtime change path.

KNOWN GAP — in-DB config history (deferred to Phase 2):
    ``key`` is a PRIMARY KEY, so a parameter change is an overwrite (upsert on the key)
    and the prior value is gone. That contradicts the "new rows for full historical
    auditability" language in blueprint §4 / CLAUDE.md — config is current-value-only as
    built. This is NOT a Law-6 hole today: the pre-registered gates are pinned in the
    git-tracked, dated blueprint, so the pre-registration record exists independent of
    this table and an in-DB overwrite would still be contradicted by git history. Proper
    in-DB tamper-evidence (a ``config_history`` table + an AFTER UPDATE/INSERT trigger,
    which leaves config's simple current-value reads untouched everywhere) is deferred to
    Phase 2, where the gates formally go live and tamper-evidence actually bites.

Run:  python -m ingestion.seed_config   (or: python ingestion/seed_config.py)
"""

from __future__ import annotations

# Allow both `python -m ingestion.seed_config` and direct
# `python ingestion/seed_config.py`: put the repo root on sys.path so the `shared`
# package imports cleanly when run as a loose script.
if __name__ == "__main__" and __package__ in (None, ""):
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from shared.db import get_client

# The seven pre-registered parameters (blueprint §8 / §1.5). The JSON shapes match the
# migration's config.value column comment exactly. value is a jsonb column, so scalars
# (0.20, 17, "A", 2), an object (bracket, kill_criteria) and an array (watchlist) all
# store as-is.
CONFIG: dict[str, object] = {
    "sleeve_pct": 0.20,
    "sleeve_shares": 17,  # auto-adjusts on splits via Corporate Actions — not hardcoded downstream
    "bracket": {"target": 1.50, "stop": 1.50, "time_stop": "15:50 ET"},
    "phase": "A",
    "weekly_trade_cap": 2,
    # Lead-time window for checkpoint proximity pushes (§9): warn when the next gate is
    # within this many trades. Default 2 ≈ one week of lead at 1–2 trips/wk. Tunable row
    # so it changes by edit, not migration (journal/checkpoint_push.py reads it, fallback 2).
    "proximity_window": 2,
    "watchlist": ["TSLA", "SPCX", "SPY", "QQQ"],
    "kill_criteria": {
        "early_warning": {"trade": 10, "delta_shares_lt": -1.0},
        "checkpoint": {"trade": 20, "delta_shares_lt": 0},
        "verdict": {"trade": 50, "delta_shares_lt": 0},
    },
}


def seed_config() -> None:
    """Upsert the seven pre-registered parameters, idempotent on the ``key`` primary key.

    Re-running is safe: existing rows are updated in place, none are duplicated. Static
    data only — no external API call. ``updated_at`` is set by the column default on the
    bootstrap insert (see the module docstring on the deferred history path).
    """
    client = get_client()
    rows = [{"key": k, "value": v} for k, v in CONFIG.items()]
    client.table("config").upsert(rows, on_conflict="key").execute()
    keys = ", ".join(CONFIG)
    print(f"[seed_config] upserted {len(rows)} config keys: {keys}.")


if __name__ == "__main__":
    seed_config()
