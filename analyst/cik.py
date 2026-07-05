"""Argus analyst — ticker→CIK resolution (SEC company_tickers.json; free, no key).

The SEC publishes the complete ticker→CIK map as one JSON document. It is fetched
ONCE per process through the wrapped fetcher (§12) and cached in-module; both peer
fundamentals ingestion and the filings layer resolve through here. An unknown
ticker returns None — the caller renders a reduced-depth dossier / "not available"
(Law 2: never a guessed CIK, which would file another issuer's numbers).

Run:  python -m analyst.cik TSLA GM F   (prints resolved 10-digit CIKs)
"""

from __future__ import annotations

if __name__ == "__main__" and __package__ in (None, ""):
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from ingestion.sec_facts import USER_AGENT
from shared.fetcher_base import fetch_with_retry

COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"

# Process-wide cache: the map is ~1 MB and changes rarely; one fetch serves every
# resolution in a run (peer ingestion + filings for target and peers).
_MAP: dict[str, dict] | None = None


def _load_map(run_id: str) -> dict[str, dict]:
    """The full {TICKER: {cik, title}} map, fetched once per process (wrapped, §12)."""
    global _MAP
    if _MAP is None:
        doc = fetch_with_retry(
            COMPANY_TICKERS_URL, {"User-Agent": USER_AGENT}, {}, "analyst:cik", run_id
        )
        # Document shape: {"0": {"cik_str": 320193, "ticker": "AAPL", "title": ...}, ...}
        _MAP = {
            str(entry["ticker"]).upper(): {
                "cik": f"{int(entry['cik_str']):010d}",
                "title": entry.get("title") or None,
            }
            for entry in doc.values()
            if entry.get("ticker") and entry.get("cik_str") is not None
        }
    return _MAP


def resolve_cik(symbol: str, run_id: str) -> str | None:
    """The 10-digit zero-padded CIK for ``symbol``, or None if the SEC map lacks it."""
    entry = _load_map(run_id).get(symbol.strip().upper())
    return entry["cik"] if entry else None


def resolve_title(symbol: str, run_id: str) -> str | None:
    """The SEC-registered entity name for ``symbol``, or None if the map lacks it."""
    entry = _load_map(run_id).get(symbol.strip().upper())
    return entry["title"] if entry else None


if __name__ == "__main__":
    import sys
    import uuid

    rid = f"manual-cik-{uuid.uuid4().hex[:12]}"
    for sym in sys.argv[1:] or ["TSLA"]:
        print(f"{sym.upper():<8} -> {resolve_cik(sym, rid)}")
