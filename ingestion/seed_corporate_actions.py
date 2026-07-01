"""Argus ingestion — seed ``corporate_actions`` with TSLA's two historical splits.

The split layer (quant/splits.py) needs the split ledger to put EDGAR's mixed-basis
share counts on today's basis: a filing reports shares on the basis in effect at its
FILED date, so every split with ``effective_date`` AFTER that date multiplies the
count. Without these two rows every per-share figure (EPS, reverse-DCF) is garbage
(PHASE0-TODO item 1; analyst module §data).

Ratio semantics: ``ratio`` is new-shares-per-old-share — a 5-for-1 split stores 5.0
(share counts multiply by ratio; per-share prices divide by it).

The two rows are public record, not computed here (Law 2 — the dates/ratios are the
facts Tesla announced; provenance pinned in ``raw_json``):
  * 5-for-1, effective at market open 2020-08-31 (announced 2020-08-11; SEC 8-K).
  * 3-for-1, effective at market open 2022-08-25 (approved at the 2022-08-04 annual
    meeting; SEC 8-K).

Idempotent: upsert keyed on (symbol, action_type, effective_date) with DO-NOTHING on
conflict (``ignore_duplicates=True``) — re-running inserts nothing new and never
clobbers a live row. ``source='manual_seed'`` distinguishes these from a future IBKR
Flex Corporate Actions feed (PHASE0-TODO item 1), which would write its own rows.

No external API call — static reference data, like seed_config.py.

Run:  python -m ingestion.seed_corporate_actions
"""

from __future__ import annotations

if __name__ == "__main__" and __package__ in (None, ""):
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import json

from shared.db import get_client

SPLITS: tuple[dict, ...] = (
    {
        "symbol": "TSLA",
        "action_type": "split",
        "ratio": 5.0,
        "effective_date": "2020-08-31",
        "source": "manual_seed",
        "raw_json": {
            "note": "5-for-1 stock split, effective at market open 2020-08-31",
            "announced": "2020-08-11",
            "provenance": "Tesla, Inc. press release + SEC Form 8-K (public record)",
        },
    },
    {
        "symbol": "TSLA",
        "action_type": "split",
        "ratio": 3.0,
        "effective_date": "2022-08-25",
        "source": "manual_seed",
        "raw_json": {
            "note": "3-for-1 stock split, effective at market open 2022-08-25",
            "announced": "2022-08-05",
            "provenance": "Tesla, Inc. press release + SEC Form 8-K (public record)",
        },
    },
)

UPSERT_CONFLICT = "symbol,action_type,effective_date"


def seed_corporate_actions() -> list[dict]:
    """Upsert the split rows (insert-once) and return the table's rows after.

    ``ignore_duplicates=True`` makes a re-run a no-op: an existing
    (symbol, action_type, effective_date) row is left untouched, never overwritten.
    """
    client = get_client()
    client.table("corporate_actions").upsert(
        list(SPLITS),
        on_conflict=UPSERT_CONFLICT,
        ignore_duplicates=True,
    ).execute()
    rows = (
        client.table("corporate_actions")
        .select("id,symbol,action_type,ratio,effective_date,source")
        .order("effective_date")
        .execute()
        .data
        or []
    )
    print(f"[seed_corporate_actions] {len(rows)} row(s) in corporate_actions after upsert:")
    for row in rows:
        print(f"  {json.dumps(row, default=str)}")
    return rows


if __name__ == "__main__":
    seed_corporate_actions()
