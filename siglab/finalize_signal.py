"""Argus Signal Lab — finalize signal_v1 as INCONCLUSIVE (Omar's authorized amendment).

This is NOT a Law-6 violation. Law 6 forbids MOVING a gate after seeing the data; this
records the honest verdict that the experiment as specified was UNMEASURABLE — the daily-bar
shadow scorer could not test the rule (74% of triggered days hit both bands from the open;
100% had a range wider than the whole bracket window), so its record measured TSLA's daily
range, not the signal. The rule, params, gates and ``registered_at`` are PRESERVED verbatim
(the immutable registration); only the finalization fields are added/set:

    status         -> "INCONCLUSIVE"   (authoritative over the ledger-derived gate verdict)
    status_reason  -> the measured why (siglab.registry.SIGNAL_V1_STATUS_REASON)
    promotion_path -> forward-only: the real ledger becomes live round-trips once a sleeve exists
    finalized_at   -> 2026-07-18

Idempotent: re-running writes the same amended blob. A config-row upsert (JSONB), never DDL.

Run:  python -m siglab.finalize_signal            (apply — writes config.signal_v1)
      python -m siglab.finalize_signal --dry-run  (print the amended blob, no write)
"""

from __future__ import annotations

if __name__ == "__main__" and __package__ in (None, ""):
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import json

from shared.db import get_client
from siglab.registry import (
    SIGNAL_CONFIG_KEY,
    SIGNAL_V1_FINAL_STATUS,
    SIGNAL_V1_FINALIZED_AT,
    SIGNAL_V1_PROMOTION_PATH,
    SIGNAL_V1_STATUS_REASON,
    load_signal,
)


def amended_blob(client) -> dict:
    """The stored (or default) registration blob with ONLY the finalization fields set."""
    blob = dict(load_signal(client))   # preserves rule/params/gates/registered_at verbatim
    blob["status"] = SIGNAL_V1_FINAL_STATUS
    blob["status_reason"] = SIGNAL_V1_STATUS_REASON
    blob["promotion_path"] = SIGNAL_V1_PROMOTION_PATH
    blob["finalized_at"] = SIGNAL_V1_FINALIZED_AT
    return blob


def finalize(client, *, dry_run: bool = False) -> dict:
    """Upsert the amended blob to config.signal_v1 (idempotent). Returns the blob written."""
    blob = amended_blob(client)
    if dry_run:
        print("[finalize_signal] DRY RUN — would write config.signal_v1:")
        print(json.dumps(blob, indent=2, ensure_ascii=False))
        return blob
    client.table("config").upsert(
        [{"key": SIGNAL_CONFIG_KEY, "value": blob}], on_conflict="key"
    ).execute()
    back = (
        client.table("config").select("value").eq("key", SIGNAL_CONFIG_KEY).limit(1)
        .execute().data or []
    )
    stored = back[0]["value"] if back else {}
    ok = (
        stored.get("status") == SIGNAL_V1_FINAL_STATUS
        and stored.get("rule") == blob.get("rule")           # registration preserved
        and stored.get("registered_at") == blob.get("registered_at")
    )
    print(f"[finalize_signal] wrote config.{SIGNAL_CONFIG_KEY} "
          f"({'verified' if ok else 'MISMATCH (!)'}): status={stored.get('status')!r}, "
          f"registered_at={stored.get('registered_at')!r} (preserved).")
    return stored


if __name__ == "__main__":
    import sys

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
    finalize(get_client(), dry_run="--dry-run" in sys.argv)
