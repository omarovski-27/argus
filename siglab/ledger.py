"""Argus Signal Lab — ledger stats + the monotonic verdict gates (pure).

The gates mirror the journal's design (Law 6): pre-registered in the immutable blob, never
reinterpreted post-hoc. N counts FAVORABLE days that TRIGGERED a scored outcome (win +
loss) — no_trigger and UNFAVORABLE days are logged but do not advance N, because only
win/loss observations inform an edge.

  * N=30 RETIRE gate: when the 30th trigger lands, if winrate < 0.55 OR cumulative shadow
    P&L (through that point) <= 0 → status RETIRED, terminal. It is evaluated on the FIXED
    first-30-trigger prefix, so once RETIRED it can never un-retire as more data arrives
    (the "renders permanently" requirement).
  * N=60 PASS gate: at the 60th trigger, if winrate >= 0.58 AND cumulative P&L > fee×60×0.5
    → status PASS, terminal — which UNLOCKS (never executes) a promotion decision only Omar
    records. If instead the record has decayed below the retire bar at 60, it RETIRES.

Everything here is a pure function of the ledger rows, so a stored ledger reproduces its
verdict forever (Law 2).
"""

from __future__ import annotations

from siglab.registry import signal_gates, signal_params

_TRIGGERED = ("win", "loss")


def _pnl(row: dict) -> float:
    value = row.get("shadow_pnl")
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _winrate_at(triggered: list[dict], k: int) -> float:
    return sum(1 for r in triggered[:k] if r.get("outcome") == "win") / k


def _pnl_at(triggered: list[dict], k: int) -> float:
    return sum(_pnl(r) for r in triggered[:k])


def derive_status(triggered: list[dict], gates: dict, fee_per_round_trip: float) -> str:
    """testing / RETIRED / PASS from the ordered list of triggered (win/loss) rows.

    Monotonic + terminal: RETIRED is decided on the fixed first-30 prefix (so it never
    flips back); PASS on the first-60 prefix. ``triggered`` MUST be date-ascending."""
    n = len(triggered)
    g30, g60 = gates["n30"], gates["n60"]
    n30, n60 = int(g30["n"]), int(g60["n"])

    if n >= n30:
        if _winrate_at(triggered, n30) < g30["min_winrate"] or _pnl_at(triggered, n30) <= g30["retire_if_pnl_le"]:
            return "RETIRED"
    if n >= n60:
        pass_floor = fee_per_round_trip * n60 * g60["pass_pnl_gt_fee_mult"]
        if _winrate_at(triggered, n60) >= g60["min_winrate"] and _pnl_at(triggered, n60) > pass_floor:
            return "PASS"
        # A rule that survived 30 but decayed below the retire bar by 60 retires.
        if _winrate_at(triggered, n60) < g30["min_winrate"] or _pnl_at(triggered, n60) <= 0:
            return "RETIRED"
    return "testing"


def _evidence_label(status: str, winrate: float | None) -> str:
    """The mandatory plain-language evidence phrase (no advice; a hard render test guards it)."""
    if status == "RETIRED":
        return "RETIRED — failed its gate"
    if status == "PASS":
        return "PASS — cleared its gate; promotion is Omar's alone"
    if winrate is not None and winrate > 0.5:
        return "Tracking above coin-flip"
    return "No evidence of edge yet"


def _next_gate(status: str, n_triggered: int, gates: dict) -> dict | None:
    """The next unreached gate and how many triggers remain (None once terminal)."""
    if status in ("RETIRED", "PASS"):
        return None
    for key in ("n30", "n60"):
        n = int(gates[key]["n"])
        if n_triggered < n:
            return {"gate": key, "n": n, "remaining": n - n_triggered}
    return None


def compute_stats(rows: list[dict], blob: dict) -> dict:
    """Full signal stats from the ledger rows + the registered blob. Pure.

    ``rows`` are ledger dicts {date, signal_state, outcome, shadow_pnl}. The result feeds
    every render surface (``signal.render``) so /today, /signal and the digest agree."""
    params = signal_params(blob)
    gates = signal_gates(blob)
    rows = sorted(rows, key=lambda r: str(r.get("date")))

    triggered = [
        r for r in rows
        if r.get("signal_state") == "FAVORABLE" and r.get("outcome") in _TRIGGERED
    ]
    wins = sum(1 for r in triggered if r.get("outcome") == "win")
    losses = sum(1 for r in triggered if r.get("outcome") == "loss")
    no_trigger = sum(1 for r in rows if r.get("outcome") == "no_trigger")
    unknown = sum(1 for r in rows if r.get("outcome") == "unknown")
    n_triggered = wins + losses
    winrate = (wins / n_triggered) if n_triggered else None
    cum_pnl = sum(_pnl(r) for r in rows)
    fee = float(params.get("fee_per_round_trip", 2.0))
    status = derive_status(triggered, gates, fee)

    return {
        "version": blob.get("version", "v1"),
        "registered_at": blob.get("registered_at"),
        "rule": blob.get("rule"),
        "n_days": len(rows),
        "today_state": rows[-1].get("signal_state") if rows else None,
        "today_date": rows[-1].get("date") if rows else None,
        "wins": wins,
        "losses": losses,
        "no_trigger": no_trigger,
        "unknown": unknown,
        "n_triggered": n_triggered,
        "winrate": winrate,
        "cum_pnl": cum_pnl,
        "status": status,
        "evidence_label": _evidence_label(status, winrate),
        "next_gate": _next_gate(status, n_triggered, gates),
    }
