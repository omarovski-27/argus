"""Argus quant — the Stage-7 scenario engine + reverse-DCF (module spec §1, Law 4).

Deterministic math, zero LLM. Every forward view is a RANGE from explicit
assumptions — no single-point forecast exists anywhere in this module (Law 4).
The assumption grid lives in ``config.valuation_assumptions`` (JSONB, per-run
overridable), never hardcoded; the grid ships verbatim inside every output so
assumptions are always visible next to their consequences.

The model (per scenario s over horizon H, discounted at required_return r):

    revenue_H   = revenue_0 x (1 + s.revenue_cagr)^H
    earnings_H  = revenue_H x s.terminal_margin          (owner-earnings margin)
    equity_H    = earnings_H x s.exit_multiple
    shares_H    = shares_0 x (1 + s.annual_dilution)^H   (split-adjusted shares_0)
    value/share = equity_H / shares_H / (1 + r)^H

Owner-earnings base: NI + D&A - capex per fiscal year (basis 'ni_da_capex');
where D&A is unfiled the year falls back to OCF - capex (basis
'ocf_minus_capex', labeled — Law 7 style: the degradation is visible, never
silent). Reverse-DCF inverts the identity in closed form: the revenue CAGR the
CURRENT price implies at base-case margin/multiple/dilution.

Everything per-share reads the split-adjusted layer (``value`` from
``fundamentals_latest`` via quant.splits) — raw share counts straddling a split
corrupt every per-share figure (LCID's raw series shows a fake -81% 3y
"buyback" from its reverse split).

Run:  python -m quant.valuation TSLA   (builds a fresh pack, prints the tables)
"""

from __future__ import annotations

if __name__ == "__main__" and __package__ in (None, ""):
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import math

from shared.db import get_client

CONFIG_KEY = "valuation_assumptions"

_SCENARIOS = ("bear", "base", "bull")
_SCENARIO_VARS = ("revenue_cagr", "terminal_margin", "exit_multiple", "annual_dilution")


# --------------------------------------------------------------------------- #
# Assumption grid
# --------------------------------------------------------------------------- #
def _num_ok(value, lo: float | None = None, hi: float | None = None) -> bool:
    """True for a FINITE number within [lo, hi] (bounds optional). Bools rejected."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if not math.isfinite(value):
        return False
    return (lo is None or value >= lo) and (hi is None or value <= hi)


# Per-variable range contract. The math itself defines the hard edges (the P2
# review's confirmed cluster): dilution/cagr at or below -100% turn share counts
# and revenues negative — reverse-DCF then takes a fractional power of a negative
# number and returns a COMPLEX growth rate; required_return at -100% divides by
# zero; non-positive margins/multiples render "value" as a negative equity a
# going-concern exit cannot mean. Sane economic caps bound the rest.
_SCENARIO_VAR_RANGES: dict[str, tuple[float, float]] = {
    "revenue_cagr": (-0.99, 2.0),      # > -100%/yr; 200%/yr is already fantasy
    "terminal_margin": (1e-6, 1.0),    # a going-concern exit needs positive earnings
    "exit_multiple": (1e-6, 200.0),
    "annual_dilution": (-0.5, 1.0),    # buybacks capped at -50%/yr; > -100% required
}

# Value-monotonicity: bear <= base <= bull per-share value is guaranteed iff each
# variable moves monotonically in the value-increasing direction across scenarios
# (dilution DECREASES value, so it must descend bear -> bull).
_ASCENDING_VARS = ("revenue_cagr", "terminal_margin", "exit_multiple")


def validate_assumptions(grid: dict) -> None:
    """Raise ValueError naming every missing/malformed/out-of-range piece (Law 7).

    A malformed grid must fail loud before any math — a silently-defaulted or
    out-of-range assumption would put a number in front of Omar whose premise he
    never saw (or one that is literally complex-valued).
    """
    problems: list[str] = []
    if not isinstance(grid, dict):
        raise ValueError(f"valuation_assumptions must be a dict, got {type(grid).__name__}")
    for key in ("horizon_years", "required_return", "weights", "base_rate_cagr_flag", "scenarios"):
        if key not in grid:
            problems.append(f"missing '{key}'")

    if "horizon_years" in grid and not _num_ok(grid["horizon_years"], 1.0, 50.0):
        problems.append("'horizon_years' must be a finite number in [1, 50]")
    if "required_return" in grid and not _num_ok(grid["required_return"], 0.0, 1.0):
        problems.append("'required_return' must be a finite number in [0, 1]")
    if "base_rate_cagr_flag" in grid and not _num_ok(grid["base_rate_cagr_flag"], 0.0):
        problems.append("'base_rate_cagr_flag' must be a finite number >= 0")

    scenarios = grid.get("scenarios") or {}
    for name in _SCENARIOS:
        s = scenarios.get(name)
        if not isinstance(s, dict):
            problems.append(f"missing scenario '{name}'")
            continue
        for var in _SCENARIO_VARS:
            lo, hi = _SCENARIO_VAR_RANGES[var]
            if not _num_ok(s.get(var), lo, hi):
                problems.append(f"scenario '{name}': '{var}' must be finite in [{lo}, {hi}]")

    if all(isinstance(scenarios.get(n), dict) for n in _SCENARIOS):
        bear, base, bull = (scenarios[n] for n in _SCENARIOS)
        for var in _SCENARIO_VARS:
            vals = (bear.get(var), base.get(var), bull.get(var))
            if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in vals):
                continue  # already reported above
            if var in _ASCENDING_VARS and not (vals[0] <= vals[1] <= vals[2]):
                problems.append(f"'{var}' must ascend bear <= base <= bull (got {vals})")
            if var == "annual_dilution" and not (vals[0] >= vals[1] >= vals[2]):
                problems.append(
                    f"'annual_dilution' must descend bear >= base >= bull (got {vals})"
                )

    weights = grid.get("weights") or {}
    weight_vals = []
    for name in _SCENARIOS:
        if not _num_ok(weights.get(name), 0.0, 1.0):
            problems.append(f"weights: '{name}' must be a finite number in [0, 1]")
        else:
            weight_vals.append(weights[name])
    if len(weight_vals) == len(_SCENARIOS) and abs(sum(weight_vals) - 1.0) > 1e-6:
        problems.append(f"weights must sum to 1 (got {sum(weight_vals):g})")

    if problems:
        raise ValueError("valuation_assumptions invalid: " + "; ".join(problems))


def load_assumptions(client=None) -> dict:
    """The grid from ``config.valuation_assumptions``, validated (fail loud on absent).

    There is deliberately no in-code default grid: assumptions are Omar's editable
    judgments, and a hardcoded fallback would silently reintroduce the constant the
    config row exists to replace (same posture as ``sleeve_symbol``).
    """
    client = client or get_client()
    rows = (
        client.table("config").select("value").eq("key", CONFIG_KEY).limit(1).execute().data or []
    )
    if not rows:
        raise ValueError(
            f"config.{CONFIG_KEY} is not seeded; run `python -m analyst.seed_valuation` "
            "(the grid is config, never a hardcoded default)."
        )
    grid = rows[0]["value"]
    validate_assumptions(grid)
    return grid


# --------------------------------------------------------------------------- #
# Inputs from the frozen pack
# --------------------------------------------------------------------------- #
def _latest(series: list[dict] | None) -> dict | None:
    """The latest-period row of a pack concept series (they are period-ascending)."""
    return series[-1] if series else None


def _prov(row: dict | None) -> dict | None:
    """The compact provenance triple carried beside every input (Law 2)."""
    if row is None:
        return None
    return {
        "value": row.get("value"),
        "period_end": row.get("period_end"),
        "accn": row.get("accn"),
        "filed": row.get("filed"),
    }


def owner_earnings_series(series: dict) -> list[dict]:
    """Owner earnings per fiscal year, basis-labeled (pure; pack['series'] in).

    OE = net_income + depreciation_amortization - capex where all three align on
    period_end ('ni_da_capex'); years missing D&A fall back to OCF - capex
    ('ocf_minus_capex'); years missing capex too are omitted — absence over a
    fabricated add-back (Law 2). Ascending by period_end.
    """
    def by_period(concept: str) -> dict[str, dict]:
        # period_end is the alignment key: a None slips figures from DIFFERENT
        # fiscal years into one fake OE row (P2 review) — such rows are dropped.
        return {
            r["period_end"]: r
            for r in (series.get(concept) or [])
            if r.get("value") is not None and r.get("period_end")
        }

    ni, da = by_period("net_income"), by_period("depreciation_amortization")
    capex, ocf = by_period("capex"), by_period("operating_cash_flow")

    out: list[dict] = []
    for pe in sorted(set(ni) | set(ocf)):
        if pe in ni and pe in da and pe in capex:
            out.append(
                {
                    "period_end": pe,
                    "owner_earnings": ni[pe]["value"] + da[pe]["value"] - capex[pe]["value"],
                    "basis": "ni_da_capex",
                    "inputs": {
                        "net_income": _prov(ni.get(pe)),
                        "depreciation_amortization": _prov(da.get(pe)),
                        "capex": _prov(capex.get(pe)),
                    },
                }
            )
        elif pe in ocf and pe in capex:
            out.append(
                {
                    "period_end": pe,
                    "owner_earnings": ocf[pe]["value"] - capex[pe]["value"],
                    "basis": "ocf_minus_capex",
                    "inputs": {"operating_cash_flow": _prov(ocf.get(pe)), "capex": _prov(capex.get(pe))},
                }
            )
    return out


def valuation_inputs(pack: dict) -> dict:
    """The engine's base-year inputs from a frozen pack, each with provenance.

    Missing pieces are explicit None + a reason in ``notes`` — the caller decides
    whether a valuation is renderable at all (Law 2: never a filled gap).
    """
    series = pack.get("series") or {}
    notes: list[str] = []

    rev = _latest([r for r in (series.get("revenue") or []) if r.get("value") is not None])
    shares = _latest(
        [r for r in (series.get("shares_diluted") or []) if r.get("value") is not None]
    )
    ni = _latest([r for r in (series.get("net_income") or []) if r.get("value") is not None])
    oe_hist = owner_earnings_series(series)
    oe = oe_hist[-1] if oe_hist else None

    price_block = pack.get("price") or {}
    price, price_date = price_block.get("close"), price_block.get("date")

    if rev is None:
        notes.append("revenue: no filed annual figure")
    if shares is None:
        notes.append("shares_diluted: no filed annual figure")
    if oe is None:
        notes.append("owner_earnings: no year has (NI, D&A, capex) or (OCF, capex) aligned")
    if price is None:
        notes.append("price: unavailable")
    if rev is not None and oe is not None and rev["period_end"] != oe["period_end"]:
        notes.append(
            f"latest revenue ({rev['period_end']}) and owner earnings "
            f"({oe['period_end']}) are different fiscal years"
        )

    oe_margin = None
    if rev is not None and oe is not None and rev["value"]:
        oe_margin = oe["owner_earnings"] / rev["value"]

    return {
        "symbol": pack.get("symbol"),
        "revenue_0": _prov(rev),
        # shares_diluted 'value' IS the split-adjusted figure (quant/splits layer);
        # value_raw would corrupt every per-share number across a split boundary.
        "shares_0": _prov(shares),
        "net_income_0": _prov(ni),
        "owner_earnings_0": oe,
        "owner_earnings_margin_0": oe_margin,  # calibrates the assumed terminal margins
        "owner_earnings_history": oe_hist,
        "price": price,
        "price_date": price_date,
        "notes": notes,
    }


# --------------------------------------------------------------------------- #
# The scenario model (pure math)
# --------------------------------------------------------------------------- #
def scenario_value(
    revenue_0: float,
    shares_0: float,
    scenario: dict,
    horizon_years: float,
    required_return: float,
) -> dict:
    """One scenario through the model (module docstring); returns every step shown.

    All intermediate figures ship in the result — the dossier renders the chain,
    not just the endpoint, so no consequence appears without its premise (Law 4).
    """
    g, m = scenario["revenue_cagr"], scenario["terminal_margin"]
    x, d = scenario["exit_multiple"], scenario["annual_dilution"]
    h, r = horizon_years, required_return

    revenue_h = revenue_0 * (1 + g) ** h
    earnings_h = revenue_h * m
    equity_h = earnings_h * x
    shares_h = shares_0 * (1 + d) ** h
    per_share_future = equity_h / shares_h if shares_h else None
    discount = (1 + r) ** h
    per_share_pv = per_share_future / discount if per_share_future is not None else None
    return {
        "assumptions": dict(scenario),
        "revenue_h": revenue_h,
        "earnings_h": earnings_h,
        "equity_h": equity_h,
        "shares_h": shares_h,
        "per_share_future": per_share_future,
        "per_share_pv": per_share_pv,
    }


def reverse_dcf(
    price: float,
    revenue_0: float,
    shares_0: float,
    base: dict,
    horizon_years: float,
    required_return: float,
) -> dict:
    """The revenue CAGR the CURRENT price implies (closed form), or why not.

    Inverts the scenario identity at base-case margin/multiple/dilution:
        (1+g)^H = price x shares_H x (1+r)^H / (revenue_0 x margin x multiple)
    Solvable only when every factor is positive; a non-positive input (e.g. a
    negative assumed margin) gets an explicit reason instead of a NaN (Law 7).
    """
    m, x, d = base["terminal_margin"], base["exit_multiple"], base["annual_dilution"]
    h, r = horizon_years, required_return
    # d <= -1 turns shares_H negative on an odd horizon and the fractional power
    # below then yields a COMPLEX growth rate (P2 review); h/r guards mirror the
    # grid validator for callers invoking this directly with a hand-built base.
    if (
        not all(v and v > 0 for v in (price, revenue_0, shares_0))
        or m <= 0
        or x <= 0
        or d <= -1
        or h <= 0
        or r <= -1
    ):
        return {
            "implied_revenue_cagr": None,
            "reason": (
                "unsolvable: requires positive price/revenue/shares/margin/multiple, "
                "dilution > -100%, horizon > 0 and required_return > -100%"
            ),
        }
    shares_h = shares_0 * (1 + d) ** h
    growth_factor = (price * shares_h * (1 + r) ** h) / (revenue_0 * m * x)
    return {
        "implied_revenue_cagr": growth_factor ** (1.0 / h) - 1.0,
        "at": {"terminal_margin": m, "exit_multiple": x, "annual_dilution": d,
               "horizon_years": h, "required_return": r},
        "reason": None,
    }


def run_valuation(pack: dict, assumptions: dict | None = None, client=None) -> dict:
    """The full Stage-7 output for a frozen pack: range, sensitivity, MoS, reverse-DCF.

    ``assumptions`` overrides the config grid for this run (it is validated either
    way). Returns explicit ``{"renderable": False, ...}`` when base inputs are
    missing — the dossier then says "not available", never a filled gap (Law 2).
    Deterministic: same pack + same grid = same output, forever.
    """
    grid = assumptions if assumptions is not None else load_assumptions(client)
    validate_assumptions(grid)
    inputs = valuation_inputs(pack)

    result: dict = {"symbol": inputs["symbol"], "inputs": inputs, "assumption_grid": grid}
    rev0 = (inputs["revenue_0"] or {}).get("value")
    shares0 = (inputs["shares_0"] or {}).get("value")
    price = inputs["price"]
    # Strictly positive, not merely truthy: a filed negative/zero revenue or share
    # count is unusable as a growth base (P2 review — `not rev0` let negatives by).
    if not rev0 or rev0 <= 0 or not shares0 or shares0 <= 0:
        result["renderable"] = False
        reasons = list(inputs["notes"])
        if rev0 is not None and rev0 <= 0:
            reasons.append(f"revenue_0 is non-positive ({rev0:,.0f}); no growth base")
        if shares0 is not None and shares0 <= 0:
            reasons.append(f"shares_0 is non-positive ({shares0:,.0f})")
        result["reason"] = "; ".join(reasons) or "missing base inputs"
        return result
    result["renderable"] = True

    h, r = grid["horizon_years"], grid["required_return"]
    scen_grid = grid["scenarios"]

    scenarios = {
        name: scenario_value(rev0, shares0, scen_grid[name], h, r) for name in _SCENARIOS
    }
    result["scenarios"] = scenarios

    # Bear-weighted estimate + margin of safety vs the CURRENT dated price.
    weights = grid["weights"]
    weighted = sum(
        weights[name] * scenarios[name]["per_share_pv"]
        for name in _SCENARIOS
        if scenarios[name]["per_share_pv"] is not None
    )
    result["weighted_value_per_share"] = weighted
    result["margin_of_safety_pct"] = (1.0 - price / weighted) if price and weighted > 0 else None

    # Sensitivity: swing ONE variable bear->bull with the others held at base;
    # the widest spread is the assumption that owns the answer — said out loud.
    sensitivity: dict[str, dict] = {}
    for var in _SCENARIO_VARS:
        lo_s = {**scen_grid["base"], var: scen_grid["bear"][var]}
        hi_s = {**scen_grid["base"], var: scen_grid["bull"][var]}
        lo = scenario_value(rev0, shares0, lo_s, h, r)["per_share_pv"]
        hi = scenario_value(rev0, shares0, hi_s, h, r)["per_share_pv"]
        sensitivity[var] = {
            "bear_setting": lo,
            "bull_setting": hi,
            "spread": abs(hi - lo) if lo is not None and hi is not None else None,
        }
    result["sensitivity"] = sensitivity
    movers = [(v, s["spread"]) for v, s in sensitivity.items() if s["spread"] is not None]
    result["biggest_mover"] = max(movers, key=lambda t: t[1])[0] if movers else None

    # Base-rate check (Law 4): sustained high growth is historically rare; any
    # scenario leaning on it carries the flag next to its output.
    threshold = grid["base_rate_cagr_flag"]
    result["base_rate_flags"] = [
        f"scenario '{name}' assumes {scen_grid[name]['revenue_cagr']:.0%}/yr revenue growth "
        f"for {h:g} years — above the {threshold:.0%} base-rate line (few companies sustain it)"
        for name in _SCENARIOS
        if scen_grid[name]["revenue_cagr"] > threshold
    ]

    result["reverse_dcf"] = (
        reverse_dcf(price, rev0, shares0, scen_grid["base"], h, r)
        if price
        else {"implied_revenue_cagr": None, "reason": "no current price in pack"}
    )

    # Trailing multiples for calibration (current dated price over latest-FY figures).
    oe0 = (inputs["owner_earnings_0"] or {}).get("owner_earnings")
    ni0 = (inputs["net_income_0"] or {}).get("value")
    result["current_multiples"] = {
        "price_to_owner_earnings": (price * shares0 / oe0) if price and oe0 and oe0 > 0 else None,
        "pe_trailing": (price * shares0 / ni0) if price and ni0 and ni0 > 0 else None,
        "note": "current price over latest fiscal-year figures (dated in inputs)",
    }
    return result


# --------------------------------------------------------------------------- #
# Probe
# --------------------------------------------------------------------------- #
def _print_valuation(v: dict) -> None:
    """The GATE evidence table: grid, scenarios, sensitivity, MoS, reverse-DCF."""
    inp = v["inputs"]
    print(f"[valuation] {v['symbol']}  renderable={v['renderable']}")
    if not v["renderable"]:
        print(f"  reason: {v['reason']}")
        return
    rev0, sh0 = inp["revenue_0"], inp["shares_0"]
    oe = inp["owner_earnings_0"]
    print(f"  revenue_0: {rev0['value']:,.0f}  (FY end {rev0['period_end']}, accn {rev0['accn']})")
    print(f"  shares_0 (split-adj diluted): {sh0['value']:,.0f}  (FY end {sh0['period_end']})")
    print(
        f"  owner_earnings_0: {oe['owner_earnings']:,.0f}  basis={oe['basis']}"
        f"  (FY end {oe['period_end']}); OE margin {inp['owner_earnings_margin_0']:.2%}"
    )
    print(f"  price: {inp['price']} ({inp['price_date']})")
    for n in inp["notes"]:
        print(f"  note: {n}")
    grid = v["assumption_grid"]
    print(f"  grid: horizon={grid['horizon_years']}y r={grid['required_return']:.0%} "
          f"weights={grid['weights']}")
    print(f"  {'':<6} {'cagr':>6} {'margin':>7} {'mult':>5} {'dilut':>6}"
          f" {'rev_H':>12} {'earn_H':>11} {'PV/share':>9}")
    for name in _SCENARIOS:
        s = v["scenarios"][name]
        a = s["assumptions"]
        print(
            f"  {name:<6} {a['revenue_cagr']:>6.1%} {a['terminal_margin']:>7.1%}"
            f" {a['exit_multiple']:>5.1f} {a['annual_dilution']:>6.2%}"
            f" {s['revenue_h']:>12,.0f} {s['earnings_h']:>11,.0f} {s['per_share_pv']:>9.2f}"
        )
    print(f"  weighted value/share: {v['weighted_value_per_share']:.2f}"
          f"  -> margin of safety vs price: "
          + (f"{v['margin_of_safety_pct']:.1%}" if v['margin_of_safety_pct'] is not None else "n/a"))
    print("  sensitivity (base, one var swung bear->bull):")
    for var, s in v["sensitivity"].items():
        mark = "  <-- biggest mover" if var == v["biggest_mover"] else ""
        print(f"    {var:<16} {s['bear_setting']:>8.2f} .. {s['bull_setting']:>8.2f}"
              f"  spread {s['spread']:>8.2f}{mark}")
    for flag in v["base_rate_flags"]:
        print(f"  BASE-RATE FLAG: {flag}")
    rd = v["reverse_dcf"]
    if rd["implied_revenue_cagr"] is not None:
        print(f"  reverse-DCF: current price implies {rd['implied_revenue_cagr']:.1%}/yr revenue "
              f"growth at base margin/multiple/dilution")
    else:
        print(f"  reverse-DCF: {rd['reason']}")
    cm = v["current_multiples"]
    pe = f"{cm['pe_trailing']:.1f}" if cm["pe_trailing"] else "n/a"
    poe = f"{cm['price_to_owner_earnings']:.1f}" if cm["price_to_owner_earnings"] else "n/a"
    print(f"  current multiples: P/E {pe}, P/OE {poe}  ({cm['note']})")


if __name__ == "__main__":
    import sys

    from analyst.data_pack import build_data_pack  # probe-only import (analyst layers on quant)

    symbol_arg = sys.argv[1] if len(sys.argv) > 1 else "TSLA"
    _print_valuation(run_valuation(build_data_pack(symbol_arg)))
