"""Argus analyst — self-healing fundamentals ingestion (target + peers, any ticker).

``ensure_fundamentals(symbol)`` makes the fundamentals layer exist for one issuer:
already-present rows short-circuit (one cheap count); otherwise resolve the CIK
from the SEC ticker map, upsert the ``instruments`` row the fundamentals FK needs
(symbol + SEC-registered title; no price history is ever fetched for it — Law 3),
and run the 11-concept mapper. This is what makes ``/analyze <any ticker>`` and
peer tables work without a human pre-seeding each issuer.

An unresolvable ticker (not an SEC filer — foreign, delisted, crypto) returns
False and the caller renders reduced depth; it never guesses a CIK (Law 2). The
symbol→CIK pairing is re-verified inside ``ingest_all`` before any write.

Run:  python -m analyst.ingest GM   (ensures one issuer, prints the outcome)
"""

from __future__ import annotations

if __name__ == "__main__" and __package__ in (None, ""):
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from analyst.cik import resolve_cik, resolve_title
from ingestion.sec_facts import ingest_all
from shared.db import get_client
from shared.exceptions import FetchError


def has_fundamentals(symbol: str, client=None) -> bool:
    """True when any ``fundamentals`` row exists for ``symbol`` (one count query)."""
    client = client or get_client()
    resp = (
        client.table("fundamentals")
        .select("id", count="exact")
        .eq("symbol", symbol.strip().upper())
        .limit(1)
        .execute()
    )
    return bool(resp.count)


def ensure_fundamentals(symbol: str, run_id: str, client=None) -> bool:
    """Make fundamentals exist for ``symbol``; True on present-or-ingested.

    False means the issuer cannot be ingested (no SEC mapping, or company-facts
    unavailable — the latter already in fetch_log via the wrapped fetcher, Law 7);
    the caller renders the absence. DB errors during the upserts propagate.
    """
    client = client or get_client()
    sym = symbol.strip().upper()
    if has_fundamentals(sym, client):
        return True

    cik = resolve_cik(sym, run_id)
    if cik is None:
        print(f"[analyst.ingest] {sym}: not in the SEC ticker map; no fundamentals.")
        return False

    # The fundamentals FK requires an instruments row. Reference-only for analyst
    # issuers: nothing schedules prices for it (tiingo's list is hardcoded), and the
    # indicators loop suppresses it visibly on zero price history.
    client.table("instruments").upsert(
        [{"symbol": sym, "name": resolve_title(sym, run_id)}], on_conflict="symbol"
    ).execute()

    try:
        ingest_all(run_id, sym, cik)
    except FetchError as exc:
        print(f"[analyst.ingest] {sym}: company-facts unavailable — {exc}")
        return False
    return True


if __name__ == "__main__":
    import sys
    import uuid

    target = sys.argv[1] if len(sys.argv) > 1 else "TSLA"
    ok = ensure_fundamentals(target, f"manual-ensure-{uuid.uuid4().hex[:12]}")
    print(f"[analyst.ingest] {target.upper()}: {'ready' if ok else 'NOT ingestable'}")
