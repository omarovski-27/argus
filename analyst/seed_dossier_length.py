"""Argus analyst — seed ``config.dossier_length`` (SINGLE-KEY upsert; brief/full).

The dossier delivery length (module spec §3, amended 2026-07-12) is a runtime knob:
the FULL dossier is always synthesized, gated and stored, and this key selects only
what Telegram receives. Seeded ``brief`` — the readable-on-a-phone default Omar asked
for. The read path (``analyst.dossier.resolve_dossier_length``) is FAIL-LOUD: it
refuses to guess a format on a missing/blank/off-vocabulary row, so seeding the key
is what makes delivery work.

Single-key by construction (L6 seed-guard rule): re-running overwrites JUST this key
back to the given value; it can never full-re-seed a live config. Unlike
``seed_synthesis_model`` it does NOT refuse a changed row — length is a display
preference Omar flips freely (and ``/analyze TICKER full`` overrides per-run anyway),
not a registered gate — but it stays a plain single-key upsert.

Run:  python -m analyst.seed_dossier_length            (seeds 'brief')
      python -m analyst.seed_dossier_length full       (flip the default)
"""

from __future__ import annotations

if __name__ == "__main__" and __package__ in (None, ""):
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import sys

from analyst.dossier import DOSSIER_LENGTHS
from shared.db import get_client

CONFIG_KEY = "dossier_length"
DEFAULT_LENGTH = "brief"


def seed_dossier_length(value: str = DEFAULT_LENGTH) -> None:
    """Upsert the single config row, then read it back (verify)."""
    if value not in DOSSIER_LENGTHS:
        raise ValueError(f"dossier_length must be one of {sorted(DOSSIER_LENGTHS)}, got {value!r}")
    client = get_client()
    client.table("config").upsert(
        [{"key": CONFIG_KEY, "value": value}], on_conflict="key"
    ).execute()
    stored = (
        client.table("config").select("value").eq("key", CONFIG_KEY).limit(1).execute().data
    )
    ok = bool(stored) and stored[0]["value"] == value
    print(f"[seed_dossier_length] {CONFIG_KEY} = {value!r} seeded + read-back "
          f"{'verified' if ok else 'MISMATCH (!)'}")


if __name__ == "__main__":
    length = sys.argv[1].lower() if len(sys.argv) > 1 else DEFAULT_LENGTH
    seed_dossier_length(length)
