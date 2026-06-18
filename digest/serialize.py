"""Argus digest — serialize the frozen bundle into the labeled text block for synthesis.

Sonnet is fed THIS block, never raw JSON (§7 / Law 1 / Law 2). Two reasons:
  * Grounding is enforceable — the model can only cite labels that appear in the block,
    and we drop noise (the granular ``source_health.fetches`` audit, off-watchlist news).
  * The prompt is written against stable LABELS (PRICES / INDICATORS / MACRO / HEADLINES /
    CALENDAR / BOOK / SOURCE_HEALTH), so field-name churn in the bundle never breaks it —
    this module owns the bundle-field -> label mapping.

Deterministic: ``serialize_bundle`` is a pure function of the frozen ``bundle_json``, so a
stored digest's prompt input reproduces exactly (Law 2 / §6 reproducibility).

Headlines are tiered, not dumped: WATCHLIST news (title names a watchlist ticker), then
broad-market MarketWatch, then low-reliability Reddit retail chatter. Relevance is judged
on the TITLE because AV ``ticker_tags`` can't discriminate — every AV article was fetched
via the ``tickers=TSLA`` query, so TSLA clears AV's 0.3 relevance gate (ingestion/news_av.py)
on nearly all of them. Proper long-term fix: persist AV's RAW relevance_score and gate on it.

Run:  python -m digest.serialize   (prints the block for the frozen dry-run bundle)
"""

from __future__ import annotations

if __name__ == "__main__" and __package__ in (None, ""):
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from datetime import date

_NEWS_TOP_N = 18    # watchlist + broad-market news fed to the digest
_RETAIL_TOP_N = 8   # Reddit retail-chatter items (sentiment signal only)

_PRICE_ORDER = ("TSLA", "SPY", "QQQ", "SPCX")  # majors first, young ticker last
_NEWS_SOURCES = ("av", "marketwatch")
_SOURCE_DISPLAY = {"av": "Alpha Vantage", "marketwatch": "MarketWatch", "reddit": "Reddit"}
_TITLE_ALIASES = ("TSLA", "TESLA", "SPCX", "SPACEX", "SPY", "QQQ")
_MACRO_LABELS = {
    "DFF": "Fed Funds Rate (%)",
    "CPIAUCSL": "CPI (index)",
    "UNRATE": "Unemployment (%)",
    "DGS10": "10Y Treasury Yield (%)",
    "T10Y2Y": "10Y-2Y Spread (pct pts)",
    "VIXCLS": "VIX (index)",
}
_CAL_LABELS = {
    "fomc": "FOMC", "cpi": "CPI release", "nfp": "Jobs report (NFP)",
    "earnings": "earnings", "lockup": "lockup expiry", "research": "analyst/research date",
}


def _fmt(value, nd: int = 2) -> str:
    """Round a number to nd decimals for display; pass non-numerics through as str."""
    try:
        return f"{float(value):.{nd}f}"
    except (TypeError, ValueError):
        return str(value)


def _pct(value) -> str:
    """Render a 0-1 fraction as a percent (0.2 -> '20%'); pass non-numerics through."""
    try:
        return f"{float(value) * 100:.0f}%"
    except (TypeError, ValueError):
        return str(value)


def _prices_block(prices: dict) -> str:
    lines = []
    for sym in _PRICE_ORDER:
        rows = prices.get(sym) or []
        if not rows:
            lines.append(f"  {sym:<5} not available")
            continue
        last = rows[-1]
        extra = ""
        if len(rows) >= 2 and rows[-2].get("close") and last.get("close"):
            prev = rows[-2]["close"]
            delta = last["close"] - prev
            pct = (delta / prev * 100) if prev else 0.0
            extra = f"  (prev {_fmt(prev)}, Δ {delta:+.2f} / {pct:+.1f}%)"
        note = "  [limited history — recently listed]" if sym == "SPCX" else ""
        lines.append(f"  {sym:<5} {_fmt(last.get('close'))} on {last.get('date')}{extra}{note}")
    return "PRICES (last close per ticker; Tiingo EOD)\n" + "\n".join(lines)


def _indicators_block(indicators: dict) -> str:
    lines = []
    for sym in _PRICE_ORDER:
        ind = indicators.get(sym) or {}
        vals = ind.get("values") or {}
        if not vals:
            lines.append(f"  {sym:<5} not available — indicators suppressed (insufficient history)")
            continue
        lines.append(
            f"  {sym:<5} (as_of {ind.get('as_of')}): "
            f"SMA50 {_fmt(vals.get('sma50'))} | SMA200 {_fmt(vals.get('sma200'))} | "
            f"RSI14 {_fmt(vals.get('rsi14'), 1)} | "
            f"MACD {_fmt(vals.get('macd'))} (signal {_fmt(vals.get('macd_signal'))}, "
            f"hist {_fmt(vals.get('macd_hist'))})"
        )
    return "INDICATORS (latest local pandas_ta values)\n" + "\n".join(lines)


def _macro_block(macro: dict) -> str:
    lines = []
    for series_id, label in _MACRO_LABELS.items():
        row = macro.get(series_id)
        if row:
            lines.append(f"  {label}: {row.get('value')}  ({row.get('date')})")
        else:
            lines.append(f"  {label}: not available")
    return "MACRO (latest observation per FRED series)\n" + "\n".join(lines)


def _haiku_sentiment(h: dict):
    """Return (direction, magnitude) from the haiku row (canonical scorer), else any row."""
    rows = h.get("sentiment") or []
    for s in rows:
        if s.get("method") == "haiku":
            return s.get("direction"), s.get("magnitude")
    return (rows[0].get("direction"), rows[0].get("magnitude")) if rows else (None, None)


def _salience(h: dict) -> float:
    """Haiku magnitude (0-1) — the salience signal used for ranking; missing -> 0.0."""
    return _haiku_sentiment(h)[1] or 0.0


def _fmt_headline(h: dict) -> str:
    direction, magnitude = _haiku_sentiment(h)
    src = _SOURCE_DISPLAY.get(h.get("source"), h.get("source"))
    return f"  [{direction or '?'} {(magnitude or 0.0):.2f}] {src} — {h.get('title')}"


def _title_matches_watchlist(h: dict) -> bool:
    """True if the headline title names a watchlist ticker/alias (the relevance signal)."""
    title = (h.get("title") or "").upper()
    return any(alias in title for alias in _TITLE_ALIASES)


def _headlines_block(headlines: list, news_top: int, retail_top: int) -> str:
    """Three tiers: watchlist-titled NEWS, broad-market MarketWatch, low-reliability RETAIL.

    Relevance is judged on the TITLE (see module docstring): an AV item whose title names
    no watchlist ticker is a tangential mention (TSLA cleared AV's 0.3 gate but the piece
    is really about KGC/LYV/DG/etc.) and is dropped. MarketWatch is an untagged macro wire,
    so its non-watchlist items are kept as broad-market context (capped after the lead tier).
    """
    by_sal = lambda h: (_salience(h), h.get("published_at") or "")

    lead, broad = [], []
    for h in headlines:
        src = h.get("source")
        if src not in _NEWS_SOURCES:
            continue
        if _title_matches_watchlist(h):
            lead.append(h)
        elif src == "marketwatch":
            broad.append(h)
        # else: an AV item not about the watchlist -> dropped as a tangential mention
    news_total = sum(1 for h in headlines if h.get("source") in _NEWS_SOURCES)
    dropped = news_total - len(lead) - len(broad)

    lead.sort(key=by_sal, reverse=True)
    broad.sort(key=by_sal, reverse=True)
    lead_kept = lead[:news_top]
    broad_kept = broad[: max(0, news_top - len(lead_kept))]

    retail = [h for h in headlines if h.get("source") == "reddit"]
    retail.sort(
        key=lambda h: (_title_matches_watchlist(h), _salience(h), h.get("published_at") or ""),
        reverse=True,
    )
    retail_kept = retail[:retail_top]

    lines = [
        f"HEADLINES ({len(headlines)} total; {dropped} off-watchlist news item(s) dropped — "
        f"filtered on title, since AV ticker_tags carry TSLA on nearly every item). "
        f"[direction magnitude 0-1] source — title",
        "",
        "WATCHLIST NEWS (title names Tesla / SpaceX-SPCX / SPY / QQQ)",
    ]
    lines += [_fmt_headline(h) for h in lead_kept] or ["  none available"]
    lines += ["", "BROAD MARKET / MACRO (MarketWatch wire)"]
    lines += [_fmt_headline(h) for h in broad_kept] or ["  none available"]
    lines += ["", "RETAIL CHATTER — Reddit (low reliability; sentiment signal only, not verified fact)"]
    lines += [_fmt_headline(h) for h in retail_kept] or ["  none available"]
    return "\n".join(lines)


def _calendar_block(calendar: list) -> str:
    if not calendar:
        return "CALENDAR (next 14 days)\n  none on record"
    lines = []
    for e in calendar:
        label = _CAL_LABELS.get(e.get("type"), e.get("type") or "event")
        sym = f" {e['symbol']}" if e.get("symbol") else ""
        lines.append(f"  {e.get('date')} — {label}{sym} (materiality: {e.get('materiality')})")
    return "CALENDAR (next 14 days)\n" + "\n".join(lines)


def _book_block(bundle: dict) -> str:
    cfg = bundle.get("config") or {}
    pos = bundle.get("positions") or {}
    rt = bundle.get("round_trips") or {}
    sh = bundle.get("source_health") or {}
    flex_stale = ((sh.get("staleness") or {}).get("flex") or {}).get("stale")

    # round trips used THIS calendar week vs the weekly cap
    used = 0
    try:
        gen_wk = date.fromisoformat(bundle["generated_for"]).isocalendar()[:2]
        for r in rt.get("recent_30d") or []:
            d = r.get("date")
            if d and date.fromisoformat(d[:10]).isocalendar()[:2] == gen_wk:
                used += 1
    except (ValueError, KeyError):
        used = len(rt.get("recent_30d") or [])

    rows = pos.get("rows") or []
    if rows:
        pos_line = f"Positions (snapshot {pos.get('date')}): " + "; ".join(
            f"{r.get('symbol')} qty {r.get('qty')}" for r in rows
        )
    else:
        blind = " Flex feed UNAVAILABLE (journal blind) — no portfolio data retrieved." if flex_stale else ""
        pos_line = f"Positions: none on record (no positions_snapshot stored).{blind}"

    gates = cfg.get("kill_criteria") or {}
    gate_line = ", ".join(
        f"{name} @ trade {g.get('trade')}"
        for name, g in (
            ("early-warning", gates.get("early_warning") or {}),
            ("checkpoint", gates.get("checkpoint") or {}),
            ("verdict", gates.get("verdict") or {}),
        )
        if g.get("trade")
    )
    cap = cfg.get("weekly_trade_cap")
    lines = [
        f"  {pos_line}",
        f"  Round trips this week: {used} / {cap} (weekly cap).",
        f"  Cumulative sleeve Δshares: {rt.get('cumulative_delta_shares')}.",
        f"  Sleeve: {cfg.get('sleeve_shares')} shares (sleeve {_pct(cfg.get('sleeve_pct'))}); phase {cfg.get('phase')}.",
        f"  Pre-registered gates: {gate_line}.",
    ]
    return "BOOK (core untouchable; sleeve-only metrics)\n" + "\n".join(lines)


def _source_health_block(sh: dict) -> str:
    st = sh.get("staleness") or {}
    p = st.get("prices") or {}
    fx = st.get("flex") or {}
    tok = sh.get("flex_token") or {}
    p_line = (
        f"STALE — latest {p.get('latest_date')} ({p.get('trading_days_old')} trading days old; "
        f"threshold {p.get('threshold_trading_days')})"
        if p.get("stale")
        else f"fresh — latest {p.get('latest_date')} ({p.get('trading_days_old')} trading day(s) old)"
    )
    fx_line = (
        "STALE/BLIND — no successful Flex statement store (journal blind)"
        if fx.get("stale")
        else f"fresh — last store {fx.get('hours_old')}h ago"
    )
    tok_line = (
        f"{tok.get('days_to_expiry')} days to expiry"
        if tok.get("known")
        else "days-to-expiry unknown (not configured)"
    )
    return (
        "SOURCE_HEALTH\n"
        f"  Summary: {sh.get('summary')}\n"
        f"  Prices: {p_line}\n"
        f"  Flex: {fx_line}\n"
        f"  Flex token: {tok_line}"
    )


def serialize_bundle(
    bundle: dict, news_top: int = _NEWS_TOP_N, retail_top: int = _RETAIL_TOP_N
) -> str:
    """Render the frozen bundle as the labeled text block the synthesis prompt consumes."""
    parts = [
        "=== ARGUS DIGEST INPUT ===",
        f"GENERATED_FOR: {bundle.get('generated_for')}   RUN_TYPE: {bundle.get('run_type')}",
        "",
        _prices_block(bundle.get("prices") or {}),
        "",
        _indicators_block(bundle.get("indicators") or {}),
        "",
        _macro_block(bundle.get("macro") or {}),
        "",
        _headlines_block(bundle.get("headlines") or [], news_top, retail_top),
        "",
        _calendar_block(bundle.get("calendar") or []),
        "",
        _book_block(bundle),
        "",
        _source_health_block(bundle.get("source_health") or {}),
    ]
    return "\n".join(parts)


if __name__ == "__main__":
    import json

    try:
        import sys

        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 — best-effort on non-reconfigurable streams
        pass
    with open("_bundle_dryrun.json", encoding="utf-8") as fh:
        print(serialize_bundle(json.load(fh)))
