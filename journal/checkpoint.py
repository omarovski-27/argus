"""Argus journal — checkpoint engine (Phase 2; blueprint §8 / §9).

Reads the paired sleeve ledger (``round_trips``), the latest sleeve price (``prices_eod``),
and the PRE-REGISTERED gates (``config.kill_criteria``), and reports the checkpoint state:
trade count, cumulative dollar P&L, the sleeve-only Δshares view (one conversion at read
time), the next gate and trades-to-it, and — when the trade count lands on a gate — that
gate's verdict.

It REPORTS, it never ACTS (Law 1). Halting the sleeve, or bumping it to Phase B/C, stays
Omar's decision. This engine never touches the allocation, never emits buy/sell/sizing
language — it surfaces the pre-registered verdict and the numbers behind it, nothing more.

The gates are pre-registered in ``config`` before trade #1 (Law 6 — pre-register, never
reinterpret). This engine reads them from ``config.kill_criteria`` at runtime; it never
hardcodes the 10/20/50 counts or the thresholds. The locked §9 values:
  • early_warning — trade 10, Δshares < −1.0 → pause & examine
  • checkpoint    — trade 20, Δshares < 0    → halt & review   (pass → Phase B eligible)
  • verdict       — trade 50, Δshares < 0    → permanent stop  (pass → Phase C discussion)

The metric→gate mapping (the crux — why a price-dependent metric still yields an
immutable verdict, Law 6):
  • Sign gates (threshold 0: checkpoint, verdict). Δshares = cumulative_pnl ÷ price, and a
    positive price never flips a sign, so "Δshares < 0" ≡ "cumulative_pnl < 0". These are
    evaluated on the dollar sign alone — price-independent and rock-stable.
  • Magnitude gate (threshold ≠ 0: early_warning). "Δshares < −1.0" needs a price. To keep
    the verdict immutable as the market drifts day to day, it is evaluated ONCE at a FIXED
    price — the rebuy price of the gate's own trip (trip #10's ``rebuy_px``), which is
    right there in the row. The live proximity display may use the current price; the gate
    VERDICT always uses the fixed one.

The cumulative P&L for a gate is summed over the FIRST N trips (N = the gate's trade count),
ordered by (date, id). round_trips rows are append-only (Law 6), so the first N never change
once reached — a gate verdict, once landed, is history and re-runs reproduce it exactly.

Run:  python -m journal.checkpoint   (or: python journal/checkpoint.py)
"""

from __future__ import annotations

if __name__ == "__main__" and __package__ in (None, ""):
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import time
import uuid

from shared.db import get_client
from shared.fetch_logger import write_fetch_log

# The sleeve is TSLA-only (§8): the round-trip strategy trades a single ticker, so the
# Δshares view divides the cumulative dollar P&L by one price — TSLA's latest close.
_SLEEVE_SYMBOL = "TSLA"

# Sample size below which any win-rate read is statistically meaningless (§9): "10–20
# trades = early signal only; ~40–50 needed before win-rate claims mean anything."
_SIGNIFICANCE_N = 40

# Verdict vocabulary per gate role (§2c / §9). (verdict_word, human action text). The
# engine SURFACES these; it never executes them — pausing / halting / Phase-B-or-C is
# Omar's call (Law 1). Keyed by the config.kill_criteria role names.
_ROLE_LABELS: dict[str, dict[str, object]] = {
    "early_warning": {
        "name": "early warning",
        "breach": ("pause", "pause & examine"),
        "pass": ("continue", "continue"),
    },
    "checkpoint": {
        "name": "checkpoint",
        "breach": ("halt", "mandatory halt & review"),
        "pass": ("pass", "pass → Phase B eligible (30–40%)"),
    },
    "verdict": {
        "name": "verdict",
        "breach": ("stop", "permanent stop — sleeve rejoins core"),
        "pass": ("pass", "pass → Phase C discussion"),
    },
}


# --------------------------------------------------------------------------- #
# Pure checkpoint logic (no DB / no network — unit-tested in tests/test_checkpoint.py)
# --------------------------------------------------------------------------- #
def _pnl(trip: dict) -> float:
    """A trip's stored dollar P&L, treating a missing/None value as 0.0."""
    return float(trip.get("pnl_usd") or 0.0)


def _ordered(round_trips: list[dict]) -> list[dict]:
    """Trips in trade order: by (date, id). Insertion index breaks a missing id.

    The trade number ("trade #10") is the 1-based position in this order. Stable because
    round_trips are append-only and pairing inserts in (date, exec_time) order, so id
    ascending tracks chronology.
    """
    return [
        t
        for _, t in sorted(
            enumerate(round_trips), key=lambda p: (p[1].get("date"), p[1].get("id", p[0]))
        )
    ]


def _parse_gates(kill_criteria: dict) -> list[dict]:
    """Parse ``config.kill_criteria`` into gate specs sorted by trade count.

    Each spec: {role, trade, threshold}. Only roles the engine knows how to render a
    verdict for (the §9 three) are kept; an unknown role is ignored rather than guessed.
    """
    specs = []
    for role, body in (kill_criteria or {}).items():
        if role not in _ROLE_LABELS:
            continue
        specs.append(
            {
                "role": role,
                "trade": int(body["trade"]),
                "threshold": float(body["delta_shares_lt"]),
            }
        )
    specs.sort(key=lambda s: s["trade"])
    return specs


def _evaluate_gate(spec: dict, ordered: list[dict]) -> dict:
    """Evaluate one gate against the ordered ledger → its immutable verdict (or unreached).

    Sign gate (threshold == 0): breached when cumulative P&L over the first N trips < 0 —
    price-independent. Magnitude gate (threshold != 0): breached when cumulative ÷ FIXED
    price < threshold, where the fixed price is the gate trip's own ``rebuy_px`` (Law 6:
    the verdict cannot flip as the live price drifts).
    """
    trade_n, threshold, role = spec["trade"], spec["threshold"], spec["role"]
    labels = _ROLE_LABELS[role]
    out: dict = {
        "role": role,
        "name": labels["name"],
        "trade": trade_n,
        "threshold_delta_shares": threshold,
        "price_independent": threshold == 0,
        "reached": len(ordered) >= trade_n,
        "cumulative_pnl_at_gate": None,
        "metric_delta_shares": None,  # fixed-price share view; only set for magnitude gates
        "price_used": None,
        "breached": None,
        "verdict": None,  # 'pause' | 'halt' | 'stop' | 'continue' | 'pass'
        "action": None,
    }
    if not out["reached"]:
        return out

    cum = sum(_pnl(t) for t in ordered[:trade_n])
    out["cumulative_pnl_at_gate"] = cum

    if threshold == 0:
        # Sign gate — the share view is sign-equivalent to the dollar sign, so dividing by
        # a price would add no information and a spurious price dependency. Decide on sign.
        breached = cum < 0
    else:
        # Magnitude gate — evaluate ONCE at the fixed price (the gate trip's rebuy_px).
        fixed_price = ordered[trade_n - 1].get("rebuy_px")
        if not fixed_price:  # 0 / None → cannot convert; leave unevaluated (surfaced as None)
            return out
        delta = cum / fixed_price
        out["metric_delta_shares"] = delta
        out["price_used"] = fixed_price
        breached = delta < threshold

    out["breached"] = breached
    out["verdict"], out["action"] = labels["breach"] if breached else labels["pass"]
    return out


def _statistical_note(count: int) -> str:
    """The §9 honesty line — small samples are early signal only, never a verdict on skill."""
    if count < _SIGNIFICANCE_N:
        return (
            f"{count} trades — early signal only; ~40–50 needed before win-rate claims "
            "mean anything."
        )
    return (
        f"{count} trades — approaching a meaningful sample; win-rate reads start to carry "
        "weight near 50."
    )


def _proximity_text(count: int, next_gate: dict | None, live_delta: float | None) -> str:
    """The push-style proximity line, e.g. 'Trade 18 of 20 — checkpoint in 2. Sleeve Δshares: +0.31.'"""
    if next_gate is None:
        head = f"Trade {count} — past the final gate (50)."
    else:
        head = (
            f"Trade {count} of {next_gate['trade']} — "
            f"{next_gate['name']} in {next_gate['trades_to_go']}."
        )
    tail = (
        " Sleeve Δshares: n/a (no current price)."
        if live_delta is None
        else f" Sleeve Δshares: {live_delta:+.2f}."
    )
    return head + tail


def checkpoint_state(
    round_trips: list[dict], kill_criteria: dict, current_price: float | None
) -> dict:
    """Compute the full checkpoint state (pure; no I/O).

    Args:
        round_trips: ``round_trips`` rows (need ``date``, ``pnl_usd``, ``rebuy_px``, and
            ideally ``id`` for ordering). Read in full — the count and cumulative P&L are
            the whole ledger; per-gate cumulatives are the first N.
        kill_criteria: the ``config.kill_criteria`` payload (pre-registered gates, §9).
        current_price: latest sleeve close for the LIVE Δshares view. None ⇒ Δshares is
            reported as None, but gate verdicts (sign + fixed-price) are UNAFFECTED.

    Returns:
        A state dict: trade_count, cumulative_pnl_usd, current_price, delta_shares (live),
        next_gate {role,name,trade,trades_to_go} | None, at_gate (the gate whose trade ==
        count, i.e. a verdict just landed) | None, gates (every gate with its verdict or
        unreached flag), statistical_note, proximity_text.
    """
    ordered = _ordered(round_trips)
    count = len(ordered)
    cumulative = sum(_pnl(t) for t in ordered)
    live_delta = (cumulative / current_price) if current_price else None

    specs = _parse_gates(kill_criteria)
    gates = [_evaluate_gate(s, ordered) for s in specs]
    at_gate = next((g for g in gates if g["trade"] == count), None)

    next_spec = next((s for s in specs if s["trade"] > count), None)
    next_gate = (
        {
            "role": next_spec["role"],
            "name": _ROLE_LABELS[next_spec["role"]]["name"],
            "trade": next_spec["trade"],
            "trades_to_go": next_spec["trade"] - count,
        }
        if next_spec
        else None
    )

    return {
        "trade_count": count,
        "cumulative_pnl_usd": cumulative,
        "current_price": current_price,
        "delta_shares": live_delta,
        "next_gate": next_gate,
        "at_gate": at_gate,
        "gates": gates,
        "statistical_note": _statistical_note(count),
        "proximity_text": _proximity_text(count, next_gate, live_delta),
    }


# --------------------------------------------------------------------------- #
# DB runner (wrapped + logged; mirrors journal.pairing conventions)
# --------------------------------------------------------------------------- #
def _elapsed_ms(start: float) -> int:
    """Whole milliseconds since a ``time.monotonic()`` reading (for fetch_log)."""
    return int((time.monotonic() - start) * 1000)


def _latest_close(client) -> float | None:
    """Latest sleeve (TSLA) close from prices_eod, or None if no price row exists."""
    rows = (
        client.table("prices_eod")
        .select("close,date")
        .eq("symbol", _SLEEVE_SYMBOL)
        .order("date", desc=True)
        .limit(1)
        .execute()
        .data
    ) or []
    return float(rows[0]["close"]) if rows and rows[0].get("close") is not None else None


def run_checkpoint(run_id: str) -> dict:
    """Assemble inputs from the spine and compute the checkpoint state.

    Wrapped + logged (Law 7): writes one ``fetch_log`` row under ``journal:checkpoint`` and,
    on failure, re-raises so a scheduled job fails loud. Read-only on the allocation — it
    computes and reports; the Telegram push + fire-once dedup live in Step 2b, /journal in
    Step 5. Both consume this state.

    Returns the checkpoint state dict (also printed for the manual run).
    """
    start = time.monotonic()
    try:
        client = get_client()
        round_trips = (
            client.table("round_trips")
            .select("id,date,pnl_usd,rebuy_px")
            .order("date")
            .order("id")
            .execute()
            .data
        ) or []
        kc_rows = (
            client.table("config").select("value").eq("key", "kill_criteria").execute().data
        ) or []
        kill_criteria = kc_rows[0]["value"] if kc_rows else {}
        current_price = _latest_close(client)

        state = checkpoint_state(round_trips, kill_criteria, current_price)

        write_fetch_log("journal:checkpoint", run_id, "success", _elapsed_ms(start))
        _print_state(state)
        return state
    except Exception as exc:  # noqa: BLE001 — surface, never swallow (Law 7)
        write_fetch_log("journal:checkpoint", run_id, "failure", _elapsed_ms(start), str(exc))
        print(f"[checkpoint] FAILED — {exc}")
        raise


def _print_state(state: dict) -> None:
    """Human-readable dump of the checkpoint state for the manual run."""
    print(f"[checkpoint] {state['proximity_text']}")
    print(f"[checkpoint] {state['statistical_note']}")
    cum = state["cumulative_pnl_usd"]
    px = state["current_price"]
    print(
        f"[checkpoint] cumulative P&L ${cum:+.2f} over {state['trade_count']} trip(s)"
        + (f" @ {_SLEEVE_SYMBOL} {px:.2f}" if px is not None else f" ({_SLEEVE_SYMBOL} price n/a)")
    )
    if state["at_gate"]:
        g = state["at_gate"]
        print(f"[checkpoint] >>> GATE {g['trade']} ({g['name']}) VERDICT: {g['verdict']} — {g['action']}")
    for g in state["gates"]:
        if not g["reached"]:
            print(f"[checkpoint]   - {g['name']} (trade {g['trade']}): not yet reached")
            continue
        extra = (
            f"Δshares {g['metric_delta_shares']:+.3f} @ fixed {g['price_used']:.2f}"
            if g["metric_delta_shares"] is not None
            else f"cumulative ${g['cumulative_pnl_at_gate']:+.2f} (sign gate)"
        )
        print(
            f"[checkpoint]   - {g['name']} (trade {g['trade']}): {g['verdict']} — {extra}"
        )


if __name__ == "__main__":
    import sys

    # The proximity line carries 'Δ' / '→'; force UTF-8 stdout so a Windows cp1252 console
    # prints them instead of raising (the Telegram/DB consumers always see proper UTF-8).
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 — best-effort; ASCII-only terminals still run fine
        pass
    run_checkpoint(f"manual-checkpoint-{uuid.uuid4().hex[:12]}")
