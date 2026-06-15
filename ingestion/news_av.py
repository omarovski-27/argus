"""Argus ingestion — Alpha Vantage NEWS_SENTIMENT fetcher (blueprint §5 / §7 / §8).

Alpha Vantage is Argus's company-news layer and the ONLY consumer of its free-tier
budget — 25 req/day reserved 100% for news sentiment (Law 3). One call PER TICKER pulls
the latest NEWS_SENTIMENT feed (AV's comma-joined ``tickers=`` filter is AND, not OR,
so a joined call matches only stories naming every ticker — almost never), and the
accumulated feed is de-duped by url before storing two things:

    feed item -> headlines   (source='av', url dedup key, title, published_at, ticker_tags)
    feed item -> sentiment   (method='av_native', from AV's own overall_sentiment_*)

AV's native scores ride along free with the same call, so we persist them immediately;
a second, swappable Haiku pass scores the same headlines later (digest/sentiment.py, §8).

Endpoint (one request per ticker):
    GET https://www.alphavantage.co/query
        ?function=NEWS_SENTIMENT&tickers=TSLA&apikey=...&limit=50
    Response: {"feed": [{url, title, time_published, overall_sentiment_label,
                         overall_sentiment_score, ticker_sentiment: [...]}, ...]}.
    Each ticker logs to fetch_log under ``av:<ticker>`` (mirrors ``fred:<series>``).
    An over-budget / throttled response carries NO "feed" key (an "Information" or
    "Note" string instead); that ticker is surfaced as `unavailable` (Law 7). A ``feed``
    key present but EMPTY is honest no-coverage (a ticker AV does not currently cover), NOT an
    outage — it is printed but never logged unavailable.

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
# SPCX excluded: AV's symbol resolver maps it to the unrelated Tuttle "SPAC & New
# Issue ETF" (ticker SPCK), not SpaceX — wrong entity (Law 2). Revisit if AV ever
# maps the real SpaceX SPCX (IPO 2026-06-12).
_AV_TICKERS: tuple[str, ...] = ("TSLA",)
_AV_LIMIT = 50                 # per-ticker page size; 1 call/ticker (TSLA only → 1/25 daily, Law 3)
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


def _magnitude(value: object) -> float | None:
    """Coerce AV's signed ``overall_sentiment_score`` to an unsigned magnitude; else None.

    The schema convention is magnitude = strength, with the bull/bear sign carried by
    ``direction``. AV scores are signed (~-0.35..+0.35), so we store ``abs(score)`` —
    otherwise a [0,1] clamp would map every bearish article to 0.0 and make strong- and
    mild-bearish indistinguishable. This matches how the Haiku path writes the column
    (a 0–1 strength). Bounded to 1.0 in case AV ever returns a magnitude beyond ±1.
    """
    try:
        return min(1.0, abs(float(value)))  # type: ignore[arg-type]
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

    One HTTP call PER TICKER (AV's comma-joined tickers= filter is AND, not OR, so a
    joined call matches only stories naming every ticker — almost never). With the one
    tracked ticker (TSLA) that is 1 of the 25/day budget (Law 3); each logs under
    ``av:<ticker>``.
    The accumulated feed is de-duped by ``url`` before storing. Headlines upsert on the
    ``url`` dedup key (ignore duplicates); av_native sentiment rows upsert on the
    UNIQUE(headline_id, method) constraint (``ignore_duplicates=True``), so re-runs stay
    idempotent. A per-ticker outage is surfaced (already in fetch_log via the shared
    fetcher, Law 7) and skipped so one ticker cannot blind the other.
    """
    api_key = os.environ.get("ALPHAVANTAGE_API_KEY")
    if not api_key:
        raise RuntimeError("Missing ALPHAVANTAGE_API_KEY (see .env.example).")

    client = get_client()

    # One call per ticker — AV's tickers= filter ANDs multiple symbols, so a joined
    # request returns near-nothing. Accumulate every ticker's feed, then de-dup by url.
    feed: list[dict] = []
    for ticker in _AV_TICKERS:
        params = {
            "function": "NEWS_SENTIMENT",
            "tickers": ticker,
            "apikey": api_key,
            "limit": _AV_LIMIT,
        }

        try:
            payload = fetch_with_retry(AV_QUERY_URL, {}, params, f"av:{ticker}", run_id)
        except FetchError as exc:
            # Already logged by the shared fetcher; surface and move on so one ticker's
            # outage does not blind the other (Law 7).
            print(f"[news_av] {ticker}: unavailable — {exc}")
            continue

        feed_items = payload.get("feed") if isinstance(payload, dict) else None
        if not isinstance(feed_items, list):
            # HTTP 200 but no 'feed' key: budget exhausted or throttled. AV explains via
            # an Information/Note string. Surface it as a logical outage (Law 7).
            note = ""
            if isinstance(payload, dict):
                note = (
                    payload.get("Information")
                    or payload.get("Note")
                    or payload.get("Error Message")
                    or ""
                )
            write_fetch_log(
                f"av:{ticker}", run_id, "unavailable", 0,
                note or "'feed' key missing",
            )
            print(f"[news_av] {ticker}: no feed (budget/throttle?): {note or 'empty response'}")
            continue

        if not feed_items:
            # 'feed' present but empty: honest no-coverage (e.g. SPCX not in AV yet).
            # A real fact, not an outage — printed, never logged unavailable (Law 7).
            print(f"[news_av] {ticker}: no AV coverage yet (empty feed).")
            continue

        feed.extend(feed_items)
        print(f"[news_av] {ticker}: {len(feed_items)} feed item(s).")

    # De-dup the accumulated feed by url (keep one item per url). A story tagging both
    # tickers arrives in both feeds with an identical ticker_sentiment array, so this is
    # lossless — it just collapses the cross-tagged duplicate before storing.
    deduped: dict[str, dict] = {}
    for item in feed:
        url = item.get("url")
        if url and url not in deduped:
            deduped[url] = item
    feed = list(deduped.values())

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
            "magnitude": _magnitude(item.get("overall_sentiment_score")),
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
