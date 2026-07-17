"""/today trade-context card tests (bot/handlers.handle_today).

Both cards are DETERMINISTIC DB renders — no LLM, grounding-exempt by construction — but
Law 1 binds hard: they DESCRIBE state and never advise. The load-bearing test is
``test_card_never_advises``: NEITHER the default one-glance card NOR ``/today full`` may
contain good/bad-day, should/could-trade, or buy/sell/enter/exit language. The rest pin
the new default card (conditions / one-word direction / market collapse / young / event /
signal) and the detailed ``/today full`` rendering.

A fake client returns canned rows per table and ignores the filter chain (the handler's
Python-side selection — latest-per-name, arm predicate, VIX percentile — is what's under test).
"""

import re
from datetime import date, timedelta

import pytest

import bot.handlers as handlers
from bot.handlers import (
    _conditions_line,
    _direction,
    _market_line,
    _momentum_phrase,
    _sessions_until,
    _trend_phrase,
    _young_line,
    handle_today,
)

_FULL = {"text": "/today full"}


# --------------------------------------------------------------------------- #
# Fake client — chainable query that ignores filters, returns canned rows/table
# --------------------------------------------------------------------------- #
class _Query:
    """Chainable fake. Applies ``eq(col, val)`` filters (the real server does this
    per-symbol); ignores order/limit/gte/lte/in_/range (the handler's Python-side
    selection is what's under test)."""

    def __init__(self, rows):
        self._rows = rows
        self._eqs: list[tuple[str, object]] = []

    def select(self, *_a, **_k):
        return self

    def eq(self, col, val):
        self._eqs.append((col, val))
        return self

    def in_(self, *_a):
        return self

    def gte(self, *_a):
        return self

    def lte(self, *_a):
        return self

    def order(self, *_a, **_k):
        return self

    def limit(self, *_a):
        return self

    def range(self, *_a):
        return self

    def execute(self):
        rows = [r for r in self._rows if all(r.get(c) == v for c, v in self._eqs)]
        return type("R", (), {"data": rows})()


class _FakeClient:
    def __init__(self, tables):
        self._tables = tables

    def table(self, name):
        return _Query(self._tables.get(name, []))


def _ind(symbol, name, value, d="2026-07-12"):
    return {"symbol": symbol, "date": d, "name": name, "value": value}


def _vix_rows(values, start="2026-07-01"):
    """VIXCLS macro_series rows (ascending dates from ``start``)."""
    base = date.fromisoformat(start)
    return [
        {"series_id": "VIXCLS", "date": (base + timedelta(days=i)).isoformat(), "value": v}
        for i, v in enumerate(values)
    ]


@pytest.fixture()
def cpi_eve(monkeypatch):
    """A CPI-tomorrow world: event within 2 sessions → STORMY + ⛔. Returns (today, tables)."""
    today = date(2026, 7, 13)
    tomorrow = (today + timedelta(days=1)).isoformat()
    tables = {
        "config": [
            {"key": "watchlist", "value": ["TSLA", "SPCX"]},
            {"key": "weekly_trade_cap", "value": 2},
            {"key": "sleeve_symbol", "value": "TSLA"},
            # sleeve_shares deliberately absent → "not yet registered"
        ],
        "prices_eod": [
            {"symbol": "TSLA", "date": "2026-07-12", "close": 407.76},
            {"symbol": "SPCX", "date": "2026-07-12", "close": 180.0},
        ],
        "indicators": [
            _ind("TSLA", "sma50", 330.0), _ind("TSLA", "sma200", 300.0),
            _ind("TSLA", "rsi14", 62.0), _ind("TSLA", "macd", 5.0),
            _ind("TSLA", "macd_signal", 3.0),
            # SPCX young: only sma50, no sma200
            _ind("SPCX", "sma50", 170.0), _ind("SPCX", "rsi14", 44.0),
        ],
        "macro_series": _vix_rows([18.0, 19.0, 17.0, 18.5, 18.0]),
        "calendar_events": [
            {"date": tomorrow, "type": "cpi", "symbol": None, "materiality": "high"},
        ],
        "round_trips": [{"id": 1}],  # 1 this week
    }
    monkeypatch.setattr(handlers, "get_client", lambda: _FakeClient(tables))
    monkeypatch.setattr(handlers, "_utc_today", lambda: today)
    return today, tables


# --------------------------------------------------------------------------- #
# The hard Law-1 exclusion — the load-bearing test (BOTH cards)
# --------------------------------------------------------------------------- #
_BANNED = [
    r"\bbuy\b", r"\bsell\b", r"\benter\b", r"\bexit\b",
    r"\bshould\b", r"\bcould\b", r"\bgood day\b", r"\bbad day\b",
    r"\bsafe to\b", r"\brecommend", r"\ballocate\b",
]


def test_card_never_advises(cpi_eve):
    for msg in ({}, _FULL):  # default one-glance AND /today full
        text = handle_today(msg).lower()
        for pattern in _BANNED:
            assert not re.search(pattern, text), f"card must not advise — matched {pattern!r}"


# --------------------------------------------------------------------------- #
# The default one-glance card
# --------------------------------------------------------------------------- #
def test_default_conditions_stormy_on_near_event(cpi_eve):
    text = handle_today({})
    assert "*Conditions: STORMY*" in text
    assert "blocking event is close" in text


def test_default_tsla_one_word_direction(cpi_eve):
    text = handle_today({})
    # 407.76 above both 330/300 and macd 5 >= signal 3 → UP; no jargon on the default card.
    assert "*TSLA*: pointing UP (above its trend lines, push strengthening)." in text
    assert "momentum score" not in text and "MACD" not in text and "RSI" not in text


def test_default_spcx_young_line(cpi_eve):
    text = handle_today({})
    assert "*SPCX*: too young for trend lines; recent push soft." in text  # rsi 44 < 50


def test_default_event_block_within_two_sessions(cpi_eve):
    text = handle_today({})
    assert "⛔ inflation report (CPI) tomorrow — your rules block sleeve round trips today." in text
    assert "§8" not in text and "armed" not in text and "materiality" not in text


def test_default_signal_line_pending_when_no_ledger(cpi_eve):
    text = handle_today({})
    # No signal_ledger table in the fake → labelled pending line, 🧪 still present.
    assert "🧪 Signal v1: registered 2026-07-13 — no track record yet (backfill pending)." in text


def test_default_drops_boilerplate_and_workings(cpi_eve):
    text = handle_today({})
    assert "Context, not advice" not in text
    assert "weekly cap" not in text          # the cap is a /today full working
    assert "*Watchlist*" not in text         # the per-ticker detail is /today full


def test_default_signal_line_measured_when_ledger_present(monkeypatch):
    today = date(2026, 7, 13)
    tables = {
        "config": [
            {"key": "watchlist", "value": ["TSLA"]},
            {"key": "sleeve_symbol", "value": "TSLA"},
        ],
        "prices_eod": [{"symbol": "TSLA", "date": "2026-07-12", "close": 407.76}],
        "indicators": [_ind("TSLA", "sma50", 330.0), _ind("TSLA", "sma200", 300.0)],
        "macro_series": _vix_rows([18.0, 19.0, 17.0]),
        "calendar_events": [],
        "round_trips": [],
        "signal_ledger": [
            {"signal_version": "v1", "date": "2026-07-10", "signal_state": "FAVORABLE",
             "outcome": "win", "shadow_pnl": 23.50},
            {"signal_version": "v1", "date": "2026-07-11", "signal_state": "FAVORABLE",
             "outcome": "loss", "shadow_pnl": -27.50},
            {"signal_version": "v1", "date": "2026-07-12", "signal_state": "FAVORABLE",
             "outcome": "win", "shadow_pnl": 23.50},
        ],
    }
    monkeypatch.setattr(handlers, "get_client", lambda: _FakeClient(tables))
    monkeypatch.setattr(handlers, "_utc_today", lambda: today)
    text = handle_today({})
    # today_state = last row FAVORABLE; 2 win / 1 loss → 67%, n=3, shadow +$19.50.
    assert "🧪 Signal: FAVORABLE — days like today won 67% historically (n=3, shadow +$19.50)." in text


def test_default_market_overall_collapses_spy_qqq(monkeypatch):
    today = date(2026, 7, 13)
    tables = {
        "config": [
            {"key": "watchlist", "value": ["TSLA", "SPCX", "SPY", "QQQ"]},
            {"key": "sleeve_symbol", "value": "TSLA"},
        ],
        "prices_eod": [
            {"symbol": "TSLA", "date": "2026-07-12", "close": 407.76},
            {"symbol": "SPCX", "date": "2026-07-12", "close": 180.0},
            {"symbol": "SPY", "date": "2026-07-12", "close": 600.0},
            {"symbol": "QQQ", "date": "2026-07-12", "close": 400.0},
        ],
        "indicators": [
            _ind("TSLA", "sma50", 330.0), _ind("TSLA", "sma200", 300.0),
            _ind("SPCX", "sma50", 170.0),
            # SPY UP: above both, macd improving
            _ind("SPY", "sma50", 580.0), _ind("SPY", "sma200", 560.0),
            _ind("SPY", "macd", 2.0), _ind("SPY", "macd_signal", 1.0),
            # QQQ DOWN: below both, macd fading
            _ind("QQQ", "sma50", 420.0), _ind("QQQ", "sma200", 410.0),
            _ind("QQQ", "macd", 1.0), _ind("QQQ", "macd_signal", 3.0),
        ],
        "macro_series": _vix_rows([18.0, 19.0, 17.0]),
        "calendar_events": [],
        "round_trips": [],
    }
    monkeypatch.setattr(handlers, "get_client", lambda: _FakeClient(tables))
    monkeypatch.setattr(handlers, "_utc_today", lambda: today)
    text = handle_today({})
    assert "*Market overall*: pointing up, tech wobbling." in text


def test_default_next_event_when_far_off(monkeypatch):
    today = date(2026, 7, 13)
    tables = {
        "config": [
            {"key": "watchlist", "value": ["TSLA"]},
            {"key": "sleeve_symbol", "value": "TSLA"},
        ],
        "prices_eod": [{"symbol": "TSLA", "date": "2026-07-12", "close": 407.76}],
        "indicators": [_ind("TSLA", "sma50", 330.0), _ind("TSLA", "sma200", 300.0)],
        "macro_series": _vix_rows([18.0, 19.0, 17.0]),
        "calendar_events": [
            {"date": "2026-07-29", "type": "fomc", "symbol": None, "materiality": "high"},
        ],
        "round_trips": [],
    }
    monkeypatch.setattr(handlers, "get_client", lambda: _FakeClient(tables))
    monkeypatch.setattr(handlers, "_utc_today", lambda: today)
    text = handle_today({})
    assert "Next event that matters: Fed decision, 2026-07-29 (16 days)." in text
    assert "⛔" not in text


# --------------------------------------------------------------------------- #
# /today full — the detailed card (the workings)
# --------------------------------------------------------------------------- #
def test_full_cpi_tomorrow_shows_filter_in_effect(cpi_eve):
    text = handle_today(_FULL)
    assert "Event filter (§8)" in text
    assert "IN EFFECT" in text and "CPI" in text
    assert "within 24h" in text and "blocked" in text


def test_full_watchlist_trend_and_momentum_render(cpi_eve):
    text = handle_today(_FULL)
    # TSLA: 407.76 above both 330 and 300.
    assert "trading above its 50-day and 200-day average price" in text
    assert "momentum score 62 (50 is neutral)" in text
    assert "trend momentum improving" in text  # macd 5 >= signal 3


def test_full_young_ticker_has_no_200day(cpi_eve):
    text = handle_today(_FULL)
    # SPCX has sma50 (170) but no sma200; close 180.0 > 170 → above its 50-day only.
    spcx_line = next(ln for ln in text.splitlines() if ln.startswith("• *SPCX*"))
    assert "above its 50-day average price" in spcx_line
    assert "200-day" not in spcx_line


def test_full_weekly_cap_and_sleeve_status(cpi_eve):
    text = handle_today(_FULL)
    assert "1/2 round trips (weekly cap)" in text
    assert "not yet registered — no active sleeve" in text


def test_full_no_arming_event_shows_not_active(monkeypatch):
    today = date(2026, 7, 13)
    tables = {
        "config": [{"key": "watchlist", "value": ["TSLA"]}, {"key": "weekly_trade_cap", "value": 2}],
        "prices_eod": [{"symbol": "TSLA", "date": "2026-07-12", "close": 407.76}],
        "indicators": [_ind("TSLA", "sma50", 330.0), _ind("TSLA", "sma200", 300.0)],
        "calendar_events": [],  # nothing arming
        "round_trips": [],
    }
    monkeypatch.setattr(handlers, "get_client", lambda: _FakeClient(tables))
    monkeypatch.setattr(handlers, "_utc_today", lambda: today)
    text = handle_today(_FULL)
    assert "not active — no arming event" in text


def test_full_registered_sleeve_shows_the_unit(monkeypatch):
    today = date(2026, 7, 13)
    tables = {
        "config": [
            {"key": "watchlist", "value": ["TSLA"]},
            {"key": "weekly_trade_cap", "value": 2},
            {"key": "sleeve_shares", "value": 17},
        ],
        "prices_eod": [{"symbol": "TSLA", "date": "2026-07-12", "close": 407.76}],
        "indicators": [_ind("TSLA", "sma50", 330.0)],
        "calendar_events": [],
        "round_trips": [],
    }
    monkeypatch.setattr(handlers, "get_client", lambda: _FakeClient(tables))
    monkeypatch.setattr(handlers, "_utc_today", lambda: today)
    text = handle_today(_FULL)
    assert "registered — 17 shares (frozen unit)" in text


# --------------------------------------------------------------------------- #
# The deterministic helpers directly
# --------------------------------------------------------------------------- #
def test_direction_up_down_mixed():
    assert _direction(100.0, 90.0, 80.0, 5.0, 3.0) == ("UP", "above its trend lines, push strengthening")
    assert _direction(70.0, 90.0, 80.0, 2.0, 4.0) == ("DOWN", "below its trend lines, push weakening")
    # Above 200 but below 50 → between → MIXED even with improving push.
    assert _direction(85.0, 90.0, 80.0, 5.0, 3.0)[0] == "MIXED"
    # Above both but push fading → MIXED (both legs must agree for UP).
    assert _direction(100.0, 90.0, 80.0, 2.0, 4.0)[0] == "MIXED"


def test_market_line_collapse():
    assert _market_line({"SPY": "UP", "QQQ": "UP"}) == "*Market overall*: pointing UP."
    assert _market_line({"SPY": "DOWN", "QQQ": "DOWN"}) == "*Market overall*: pointing DOWN."
    assert _market_line({"SPY": "UP", "QQQ": "DOWN"}) == "*Market overall*: pointing up, tech wobbling."
    assert _market_line({"SPY": "DOWN", "QQQ": "UP"}) == "*Market overall*: pointing down, tech out in front."
    assert _market_line({}) is None


def test_young_line_push():
    assert _young_line("SPCX", {"macd": 2.0, "macd_signal": 1.0}).endswith("recent push strong.")
    assert _young_line("SPCX", {"macd": 1.0, "macd_signal": 2.0}).endswith("recent push soft.")
    assert _young_line("SPCX", {"rsi14": 70.0}).endswith("recent push strong.")
    assert _young_line("SPCX", {}).endswith("still building history.")


def test_conditions_line_weather():
    assert "STORMY" in _conditions_line(True, 30.0)          # near event
    assert "STORMY" in _conditions_line(False, 85.0)         # high fear
    assert "CALM" in _conditions_line(False, 20.0)           # calm + low fear
    assert "NORMAL" in _conditions_line(False, 55.0)         # mid
    assert "NORMAL" in _conditions_line(False, None)         # no VIX


def test_sessions_until_weekday_proxy():
    mon = date(2026, 7, 13)  # Monday
    assert _sessions_until("2026-07-13", mon) == 0          # today
    assert _sessions_until("2026-07-14", mon) == 1          # Tuesday
    assert _sessions_until("2026-07-18", mon) == 4          # Sat: only Tue–Fri count
    assert _sessions_until("2026-07-10", mon) is None       # past
    assert _sessions_until(None, mon) is None


# --------------------------------------------------------------------------- #
# The full-card plain-language helpers directly (unchanged)
# --------------------------------------------------------------------------- #
def test_trend_phrase_all_cases():
    assert _trend_phrase(100.0, 90.0, 80.0) == "trading above its 50-day and 200-day average price"
    assert _trend_phrase(70.0, 90.0, 80.0) == "trading below its 50-day and 200-day average price"
    assert _trend_phrase(85.0, 90.0, 80.0) == "trading above its 200-day but below its 50-day average price"
    assert _trend_phrase(100.0, 90.0, None) == "trading above its 50-day average price"
    assert _trend_phrase(100.0, None, None) == "no moving-average history yet (young listing)"
    assert _trend_phrase(None, 90.0, 80.0) == "price not available"


def test_momentum_phrase_direction():
    assert "improving" in _momentum_phrase(60.0, 5.0, 3.0)
    assert "fading" in _momentum_phrase(40.0, 2.0, 4.0)
    assert _momentum_phrase(None, None, None) == "momentum not yet available"
    # RSI present, MACD missing → only the score renders.
    assert _momentum_phrase(55.0, None, None) == "momentum score 55 (50 is neutral)"
