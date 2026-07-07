"""Argus analyst — seed the ``config.synthesis_model`` key (SINGLE-KEY upsert).

The dossier synthesizer (``analyst/dossier.py``) reads its model id from
``config.synthesis_model`` at runtime, so upgrading the model is a config upsert,
never a deploy — and moving to a pricier tier is an explicit Law-3 decision Omar
records by editing this row. The Sonnet default here matches the module's own
soft default (``analyst.dossier.DEFAULT_MODEL``); seeding it makes the knob
visible in the config table rather than implicit in code.

Single-key by construction (L6 seed-guard rule): re-running overwrites JUST this
key. This module can never full-re-seed a live config.

Run:  python -m analyst.seed_synthesis_model
"""

from __future__ import annotations

if __name__ == "__main__" and __package__ in (None, ""):
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from analyst.dossier import DEFAULT_MODEL
from shared.db import get_client

CONFIG_KEY = "synthesis_model"


def seed_synthesis_model() -> None:
    """Insert the single config row when absent; REFUSE to overwrite a changed one.

    A deliberately upgraded ``synthesis_model`` (the documented upgrade path is a
    config edit) must never be silently reverted by a re-run — the same
    drift-revert shape the ``seed_config`` live-DB guard exists to prevent (L6).
    """
    client = get_client()
    stored = (
        client.table("config").select("value").eq("key", CONFIG_KEY).limit(1).execute().data
    )
    if stored and stored[0]["value"] != DEFAULT_MODEL:
        print(
            f"[seed_synthesis_model] REFUSED: config.{CONFIG_KEY} is already "
            f"{stored[0]['value']!r} — a deliberate setting. Change it with a manual "
            f"single-key upsert, never a re-seed (L6)."
        )
        return
    client.table("config").upsert(
        [{"key": CONFIG_KEY, "value": DEFAULT_MODEL}], on_conflict="key"
    ).execute()
    stored = (
        client.table("config").select("value").eq("key", CONFIG_KEY).limit(1).execute().data
    )
    ok = bool(stored) and stored[0]["value"] == DEFAULT_MODEL
    print(f"[seed_synthesis_model] {CONFIG_KEY} = {DEFAULT_MODEL!r} seeded + read-back "
          f"{'verified' if ok else 'MISMATCH (!)'}")


if __name__ == "__main__":
    seed_synthesis_model()
