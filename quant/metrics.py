"""Argus quant — read-time fundamental metrics over the split-adjusted history.

Derivations the Phase-5 analyst frameworks consume: margins per fiscal year, revenue
CAGR (3/5/10y), earnings consistency (loss years), the FCF proxy (OCF − capex), and
split-adjusted EPS. NOTHING here is stored and NO LLM is involved — every figure is
computed at read time from ``fundamentals_latest`` rows (via
:func:`quant.splits.read_concept`, which owns the split basis), and every metric
record carries the ``period_end`` / ``accn`` / ``filed`` provenance of each input, so
any rendered number traces to the exact SEC filing it came from (Law 2).

Absence stays visible (Law 7): a year missing an input yields that metric as None
with the gap named — never interpolated, never silently substituted. In particular
the FCF proxy degrades to OCF-only ONLY with an explicit ``capex unavailable`` basis
flag on the record.

Period matching: within one symbol, fiscal years are joined on the exact
``period_end`` date (TSLA is a calendar-year filer; every duration row ends 12-31).
CAGR horizons match on the period_end YEAR n years apart.

Run:  python -m quant.metrics   (prints TSLA's full metrics table + the gross-margin
      identity reconciliation vs the stored gross_profit concept)
"""

from __future__ import annotations

if __name__ == "__main__" and __package__ in (None, ""):
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from quant.splits import read_concept

# Every concept the metrics below read. Loaded once per call via load_inputs.
_INPUT_CONCEPTS: tuple[str, ...] = (
    "revenue",
    "cost_of_revenue",
    "gross_profit",
    "operating_income",
    "net_income",
    "operating_cash_flow",
    "capex",
    "shares_diluted",
)

_CAGR_HORIZONS: tuple[int, ...] = (3, 5, 10)


def _prov(record: dict) -> dict:
    """The provenance triple of one input record (which filing supplied the value)."""
    return {
        "value": record.get("value"),
        "period_end": record.get("period_end"),
        "accn": record.get("accn"),
        "filed": record.get("filed"),
    }


def _by_period_end(records: list[dict]) -> dict[str, dict]:
    """Index one concept's records by their exact period_end (the fiscal-year join key)."""
    return {r["period_end"]: r for r in records if r.get("period_end")}


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    """numerator/denominator, or None when either is missing or the denominator is 0."""
    if numerator is None or denominator in (None, 0):
        return None
    return numerator / denominator


def load_inputs(symbol: str, client=None) -> dict[str, list[dict]]:
    """All input concepts' split-adjusted annual series, one dict per concept."""
    return {c: read_concept(symbol, c, client) for c in _INPUT_CONCEPTS}


def margins(symbol: str, data: dict[str, list[dict]] | None = None) -> list[dict]:
    """Gross / operating / net margin per fiscal year, with per-input provenance.

    gross_margin = (revenue − cost_of_revenue) / revenue — computed from the two
    stored components; the stored ``gross_profit`` concept is carried alongside so the
    identity (gross_profit == revenue − cost_of_revenue) is reconcilable on read.
    A year missing a component yields that margin as None (the gap stays visible).
    """
    data = data or load_inputs(symbol)
    revenue = _by_period_end(data["revenue"])
    cogs = _by_period_end(data["cost_of_revenue"])
    gp = _by_period_end(data["gross_profit"])
    op = _by_period_end(data["operating_income"])
    ni = _by_period_end(data["net_income"])

    out: list[dict] = []
    for period_end in sorted(revenue):
        rev = revenue[period_end].get("value")
        cogs_v = (cogs.get(period_end) or {}).get("value")
        gp_v = (gp.get(period_end) or {}).get("value")
        op_v = (op.get(period_end) or {}).get("value")
        ni_v = (ni.get(period_end) or {}).get("value")
        computed_gp = rev - cogs_v if rev is not None and cogs_v is not None else None
        out.append(
            {
                "period_end": period_end,
                "gross_margin": _ratio(computed_gp, rev),
                "operating_margin": _ratio(op_v, rev),
                "net_margin": _ratio(ni_v, rev),
                # Identity check inputs: stored gross_profit vs revenue − cost_of_revenue.
                "gross_profit_stored": gp_v,
                "gross_profit_computed": computed_gp,
                "inputs": {
                    "revenue": _prov(revenue[period_end]),
                    "cost_of_revenue": _prov(cogs.get(period_end) or {}),
                    "gross_profit": _prov(gp.get(period_end) or {}),
                    "operating_income": _prov(op.get(period_end) or {}),
                    "net_income": _prov(ni.get(period_end) or {}),
                },
            }
        )
    return out


def revenue_cagr(
    symbol: str,
    data: dict[str, list[dict]] | None = None,
    horizons: tuple[int, ...] = _CAGR_HORIZONS,
) -> dict[int, dict]:
    """Revenue CAGR over each horizon, anchored on the latest fiscal year.

    CAGR_n = (latest / base)^(1/n) − 1 where base is the fiscal year exactly n years
    before the latest (matched on period_end year). Missing base year, or a base
    value <= 0 (the root is undefined), yields value None with the reason named.
    """
    data = data or load_inputs(symbol)
    records = [r for r in data["revenue"] if r.get("value") is not None]
    if not records:
        return {n: {"value": None, "reason": "no revenue rows"} for n in horizons}
    by_year = {int(r["period_end"][:4]): r for r in records}
    latest_year = max(by_year)
    latest = by_year[latest_year]

    out: dict[int, dict] = {}
    for n in horizons:
        base = by_year.get(latest_year - n)
        if base is None:
            out[n] = {"value": None, "reason": f"no revenue row for FY{latest_year - n}"}
            continue
        if base["value"] <= 0:
            out[n] = {"value": None, "reason": f"base FY{latest_year - n} revenue <= 0"}
            continue
        out[n] = {
            "value": (latest["value"] / base["value"]) ** (1.0 / n) - 1.0,
            "from": _prov(base),
            "to": _prov(latest),
        }
    return out


def earnings_consistency(symbol: str, data: dict[str, list[dict]] | None = None) -> dict:
    """Loss-year count over the available net_income history (+ which years, with provenance)."""
    data = data or load_inputs(symbol)
    records = [r for r in data["net_income"] if r.get("value") is not None]
    losses = [r for r in records if r["value"] < 0]
    return {
        "years_covered": len(records),
        "first_period_end": records[0]["period_end"] if records else None,
        "last_period_end": records[-1]["period_end"] if records else None,
        "loss_years": len(losses),
        "profit_years": len(records) - len(losses),
        "losses": [_prov(r) for r in losses],
    }


def fcf_proxy(symbol: str, data: dict[str, list[dict]] | None = None) -> list[dict]:
    """FCF proxy per fiscal year: OCF − capex, degrading EXPLICITLY when capex is missing.

    basis:
      'ocf_minus_capex'            — both inputs present; fcf = ocf − capex.
      'ocf_only_capex_unavailable' — no capex row for the year; fcf = ocf, flagged.
    Never a silent substitution (Law 7): the basis names what the figure is.
    """
    data = data or load_inputs(symbol)
    capex = _by_period_end(data["capex"])
    out: list[dict] = []
    for row in data["operating_cash_flow"]:
        ocf = row.get("value")
        if ocf is None:
            continue
        period_end = row["period_end"]
        capex_row = capex.get(period_end)
        capex_v = (capex_row or {}).get("value")
        if capex_v is not None:
            fcf, basis = ocf - capex_v, "ocf_minus_capex"
        else:
            fcf, basis = ocf, "ocf_only_capex_unavailable"
        out.append(
            {
                "period_end": period_end,
                "fcf": fcf,
                "basis": basis,
                "capex_available": capex_v is not None,
                "inputs": {
                    "operating_cash_flow": _prov(row),
                    "capex": _prov(capex_row or {}),
                },
            }
        )
    return out


def eps_history(symbol: str, data: dict[str, list[dict]] | None = None) -> list[dict]:
    """Split-adjusted diluted EPS per fiscal year: net_income ÷ adjusted diluted shares.

    Shares come through the split layer (today's basis), so the series is comparable
    across the 2020/2022 splits. A year missing either input yields eps None with the
    missing side visible in ``inputs``.
    """
    data = data or load_inputs(symbol)
    ni = _by_period_end(data["net_income"])
    shares = _by_period_end(data["shares_diluted"])
    out: list[dict] = []
    for period_end in sorted(set(ni) | set(shares)):
        ni_v = (ni.get(period_end) or {}).get("value")
        sh_row = shares.get(period_end)
        sh_v = (sh_row or {}).get("value")
        out.append(
            {
                "period_end": period_end,
                "eps": _ratio(ni_v, sh_v),
                "shares_split_factor": (sh_row or {}).get("split_factor"),
                "inputs": {
                    "net_income": _prov(ni.get(period_end) or {}),
                    "shares_diluted_adjusted": _prov(sh_row or {}),
                },
            }
        )
    return out


def metrics_table(symbol: str, client=None) -> dict:
    """Every metric family for one symbol, from a single load of the input concepts."""
    data = load_inputs(symbol, client)
    return {
        "symbol": symbol,
        "margins": margins(symbol, data),
        "revenue_cagr": revenue_cagr(symbol, data),
        "earnings_consistency": earnings_consistency(symbol, data),
        "fcf_proxy": fcf_proxy(symbol, data),
        "eps_history": eps_history(symbol, data),
    }


# --------------------------------------------------------------------------- #
# Manual probe — TSLA's full table + the gross-margin identity reconciliation
# --------------------------------------------------------------------------- #
def _fmt_pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:+.1f}%"


def _fmt_b(value: float | None) -> str:
    """Billions with 2 decimals for table display; None -> n/a."""
    return "n/a" if value is None else f"{value / 1e9:+.2f}B"


def _print_table(symbol: str) -> None:
    table = metrics_table(symbol)

    print(f"[metrics] {symbol} margins (per fiscal year):")
    print(f"  {'period_end':<12} {'gross':>8} {'operating':>10} {'net':>8}")
    for m in table["margins"]:
        print(
            f"  {m['period_end']:<12} {_fmt_pct(m['gross_margin']):>8} "
            f"{_fmt_pct(m['operating_margin']):>10} {_fmt_pct(m['net_margin']):>8}"
        )

    print(f"\n[metrics] {symbol} revenue CAGR (anchored on latest fiscal year):")
    for n, r in table["revenue_cagr"].items():
        if r["value"] is None:
            print(f"  {n:>2}y: n/a ({r['reason']})")
        else:
            print(
                f"  {n:>2}y: {_fmt_pct(r['value'])}  "
                f"({r['from']['period_end']} {_fmt_b(r['from']['value'])} -> "
                f"{r['to']['period_end']} {_fmt_b(r['to']['value'])})"
            )

    ec = table["earnings_consistency"]
    print(
        f"\n[metrics] {symbol} earnings consistency: {ec['loss_years']} loss year(s) / "
        f"{ec['profit_years']} profit year(s) over {ec['years_covered']} covered "
        f"({ec['first_period_end']} .. {ec['last_period_end']})"
    )
    for loss in ec["losses"]:
        print(f"  loss {loss['period_end']}: {_fmt_b(loss['value'])}  (accn {loss['accn']})")

    print(f"\n[metrics] {symbol} FCF proxy (OCF - capex):")
    print(f"  {'period_end':<12} {'OCF':>10} {'capex':>10} {'FCF':>10}  basis")
    for f in table["fcf_proxy"]:
        ocf = f["inputs"]["operating_cash_flow"]["value"]
        cap = f["inputs"]["capex"]["value"]
        print(
            f"  {f['period_end']:<12} {_fmt_b(ocf):>10} {_fmt_b(cap):>10} "
            f"{_fmt_b(f['fcf']):>10}  {f['basis']}"
        )

    print(f"\n[metrics] {symbol} split-adjusted diluted EPS:")
    print(f"  {'period_end':<12} {'net income':>12} {'adj shares':>16} {'EPS':>8}")
    for e in table["eps_history"]:
        ni = e["inputs"]["net_income"]["value"]
        sh = e["inputs"]["shares_diluted_adjusted"]["value"]
        eps = "n/a" if e["eps"] is None else f"{e['eps']:+.2f}"
        sh_s = "n/a" if sh is None else f"{sh:,.0f}"
        print(f"  {e['period_end']:<12} {_fmt_b(ni):>12} {sh_s:>16} {eps:>8}")

    print(f"\n[metrics] {symbol} gross-margin identity reconciliation "
          "(stored gross_profit vs revenue - cost_of_revenue):")
    worst = 0.0
    for m in table["margins"]:
        stored, computed = m["gross_profit_stored"], m["gross_profit_computed"]
        if stored is None or computed is None:
            print(f"  {m['period_end']}: identity not checkable (a component is missing)")
            continue
        delta = abs(stored - computed)
        worst = max(worst, delta)
        rev = m["inputs"]["revenue"]["value"]
        margin_from_stored = stored / rev if rev else None
        print(
            f"  {m['period_end']}: stored {_fmt_b(stored)} vs computed {_fmt_b(computed)} "
            f"(delta ${delta:,.0f}); margin {_fmt_pct(m['gross_margin'])} vs "
            f"{_fmt_pct(margin_from_stored)} from stored"
        )
    print(f"  worst identity delta: ${worst:,.0f}")


if __name__ == "__main__":
    import sys

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 — best-effort; ASCII-only terminals still run fine
        pass
    _print_table("TSLA")
