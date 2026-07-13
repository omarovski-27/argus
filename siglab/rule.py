"""Argus Signal Lab — the pre-registered rule evaluator (pure, deterministic).

Rule v1 (registered wording, `config.signal_v1.rule`):
    FAVORABLE iff  TSLA close > SMA50
              AND  MACD histogram > the previous day's MACD histogram
              AND  the event filter is clear for the next session
              AND  VIX percentile < 80
    else UNFAVORABLE.

Timing: the signal is computed at the CLOSE of day D-1 (all four inputs are D-1 state,
except the event-filter check which looks at day D's calendar) and then SCORED against
day D (``signal.shadow``). Editing the rule wording is allowed only BEFORE registration;
after registration an edit creates ``signal_v2`` with a fresh ledger, never a mutation of
the v1 record (Law 6).

Fail-loud (Law 7): a missing input is never silently treated as False — that would
fabricate an UNFAVORABLE reading. ``evaluate_signal`` raises ``MissingInputError`` and the
caller logs ``signal:inputs_missing`` and writes an ``unknown`` ledger outcome instead.
"""

from __future__ import annotations

from dataclasses import dataclass


class MissingInputError(RuntimeError):
    """A rule input was absent — the signal cannot be evaluated (never defaulted)."""


@dataclass(frozen=True)
class SignalInputs:
    """The six inputs Rule v1 needs, all as of close D-1 (event_clear looks at day D)."""

    close: float | None            # TSLA close, D-1
    sma50: float | None            # TSLA SMA50, D-1
    macd_hist: float | None        # TSLA MACD histogram, D-1
    macd_hist_prev: float | None   # TSLA MACD histogram, D-2
    event_clear: bool | None       # True iff NO arming event on day D (§8)
    vix_percentile: float | None   # VIX percentile within its trailing window, D-1

    def missing(self) -> list[str]:
        """Names of any absent inputs (empty list = fully specified)."""
        return [
            name for name, value in (
                ("close", self.close), ("sma50", self.sma50),
                ("macd_hist", self.macd_hist), ("macd_hist_prev", self.macd_hist_prev),
                ("event_clear", self.event_clear), ("vix_percentile", self.vix_percentile),
            ) if value is None
        ]


def evaluate_signal(inputs: SignalInputs, vix_percentile_max: float = 80.0) -> dict:
    """Evaluate Rule v1. Returns {state, conditions{...}}; raises on any missing input.

    ``conditions`` records each leg's boolean so the ledger's ``inputs_json`` shows WHY a
    day was FAVORABLE/UNFAVORABLE (transparency; a stored row reproduces the verdict).
    """
    absent = inputs.missing()
    if absent:
        raise MissingInputError(f"signal inputs missing: {', '.join(absent)}")

    conditions = {
        "close_above_sma50": inputs.close > inputs.sma50,
        "macd_hist_rising": inputs.macd_hist > inputs.macd_hist_prev,
        "event_filter_clear": bool(inputs.event_clear),
        "vix_percentile_below_max": inputs.vix_percentile < vix_percentile_max,
    }
    state = "FAVORABLE" if all(conditions.values()) else "UNFAVORABLE"
    return {"state": state, "conditions": conditions}
