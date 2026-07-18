"""Argus Signal Lab — the pure computation engine over stored series (backfill + nightly).

``build_ledger_rows`` turns already-fetched stored data (TSLA OHLC, indicators, VIX, the
arming-event dates) into ledger rows deterministically — no DB, no network — so it drives
BOTH the retrospective backfill and the live nightly evaluation, and a stored (data →
ledger) mapping reproduces forever (Law 2). ``job`` is the thin DB wrapper around it.

FORWARD-ONLY STATE LOGGING (v1 finalized INCONCLUSIVE, 2026-07-18): the engine logs each
computable day's FAVORABLE/UNFAVORABLE state + its inputs, but NO LONGER shadow-scores an
outcome — ``outcome`` stays ``'unknown'`` and ``shadow_pnl`` NULL. The shadow scorer was
retired because it could not test the rule at daily resolution: a $1.50 bracket on a ~$400
stock has both bands touched on ~74% of triggered days, so the record measured TSLA's daily
range, not the signal (see ``registry.SIGNAL_V1_STATUS_REASON``). The real ledger is now the
actual journal round-trips once a sleeve exists (``registry.SIGNAL_V1_PROMOTION_PATH``). The
band scorer (``siglab.shadow``) is kept for reference / a future instrument-scaled signal_v2
but is no longer wired here — v1 never fabricates a win/loss again.

Warmup: a day whose rule inputs are not yet computable (SMA50 needs 50 sessions, MACD needs
~34, the VIX window needs history) is a genuine WARMUP skip — the signal literally does not
exist yet — and the engine reports the count it skipped. The LIVE nightly job's fail-loud
path still applies: a mature day that yields no row is a ``signal:inputs_missing`` log (L7).

BACKFILL EVENT-FILTER CAVEAT (stated honestly): the event-filter leg reads
``calendar_events``, which is forward-looking, so historical macro events not seeded there
are treated as "clear" in backfill — this can only make a backfilled day MORE likely
FAVORABLE than live. Live nightly scoring checks the real forward calendar for day D.
"""

from __future__ import annotations

from siglab.rule import SignalInputs, evaluate_signal


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

    FORWARD-ONLY (v1 INCONCLUSIVE): each row is {date (=D), signal_state, outcome, shadow_pnl,
    inputs_json} — ``signal_state`` is the computed FAVORABLE/UNFAVORABLE, and ``outcome`` is
    always ``'unknown'`` with NULL ``shadow_pnl``. The engine records STATE + INPUTS only; it
    no longer shadow-scores a win/loss (day-D OHLC is not consulted), because the daily-bar
    scorer could not measure the rule (see the module docstring). ``inputs_json`` still carries
    each rule leg's boolean so a stored row explains WHY the day was FAVORABLE/UNFAVORABLE."""
    prices = sorted(prices, key=lambda r: str(r.get("date")))
    vix_asc = sorted(vix_asc, key=lambda r: str(r.get("date")))
    vix_max = params.get("vix_percentile_max", 80)

    rows: list[dict] = []
    warmup = 0
    for i in range(1, len(prices)):
        inputs, day = _inputs_for_day(prices, i, indicators_by_date, vix_asc, arming_dates, params)
        if inputs.missing():
            warmup += 1
            continue
        verdict = evaluate_signal(inputs, vix_percentile_max=vix_max)
        rows.append({
            "date": day,
            "signal_state": verdict["state"],
            # Forward-only: NO shadow scoring. Outcome stays 'unknown', P&L NULL — the state
            # is logged, but v1 never fabricates a win/loss (the measurement was invalid).
            "outcome": "unknown",
            "shadow_pnl": None,
            "inputs_json": {
                "conditions": verdict["conditions"],
                "close": inputs.close, "sma50": inputs.sma50,
                "macd_hist": inputs.macd_hist, "macd_hist_prev": inputs.macd_hist_prev,
                "event_clear": inputs.event_clear, "vix_percentile": inputs.vix_percentile,
            },
        })
    return rows, warmup
