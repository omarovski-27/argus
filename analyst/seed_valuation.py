"""Argus analyst — seed ONLY the ``config.valuation_assumptions`` key (single-key upsert).

Same L6 posture as ``analyst.seed_peers``: exactly one config row, never a full
re-seed. The grid below is a GENERIC starting point, not a judgment about any
company — Omar tunes it (edit here and re-run, upsert the row directly, or pass
per-run overrides to ``quant.valuation.run_valuation``). The engine validates the
grid on every load and fails loud on a malformed one; there is deliberately no
in-code default (a hardcoded fallback would reintroduce the constant this row
exists to replace).

Semantics (quant/valuation.py): terminal_margin is an OWNER-EARNINGS margin on
revenue; exit_multiple applies to horizon-year owner earnings; weights are the
bear-heavy blend behind the margin-of-safety line (Law 4: conservative by
construction); base_rate_cagr_flag marks any scenario assuming growth above it.

Run:  python -m analyst.seed_valuation
"""

from __future__ import annotations

if __name__ == "__main__" and __package__ in (None, ""):
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from quant.valuation import CONFIG_KEY, validate_assumptions
from shared.db import get_client

VALUATION_ASSUMPTIONS: dict = {
    "horizon_years": 5,
    "required_return": 0.10,
    "weights": {"bear": 0.50, "base": 0.35, "bull": 0.15},
    "base_rate_cagr_flag": 0.20,
    "scenarios": {
        "bear": {
            "revenue_cagr": 0.00,
            "terminal_margin": 0.05,
            "exit_multiple": 12.0,
            "annual_dilution": 0.03,
        },
        "base": {
            "revenue_cagr": 0.10,
            "terminal_margin": 0.08,
            "exit_multiple": 18.0,
            "annual_dilution": 0.015,
        },
        "bull": {
            "revenue_cagr": 0.20,
            "terminal_margin": 0.12,
            "exit_multiple": 25.0,
            "annual_dilution": 0.005,
        },
    },
}


def seed_valuation_assumptions() -> None:
    """Validate, upsert the single config row, and read it back (verify)."""
    validate_assumptions(VALUATION_ASSUMPTIONS)  # never seed a grid the engine rejects
    client = get_client()
    client.table("config").upsert(
        [{"key": CONFIG_KEY, "value": VALUATION_ASSUMPTIONS}], on_conflict="key"
    ).execute()
    stored = (
        client.table("config").select("value").eq("key", CONFIG_KEY).limit(1).execute().data
    )
    ok = bool(stored) and stored[0]["value"] == VALUATION_ASSUMPTIONS
    print(f"[seed_valuation] {CONFIG_KEY} seeded + read-back {'verified' if ok else 'MISMATCH (!)'}")


if __name__ == "__main__":
    seed_valuation_assumptions()
