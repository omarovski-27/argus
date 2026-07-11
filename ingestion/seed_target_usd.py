"""Argus ingestion — seed ``config.target_usd`` (SINGLE-KEY upsert; the $100K goal).

``bot.handlers`` renders the /book "distance to target" line against
``config.target_usd`` when present and falls back to a hardcoded 100_000 constant
otherwise — so today that figure is DISPLAYED but not RETRIEVED from a row (a soft
Law-2 gap). Seeding the key closes it: /book then renders the goal from the DB.

The $100K goal is the blueprint figure (§0/§13); it is not a gate or a trading
parameter, just the portfolio target, so a plain single-key upsert is the right
change path. An optional CLI arg overrides the default for a different goal.

Run:  python -m ingestion.seed_target_usd            (seeds 100000)
      python -m ingestion.seed_target_usd 120000     (a different goal)
"""

from __future__ import annotations

if __name__ == "__main__" and __package__ in (None, ""):
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import sys

from shared.db import get_client

CONFIG_KEY = "target_usd"
DEFAULT_TARGET = 100_000.0


def seed_target_usd(value: float = DEFAULT_TARGET) -> None:
    """Upsert the single config row, then read it back (verify)."""
    if not isinstance(value, (int, float)) or value <= 0:
        raise ValueError(f"target_usd must be a positive number, got {value!r}")
    value = float(value)
    client = get_client()
    client.table("config").upsert(
        [{"key": CONFIG_KEY, "value": value}], on_conflict="key"
    ).execute()
    stored = (
        client.table("config").select("value").eq("key", CONFIG_KEY).limit(1).execute().data
    )
    ok = bool(stored) and float(stored[0]["value"]) == value
    print(f"[seed_target_usd] {CONFIG_KEY} = {value:,.0f} seeded + read-back "
          f"{'verified' if ok else 'MISMATCH (!)'}")


if __name__ == "__main__":
    target = DEFAULT_TARGET
    if len(sys.argv) > 1:
        try:
            target = float(sys.argv[1])
        except ValueError:
            print(f"not a number: {sys.argv[1]!r}")
            raise SystemExit(2) from None
    seed_target_usd(target)
