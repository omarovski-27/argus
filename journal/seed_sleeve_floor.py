"""Argus journal — seed ``config.min_sleeve_shares`` (SINGLE-KEY upsert, explicit value).

The fee-dominance floor for the sleeve-entry writer (``journal.sleeve_open``): below
roughly 8–10 shares the $-bracket structurally loses to per-trade fees, so the writer
refuses to register a unit smaller than this. The value is Omar's registered decision
— it is passed EXPLICITLY on the command line, never defaulted in code (the same
no-default posture as ``sleeve_symbol``: a guessed guard is no guard, L6).

Single-key by construction: re-running overwrites just this key. This module can
never full-re-seed a live config.

Run:  python -m journal.seed_sleeve_floor 10
"""

from __future__ import annotations

if __name__ == "__main__" and __package__ in (None, ""):
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import sys

from shared.db import get_client

CONFIG_KEY = "min_sleeve_shares"


def seed_sleeve_floor(value: int) -> None:
    """Validate, upsert the single config row, read it back (verify)."""
    if not isinstance(value, int) or value < 1:
        raise ValueError(f"min_sleeve_shares must be a positive integer, got {value!r}")
    client = get_client()
    client.table("config").upsert(
        [{"key": CONFIG_KEY, "value": value}], on_conflict="key"
    ).execute()
    stored = (
        client.table("config").select("value").eq("key", CONFIG_KEY).limit(1).execute().data
    )
    ok = bool(stored) and stored[0]["value"] == value
    print(f"[seed_sleeve_floor] {CONFIG_KEY} = {value} seeded + read-back "
          f"{'verified' if ok else 'MISMATCH (!)'}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python -m journal.seed_sleeve_floor <shares>   (e.g. 8 or 10 — the "
              "fee-dominance floor is your decision; there is deliberately no default)")
        raise SystemExit(2)
    try:
        floor_value = int(sys.argv[1])
    except ValueError:
        print(f"not an integer: {sys.argv[1]!r}")
        raise SystemExit(2) from None
    seed_sleeve_floor(floor_value)
