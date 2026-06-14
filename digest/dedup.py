"""Argus digest — find headlines that still need Haiku scoring (blueprint §7 / §8).

The pipeline scores only *new* headlines each run: it passes the ids returned here to
:func:`digest.sentiment.score_headlines`. A headline needs Haiku scoring when no
``sentiment`` row exists for it with ``method='haiku'`` (an av_native row from the AV
fetcher does NOT count — Haiku is a separate, swappable scorer, §8).

There is no anti-join in PostgREST, so this reads the two id columns and diffs them in
Python (Law 8: the simplest thing that works). Both reads paginate past PostgREST's
1000-row default so a backlog beyond 1000 headlines is never silently truncated — an
untruncated read is what keeps scoring (and the digest's Source Health) honest (Law 7).

Run:  python -m digest.dedup   (or: python digest/dedup.py)
"""

from __future__ import annotations

if __name__ == "__main__" and __package__ in (None, ""):
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from shared.db import get_client

# PostgREST returns at most this many rows per request; we page in these strides.
_PAGE = 1000


def _read_all(table: str, column: str, *, method: str | None = None) -> list[dict]:
    """Read every value of ``column`` from ``table`` (optionally filtered to a method).

    Pages with ``.range()`` until a short page signals the end, defeating the 1000-row
    cap so the diff in :func:`get_unscored_headline_ids` sees the full table.
    """
    client = get_client()
    rows: list[dict] = []
    start = 0
    while True:
        # .order("id") on the unique PK gives a stable total order across .range() pages,
        # so no boundary row is skipped or repeated once a table exceeds 1000 rows (Law 7).
        query = client.table(table).select(column).order("id")
        if method is not None:
            query = query.eq("method", method)
        batch = query.range(start, start + _PAGE - 1).execute().data or []
        rows.extend(batch)
        if len(batch) < _PAGE:
            break
        start += _PAGE
    return rows


def get_unscored_headline_ids(run_id: str) -> list[int]:
    """Return the ids of headlines with no ``method='haiku'`` sentiment row.

    Args:
        run_id: The pipeline run requesting the work (used only for a traceable log line).

    Returns:
        Sorted list of headline ids still needing Haiku scoring (empty if none).
    """
    all_ids = {row["id"] for row in _read_all("headlines", "id")}
    scored_ids = {
        row["headline_id"] for row in _read_all("sentiment", "headline_id", method="haiku")
    }
    unscored = sorted(all_ids - scored_ids)
    print(f"[dedup] {len(unscored)} headline(s) need Haiku scoring (run {run_id}).")
    return unscored


if __name__ == "__main__":
    import uuid

    ids = get_unscored_headline_ids(f"manual-dedup-{uuid.uuid4().hex[:12]}")
    print(f"[dedup] unscored ids: {ids[:50]}{' ...' if len(ids) > 50 else ''}")
