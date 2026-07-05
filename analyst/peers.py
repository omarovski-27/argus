"""Argus analyst — peer discovery (analyst-module §1 Stage 3 / §3).

Peer selection is an ANALYST JUDGMENT, so the config override is the primary
source: ``config.peer_overrides`` is a ``{SYMBOL: [peer, ...]}`` JSONB map Omar
edits directly (single-key upsert only — never a full re-seed, L6; see
``analyst.seed_peers``). Finnhub ``/stock/peers`` (free tier) is the automatic
secondary when FINNHUB_API_KEY is set. When neither supplies a set, the pack
carries an explicitly empty peer list and fetch_log records ``unavailable``
(Law 7: absence is visible, never silent).

Run:  python -m analyst.peers TSLA   (prints the discovered set + source)
"""

from __future__ import annotations

if __name__ == "__main__" and __package__ in (None, ""):
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import os
import time

from shared.db import get_client
from shared.exceptions import FetchError
from shared.fetch_logger import elapsed_ms, write_fetch_log
from shared.fetcher_base import fetch_with_retry

FINNHUB_PEERS_URL = "https://finnhub.io/api/v1/stock/peers"

# Peer tables beyond this width stop being readable side-by-side (Stage 3's table);
# overrides list peers in priority order, so trimming keeps the most relevant.
MAX_PEERS = 8


def pick_peers(
    symbol: str, override_map: object, finnhub_peers: list[str] | None
) -> tuple[list[str], str | None]:
    """Choose and normalize the peer set (pure core — unit-tested).

    Precedence: config override (human judgment) beats the Finnhub list.
    Normalization: uppercase, strip, drop the symbol itself, dedup preserving
    order, cap at MAX_PEERS. Returns ``(peers, source)`` where source is
    'config' | 'finnhub' | None (no source available).
    """
    sym = symbol.strip().upper()
    raw: list | None = None
    source: str | None = None
    if isinstance(override_map, dict):
        # JSONB keys arrive as strings; match case-insensitively on the symbol.
        for key, value in override_map.items():
            if isinstance(key, str) and key.strip().upper() == sym and value:
                raw, source = value, "config"
                break
    if raw is None and finnhub_peers:
        raw, source = finnhub_peers, "finnhub"
    if not isinstance(raw, list) or not raw:
        return [], None

    peers: list[str] = []
    seen: set[str] = set()
    for candidate in raw:
        if not isinstance(candidate, str):
            continue
        norm = candidate.strip().upper()
        if norm and norm != sym and norm not in seen:
            seen.add(norm)
            peers.append(norm)
    return peers[:MAX_PEERS], source if peers else None


def _load_override_map(client) -> object:
    """The ``config.peer_overrides`` JSONB value, or None when the row is absent."""
    rows = (
        client.table("config").select("value").eq("key", "peer_overrides").limit(1).execute().data
        or []
    )
    return rows[0]["value"] if rows else None


def _finnhub_peers(symbol: str, run_id: str) -> list[str]:
    """Finnhub's peer list, or [] — keyless or failed lookups degrade, not abort.

    A FetchError here is already recorded by the wrapped fetcher (Law 7); peer
    discovery then continues to the no-source path, whose ``unavailable`` row and
    empty pack entry keep the degradation visible.
    """
    key = os.environ.get("FINNHUB_API_KEY")
    if not key:
        return []
    try:
        data = fetch_with_retry(
            FINNHUB_PEERS_URL,
            {},
            {"symbol": symbol.strip().upper(), "token": key},
            "analyst:peers_finnhub",
            run_id,
        )
    except FetchError:
        return []
    return [p for p in data if isinstance(p, str)] if isinstance(data, list) else []


def discover_peers(symbol: str, run_id: str, client=None) -> dict:
    """The peer set for ``symbol``: config override first, Finnhub second (Law 7 logged).

    Returns ``{"symbol", "peers", "source"}``; an empty set is a real, visible
    outcome (fetch_log 'unavailable'), never an exception — a dossier without a
    peer table says so instead of failing (analyst-module §3 reduced depth).
    """
    start = time.monotonic()
    client = client or get_client()
    sym = symbol.strip().upper()

    override_map = _load_override_map(client)
    finnhub = _finnhub_peers(sym, run_id)
    peers, source = pick_peers(sym, override_map, finnhub)

    if peers:
        write_fetch_log("analyst:peers", run_id, "success", elapsed_ms(start))
    else:
        write_fetch_log(
            "analyst:peers",
            run_id,
            "unavailable",
            elapsed_ms(start),
            "no peer source: config.peer_overrides has no entry and Finnhub is keyless/empty",
        )
    return {"symbol": sym, "peers": peers, "source": source}


if __name__ == "__main__":
    import sys
    import uuid

    result = discover_peers(
        sys.argv[1] if len(sys.argv) > 1 else "TSLA", f"manual-peers-{uuid.uuid4().hex[:12]}"
    )
    print(result)
