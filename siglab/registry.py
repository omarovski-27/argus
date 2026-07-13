"""Argus Signal Lab — the immutable registered blob (``config.signal_v1``) + gate params.

The rule, its scoring parameters and its verdict gates are pre-registered ONCE, before the
track record begins, and are IMMUTABLE thereafter (Law 6 — the same discipline as the
journal's kill criteria: a gate you can move after seeing the data is no gate). The seeder
refuses to overwrite an existing ``signal_v1`` row; a genuine rule change is a NEW
``signal_v2`` blob with its own fresh ledger, never a mutation of the v1 record.

The fee model is pinned here so the shadow P&L series is fixed forever: 17 shares ×
$1.50 bracket ∓ $2.00 round-trip fee reproduces the blueprint's stated +$23.50 win /
-$27.50 loss unit economics exactly. This is the registered EXPERIMENT size, held fixed
for the life of the ledger regardless of any later real sleeve registration — a
track record scored against a moving position size would be meaningless.
"""

from __future__ import annotations

SIGNAL_VERSION = "v1"
SIGNAL_CONFIG_KEY = "signal_v1"

# The canonical registered blob. ``seed_signal`` writes this verbatim on a fresh config
# and then refuses to touch it again — after that the STORED row is the source of truth
# (identical to this by construction). Omar may edit this wording only BEFORE the first
# seed; afterwards an edit means a new signal_v2 module + key, never a v1 rewrite.
SIGNAL_V1_DEFAULT: dict = {
    "version": "v1",
    "rule": (
        "FAVORABLE iff TSLA close > SMA50 AND MACD histogram > the previous day's MACD "
        "histogram AND the event filter is clear for the next session AND VIX percentile "
        "< 80; else UNFAVORABLE."
    ),
    "registered_at": "2026-07-13",
    "status": "testing",   # registration state; the LIVE verdict is derived from the ledger
    "params": {
        "symbol": "TSLA",
        "vix_percentile_max": 80,
        "vix_window_sessions": 252,
        "bracket": 1.50,
        "shadow_shares": 17,
        "fee_per_round_trip": 2.00,
    },
    "gates": {
        # N counts FAVORABLE days that TRIGGERED a scored outcome (win + loss); no_trigger
        # and UNFAVORABLE days are logged but do not advance N (only win/loss inform edge).
        "n30": {"n": 30, "min_winrate": 0.55, "retire_if_pnl_le": 0.0},
        "n60": {"n": 60, "min_winrate": 0.58, "pass_pnl_gt_fee_mult": 0.5},
    },
}


def signal_params(blob: dict | None = None) -> dict:
    """The rule/scoring params (symbol, thresholds, bracket, shares, fee)."""
    return dict((blob or SIGNAL_V1_DEFAULT).get("params") or SIGNAL_V1_DEFAULT["params"])


def signal_gates(blob: dict | None = None) -> dict:
    """The verdict-gate spec (n30 retire gate, n60 pass gate)."""
    return dict((blob or SIGNAL_V1_DEFAULT).get("gates") or SIGNAL_V1_DEFAULT["gates"])


def load_signal(client) -> dict:
    """The registered blob from ``config.signal_v1`` when present, else the canonical default.

    A soft fall-through to the default is safe: the default IS the registration (the seeder
    writes it verbatim), so an unseeded config still renders/backfills the exact registered
    rule — and once seeded, the stored row is authoritative and immutable.
    """
    rows = (
        client.table("config").select("value").eq("key", SIGNAL_CONFIG_KEY).limit(1)
        .execute().data or []
    )
    value = rows[0]["value"] if rows else None
    return value if isinstance(value, dict) and value.get("params") else dict(SIGNAL_V1_DEFAULT)
