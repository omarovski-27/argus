"""Argus analyst — the frozen data pack (analyst-module §1/§3; Law 2's foundation).

``build_data_pack(symbol)`` assembles ONE JSON document holding every retrieved
fact a dossier run may cite:

- read-time metrics + the split-adjusted 11-concept series (provenance-carrying,
  from the fundamentals layer: quant/metrics, quant/splits),
- the Stage-3 peer comparison table (config/Finnhub peer set, each peer's metrics),
- section-bounded filings text (10-K Risk Factors + MD&A, DEF 14A blocks),
- Stage-8 estimates context (targets, estimate ranges, rec trends, short interest),
- the latest dated close (prices_eod for tracked symbols, yfinance history else),
- recent symbol-tagged headlines with sentiment (the news layer Argus already has),
- a per-source health map for THIS build (Law 7: the pack carries its own gaps).

The dossier synthesis (P3) reads ONLY this pack, and the pack is persisted
verbatim to ``analyses.data_pack_json`` — every dossier is reproducible forever
(Law 2). Facts a source could not supply are explicit "unavailable" markers,
never silently absent and never filled.

Run:  python -m analyst.data_pack TSLA
      (prints the evidence summary; writes _datapack_dryrun.json, gitignored)
"""

from __future__ import annotations

if __name__ == "__main__" and __package__ in (None, ""):
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import json
import math
import time
import uuid
from datetime import date, datetime, timezone

from analyst.cik import resolve_cik
from analyst.estimates import estimates_block
from analyst.filings import filings_block
from analyst.ingest import ensure_fundamentals
from analyst.peers import discover_peers
from quant.metrics import metrics_table
from quant.splits import read_concept
from shared.db import get_client
from shared.exceptions import FetchError
from shared.fetch_logger import elapsed_ms, write_fetch_log

SCHEMA_VERSION = 1

# The 11 concepts frozen as full split-adjusted series (Stage 2's raw material).
_SERIES_CONCEPTS = (
    "revenue",
    "net_income",
    "operating_income",
    "total_assets",
    "total_liabilities",
    "cost_of_revenue",
    "gross_profit",
    "operating_cash_flow",
    "capex",
    "shares_diluted",
    "shares_outstanding",
)

_NEWS_DAYS = 14
_NEWS_LIMIT = 40


def jsonable(obj):
    """Deep-clean a structure for JSON freezing (Law 2: what is stored re-loads).

    numpy scalars -> Python, NaN/inf -> None, date/datetime -> ISO strings,
    tuples/sets -> lists. Anything else unknown -> ``str(obj)`` so freezing never
    raises mid-pack (the value stays inspectable rather than aborting the run).
    """
    if obj is None or isinstance(obj, (bool, int, str)):
        return obj
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [jsonable(v) for v in obj]
    item = getattr(obj, "item", None)  # numpy scalar duck-type
    if callable(item):
        try:
            return jsonable(item())
        except (TypeError, ValueError):
            pass
    return str(obj)


def comparison_row(symbol: str, metrics: dict) -> dict:
    """One compact Stage-3 table row from a ``metrics_table`` result (pure core).

    Latest margins, revenue CAGR (3/5y), loss-year record, latest FCF, and the
    3-year diluted-share-count change (dilution is a silent tax — flagged hard,
    analyst-module §1 Stage 2). Missing pieces stay None (Law 2).
    """
    margins = metrics.get("margins") or []
    latest_margin = margins[-1] if margins else {}
    cagr = metrics.get("revenue_cagr") or {}
    consistency = metrics.get("earnings_consistency") or {}
    fcf = metrics.get("fcf_proxy") or []
    eps = metrics.get("eps_history") or []

    dilution_3y = None
    shares = [
        (row.get("inputs", {}).get("shares_diluted_adjusted") or {}).get("value")
        for row in eps
    ]
    shares = [s for s in shares if s]
    if len(shares) >= 4:  # latest vs three fiscal years earlier
        dilution_3y = shares[-1] / shares[-4] - 1.0

    return {
        "symbol": symbol,
        "period_end": latest_margin.get("period_end"),
        "gross_margin": latest_margin.get("gross_margin"),
        "operating_margin": latest_margin.get("operating_margin"),
        "net_margin": latest_margin.get("net_margin"),
        "revenue_cagr_3y": (cagr.get(3) or {}).get("value"),
        "revenue_cagr_5y": (cagr.get(5) or {}).get("value"),
        "years_covered": consistency.get("years_covered"),
        "loss_years": consistency.get("loss_years"),
        "fcf_latest": fcf[-1]["fcf"] if fcf else None,
        "fcf_basis": fcf[-1]["basis"] if fcf else None,
        "eps_latest": eps[-1]["eps"] if eps else None,
        "diluted_shares_change_3y": dilution_3y,
    }


def _latest_price(symbol: str, client, run_id: str) -> dict:
    """The latest DATED close: prices_eod for tracked symbols, yfinance history else.

    Never an undated "current" quote — a price the dossier cites needs its own
    date (Law 2). Unavailable -> an explicit marker, not an absent key (Law 7).
    """
    rows = (
        client.table("prices_eod")
        .select("date,close")
        .eq("symbol", symbol)
        .order("date", desc=True)
        .limit(1)
        .execute()
        .data
        or []
    )
    if rows:
        return {
            "close": float(rows[0]["close"]),
            "date": rows[0]["date"],
            "source": "prices_eod",
        }

    start = time.monotonic()
    try:
        import yfinance as yf

        history = yf.Ticker(symbol).history(period="5d", auto_adjust=False)
        if history is not None and not history.empty:
            last = history.iloc[-1]
            write_fetch_log("analyst:price_yf", run_id, "success", elapsed_ms(start))
            return {
                "close": float(last["Close"]),
                "date": str(history.index[-1].date()),
                "source": "yfinance",
            }
        status_error = "yfinance returned an empty history"
    except Exception as exc:  # noqa: BLE001 — degrade to an explicit marker (Law 7)
        status_error = f"{type(exc).__name__}: {str(exc)[:120]}"
    write_fetch_log("analyst:price_yf", run_id, "unavailable", elapsed_ms(start), status_error)
    return {"close": None, "date": None, "source": None, "note": status_error}


def _news(symbol: str, client) -> dict:
    """Recent symbol-tagged headlines with sentiment, from the existing news layer.

    Read-only reuse of the digest's tables (headlines.ticker_tags GIN, sentiment
    by headline_id). Absence of coverage is a real, renderable state, not an error.
    """
    since = None
    try:
        from datetime import timedelta

        since = (datetime.now(timezone.utc) - timedelta(days=_NEWS_DAYS)).isoformat()
        heads = (
            client.table("headlines")
            .select("id,title,source,published_at,url")
            .contains("ticker_tags", [symbol])
            .gte("published_at", since)
            .order("published_at", desc=True)
            .limit(_NEWS_LIMIT)
            .execute()
            .data
            or []
        )
        if not heads:
            return {"window_days": _NEWS_DAYS, "headlines": [], "note": "no tagged headlines"}
        ids = [h["id"] for h in heads]
        sentiment = (
            client.table("sentiment")
            .select("headline_id,method,direction,magnitude")
            .in_("headline_id", ids)
            .execute()
            .data
            or []
        )
        by_headline: dict = {}
        for s in sentiment:
            by_headline.setdefault(s["headline_id"], []).append(
                {"method": s["method"], "direction": s["direction"], "magnitude": s["magnitude"]}
            )
        for h in heads:
            h["sentiment"] = by_headline.get(h.pop("id"), [])
        return {"window_days": _NEWS_DAYS, "headlines": heads}
    except Exception as exc:  # noqa: BLE001 — news context is optional; say why it's gone
        return {
            "window_days": _NEWS_DAYS,
            "headlines": [],
            "note": f"news layer unavailable: {type(exc).__name__}: {str(exc)[:120]}",
        }


def build_data_pack(symbol: str, run_id: str | None = None, client=None) -> dict:
    """Assemble the frozen data pack for ``symbol`` (see module docstring).

    Raises only on a spine outage (DB unreachable); every EXTERNAL source degrades
    into an explicit in-pack marker plus its fetch_log rows (Law 7). The final
    pack is passed through :func:`jsonable`, so what returns is exactly what
    ``analyses.data_pack_json`` will store (Law 2: frozen = reloadable).
    """
    run_id = run_id or f"datapack-{uuid.uuid4().hex[:12]}"
    client = client or get_client()
    sym = symbol.strip().upper()
    build_start = time.monotonic()
    health: dict[str, str] = {}

    cik = None
    try:
        cik = resolve_cik(sym, run_id)
        health["cik"] = "success" if cik else "unresolved"
    except FetchError as exc:
        health["cik"] = f"unavailable: {exc}"

    # Self-healing fundamentals (target first, then peers): an issuer analyzed for
    # the first time is ingested on the spot — /analyze <any ticker> and the peer
    # table never depend on a human having pre-seeded it. False = not an SEC filer
    # or company-facts down; both stay visible below (Law 7), never guessed (Law 2).
    ensure_fundamentals(sym, run_id, client)

    metrics = metrics_table(sym, client)
    series = {c: read_concept(sym, c, client) for c in _SERIES_CONCEPTS}
    fundamentals_present = any(series[c] for c in _SERIES_CONCEPTS)
    health["fundamentals"] = "success" if fundamentals_present else "empty (not ingestable)"

    peer_info = discover_peers(sym, run_id, client)
    health["peers"] = peer_info["source"] or "unavailable"
    peer_rows = [comparison_row(sym, metrics)]
    peers_missing_fundamentals: list[str] = []
    for peer in peer_info["peers"]:
        ensure_fundamentals(peer, run_id, client)
        peer_metrics = metrics_table(peer, client)
        row = comparison_row(peer, peer_metrics)
        if row["period_end"] is None:
            peers_missing_fundamentals.append(peer)
        peer_rows.append(row)

    filings = filings_block(sym, cik, run_id)
    health["filings"] = (
        "success"
        if any("sections" in (v or {}) for v in filings.values())
        else next(iter(filings.values()), {}).get("note", "unavailable")
        if isinstance(filings, dict) and filings
        else "unavailable"
    )

    estimates = estimates_block(sym, run_id)
    health["estimates"] = (
        "success"
        if any(
            estimates.get(k) is not None
            for k in ("price_targets", "recommendation_trend", "short_interest")
        )
        else "unavailable"
    )

    price = _latest_price(sym, client, run_id)
    health["price"] = price["source"] or "unavailable"

    pack = jsonable(
        {
            "schema_version": SCHEMA_VERSION,
            "symbol": sym,
            "run_id": run_id,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "cik": cik,
            "price": price,
            "metrics": metrics,
            "series": series,
            "peers": {
                **peer_info,
                "table": peer_rows,
                "missing_fundamentals": peers_missing_fundamentals,
            },
            "filings": filings,
            "estimates": estimates,
            "news": _news(sym, client),
            "source_health": health,
        }
    )
    write_fetch_log("analyst:pack", run_id, "success", elapsed_ms(build_start))
    return pack


def _print_evidence(pack: dict) -> None:
    """The GATE evidence summary: keys, sizes, peer table, filings, estimates."""
    blob = json.dumps(pack)
    print(f"[data_pack] {pack['symbol']} run={pack['run_id']}")
    print(f"[data_pack] total size: {len(blob) / 1024:.1f} KB; top-level keys: {list(pack)}")
    for key in ("metrics", "series", "peers", "filings", "estimates", "news"):
        print(f"  {key:<10} {len(json.dumps(pack[key])) / 1024:>8.1f} KB")
    print(f"  price: {pack['price']}")
    print(f"  source_health: {pack['source_health']}")
    print(f"  peers({pack['peers']['source']}): {pack['peers']['peers']}")
    for row in pack["peers"]["table"]:
        gm = row["gross_margin"]
        c3 = row["revenue_cagr_3y"]
        print(
            f"    {row['symbol']:<6} pe={row['period_end']} gm="
            f"{gm if gm is None else round(gm, 4)} cagr3="
            f"{c3 if c3 is None else round(c3, 4)} loss_years={row['loss_years']}"
            f" dilution3y={row['diluted_shares_change_3y'] if row['diluted_shares_change_3y'] is None else round(row['diluted_shares_change_3y'], 4)}"
        )
    for fkey, fval in pack["filings"].items():
        if not isinstance(fval, dict) or "sections" not in fval:
            print(f"  filings.{fkey}: {fval}")
            continue
        print(f"  filings.{fkey}: {fval['form']} filed={fval['filed']} accn={fval['accn']}")
        for sname, sval in fval["sections"].items():
            if sval is None:
                print(f"    {sname}: NOT AVAILABLE")
            else:
                print(
                    f"    {sname}: {sval['chars_original']} chars (truncated={sval['truncated']})"
                )
                print(f"      {sval['text'][:200]!r}")
    est = pack["estimates"]
    print(f"  estimates: targets={est.get('price_targets')}")
    print(f"             short_interest={est.get('short_interest')}")
    print(f"  news: {len(pack['news']['headlines'])} headline(s) in {_NEWS_DAYS}d")


if __name__ == "__main__":
    import sys

    symbol_arg = sys.argv[1] if len(sys.argv) > 1 else "TSLA"
    result = build_data_pack(symbol_arg)
    _print_evidence(result)
    out = "_datapack_dryrun.json"
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=1)
    print(f"[data_pack] full pack written to {out} (gitignored dry-run scratch).")
