"""Argus ingestion — Reuters business-news RSS fetcher (blueprint §6 / §7).

Reuters is Argus's wire/macro news layer — one of the three non-overlapping sources
(CNBC excluded as derivative, §7). This pulls the business-news RSS feed and upserts
each item into ``headlines`` (source='reuters'). No sentiment is scored here; the
swappable Haiku pass (digest/sentiment.py) scores all unscored headlines later (§8).

Feed (RSS 2.0, XML):
    GET https://feeds.reuters.com/reuters/businessNews
    <rss><channel><item><title/><link/><pubDate/></item>...</channel></rss>
    pubDate is RFC 2822 (e.g. 'Sat, 13 Jun 2026 15:30:00 GMT').

All HTTP goes through :func:`shared.fetcher_base.fetch_with_retry` with
``parse="text"`` (RSS is XML, not JSON); parsing uses the stdlib
``xml.etree.ElementTree``. ``ticker_tags`` is NULL for wire items (no per-ticker
tagging). One ``fetch_log`` row is written per run by the shared fetcher; a transport
outage is surfaced and the run continues (the digest still generates, Law 7).

Run:  python -m ingestion.news_reuters   (or: python ingestion/news_reuters.py)
"""

from __future__ import annotations

if __name__ == "__main__" and __package__ in (None, ""):
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

from shared.db import get_client
from shared.exceptions import FetchError
from shared.fetch_logger import write_fetch_log
from shared.fetcher_base import fetch_with_retry

REUTERS_RSS_URL = "https://feeds.reuters.com/reuters/businessNews"


def _rfc2822_to_iso(value: str | None) -> str | None:
    """Parse an RFC 2822 ``pubDate`` to an ISO-8601 string, or None if unparseable.

    Reuters stamps a timezone (GMT / +0000), so the parsed datetime is tz-aware and
    isoformat() carries the offset. A bad date yields None — published_at is nullable
    and we keep the headline rather than drop it (Law 7).
    """
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value.strip())
    except (TypeError, ValueError):
        return None
    return parsed.isoformat() if parsed is not None else None


def fetch_reuters_news(run_id: str) -> None:
    """Fetch the Reuters business-news RSS feed and upsert it into ``headlines`` (§7).

    Args:
        run_id: Run identifier, logged to ``fetch_log`` to group this run's fetches.

    Items upsert on the ``url`` dedup key (ignore duplicates). A transport outage is
    surfaced (already in fetch_log via the shared fetcher) and the run continues; a
    post-fetch XML parse failure is logged to fetch_log and surfaced (Law 7).
    """
    try:
        xml_text = fetch_with_retry(
            REUTERS_RSS_URL, {}, {}, "reuters", run_id, parse="text"
        )
    except FetchError as exc:
        print(f"[news_reuters] unavailable — {exc}")
        return

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        write_fetch_log("reuters", run_id, "failure", 0, f"XML parse error: {exc}")
        print(f"[news_reuters] XML parse FAILED — {exc}")
        return

    rows = []
    for item in root.findall(".//item"):
        url = (item.findtext("link") or "").strip()
        if not url:
            continue  # url is the NOT NULL dedup key
        title = (item.findtext("title") or "").strip() or None
        rows.append(
            {
                "source": "reuters",
                "url": url,
                "title": title,
                "published_at": _rfc2822_to_iso(item.findtext("pubDate")),
                "ticker_tags": None,
            }
        )

    if rows:
        get_client().table("headlines").upsert(
            rows, on_conflict="url", ignore_duplicates=True
        ).execute()
    print(f"[news_reuters] upserted {len(rows)} headline(s).")


if __name__ == "__main__":
    import uuid

    fetch_reuters_news(f"manual-reuters-{uuid.uuid4().hex[:12]}")
