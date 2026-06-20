"""Argus journal — annotation reconciler (Phase 2 Step 4; blueprint §8 / §9).

Attaches in-the-moment ``/felt`` notes to the round trips they describe. ``/felt`` stages a
note into ``pending_annotations`` at trade time, but the ``round_trip`` does not exist until
``journal.pairing`` derives it the next morning. So this runs right AFTER pairing in the daily
job: it pairs each unconsumed pending note to a round trip on the SAME symbol and SAME UTC date,
writes a ``trade_annotations`` row (reason / feeling / confidence, plus ``captured_at`` = the
note's in-the-moment time), and marks the note consumed.

Match basis (the crux): ``pending.trade_date`` == ``round_trips.date`` — both plain UTC date
columns (trade_date is stamped by the handler at /felt time, not derived from a timestamp here).
The sleeve session never crosses UTC midnight (a fill's UTC date is its trading date), so a
``/felt`` typed during the trade shares that date — exact match, no window. A note with no trade
that day finds no trip bucket, is never consumed, and can never mis-attach to a later trip.

Idempotency (Law 6) — two guards + self-heal:
  • The runner loads only UNCONSUMED pending notes, so a consumed note never re-matches.
  • ``trade_annotations`` has UNIQUE(round_trip_id); the upsert is ``ignore_duplicates`` — never
    a duplicate or an overwrite (mirrors pairing's ``sell_ext_id``).
  • It deliberately does NOT exclude already-annotated trips. Trace a crash between the two
    writes: run 1 upserts trip T's annotation, dies before marking note N consumed; run 2 still
    loads N (unconsumed) and — because T is not excluded — re-matches it, the upsert no-ops, and
    N is finally marked consumed. Self-heals. Excluding annotated trips would instead strand N as
    a permanent false "stale note", poisoning the Law-7 unmatched-note audit. Write order is
    upsert-annotation FIRST, then mark-consumed (at-least-once).

Reliability (Law 7): wrapped; success and failure both write one ``fetch_log`` row under source
``journal:annotation_reconcile``, and a failure is re-raised so the scheduled job fails loud.

Run:  python -m journal.annotation_reconcile   (or: python journal/annotation_reconcile.py)
"""

from __future__ import annotations

if __name__ == "__main__" and __package__ in (None, ""):
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import time
import uuid

from shared.db import get_client
from shared.fetch_logger import write_fetch_log


# --------------------------------------------------------------------------- #
# Pure matching logic (no DB / no network — unit-tested in tests/test_annotation_reconcile.py)
# --------------------------------------------------------------------------- #
def _date_key(value) -> str | None:
    """The ``YYYY-MM-DD`` date of a date string; None if absent.

    Both sides of the match are now plain date columns — ``round_trips.date`` and
    ``pending_annotations.trade_date`` (the UTC calendar day the handler stamped). The slice is a
    defensive no-op on an already-``YYYY-MM-DD`` value; matching no longer derives a date from the
    ``created_at`` timestamp, so there is no timezone-offset ambiguity (boring beats clever, Law 8).
    """
    if not value:
        return None
    return str(value)[:10]


def match_annotations(
    round_trips: list[dict], pending: list[dict]
) -> tuple[list[dict], list[tuple]]:
    """Pair unconsumed pending notes to round trips → (annotation rows, (note_id, trip_id) pairs).

    Args:
        round_trips: ``round_trips`` rows (need ``id``, ``date``, ``symbol``).
        pending: UNCONSUMED ``pending_annotations`` rows (``id``, ``created_at``, ``trade_date``,
            ``symbol``, ``reason``, ``feeling``, ``confidence_1to5``). The runner pre-filters to
            unconsumed.

    Returns:
        ``(new_annotation_rows, consumed_pairs)``. Each annotation row carries the note's
        ``created_at`` as ``captured_at`` (the honest in-the-moment time). Matching is by
        (symbol, trade_date) — both plain UTC date columns; within a bucket, the i-th trip
        (date,id order) pairs with the i-th note (created_at order) — same positional zip pairing
        uses for sell↔rebuy legs.
    """
    # Unconsumed notes bucketed by (symbol, trade_date), FIFO by created_at.
    note_buckets: dict[tuple[str, str], list[dict]] = {}
    for note in sorted(pending, key=lambda n: str(n.get("created_at") or "")):
        key = (note.get("symbol"), _date_key(note.get("trade_date")))
        if key[0] is None or key[1] is None:
            continue
        note_buckets.setdefault(key, []).append(note)

    # Trips bucketed by (symbol, date), FIFO by (date, id). NOT excluding annotated trips — the
    # UNIQUE(round_trip_id) + ignore_duplicates upsert makes a re-match a harmless no-op, which
    # is what lets a crash-orphaned note self-heal (see module docstring).
    trip_buckets: dict[tuple[str, str], list[dict]] = {}
    for trip in sorted(round_trips, key=lambda t: (str(t.get("date") or ""), t.get("id") or 0)):
        key = (trip.get("symbol"), _date_key(trip.get("date")))
        if key[0] is None or key[1] is None:
            continue
        trip_buckets.setdefault(key, []).append(trip)

    new_rows: list[dict] = []
    consumed: list[tuple] = []
    for key, notes in note_buckets.items():
        trips = trip_buckets.get(key, [])
        for trip, note in zip(trips, notes):
            new_rows.append(
                {
                    "round_trip_id": trip["id"],
                    "reason": note.get("reason"),
                    "feeling": note.get("feeling"),
                    "confidence_1to5": note.get("confidence_1to5"),
                    "captured_at": note.get("created_at"),
                }
            )
            consumed.append((note["id"], trip["id"]))
    return new_rows, consumed


# --------------------------------------------------------------------------- #
# DB runner (wrapped + logged; mirrors journal.pairing conventions)
# --------------------------------------------------------------------------- #
def _elapsed_ms(start: float) -> int:
    """Whole milliseconds since a ``time.monotonic()`` reading (for fetch_log)."""
    return int((time.monotonic() - start) * 1000)


def run_reconcile(run_id: str) -> int:
    """Attach unconsumed pending notes to their round trips and mark them consumed.

    Wrapped + logged (Law 7): writes one ``fetch_log`` row under ``journal:annotation_reconcile``
    and, on failure, re-raises so the scheduled job fails loud. Idempotent (Law 6): upserts on the
    UNIQUE round_trip_id with ignore_duplicates, then marks the matched notes consumed — a re-run
    (or a crash-orphaned note) re-derives the same no-op and re-marks consumed.

    Returns the number of annotation rows attached this run.
    """
    start = time.monotonic()
    try:
        client = get_client()
        round_trips = (
            client.table("round_trips").select("id,date,symbol").execute().data
        ) or []
        pending = (
            client.table("pending_annotations")
            .select("id,created_at,trade_date,symbol,reason,feeling,confidence_1to5")
            .is_("consumed_round_trip_id", "null")
            .execute()
            .data
        ) or []

        new_rows, consumed = match_annotations(round_trips, pending)
        if new_rows:
            # Annotation FIRST (idempotent via UNIQUE round_trip_id), then mark consumed —
            # at-least-once: a crash between the two re-derives the same no-op next run.
            client.table("trade_annotations").upsert(
                new_rows, on_conflict="round_trip_id", ignore_duplicates=True
            ).execute()
        for note_id, trip_id in consumed:
            client.table("pending_annotations").update(
                {"consumed_round_trip_id": trip_id}
            ).eq("id", note_id).execute()

        write_fetch_log("journal:annotation_reconcile", run_id, "success", _elapsed_ms(start))
        print(
            f"[annotation_reconcile] attached {len(new_rows)} annotation(s) "
            f"from {len(pending)} unconsumed note(s)."
        )
        return len(new_rows)
    except Exception as exc:  # noqa: BLE001 — surface, never swallow (Law 7)
        write_fetch_log(
            "journal:annotation_reconcile", run_id, "failure", _elapsed_ms(start), str(exc)
        )
        print(f"[annotation_reconcile] FAILED — {exc}")
        raise


if __name__ == "__main__":
    import sys

    # captured_at / symbols may carry non-ASCII downstream; force UTF-8 stdout so a Windows
    # cp1252 console prints cleanly (the DB consumers always see proper UTF-8).
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 — best-effort; ASCII-only terminals still run fine
        pass
    run_reconcile(f"manual-annotation-reconcile-{uuid.uuid4().hex[:12]}")
