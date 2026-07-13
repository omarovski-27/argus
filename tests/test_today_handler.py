"""/today trade-context card tests (bot/handlers.handle_today).

The card is a DETERMINISTIC DB render — no LLM, so grounding-exempt by construction —
but Law 1 binds hard: it DESCRIBES state and never advises. The load-bearing test is
``test_card_never_advises``: the output must contain no good/bad-day, should/could-trade,
or buy/sell/enter/exit language. The rest pin the per-section rendering and the CPI-eve
event-filter ACTIVE state (the live test the day before a CPI print).

A fake client returns canned rows per table and ignores the filter chain (the handler's
Python-side selection — latest-per-name, arm predicate — is what's under test).
"""

import re
from datetime import date, timedelta

import pytest

import bot.handlers as handlers
from bot.handlers import (
    _momentum_phrase,
    _trend_phrase,
    handle_today,
)


# --------------------------------------------------------------------------- #
# Fake client — chainable query that ignores filters, returns canned rows/table
# --------------------------------------------------------------------------- #
class _Query:
    """Chainable fake. Applies ``eq(col, val)`` filters (the real server does this
    per-symbol); ignores order/limit/gte/lte/in_ (the handler's Python-side selection
    is what's under test)."""

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


@pytest.fixture()
def cpi_eve(monkeypatch):
    """A CPI-tomorrow world: filter should render IN EFFECT. Returns (today, tables)."""
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
        "calendar_events": [
            {"date": tomorrow, "type": "cpi", "symbol": None, "materiality": "high"},
        ],
        "round_trips": [{"id": 1}],  # 1 this week
    }
    monkeypatch.setattr(handlers, "get_client", lambda: _FakeClient(tables))
    monkeypatch.setattr(handlers, "_utc_today", lambda: today)
    return today, tables


# --------------------------------------------------------------------------- #
# The hard Law-1 exclusion — the load-bearing test
# --------------------------------------------------------------------------- #
_BANNED = [
    r"\bbuy\b", r"\bsell\b", r"\benter\b", r"\bexit\b",
    r"\bshould\b", r"\bcould\b", r"\bgood day\b", r"\bbad day\b",
    r"\bsafe to\b", r"\brecommend", r"\ballocate\b",
]


def test_card_never_advises(cpi_eve):
    text = handle_today({}).lower()
    for pattern in _BANNED:
        assert not re.search(pattern, text), f"card must not advise — matched {pattern!r}"


# --------------------------------------------------------------------------- #
# CPI-eve: the event filter renders IN EFFECT (the live test)
# --------------------------------------------------------------------------- #
def test_cpi_tomorrow_shows_filter_in_effect(cpi_eve):
    text = handle_today({})
    assert "Event filter (§8)" in text
    assert "IN EFFECT" in text and "CPI" in text
    assert "within 24h" in text and "blocked" in text


def test_no_arming_event_shows_not_active(monkeypatch):
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
    text = handle_today({})
    assert "not active — no arming event" in text


# --------------------------------------------------------------------------- #
# Per-section rendering
# --------------------------------------------------------------------------- #
def test_watchlist_trend_and_momentum_render(cpi_eve):
    text = handle_today({})
    # TSLA: 407.76 above both 330 and 300.
    assert "trading above its 50-day and 200-day average price" in text
    assert "momentum score 62 (50 is neutral)" in text
    assert "trend momentum improving" in text  # macd 5 >= signal 3


def test_young_ticker_has_no_200day(cpi_eve):
    text = handle_today({})
    # SPCX has sma50 (170) but no sma200; close 407.76 > 170 → above its 50-day only.
    spcx_line = next(ln for ln in text.splitlines() if ln.startswith("• *SPCX*"))
    assert "above its 50-day average price" in spcx_line
    assert "200-day" not in spcx_line


def test_weekly_cap_and_sleeve_status(cpi_eve):
    text = handle_today({})
    assert "1/2 round trips (weekly cap)" in text
    assert "not yet registered — no active sleeve" in text


def test_registered_sleeve_shows_the_unit(monkeypatch):
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
    text = handle_today({})
    assert "registered — 17 shares (frozen unit)" in text


# --------------------------------------------------------------------------- #
# The plain-language helpers directly
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
