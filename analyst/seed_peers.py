"""Argus analyst — seed the ``config.peer_overrides`` key + the peer instruments rows.

Config side is a SINGLE-KEY upsert, in line with the seed-guard rule (L6): a full
config re-seed against a live DB is forbidden, and this module can never do one.
Re-running overwrites just this key with the map below.

Instruments side: ``fundamentals.symbol`` FKs to ``instruments(symbol)``, so each
peer needs an instruments row before its SEC facts can land. Adding peers there is
deliberate and bounded: the price fetcher (hardcoded DEFAULT_TICKERS) and the
digest bundle (its own _TRACKED constant) ignore them; the indicators loop sees
them, finds zero prices_eod rows, and suppresses with a printed notice; the Flex
known-set now stores (rather than skips) a peer trade if one ever fills — the more
correct behavior. No price history is fetched for peers (Law 3: no budget change).

The TSLA set is an analyst judgment, editable at will: US-listed vehicle
manufacturers that file US-GAAP with the SEC — GM and F (legacy scale), RIVN and
LCID (EV pure-plays). Foreign IFRS filers (Toyota, BYD) are excluded because the
11-concept registry maps us-gaap tags; they would ingest as empty (reduced depth)
rather than wrong, but an empty peer column is noise, not signal.

Run:  python -m analyst.seed_peers
"""

from __future__ import annotations

if __name__ == "__main__" and __package__ in (None, ""):
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from shared.db import get_client

PEER_OVERRIDES: dict[str, list[str]] = {
    "TSLA": ["GM", "F", "RIVN", "LCID"],
}

# Instruments rows the FK needs. first_trade_date is reference-only for peers (no
# prices are fetched for them, so indicator suppression never consults it).
PEER_INSTRUMENTS: list[dict[str, str]] = [
    {"symbol": "GM", "name": "General Motors Company", "first_trade_date": "2010-11-18"},
    {"symbol": "F", "name": "Ford Motor Company", "first_trade_date": "1956-03-07"},
    {"symbol": "RIVN", "name": "Rivian Automotive, Inc.", "first_trade_date": "2021-11-10"},
    {"symbol": "LCID", "name": "Lucid Group, Inc.", "first_trade_date": "2021-07-26"},
]


def seed_peer_overrides() -> None:
    """Upsert the peer instruments rows + the single ``peer_overrides`` config row.

    Idempotent on both sides (symbol PK / config key); read back to verify.
    """
    client = get_client()
    client.table("instruments").upsert(PEER_INSTRUMENTS, on_conflict="symbol").execute()
    print(
        f"[seed_peers] upserted {len(PEER_INSTRUMENTS)} peer instrument(s): "
        + ", ".join(r["symbol"] for r in PEER_INSTRUMENTS)
    )
    client.table("config").upsert(
        [{"key": "peer_overrides", "value": PEER_OVERRIDES}], on_conflict="key"
    ).execute()
    stored = (
        client.table("config").select("value").eq("key", "peer_overrides").limit(1).execute().data
    )
    print(f"[seed_peers] peer_overrides = {stored[0]['value'] if stored else 'MISSING (!)'}")


if __name__ == "__main__":
    seed_peer_overrides()
