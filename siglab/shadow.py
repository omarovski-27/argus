"""Argus Signal Lab — the pre-registered SHADOW scorer (pure, deterministic, conservative).

For a FAVORABLE day D, simulate the strategy's mechanical bracket from day D's EOD OHLC —
no real order is ever placed. The scoring rule is pre-registered and deliberately
CONSERVATIVE because EOD data cannot reveal intraday order:

    entry at day-D open; target = open + bracket; stop = open - bracket
    * high >= target AND low <= stop (both bands touched)  -> LOSS  (can't know which
      fired first intraday, so assume the worse — the honest conservative read)
    * only high >= target                                  -> WIN
    * only low <= stop                                     -> LOSS
    * neither band touched                                 -> NO_TRIGGER (no trade)

EOD-DATA LIMITATION (stated in docs): a real intraday fill sequence could turn some
"both-bands" losses into wins; the shadow record is therefore a LOWER bound on the rule's
performance, never flattering it. Shadow P&L uses the strategy's real fee model, pinned in
the registered config (``shares`` × ``bracket`` ∓ ``fee_per_round_trip``) so the ledger's
dollar series is immutable and reproduces the blueprint's stated +$23.50 / -$27.50 unit
economics exactly (17 shares × $1.50 ∓ $2.00).
"""

from __future__ import annotations

from siglab.rule import MissingInputError

# The four scored outcomes (ledger CHECK vocabulary). ``unknown`` (a FAVORABLE day whose
# day-D OHLC is missing) is assigned by the caller, not here — this scorer needs OHLC.
WIN, LOSS, NO_TRIGGER = "win", "loss", "no_trigger"


def score_bracket(
    open_: float | None,
    high: float | None,
    low: float | None,
    *,
    bracket: float,
    shares: float,
    fee_per_round_trip: float,
) -> tuple[str, float]:
    """(outcome, shadow_pnl) for a FAVORABLE day's mechanical bracket. Raises on missing OHLC.

    Pure. ``outcome`` ∈ {win, loss, no_trigger}; a no_trigger books zero P&L (no trade).
    """
    if open_ is None or high is None or low is None:
        raise MissingInputError("shadow scoring needs day-D open/high/low")
    target = open_ + bracket
    stop = open_ - bracket
    hit_target = high >= target
    hit_stop = low <= stop

    if hit_target and hit_stop:
        outcome = LOSS          # both bands touched → conservative loss
    elif hit_target:
        outcome = WIN
    elif hit_stop:
        outcome = LOSS
    else:
        outcome = NO_TRIGGER

    gross = bracket * shares
    if outcome == WIN:
        pnl = gross - fee_per_round_trip
    elif outcome == LOSS:
        pnl = -gross - fee_per_round_trip
    else:
        pnl = 0.0
    return outcome, pnl
