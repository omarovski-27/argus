"""Argus analyst — seed the rating-mapper thresholds (SINGLE-KEY upserts; mapper v2).

The growth-aware bottom-line rating (``analyst/rating.py``, mapper v2) reads three
tunables at derive time: ``rating_growth_cagr_min`` (the value/growth regime split),
``rating_gap_extreme`` and ``rating_gap_ok`` (the reverse-DCF gap-ratio bands). Seeding
makes them visible, editable config rows (§2 item 3) rather than implicit constants —
and the mapper's read (``load_rating_config``) treats an absent row as the documented
seed default, so seeding is optional-but-honest, and a present-but-corrupt value fails
loud.

Single-key by construction (L6 seed-guard rule): each threshold is its own upsert, so
re-running overwrites JUST these three keys back to the seeds — never a full config
reseed. Change one deliberately with a manual single-key upsert; a re-run here reverts
it, so treat this as the bootstrap, not the edit path.

Run:  python -m analyst.seed_rating_config
"""

from __future__ import annotations

if __name__ == "__main__" and __package__ in (None, ""):
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from analyst.rating import RATING_CONFIG_DEFAULTS
from shared.db import get_client


def seed_rating_config() -> None:
    """Upsert each rating threshold as its own single-key row, then read them back."""
    client = get_client()
    for key, value in RATING_CONFIG_DEFAULTS.items():
        client.table("config").upsert(
            [{"key": key, "value": value}], on_conflict="key"
        ).execute()
    rows = (
        client.table("config").select("key,value")
        .in_("key", list(RATING_CONFIG_DEFAULTS)).execute().data or []
    )
    stored = {r["key"]: r["value"] for r in rows}
    for key, value in RATING_CONFIG_DEFAULTS.items():
        ok = key in stored and float(stored[key]) == value
        print(f"[seed_rating_config] {key} = {value} seeded + read-back "
              f"{'verified' if ok else 'MISMATCH (!)'}")


if __name__ == "__main__":
    seed_rating_config()
