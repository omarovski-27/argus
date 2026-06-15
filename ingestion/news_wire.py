"""Argus ingestion — MarketWatch top-stories RSS fetcher (blueprint §6 / §7).

MarketWatch is Argus's wire/macro news layer — one of the three non-overlapping
sources (§7). It replaces Reuters RSS, which was discontinued (feeds.reuters.com is
NXDOMAIN). This pulls the top-stories RSS feed and upserts each item into
``headlines`` (source='marketwatch'). No sentiment is scored here; the swappable
Haiku pass (digest/sentiment.py) scores all unscored headlines later (§8).

Feed (RSS 2.0, XML):
    GET https://feeds.content.dowjones.io/public/rss/mw_topstories
    <rss><channel><item><title/><link/><pubDate/>...</item>...</channel></rss>
    pubDate is RFC 2822 (e.g. 'Mon, 15 Jun 2026 03:02:00 GMT'). Items also carry
    guid/description/dc:creator/media:content, which Argus does not store.

    URL note: the documented entry points (feeds.marketwatch.com/marketwatch/
    topstories and www.marketwatch.com/rss/topstories) BOTH 301-redirect here. The
    shared fetcher does not follow redirects (httpx default), and raise_for_status
    treats an unfollowed 301 as an error — so we target the resolved endpoint
    directly. Do NOT swap this back to a marketwatch.com URL without also enabling
    redirect-following in shared.fetcher_base, or this source goes dark.

All HTTP goes through :func:`shared.fetcher_base.fetch_with_retry` with
``parse="text"`` (RSS is XML, not JSON); parsing uses the stdlib
``xml.etree.ElementTree``. A browser-like User-Agent is sent because the Dow Jones
CDN can block default agents on some edges. ``ticker_tags`` is NULL for wire items
(no per-ticker tagging — only the ticker-filtered AV feed tags). One ``fetch_log``
row is written per run by the shared fetcher; a transport outage is surfaced and the
run continues (the digest still generates, Law 7).

Run:  python -m ingestion.news_wire   (or: python ingestion/news_wire.py)
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

# Resolved feed endpoint — the marketwatch.com URLs 301 here (see module docstring).
MARKETWATCH_RSS_URL = "https://feeds.content.dowjones.io/public/rss/mw_topstories"
# Dow Jones' CDN can block non-browser agents on some edges; identify as a browser.
_WIRE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}


def _rfc2822_to_iso(value: str | None) -> str | None:
    """Parse an RFC 2822 ``pubDate`` to an ISO-8601 string, or None if unparseable.

    MarketWatch stamps a timezone (GMT / +0000), so the parsed datetime is tz-aware
    and isoformat() carries the offset. A bad date yields None — published_at is
    nullable and we keep the headline rather than drop it (Law 7).
    """
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value.strip())
    except (TypeError, ValueError):
        return None
    return parsed.isoformat() if parsed is not None else None


def fetch_wire_news(run_id: str) -> None:
    """Fetch the MarketWatch top-stories RSS feed and upsert it into ``headlines`` (§7).

    Args:
        run_id: Run identifier, logged to ``fetch_log`` to group this run's fetches.

    Items upsert on the ``url`` dedup key (ignore duplicates). A transport outage is
    surfaced (already in fetch_log via the shared fetcher) and the run continues; a
    post-fetch XML parse failure is logged to fetch_log and surfaced (Law 7).
    """
    try:
        xml_text = fetch_with_retry(
            MARKETWATCH_RSS_URL, _WIRE_HEADERS, {}, "marketwatch", run_id, parse="text"
        )
    except FetchError as exc:
        print(f"[news_wire] unavailable — {exc}")
        return

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        write_fetch_log("marketwatch", run_id, "failure", 0, f"XML parse error: {exc}")
        print(f"[news_wire] XML parse FAILED — {exc}")
        return

    rows = []
    for item in root.findall(".//item"):
        url = (item.findtext("link") or "").strip()
        if not url:
            continue  # url is the NOT NULL dedup key
        title = (item.findtext("title") or "").strip() or None
        rows.append(
            {
                "source": "marketwatch",
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
    print(f"[news_wire] upserted {len(rows)} headline(s).")


if __name__ == "__main__":
    import uuid

    fetch_wire_news(f"manual-wire-{uuid.uuid4().hex[:12]}")
