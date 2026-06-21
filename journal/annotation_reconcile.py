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
  • ``trade_annotations`` has UNIQUE(round_trip_id); the upsert UPDATES on that key
    (``on_conflict="round_trip_id"``) — a re-match rewrites identical values (idempotent), and a
    row pre-written by another path gets the note's payload (reason / feeling / confidence_1to5 /
    captured_at) WRITTEN rather than silently dropped. ``ignore_duplicates`` would drop it — a
    permanent loss the moment a second writer exists. The UPDATE OVERWRITES those payload columns
    (it does not merely fill NULLs); columns NOT in the payload (a future ``checklist_passed`` /
    ``notes``) are left untouched. When the §8.2 button-capture path lands, decide explicitly
    whether /felt or the button owns ``confidence_1to5`` (today /felt is the only writer).
  • It deliberately does NOT exclude already-annotated trips. Trace a crash between the two
    writes: run 1 upserts trip T's annotation, dies before marking note N consumed; run 2 still
    loads N (unconsumed) and — because T is not excluded — re-matches it, the upsert rewrites the
    same row (idempotent), and N is finally marked consumed. Self-heals. Excluding annotated trips
    would instead strand N as a permanent false "stale note", poisoning the Law-7 unmatched-note
    audit. Write order is upsert-annotation FIRST, then mark-consumed (at-least-once).

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
from datetime import datetime, timezone

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
    # UNIQUE(round_trip_id) upsert (UPDATE on conflict) makes a re-match a harmless idempotent
    # rewrite, which is what lets a crash-orphaned note self-heal (see module docstring).
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


def stale_unmatched(pending: list[dict], matched_ids: set, today: str) -> list[dict]:
    """Unconsumed notes whose ``trade_date`` is already PAST yet matched no trip (Law-7 audit).

    ``today`` is the UTC ``YYYY-MM-DD``. A note dated TODAY that didn't match is NORMAL — you may
    simply not have traded yet — and is not flagged. A note dated before today with no trip is
    genuinely stranded (its trade either never happened or its Flex pull failed); the runner logs
    those as a ``fetch_log`` failure so /health surfaces them. ISO date strings compare lexically.
    """
    out: list[dict] = []
    for note in pending:
        if note.get("id") in matched_ids:
            continue
        day = _date_key(note.get("trade_date"))
        if day and day < today:
            out.append(note)
    return out


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
    UNIQUE round_trip_id (UPDATE on conflict), then marks the matched notes consumed — a re-run
    (or a crash-orphaned note) re-derives the same idempotent write and re-marks consumed.

    Efficiency: loads UNCONSUMED notes first and reads only the round_trips whose date carries one
    (no full-table scan); skips the trips read entirely on the common no-pending day. Marks notes
    consumed in one batched UPDATE per trip, not one network round-trip per note.

    Unmatched-note audit: a note whose trade_date is already PAST yet matched no trip is reported
    to the JOB LOG only — NOT as a fetch_log failure, because the run succeeded. Such a note is
    ambiguous: it may be a real anomaly (a Flex outage hid the day's fills) OR a normal no-trade
    (a /felt fired on intent, then no trade — it stays unattached by design). stale_unmatched can't
    yet tell those apart, so it must not flash /health red on what is usually nothing wrong. The
    note is never lost — it attaches automatically once its trip appears (match is by trade_date,
    not run day). Surfacing only the real-anomaly subset (in the digest Source Health) is a deferred
    refinement — it first needs a way to know a trade was actually expected.

    Returns the number of annotation rows attached this run.
    """
    start = time.monotonic()
    try:
        client = get_client()
        today_str = datetime.now(timezone.utc).date().isoformat()

        # Unconsumed notes bound all the work; load them first so the trips read can be scoped.
        pending = (
            client.table("pending_annotations")
            .select("id,created_at,trade_date,symbol,reason,feeling,confidence_1to5")
            .is_("consumed_round_trip_id", "null")
            .execute()
            .data
        ) or []

        # Only trips on a date that carries an unconsumed note can ever match — scope the read to
        # those dates instead of pulling the whole (ever-growing) round_trips table, and skip it
        # entirely when nothing is pending (the common daily case).
        dates = sorted({d for d in (_date_key(n.get("trade_date")) for n in pending) if d})
        if dates:
            round_trips = (
                client.table("round_trips")
                .select("id,date,symbol")
                .in_("date", dates)
                .execute()
                .data
            ) or []
        else:
            round_trips = []

        new_rows, consumed = match_annotations(round_trips, pending)
        if new_rows:
            # Annotation FIRST (idempotent via UNIQUE round_trip_id), then mark consumed —
            # at-least-once: a crash between the two re-derives the same write next run. UPDATE on
            # conflict (NOT ignore_duplicates) so a row pre-written by another path gets the note's
            # payload (reason/feeling/confidence_1to5/captured_at) WRITTEN — overwriting any prior
            # value — rather than silently dropped; a self-heal re-match rewrites identical values.
            client.table("trade_annotations").upsert(
                new_rows, on_conflict="round_trip_id"
            ).execute()

        # Mark consumed in one batched UPDATE per trip (each note in a group shares its trip id),
        # not one network round-trip per note.
        notes_by_trip: dict = {}
        for note_id, trip_id in consumed:
            notes_by_trip.setdefault(trip_id, []).append(note_id)
        for trip_id, note_ids in notes_by_trip.items():
            client.table("pending_annotations").update(
                {"consumed_round_trip_id": trip_id}
            ).in_("id", note_ids).execute()

        # The run SUCCEEDED — always log success. Unmatched past-date notes are reported to the job
        # log only (NOT a fetch_log failure): they're ambiguous (real Flex-hid-fills anomaly vs a
        # normal no-trade /felt) and must not flash /health red on what is usually nothing wrong.
        write_fetch_log("journal:annotation_reconcile", run_id, "success", _elapsed_ms(start))
        matched_ids = {note_id for note_id, _ in consumed}
        stale = stale_unmatched(pending, matched_ids, today_str)
        if stale:
            detail = "; ".join(
                f"#{n['id']} {n.get('symbol')} {_date_key(n.get('trade_date'))}" for n in stale[:10]
            )
            print(
                f"[annotation_reconcile] attached {len(new_rows)} annotation(s); "
                f"{len(stale)} unattached note(s) past their trade_date with no trip "
                f"(anomaly OR normal no-trade — not a failure): {detail}"
            )
        else:
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
