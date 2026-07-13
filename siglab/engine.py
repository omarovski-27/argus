"""Argus Signal Lab — the pure computation engine over stored series (backfill + nightly).

``build_ledger_rows`` turns already-fetched stored data (TSLA OHLC, indicators, VIX, the
arming-event dates) into ledger rows deterministically — no DB, no network — so it drives
BOTH the retrospective backfill and the live nightly evaluation, and a stored (data →
ledger) mapping reproduces forever (Law 2). ``job`` is the thin DB wrapper around it.

Warmup vs fail-loud: a day whose rule inputs are not yet computable (SMA50 needs 50
sessions, MACD needs ~34, the VIX window needs history) is a genuine WARMUP skip — the
signal literally does not exist yet — and the engine reports the count it skipped. That is
distinct from the LIVE nightly job's fail-loud path, where a mature day with a missing
input is an ``unknown`` ledger outcome + a ``signal:inputs_missing`` log (Law 7).

BACKFILL EVENT-FILTER CAVEAT (stated honestly): the event-filter leg reads
``calendar_events``, which is forward-looking, so historical macro events not seeded there
are treated as "clear" in backfill — this can only make a backfilled day MORE likely
FAVORABLE than live, so the retrospective record is, if anything, generous on that leg.
Live nightly scoring checks the real forward calendar for day D.
"""

from __future__ import annotations

from siglab.rule import SignalInputs, evaluate_signal
from siglab.shadow import score_bracket


def vix_percentile_asof(
    vix_asc: list[dict], asof_date: str, window_sessions: int
) -> float | None:
    """Percentile of the latest VIX close (<= ``asof_date``) within its trailing window.

    Matches ``digest.bundle._vix_trailing`` exactly: percentile = count(v <= current) / N
    over the last ``window_sessions`` sessions up to and including ``asof_date``. None when
    no VIX history is available at that date."""
    prior = [
        (str(r.get("date")), r.get("value"))
        for r in vix_asc
        if r.get("value") is not None and str(r.get("date")) <= str(asof_date)
    ]
    if not prior:
        return None
    window = prior[-int(window_sessions):]
    current = window[-1][1]
    at_or_below = sum(1 for _, v in window if v <= current)
    return round(at_or_below / len(window) * 100)


def _inputs_for_day(
    prices: list[dict], i: int, indicators_by_date: dict, vix_asc: list[dict],
    arming_dates: set[str], params: dict,
) -> tuple[SignalInputs, str]:
    """Build the D-1-sourced inputs for scoring day D = prices[i] (i >= 1)."""
    d1, d = prices[i - 1], prices[i]
    d1_date = str(d1.get("date"))
    ind1 = indicators_by_date.get(d1_date) or {}
    ind2 = indicators_by_date.get(str(prices[i - 2].get("date"))) if i >= 2 else {}
    vix_pctile = vix_percentile_asof(vix_asc, d1_date, params.get("vix_window_sessions", 252))
    inputs = SignalInputs(
        close=d1.get("close"),
        sma50=ind1.get("sma50"),
        macd_hist=ind1.get("macd_hist"),
        macd_hist_prev=(ind2 or {}).get("macd_hist"),
        event_clear=(str(d.get("date")) not in arming_dates),
        vix_percentile=vix_pctile,
    )
    return inputs, str(d.get("date"))


def build_ledger_rows(
    prices: list[dict],
    indicators_by_date: dict,
    vix_asc: list[dict],
    arming_dates: set[str],
    params: dict,
) -> tuple[list[dict], int]:
    """(rows, warmup_skipped). ``prices`` = TSLA OHLC ascending; one row per computable day.

    Each row: {date (=D), signal_state, outcome, shadow_pnl, inputs_json}. FAVORABLE days
    are shadow-scored on day D's OHLC (outcome 'unknown' + None P&L if D OHLC is absent);
    UNFAVORABLE days log state only (outcome 'no_trigger', P&L 0)."""
    prices = sorted(prices, key=lambda r: str(r.get("date")))
    vix_asc = sorted(vix_asc, key=lambda r: str(r.get("date")))
    vix_max = params.get("vix_percentile_max", 80)
    bracket = params.get("bracket", 1.50)
    shares = params.get("shadow_shares", 17)
    fee = params.get("fee_per_round_trip", 2.00)

    rows: list[dict] = []
    warmup = 0
    for i in range(1, len(prices)):
        inputs, day = _inputs_for_day(prices, i, indicators_by_date, vix_asc, arming_dates, params)
        if inputs.missing():
            warmup += 1
            continue
        verdict = evaluate_signal(inputs, vix_percentile_max=vix_max)
        state = verdict["state"]
        d = prices[i]
        if state == "FAVORABLE":
            o, h, low = d.get("open"), d.get("high"), d.get("low")
            if o is None or h is None or low is None:
                outcome, pnl = "unknown", None
            else:
                outcome, pnl = score_bracket(
                    o, h, low, bracket=bracket, shares=shares, fee_per_round_trip=fee
                )
        else:
            outcome, pnl = "no_trigger", 0.0
        rows.append({
            "date": day,
            "signal_state": state,
            "outcome": outcome,
            "shadow_pnl": pnl,
            "inputs_json": {
                "conditions": verdict["conditions"],
                "close": inputs.close, "sma50": inputs.sma50,
                "macd_hist": inputs.macd_hist, "macd_hist_prev": inputs.macd_hist_prev,
                "event_clear": inputs.event_clear, "vix_percentile": inputs.vix_percentile,
            },
        })
    return rows, warmup
