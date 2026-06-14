"""Argus ingestion — Reddit r/stocks RSS fetcher (blueprint §6 / §7).

Reddit r/stocks is Argus's retail-sentiment layer — one of the three non-overlapping
news sources (§7). This pulls the subreddit's Atom feed and upserts each entry into
``headlines`` (source='reddit'). No sentiment is scored here; the swappable Haiku pass
(digest/sentiment.py) scores all unscored headlines later (§8).

Feed (Atom, XML):
    GET https://www.reddit.com/r/stocks/.rss
    Header: User-Agent must be set — Reddit 429s anonymous requests.
    <feed xmlns="http://www.w3.org/2005/Atom"><entry>
        <title/><link href="..."/><updated/></entry>...</feed>
    <updated> is RFC 3339 / ISO 8601 (e.g. '2026-06-13T15:30:00+00:00').

All HTTP goes through :func:`shared.fetcher_base.fetch_with_retry` with
``parse="text"`` (Atom is XML, not JSON); parsing is namespace-aware via the stdlib
``xml.etree.ElementTree``. ``ticker_tags`` is NULL for retail items (no per-ticker
tagging). One ``fetch_log`` row is written per run by the shared fetcher; a transport
outage is surfaced and the run continues (the digest still generates, Law 7).

Run:  python -m ingestion.news_reddit   (or: python ingestion/news_reddit.py)
"""

from __future__ import annotations

if __name__ == "__main__" and __package__ in (None, ""):
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import xml.etree.ElementTree as ET
from datetime import datetime

from shared.db import get_client
from shared.exceptions import FetchError
from shared.fetch_logger import write_fetch_log
from shared.fetcher_base import fetch_with_retry

REDDIT_RSS_URL = "https://www.reddit.com/r/stocks/.rss"
# Reddit 429s requests without a User-Agent; identify the bot explicitly.
_REDDIT_HEADERS = {"User-Agent": "argus-bot/1.0"}
# Atom namespace — title/link/updated all live under it.
_ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}


def _atom_link(entry: ET.Element) -> str | None:
    """Return the entry's permalink: the rel='alternate' <link href> (else the first href)."""
    links = entry.findall("atom:link", _ATOM_NS)
    for link in links:
        if link.get("rel") in (None, "alternate") and link.get("href"):
            return link.get("href")
    for link in links:
        if link.get("href"):
            return link.get("href")
    return None


def _atom_updated_to_iso(value: str | None) -> str | None:
    """Validate/normalize an Atom ``<updated>`` (RFC 3339) to ISO-8601, or None.

    The value is already ISO; we parse it to validate and re-emit canonically. A bad
    value yields None — published_at is nullable and we keep the entry (Law 7).
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).isoformat()
    except ValueError:
        return None


def fetch_reddit_news(run_id: str) -> None:
    """Fetch the Reddit r/stocks Atom feed and upsert it into ``headlines`` (§7).

    Args:
        run_id: Run identifier, logged to ``fetch_log`` to group this run's fetches.

    Entries upsert on the ``url`` dedup key (ignore duplicates). A transport outage is
    surfaced (already in fetch_log via the shared fetcher) and the run continues; a
    post-fetch XML parse failure is logged to fetch_log and surfaced (Law 7).
    """
    try:
        xml_text = fetch_with_retry(
            REDDIT_RSS_URL, _REDDIT_HEADERS, {}, "reddit", run_id, parse="text"
        )
    except FetchError as exc:
        print(f"[news_reddit] unavailable — {exc}")
        return

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        write_fetch_log("reddit", run_id, "failure", 0, f"XML parse error: {exc}")
        print(f"[news_reddit] XML parse FAILED — {exc}")
        return

    rows = []
    for entry in root.findall("atom:entry", _ATOM_NS):
        url = _atom_link(entry)
        if not url:
            continue  # url is the NOT NULL dedup key
        title = (entry.findtext("atom:title", default="", namespaces=_ATOM_NS) or "").strip()
        rows.append(
            {
                "source": "reddit",
                "url": url,
                "title": title or None,
                "published_at": _atom_updated_to_iso(
                    entry.findtext("atom:updated", namespaces=_ATOM_NS)
                ),
                "ticker_tags": None,
            }
        )

    if rows:
        get_client().table("headlines").upsert(
            rows, on_conflict="url", ignore_duplicates=True
        ).execute()
    print(f"[news_reddit] upserted {len(rows)} headline(s).")


if __name__ == "__main__":
    import uuid

    fetch_reddit_news(f"manual-reddit-{uuid.uuid4().hex[:12]}")
