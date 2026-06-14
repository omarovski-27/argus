"""Argus ingestion — Alpha Vantage NEWS_SENTIMENT fetcher (blueprint §5 / §7 / §8).

Alpha Vantage is Argus's company-news layer and the ONLY consumer of its free-tier
budget — 25 req/day reserved 100% for news sentiment (Law 3). One call per run pulls
the latest NEWS_SENTIMENT feed for the traded tickers and stores two things:

    feed item -> headlines   (source='av', url dedup key, title, published_at, ticker_tags)
    feed item -> sentiment   (method='av_native', from AV's own overall_sentiment_*)

AV's native scores ride along free with the same call, so we persist them immediately;
a second, swappable Haiku pass scores the same headlines later (digest/sentiment.py, §8).

Endpoint:
    GET https://www.alphavantage.co/query
        ?function=NEWS_SENTIMENT&tickers=TSLA,SPCX&apikey=...&limit=50
    Response: {"feed": [{url, title, time_published, overall_sentiment_label,
                         overall_sentiment_score, ticker_sentiment: [...]}, ...]}.
    An over-budget / throttled response carries NO "feed" key (an "Information" or
    "Note" string instead); that is surfaced as `unavailable` rather than silently
    treated as empty (Law 7). SPCX simply not appearing in AV coverage yet is NOT an
    error — we store whatever the feed contains.

DEVIATIONS FROM THE TASK BRIEF (the applied schema / committed config are truth):
  • Env var is ALPHAVANTAGE_API_KEY (committed .env.example), not AV_API_KEY — used
    here so the code matches the real environment.
  • `sentiment` now carries a UNIQUE(headline_id, method) constraint, so av_native scores
    write with a real `on_conflict='headline_id,method', ignore_duplicates=True` upsert —
    idempotent at the DB, replacing the SELECT-then-insert pre-filter used while the
    constraint was absent.
  • AV's auth param is `apikey` (no underscore). shared.fetcher_base._SECRET_PARAM_RE
    now redacts `apikey` alongside api_key/token/t, so the AV key is stripped from any
    logged or raised error text and never reaches fetch_log (Law 13).

Run:  python -m ingestion.news_av   (or: python ingestion/news_av.py)
"""

from __future__ import annotations

if __name__ == "__main__" and __package__ in (None, ""):
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import os
from datetime import datetime, timezone

from shared.db import get_client
from shared.exceptions import FetchError
from shared.fetch_logger import write_fetch_log
from shared.fetcher_base import fetch_with_retry

AV_QUERY_URL = "https://www.alphavantage.co/query"
_AV_TICKERS: tuple[str, ...] = ("TSLA", "SPCX")
_AV_LIMIT = 50                 # one call/run; the entire 25/day budget (Law 3)
_RELEVANCE_THRESHOLD = 0.3     # a ticker is tagged when AV relevance_score >= 0.3


def _av_published_at(value: str | None) -> str | None:
    """Parse AV's ``YYYYMMDDTHHMMSS`` time_published to a UTC ISO-8601 string, or None.

    AV documents time_published as UTC; we tag it explicitly so it stores as a true
    ``timestamptz`` rather than being reinterpreted in the DB's zone. An unparseable
    value yields None — published_at is nullable, and we keep the headline rather than
    drop it over a bad date (Law 7).
    """
    if not value:
        return None
    try:
        parsed = datetime.strptime(value.strip(), "%Y%m%dT%H%M%S")
    except (ValueError, AttributeError):
        return None
    return parsed.replace(tzinfo=timezone.utc).isoformat()


def _av_direction(label: str | None) -> str:
    """Map AV ``overall_sentiment_label`` to a sentiment.direction CHECK value.

    Bullish/Somewhat-Bullish -> 'bullish'; Bearish/Somewhat-Bearish -> 'bearish';
    Neutral (and anything unrecognized) -> 'neutral'.
    """
    key = (label or "").strip().lower()
    if key in ("bullish", "somewhat-bullish"):
        return "bullish"
    if key in ("bearish", "somewhat-bearish"):
        return "bearish"
    return "neutral"


def _clamp01(value: object) -> float | None:
    """Coerce to float and clamp to [0.0, 1.0]; unparseable -> None.

    Per the task brief, magnitude is ``overall_sentiment_score`` clamped to 0–1. AV
    scores are signed (~-0.35..+0.35), so a bearish score clamps to 0.0 — the bull/bear
    sign is carried by ``direction``, not the magnitude. (The schema comment recommends
    magnitude=strength; the 0–1 clamp here follows the explicit Phase-1 instruction.)
    """
    try:
        return max(0.0, min(1.0, float(value)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _ticker_tags(item: dict) -> list[str]:
    """Tickers in this item's ticker_sentiment with relevance_score >= 0.3.

    AV returns relevance_score as a string; an unparseable one drops that ticker. The
    result may be empty (AV scored the article but nothing cleared the bar), which is a
    meaningful distinct value from the wire sources' NULL (no tagging at all).
    """
    tags: list[str] = []
    for entry in item.get("ticker_sentiment") or []:
        ticker = entry.get("ticker")
        try:
            relevance = float(entry.get("relevance_score"))
        except (TypeError, ValueError):
            continue
        if ticker and relevance >= _RELEVANCE_THRESHOLD:
            tags.append(ticker)
    return tags


def fetch_av_news(run_id: str) -> None:
    """Fetch the Alpha Vantage NEWS_SENTIMENT feed and store headlines + av_native scores.

    Args:
        run_id: Run identifier, logged to ``fetch_log`` to group this run's fetches.

    One HTTP call (the whole 25/day budget). Headlines upsert on the ``url`` dedup key
    (ignore duplicates); av_native sentiment rows upsert on the UNIQUE(headline_id,
    method) constraint (``ignore_duplicates=True``), so re-runs stay idempotent.
    A transport outage is surfaced (already in fetch_log via the shared fetcher, Law 7)
    and the run continues without AV headlines so the digest still generates.
    """
    api_key = os.environ.get("ALPHAVANTAGE_API_KEY")
    if not api_key:
        raise RuntimeError("Missing ALPHAVANTAGE_API_KEY (see .env.example).")

    client = get_client()
    params = {
        "function": "NEWS_SENTIMENT",
        "tickers": ",".join(_AV_TICKERS),
        "apikey": api_key,
        "limit": _AV_LIMIT,
    }

    try:
        payload = fetch_with_retry(AV_QUERY_URL, {}, params, "av", run_id)
    except FetchError as exc:
        # Already logged to fetch_log by the shared fetcher; surface and move on.
        print(f"[news_av] unavailable — {exc}")
        return

    feed = payload.get("feed") if isinstance(payload, dict) else None
    if not feed:
        # HTTP 200 but no feed: budget exhausted or throttled. AV explains via an
        # Information/Note string. Surface it as a logical outage (Law 7).
        note = ""
        if isinstance(payload, dict):
            note = (
                payload.get("Information")
                or payload.get("Note")
                or payload.get("Error Message")
                or ""
            )
        write_fetch_log("av", run_id, "unavailable", 0, note or "no 'feed' in AV response")
        print(f"[news_av] no feed returned (budget/throttle?): {note or 'empty response'}")
        return

    headline_rows = []
    for item in feed:
        url = item.get("url")
        if not url:
            continue  # url is the NOT NULL dedup key; skip an item we cannot store
        headline_rows.append(
            {
                "source": "av",
                "url": url,
                "title": item.get("title"),
                "published_at": _av_published_at(item.get("time_published")),
                "ticker_tags": _ticker_tags(item),
            }
        )
    if not headline_rows:
        print("[news_av] feed had 0 usable items (no urls).")
        return

    client.table("headlines").upsert(
        headline_rows, on_conflict="url", ignore_duplicates=True
    ).execute()

    # Need each headline's id for the sentiment FK. ignore_duplicates omits already-
    # present rows from the upsert response, so query the ids back by url.
    urls = [row["url"] for row in headline_rows]
    fetched = client.table("headlines").select("id,url").in_("url", urls).execute()
    id_by_url = {row["url"]: row["id"] for row in (fetched.data or [])}

    # Build one av_native sentiment row per headline id (dict keyed by id dedupes a
    # url that appears twice in the feed).
    sentiment_by_hid: dict[int, dict] = {}
    for item in feed:
        hid = id_by_url.get(item.get("url"))
        if hid is None:
            continue
        sentiment_by_hid[hid] = {
            "headline_id": hid,
            "method": "av_native",
            "direction": _av_direction(item.get("overall_sentiment_label")),
            "magnitude": _clamp01(item.get("overall_sentiment_score")),
        }

    new_rows = list(sentiment_by_hid.values())
    if new_rows:
        client.table("sentiment").upsert(
            new_rows, on_conflict="headline_id,method", ignore_duplicates=True
        ).execute()

    print(
        f"[news_av] upserted {len(headline_rows)} headline(s); "
        f"inserted {len(new_rows)} av_native sentiment row(s)."
    )


if __name__ == "__main__":
    import uuid

    fetch_av_news(f"manual-av-{uuid.uuid4().hex[:12]}")
