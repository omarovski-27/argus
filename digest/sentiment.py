"""Argus digest — headline sentiment scoring (blueprint §8).

Behind a swappable ``score_headlines()`` interface (Haiku now; FinBERT testable later,
§8). It takes the headline ids that still need scoring (from :func:`digest.dedup`),
batch-scores their titles with one Claude Haiku call, and writes the results to
``sentiment`` (method='haiku').

LLM calls go through the official ``anthropic`` SDK, not ``shared.fetcher_base`` — the
shared fetcher is a GET-only helper for REST data sources, whereas this is a POST to the
Messages API with its own SDK-level retries. The call is still wrapped and timed, and
its outcome is written to ``fetch_log`` (source='haiku_sentiment') so a scoring failure
is never silent (Law 7).

``sentiment`` has a UNIQUE(headline_id, method) constraint, so haiku scores write with a
real ``on_conflict='headline_id,method', ignore_duplicates=True`` upsert — idempotent at
the DB. This replaces the earlier skip-already-scored-then-insert workaround and keeps
the function safe to call with ids that were already scored (a duplicate score is
ignored, not re-inserted).

Run:  python -m digest.sentiment   (or: python digest/sentiment.py)
"""

from __future__ import annotations

if __name__ == "__main__" and __package__ in (None, ""):
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import json
import os
import re
import time

from anthropic import Anthropic
from dotenv import load_dotenv

from shared.db import get_client
from shared.fetch_logger import write_fetch_log

# Haiku is the cheap scorer (§8 / cost guardrail). Exact id per the build brief.
_HAIKU_MODEL = "claude-haiku-4-5-20251001"
_DIRECTIONS = ("bullish", "bearish", "neutral")
_IN_CHUNK = 200  # keep PostgREST `in_(...)` filters (URL length) bounded
_LLM_CHUNK = 50  # headlines per Haiku call — bound output so the JSON array can't truncate

# The classifier contract. Returns a JSON array so one call scores the whole batch.
_SYSTEM_PROMPT = (
    "Financial sentiment classifier. For each headline return a JSON array of "
    "{id, direction, magnitude} where direction is 'bullish', 'bearish', or "
    "'neutral' and magnitude is 0.0-1.0. Return ONLY the JSON array, no other text."
)


def _elapsed_ms(start: float) -> int:
    """Whole milliseconds since a ``time.monotonic()`` reading (for fetch_log)."""
    return int((time.monotonic() - start) * 1000)


def _chunks(seq: list, size: int):
    """Yield successive ``size``-length slices of ``seq``."""
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def _clamp01(value: object) -> float | None:
    """Coerce to float and clamp to [0.0, 1.0]; unparseable -> None."""
    try:
        return max(0.0, min(1.0, float(value)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _anthropic_key() -> str:
    """Read ANTHROPIC_API_KEY from the env (loading .env in dev); fail loud if absent."""
    load_dotenv(override=True)
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("Missing ANTHROPIC_API_KEY (see .env.example).")
    return key


def _parse_scores(text: str) -> list[dict]:
    """Parse the model's reply into a list of score dicts.

    Tolerates a ```` ```json ```` fence around the array. Raises ``ValueError`` /
    ``json.JSONDecodeError`` on malformed output so the caller logs the failure rather
    than silently scoring nothing (Law 7).
    """
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z0-9]*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```$", "", cleaned).strip()
    data = json.loads(cleaned)
    if not isinstance(data, list):
        raise ValueError(f"expected a JSON array, got {type(data).__name__}")
    return [entry for entry in data if isinstance(entry, dict)]


def score_headlines(headline_ids: list[int], run_id: str) -> None:
    """Score the given headlines with Haiku and insert ``sentiment`` rows (method='haiku').

    Args:
        headline_ids: Headline ids to score (typically from
            :func:`digest.dedup.get_unscored_headline_ids`). An empty list returns
            immediately — nothing to score.
        run_id: Run identifier, logged to ``fetch_log`` (source='haiku_sentiment').

    Swappable scorer interface (§8). One batch Haiku call scores all titles; the parsed
    results are validated (known id, valid direction) and upserted on the
    UNIQUE(headline_id, method) constraint (``ignore_duplicates=True``). The Haiku call
    is timed and its outcome logged; a failure is surfaced and re-raised, never swallowed
    (Law 7).
    """
    if not headline_ids:
        return

    client = get_client()

    # Titles to score.
    titles_by_id: dict[int, str] = {}
    for chunk in _chunks(headline_ids, _IN_CHUNK):
        resp = client.table("headlines").select("id,title").in_("id", chunk).execute()
        for row in resp.data or []:
            titles_by_id[row["id"]] = row.get("title") or ""

    to_score = [
        {"id": hid, "title": titles_by_id[hid]}
        for hid in headline_ids
        if hid in titles_by_id and titles_by_id[hid].strip()
    ]
    if not to_score:
        print("[sentiment] nothing to score (already scored / missing titles).")
        return

    anthropic_client = Anthropic(api_key=_anthropic_key())
    start = time.monotonic()
    parsed: list[dict] = []
    try:
        # Batch the LLM call too (not just the title fetch): one call per ~50 headlines
        # keeps each output under max_tokens so the JSON array can't truncate on a large
        # backlog (first run / post-outage), which would throw in _parse_scores and lose
        # the whole batch (Law 7).
        for batch in _chunks(to_score, _LLM_CHUNK):
            message = anthropic_client.messages.create(
                model=_HAIKU_MODEL,
                max_tokens=min(8192, 256 + 64 * len(batch)),
                system=_SYSTEM_PROMPT,
                messages=[
                    {"role": "user", "content": json.dumps(batch, ensure_ascii=False)}
                ],
            )
            text = next((b.text for b in message.content if b.type == "text"), "")
            parsed.extend(_parse_scores(text))
    except Exception as exc:  # noqa: BLE001 — surface, log, never swallow (Law 7)
        write_fetch_log("haiku_sentiment", run_id, "failure", _elapsed_ms(start), str(exc))
        print(f"[sentiment] Haiku scoring FAILED — {exc}")
        raise
    latency_ms = _elapsed_ms(start)

    rows: list[dict] = []
    seen: set[int] = set()
    for entry in parsed:
        try:
            hid = int(entry.get("id"))  # LLMs often return the id as a string ("42")
        except (TypeError, ValueError):
            continue  # missing/unparseable id
        direction = entry.get("direction")
        if hid not in titles_by_id or hid in seen:
            continue  # unknown/hallucinated id or a duplicate in the batch
        if direction not in _DIRECTIONS:
            continue  # would violate the direction CHECK
        seen.add(hid)
        rows.append(
            {
                "headline_id": hid,
                "method": "haiku",
                "direction": direction,
                "magnitude": _clamp01(entry.get("magnitude")),
            }
        )

    if rows:
        client.table("sentiment").upsert(
            rows, on_conflict="headline_id,method", ignore_duplicates=True
        ).execute()

    # Completeness check (Law 7): a partial reply (e.g. 30 of 50 ids) leaves the rest
    # unscored with no row — that must surface, not log success unconditionally.
    requested = len(to_score)
    scored = len(rows)
    if scored < requested:
        note = f"Haiku scored {scored}/{requested} headline(s) — {requested - scored} unscored"
        write_fetch_log("haiku_sentiment", run_id, "unavailable", latency_ms, note)
        print(f"[sentiment] PARTIAL — {note} ({latency_ms} ms).")
    else:
        write_fetch_log("haiku_sentiment", run_id, "success", latency_ms)
        print(f"[sentiment] scored {scored} headline(s) via Haiku ({latency_ms} ms).")


if __name__ == "__main__":
    import uuid

    from digest.dedup import get_unscored_headline_ids

    manual_run_id = f"manual-sentiment-{uuid.uuid4().hex[:12]}"
    score_headlines(get_unscored_headline_ids(manual_run_id), manual_run_id)
