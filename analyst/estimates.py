"""Argus analyst — Stage-8 consensus & sentiment context (Mr. Market's mood).

Analyst price targets, EPS/revenue estimate ranges, recommendation trends, and
short interest — framed strictly as *what is priced in and how the crowd feels*
(analyst-module §1 Stage 8): context, never evidence of value.

yfinance is the PRIMARY source (free, keyless — the blueprint §5 universal
fallback); Finnhub free-tier recommendation trends are added when
FINNHUB_API_KEY is set. yfinance is a client library, not a URL, so its calls
cannot ride ``fetch_with_retry`` — the whole block is timed and logged to
``fetch_log`` as ``analyst:estimates`` instead, and every sub-block degrades
independently to None plus a note (Law 7: absence stays visible; Law 2: a field
the source could not supply is "unavailable", never filled).

Run:  python -m analyst.estimates TSLA
"""

from __future__ import annotations

if __name__ == "__main__" and __package__ in (None, ""):
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import os
import time
from datetime import datetime, timezone

from shared.exceptions import FetchError
from shared.fetch_logger import elapsed_ms, write_fetch_log
from shared.fetcher_base import fetch_with_retry

FINNHUB_RECOMMENDATION_URL = "https://finnhub.io/api/v1/stock/recommendation"

# The yfinance sub-blocks the pack carries, in report order.
_BLOCKS = (
    "price_targets",
    "recommendation_trend",
    "earnings_estimate",
    "revenue_estimate",
    "short_interest",
)


def _num(value) -> float | None:
    """Coerce to float; None/NaN/unparseable -> None (never a fabricated number)."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f == f else None  # NaN != NaN


def frame_records(frame, index_label: str) -> list[dict] | None:
    """A DataFrame as JSON-safe records with its index preserved as ``index_label``.

    Pure shaping (unit-tested): numeric cells coerce through :func:`_num`; other
    cells become strings. None/empty frames -> None so the pack says unavailable.
    """
    if frame is None or getattr(frame, "empty", True):
        return None
    records: list[dict] = []
    for idx, row in frame.iterrows():
        rec: dict = {index_label: str(idx)}
        for col, cell in row.items():
            coerced = _num(cell)
            rec[str(col)] = coerced if coerced is not None else (
                None if cell is None or cell != cell else str(cell)
            )
        records.append(rec)
    return records or None


def shape_price_targets(raw) -> dict | None:
    """yfinance ``analyst_price_targets`` ({current, high, low, mean, median}) shaped."""
    if not isinstance(raw, dict) or not raw:
        return None
    shaped = {k: _num(v) for k, v in raw.items()}
    return shaped if any(v is not None for v in shaped.values()) else None


def shape_short_interest(info) -> dict | None:
    """The short-interest subset of yfinance ``info``, epoch date decoded."""
    if not isinstance(info, dict):
        return None
    epoch = info.get("dateShortInterest")
    as_of = (
        datetime.fromtimestamp(epoch, tz=timezone.utc).date().isoformat()
        if isinstance(epoch, (int, float))
        else None
    )
    shaped = {
        "shares_short": _num(info.get("sharesShort")),
        "short_ratio": _num(info.get("shortRatio")),
        "short_pct_of_float": _num(info.get("shortPercentOfFloat")),
        "shares_outstanding": _num(info.get("sharesOutstanding")),
        "as_of": as_of,
    }
    return shaped if any(v is not None for v in shaped.values() if v != as_of) else None


def _finnhub_recommendations(symbol: str, run_id: str) -> list[dict] | None:
    """Finnhub free-tier recommendation trends, or None (keyless / failed / empty).

    A FetchError is already in fetch_log via the wrapped fetcher (Law 7); the
    estimates block then simply lacks the Finnhub view, visibly.
    """
    key = os.environ.get("FINNHUB_API_KEY")
    if not key:
        return None
    try:
        data = fetch_with_retry(
            FINNHUB_RECOMMENDATION_URL,
            {},
            {"symbol": symbol, "token": key},
            "analyst:estimates_finnhub",
            run_id,
        )
    except FetchError:
        return None
    return data if isinstance(data, list) and data else None


def estimates_block(symbol: str, run_id: str) -> dict:
    """The pack's Stage-8 sub-document for ``symbol`` (yfinance + optional Finnhub).

    Each sub-block is fetched independently; one failing leaves the others intact
    and adds a note. The whole block's outcome lands in fetch_log: 'success' when
    anything was retrieved, 'unavailable' (with the notes) when nothing was —
    never an exception, so a pack without Mr. Market context still builds (Law 7).
    """
    import yfinance as yf  # deferred: keeps module import cheap for pure-logic tests

    start = time.monotonic()
    sym = symbol.strip().upper()
    ticker = yf.Ticker(sym)
    notes: list[str] = []
    block: dict = {"source": "yfinance", "symbol": sym}

    def _safe(name: str, supplier):
        try:
            return supplier()
        except Exception as exc:  # noqa: BLE001 — degrade per sub-block, note it (Law 7)
            notes.append(f"{name}: {type(exc).__name__}: {str(exc)[:120]}")
            return None

    block["price_targets"] = _safe(
        "price_targets", lambda: shape_price_targets(ticker.analyst_price_targets)
    )
    block["recommendation_trend"] = _safe(
        "recommendation_trend", lambda: frame_records(ticker.recommendations, "row")
    )
    block["earnings_estimate"] = _safe(
        "earnings_estimate", lambda: frame_records(ticker.earnings_estimate, "period")
    )
    block["revenue_estimate"] = _safe(
        "revenue_estimate", lambda: frame_records(ticker.revenue_estimate, "period")
    )
    block["short_interest"] = _safe("short_interest", lambda: shape_short_interest(ticker.info))

    finnhub = _finnhub_recommendations(sym, run_id)
    if finnhub is not None:
        block["finnhub_recommendation_trend"] = finnhub

    block["notes"] = notes
    retrieved = any(block.get(k) is not None for k in _BLOCKS)
    if retrieved:
        write_fetch_log("analyst:estimates", run_id, "success", elapsed_ms(start))
    else:
        write_fetch_log(
            "analyst:estimates",
            run_id,
            "unavailable",
            elapsed_ms(start),
            "; ".join(notes) or "yfinance returned nothing for every sub-block",
        )
    return block


if __name__ == "__main__":
    import json
    import sys
    import uuid

    result = estimates_block(
        sys.argv[1] if len(sys.argv) > 1 else "TSLA", f"manual-estimates-{uuid.uuid4().hex[:12]}"
    )
    print(json.dumps(result, indent=2, default=str)[:4000])
