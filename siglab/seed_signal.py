"""Argus Signal Lab — seed the IMMUTABLE registered blob ``config.signal_v1`` (once).

The rule, its scoring params and its verdict gates are registered ONCE and are immutable
thereafter (Law 6). This seeder writes ``SIGNAL_V1_DEFAULT`` only when the key is absent
and REFUSES to overwrite an existing row — a track record scored against a rule that could
be edited after the fact is worthless. A genuine rule change is a new ``signal_v2`` blob
with its own fresh ledger (PK includes signal_version), never a mutation of v1.

Run:  python -m siglab.seed_signal
"""

from __future__ import annotations

if __name__ == "__main__" and __package__ in (None, ""):
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import json

from shared.db import get_client
from siglab.registry import SIGNAL_CONFIG_KEY, SIGNAL_V1_DEFAULT


def seed_signal() -> None:
    """Register signal_v1 if absent; REFUSE to overwrite an existing registration (L6)."""
    client = get_client()
    stored = (
        client.table("config").select("value").eq("key", SIGNAL_CONFIG_KEY).limit(1)
        .execute().data or []
    )
    if stored:
        print(f"[seed_signal] REFUSED: config.{SIGNAL_CONFIG_KEY} is already registered "
              f"(status {stored[0]['value'].get('status')!r}). The rule + gates are "
              f"immutable once registered — a change is a new signal_v2, never a rewrite (L6).")
        return
    client.table("config").upsert(
        [{"key": SIGNAL_CONFIG_KEY, "value": SIGNAL_V1_DEFAULT}], on_conflict="key"
    ).execute()
    back = (
        client.table("config").select("value").eq("key", SIGNAL_CONFIG_KEY).limit(1)
        .execute().data or []
    )
    ok = bool(back) and back[0]["value"].get("rule") == SIGNAL_V1_DEFAULT["rule"]
    print(f"[seed_signal] registered {SIGNAL_CONFIG_KEY} "
          f"({'verified' if ok else 'MISMATCH (!)'}):")
    print(json.dumps(SIGNAL_V1_DEFAULT, indent=2))


if __name__ == "__main__":
    seed_signal()
