"""Argus analyst — serialize (pack + valuation) into the labeled DATA block.

One text render serves two masters, exactly like ``digest/serialize.py``:
it is the ONLY thing the synthesis model sees (never raw JSON), and it is the
grounding whitelist — ``digest.grounding.validate_text`` traces every number in
the dossier back to THIS block, so anything the dossier may cite must be printed
here, and anything printed here is thereby citable (Law 2, both directions).

Derived-display rule (mirrors the digest serializer): any figure computed HERE
(margins as %, the not-meaningful margin-of-safety render) is in the block and
therefore grounds; a figure the MODEL computes appears nowhere and fails the
gate. Keep every derivation the dossier will want inside this file.

Absences are printed as explicit "not available" lines — the model is
instructed to say so rather than fill gaps, and the harsh-reader gate checks it.
"""

from __future__ import annotations

from analyst.claims import _CONCEPTS, _points

_SERIES_TABLE_CONCEPTS = (
    ("revenue", "revenue"),
    ("gross_profit", "gross profit"),
    ("operating_income", "operating income"),
    ("net_income", "net income"),
    ("operating_cash_flow", "operating cash flow"),
    ("capex", "capex"),
    ("depreciation_amortization", "D&A"),
    ("total_assets", "total assets"),
    ("total_liabilities", "total liabilities"),
    ("shares_diluted", "diluted shares (split-adj)"),
)


def _n(value, decimals: int = 0) -> str:
    """A number for the block, or 'n/a' — never a fabricated placeholder."""
    if value is None:
        return "n/a"
    return f"{value:,.{decimals}f}"


def _pct(value, decimals: int = 1) -> str:
    return "n/a" if value is None else f"{value * 100:.{decimals}f}%"


def _fy_table(series: dict) -> str:
    """The per-fiscal-year fundamentals table (split-adjusted where applicable)."""
    periods: list[str] = sorted(
        {
            row["period_end"]
            for key, _ in _SERIES_TABLE_CONCEPTS
            for row in (series.get(key) or [])
            if row.get("period_end")
        }
    )
    if not periods:
        return "(no filed annual fundamentals — not available)"
    by = {
        key: {r["period_end"]: r.get("value") for r in (series.get(key) or [])}
        for key, _ in _SERIES_TABLE_CONCEPTS
    }
    lines = []
    for pe in periods:
        cells = [f"FY {pe}:"]
        for key, label in _SERIES_TABLE_CONCEPTS:
            value = by[key].get(pe)
            if value is not None:
                cells.append(f"{label} { _n(value) }")
        # Derived-display rule: Stage 2 reads balance-sheet posture, so the equity
        # figure the dossier wants is computed HERE (and thereby grounds) — the model
        # subtracting it itself is a Law-2 violation the gate flags (first TSLA run).
        ta, tl = by["total_assets"].get(pe), by["total_liabilities"].get(pe)
        if ta is not None and tl is not None:
            cells.append(f"equity (assets minus liabilities) {_n(ta - tl)}")
        lines.append("  " + "; ".join(cells))
    return "\n".join(lines)


def _metrics_block(metrics: dict) -> str:
    lines: list[str] = []
    margins = metrics.get("margins") or []
    for m in margins:
        lines.append(
            f"  FY {m['period_end']}: gross margin {_pct(m.get('gross_margin'))}, "
            f"operating margin {_pct(m.get('operating_margin'))}, "
            f"net margin {_pct(m.get('net_margin'))}"
        )
    if not margins:
        lines.append("  margins: not available")
    cagr = metrics.get("revenue_cagr") or {}
    for horizon in sorted(cagr):
        entry = cagr[horizon]
        if entry.get("value") is not None:
            lines.append(f"  revenue CAGR {horizon}y: {_pct(entry['value'])}")
        else:
            lines.append(f"  revenue CAGR {horizon}y: not available ({entry.get('reason')})")
    consistency = metrics.get("earnings_consistency") or {}
    if consistency:
        lines.append(
            f"  earnings consistency: {consistency.get('years_covered')} fiscal years covered "
            f"({consistency.get('first_period_end')} to {consistency.get('last_period_end')}), "
            f"{consistency.get('loss_years')} loss year(s), "
            f"{consistency.get('profit_years')} profitable year(s)"
        )
    fcf = metrics.get("fcf_proxy") or []
    for row in fcf[-6:]:
        basis = " (capex unavailable: OCF only)" if row.get("basis") != "ocf_minus_capex" else ""
        lines.append(f"  FCF FY {row['period_end']}: {_n(row.get('fcf'))}{basis}")
    eps = metrics.get("eps_history") or []
    for row in eps[-6:]:
        if row.get("eps") is not None:
            lines.append(f"  EPS (split-adj diluted) FY {row['period_end']}: {row['eps']:.2f}")
    return "\n".join(lines)


def _extrema_block(pack: dict) -> str:
    """Peak + trough (period, value) per fiscal series — the authoritative anchor for
    any superlative the dossier makes.

    Mirrors the file's derived-display rule (equity, peer spreads): a comparative the
    prompt invites — "peaked", "highest", "since its peak" — is pre-computed HERE at the
    block's own precision, so the model cites the real extremum instead of manufacturing
    one from a recent salient value (the class the claims-lint enforces). Over the FULL
    printed series, not just recent years — the systematic error is recency-anchoring.
    """
    lines: list[str] = []
    for c in _CONCEPTS:
        pts = _points(pack, c["src"])
        if len(pts) < 2:
            continue  # a single point has no meaningful peak/trough
        hi = max(pts, key=lambda t: t[1])
        lo = min(pts, key=lambda t: t[1])
        fmt = (lambda v: _pct(v)) if c["pct"] else (
            (lambda v: f"{v:.2f}") if c["key"] in ("eps",) else (lambda v: _n(v))
        )
        lines.append(
            f"  {c['label']}: peak {fmt(hi[1])} (FY {hi[0][:4]}); "
            f"trough {fmt(lo[1])} (FY {lo[0][:4]})"
        )
    if not lines:
        return "(no multi-year series — extrema not available)"
    return "\n".join(lines)


def _peer_block(peers: dict) -> str:
    lines = [f"peer set ({peers.get('source') or 'no source'}): {', '.join(peers.get('peers') or []) or 'none'}"]
    table = peers.get("table") or []
    for row in table:
        lines.append(
            f"  {row['symbol']}: latest FY {row.get('period_end') or 'n/a'}; "
            f"gross margin {_pct(row.get('gross_margin'))}; "
            f"revenue CAGR 3y {_pct(row.get('revenue_cagr_3y'))}, 5y {_pct(row.get('revenue_cagr_5y'))}; "
            f"loss years {row.get('loss_years') if row.get('loss_years') is not None else 'n/a'}"
            f" of {row.get('years_covered') if row.get('years_covered') is not None else 'n/a'} covered; "
            f"diluted share change 3y {_pct(row.get('diluted_shares_change_3y'))}; "
            f"latest FCF {_n(row.get('fcf_latest'))}"
        )
    # Derived-display rule: Stage 3 reads "margin spread vs peers", so the spreads are
    # computed HERE (row 0 is the target — data_pack puts it first). A spread the model
    # computes itself grounds to nothing and fails the gate (first TSLA run flagged
    # exactly this: "11.2 percentage points" vs Ford).
    target = table[0] if table else {}
    target_gm = target.get("gross_margin")
    if target_gm is not None:
        for row in table[1:]:
            peer_gm = row.get("gross_margin")
            if peer_gm is not None:
                lines.append(
                    f"  gross-margin spread, {target.get('symbol')} minus {row['symbol']}: "
                    f"{(target_gm - peer_gm) * 100:+.1f} percentage points"
                )
    missing = peers.get("missing_fundamentals") or []
    if missing:
        lines.append(f"  peers with no ingestable fundamentals: {', '.join(missing)}")
    return "\n".join(lines)


def _valuation_block(valuation: dict) -> str:
    """The Stage-7 render: chain, sensitivity, MoS (with the n/m display), reverse-DCF."""
    if not valuation.get("renderable"):
        return f"valuation: not renderable — {valuation.get('reason')}"

    inp = valuation["inputs"]
    grid = valuation["assumption_grid"]
    lines = [
        "These scenario outputs are CONSEQUENCES OF THE STATED ASSUMPTIONS below (an",
        "editable config grid) — they are not forecasts and not a judgment about the",
        "company. Present them only together with their assumptions.",
        f"  base year: revenue {_n(inp['revenue_0']['value'])} (FY end {inp['revenue_0']['period_end']}), "
        f"diluted shares (split-adj) {_n(inp['shares_0']['value'])}",
        f"  owner earnings (the cash the business generates after maintaining itself; "
        f"basis {inp['owner_earnings_0']['basis']}): "
        f"{_n(inp['owner_earnings_0']['owner_earnings'])} — owner-earnings margin "
        f"{_pct(inp.get('owner_earnings_margin_0'))}",
        f"  current price: {_n(inp['price'], 2)} ({inp['price_date']})",
        f"  horizon {grid['horizon_years']:g} years; required return {_pct(grid['required_return'], 0)}; "
        f"scenario weights bear {_pct(grid['weights']['bear'], 0)} / base {_pct(grid['weights']['base'], 0)} "
        f"/ bull {_pct(grid['weights']['bull'], 0)}",
    ]
    for note in inp.get("notes") or []:
        lines.append(f"  input note: {note}")
    for name in ("bear", "base", "bull"):
        s = valuation["scenarios"][name]
        a = s["assumptions"]
        lines.append(
            f"  {name}: revenue CAGR {_pct(a['revenue_cagr'])}, terminal owner-earnings margin "
            f"{_pct(a['terminal_margin'])}, exit multiple {a['exit_multiple']:g}x, annual dilution "
            f"{_pct(a['annual_dilution'], 2)} -> horizon revenue {_n(s['revenue_h'])}, horizon "
            f"owner earnings {_n(s['earnings_h'])}, present value {s['per_share_pv']:.2f}/share"
        )
    weighted = valuation["weighted_value_per_share"]
    price = inp["price"]
    mos = valuation.get("margin_of_safety_pct")
    lines.append(f"  bear-weighted estimate: {weighted:.2f}/share")
    if mos is None:
        # The engine returns None for TWO reasons: no price, OR a non-positive
        # bear-weighted estimate with a valid price. Attribute the real one (the old
        # single message mislabeled a negative-value case as "no price").
        if price is None:
            lines.append("  margin of safety: not computable (no current price)")
        else:
            lines.append(
                f"  margin of safety: not computable — the bear-weighted estimate "
                f"{weighted:.2f} is non-positive, so a percentage is meaningless"
            )
    elif mos < -1.0:
        # Display cap: a deeply negative MoS reads as noise ("-1020%"); the honest
        # render is the relationship, with both anchors printed (both then ground).
        lines.append(
            f"  margin of safety: not meaningful as a percentage — the current price "
            f"{_n(price, 2)} sits far above the bear-weighted estimate {weighted:.2f}"
        )
    else:
        lines.append(f"  margin of safety vs current price: {_pct(mos)}")
    lines.append("  sensitivity (one assumption swung bear->bull, others held at base):")
    for var, s in (valuation.get("sensitivity") or {}).items():
        mover = "  <- biggest mover" if var == valuation.get("biggest_mover") else ""
        lines.append(
            f"    {var}: value range {s['bear_setting']:.2f} to {s['bull_setting']:.2f}"
            f" (spread {s['spread']:.2f}){mover}"
        )
    for flag in valuation.get("base_rate_flags") or []:
        lines.append(f"  BASE-RATE FLAG: {flag}")
    rd = valuation.get("reverse_dcf") or {}
    if rd.get("implied_revenue_cagr") is not None:
        lines.append(
            f"  reverse-DCF (working backwards from today's price to the growth it "
            f"assumes): the current price implies {_pct(rd['implied_revenue_cagr'])} annual "
            f"revenue growth for {grid['horizon_years']:g} years at the base-case margin, multiple "
            f"and dilution"
        )
    else:
        lines.append(f"  reverse-DCF: not solvable — {rd.get('reason')}")
    cm = valuation.get("current_multiples") or {}
    lines.append(
        f"  trailing multiples at current price: P/E "
        f"{_n(cm.get('pe_trailing'), 1)}, price to owner earnings {_n(cm.get('price_to_owner_earnings'), 1)}"
    )
    return "\n".join(lines)


def _estimates_block(estimates: dict) -> str:
    lines: list[str] = []
    pt = estimates.get("price_targets")
    if pt:
        lines.append(
            f"  analyst price targets: mean {_n(pt.get('mean'), 2)}, median {_n(pt.get('median'), 2)}, "
            f"low {_n(pt.get('low'), 2)}, high {_n(pt.get('high'), 2)} (current {_n(pt.get('current'), 2)})"
        )
    rec = estimates.get("recommendation_trend")
    if rec:
        latest = rec[0]
        lines.append(
            "  analyst recommendations (current month): "
            + ", ".join(
                f"{k} {int(latest[k])}"
                for k in ("strongBuy", "buy", "hold", "sell", "strongSell")
                if isinstance(latest.get(k), (int, float))
            )
        )
    for key, label in (("earnings_estimate", "EPS estimates"), ("revenue_estimate", "revenue estimates")):
        rows = estimates.get(key)
        if rows:
            for r in rows[:4]:
                lines.append(
                    f"  {label} {r.get('period')}: avg {_n(r.get('avg'), 2)}, low {_n(r.get('low'), 2)}, "
                    f"high {_n(r.get('high'), 2)}, analysts {_n(r.get('numberOfAnalysts'))}"
                )
    si = estimates.get("short_interest")
    if si:
        lines.append(
            f"  short interest: {_n(si.get('shares_short'))} shares short "
            f"({_pct(si.get('short_pct_of_float'), 2)} of float), short ratio {_n(si.get('short_ratio'), 2)}, "
            f"as of {si.get('as_of')}"
        )
    if not lines:
        lines.append("  consensus/sentiment context: not available")
    return "\n".join(lines)


def _news_block(news: dict) -> str:
    heads = news.get("headlines") or []
    if not heads:
        note = f"; {news['note']}" if news.get("note") else ""
        return f"(no tagged headlines in the last {news.get('window_days')} days{note})"
    lines = [f"tagged headlines, last {news.get('window_days')} days ({len(heads)}):"]
    for h in heads:
        senti = h.get("sentiment") or []
        tag = ""
        if senti:
            s = senti[0]
            tag = f" [{s.get('direction')} {s.get('magnitude')}]"
        lines.append(f"  - ({h.get('source')}, {str(h.get('published_at'))[:10]}) {h.get('title')}{tag}")
    return "\n".join(lines)


def _filings_block(filings: dict) -> str:
    lines: list[str] = []
    for key, label in (("10k", "FORM 10-K"), ("def14a", "PROXY (DEF 14A)")):
        f = filings.get(key)
        if not isinstance(f, dict) or "sections" not in f:
            note = (f or {}).get("note", "not available") if isinstance(f, dict) else "not available"
            lines.append(f"{label}: {note}")
            continue
        lines.append(f"{label} — filed {f['filed']}, accession {f['accn']}:")
        for name, section in f["sections"].items():
            if section is None:
                lines.append(f"  [{name}]: not available")
                continue
            trunc = (
                f" (excerpt: first {len(section['text'])} of {section['chars_original']} chars)"
                if section.get("truncated")
                else ""
            )
            lines.append(f"  [{name}]{trunc}:")
            lines.append(section["text"])
    return "\n".join(lines)


def serialize_analysis(pack: dict, valuation: dict) -> str:
    """The one labeled DATA block: synthesis input == grounding whitelist (Law 2)."""
    price = pack.get("price") or {}
    sections = [
        f"TARGET\nsymbol {pack.get('symbol')}; SEC CIK {pack.get('cik') or 'unresolved'}; "
        f"latest close {_n(price.get('close'), 2)} on {price.get('date')} "
        f"(source {price.get('source') or 'unavailable'})",
        "FUNDAMENTALS (per fiscal year, from SEC XBRL filings; share counts split-adjusted)\n"
        + _fy_table(pack.get("series") or {}),
        "DERIVED METRICS (computed by Argus from the filed figures above)\n"
        + _metrics_block(pack.get("metrics") or {}),
        "SERIES EXTREMA (peak and trough over ALL printed fiscal years — the authoritative\n"
        "source for any superlative; never call a mid-series value a peak or a low)\n"
        + _extrema_block(pack),
        "PEER COMPARISON (latest fiscal year each; same filed basis)\n"
        + _peer_block(pack.get("peers") or {}),
        "VALUATION SCENARIOS (deterministic engine output)\n"
        + _valuation_block(valuation),
        "CONSENSUS & SENTIMENT CONTEXT (Mr. Market — what is priced in, never evidence of value)\n"
        + _estimates_block(pack.get("estimates") or {}),
        "RECENT NEWS\n" + _news_block(pack.get("news") or {}),
        "SOURCE HEALTH (this pack's build)\n"
        + "\n".join(f"  {k}: {v}" for k, v in (pack.get("source_health") or {}).items()),
        "FILINGS TEXT (verbatim excerpts from EDGAR)\n" + _filings_block(pack.get("filings") or {}),
    ]
    return "\n\n".join(sections)
