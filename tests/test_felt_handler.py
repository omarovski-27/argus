"""Tests for the /felt button flow (a tiny fake Supabase client — no live DB).

Covers the two handlers and the pure callback_data helpers:
  • handle_felt — lock-check UP FRONT, then either the feeling keyboard (Reply) or a plain-string
    refusal / already-logged. It NEVER writes (the write moved to the final tap).
  • handle_felt_callback — advance a tap (feeling→reason→confidence); on the final tap re-validate
    every field against a fresh config vocab and write lock-first (incl. the 23505 race reply).
  • _parse_felt_cb / _felt_cb_valid — the stateless state scheme + the trust boundary: a tampered
    or stale wire value fails validation so nothing is written under it.
"""

from __future__ import annotations

import pytest
from postgrest.exceptions import APIError

import bot.handlers as handlers
from bot.handlers import Reply


# --------------------------------------------------------------------------- #
# Minimal fake Supabase client: chainable builder, per-(table, op) results
# --------------------------------------------------------------------------- #
class _Result:
    def __init__(self, data):
        self.data = data


class _Builder:
    def __init__(self, client, table):
        self.client, self.table_name, self.op, self.payload = client, table, "select", None

    def select(self, *a, **k):
        self.op = "select"
        return self

    def insert(self, payload, *a, **k):
        self.op, self.payload = "insert", payload
        return self

    # all filters/ordering are chainable no-ops for the fake
    def eq(self, *a, **k):
        return self

    def order(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def execute(self):
        return self.client._execute(self.table_name, self.op, self.payload)


class FakeClient:
    """config_rows feeds _load_config; pending_reads is a queue of successive select results."""

    def __init__(self, config_rows, pending_reads, insert_exc=None):
        self.config_rows = config_rows
        self.pending_reads = list(pending_reads)
        self.insert_exc = insert_exc
        self.inserted: list[dict] = []

    def table(self, name):
        return _Builder(self, name)

    def _execute(self, table, op, payload):
        if table == "config" and op == "select":
            return _Result(self.config_rows)
        if table == "pending_annotations":
            if op == "select":
                return _Result(self.pending_reads.pop(0) if self.pending_reads else [])
            if op == "insert":
                if self.insert_exc is not None:
                    raise self.insert_exc
                self.inserted.append(payload)
                return _Result([payload])
        raise AssertionError(f"unexpected fake call: {table}.{op}")


def _api_error(code: str) -> APIError:
    return APIError({"code": code, "message": "x", "details": "", "hint": None})


def _use(monkeypatch, client):
    monkeypatch.setattr(handlers, "get_client", lambda: client)


# config.sleeve_symbol is REQUIRED (§8 / #3). The annotation vocab is left unseeded so it falls
# back to handlers._DEFAULT_* — calm/confident/… and momentum/setup/… — which the taps below use.
CFG = [{"key": "sleeve_symbol", "value": "TSLA"}]

FELT_MSG = {"text": "/felt"}


def _all_callback_data(reply: Reply) -> list[str]:
    rows = reply.reply_markup["inline_keyboard"]
    return [btn["callback_data"] for row in rows for btn in row]


# --------------------------------------------------------------------------- #
# handle_felt — launches the flow (buttons), never writes
# --------------------------------------------------------------------------- #
def test_handle_felt_shows_feeling_buttons(monkeypatch):
    client = FakeClient(config_rows=CFG, pending_reads=[[]])  # no note today
    _use(monkeypatch, client)
    reply = handlers.handle_felt(FELT_MSG)
    assert isinstance(reply, Reply)
    assert "how did you feel" in reply.text.lower()
    data = _all_callback_data(reply)
    assert data and all(d.startswith("felt:f=") and ":r=" not in d for d in data)
    assert "felt:f=calm" in data  # feeling-first, from the fallback vocab
    assert client.inserted == []  # launching the flow never writes


def test_handle_felt_already_locked_no_buttons(monkeypatch):
    existing = {"id": 1, "reason": "setup", "feeling": "calm", "confidence_1to5": 3}
    client = FakeClient(config_rows=CFG, pending_reads=[[existing]])
    _use(monkeypatch, client)
    reply = handlers.handle_felt(FELT_MSG)
    assert isinstance(reply, str) and reply.startswith("Already logged today ✓")
    assert client.inserted == []


def test_handle_felt_unconfigured_symbol_refuses(monkeypatch):
    client = FakeClient(config_rows=[], pending_reads=[[]])  # no sleeve_symbol
    _use(monkeypatch, client)
    reply = handlers.handle_felt(FELT_MSG)
    assert isinstance(reply, str) and "not configured" in reply.lower()
    assert client.inserted == []


# --------------------------------------------------------------------------- #
# handle_felt_callback — advance taps; final tap validates + writes
# --------------------------------------------------------------------------- #
def test_callback_feeling_tap_asks_reason(monkeypatch):
    client = FakeClient(config_rows=CFG, pending_reads=[])
    _use(monkeypatch, client)
    reply = handlers.handle_felt_callback({"id": "1", "data": "felt:f=calm"})
    assert isinstance(reply, Reply) and "reason" in reply.text.lower()
    data = _all_callback_data(reply)
    assert "felt:f=calm:r=setup" in data and all(":c=" not in d for d in data)
    assert client.inserted == []


def test_callback_reason_tap_asks_confidence(monkeypatch):
    client = FakeClient(config_rows=CFG, pending_reads=[])
    _use(monkeypatch, client)
    reply = handlers.handle_felt_callback({"id": "1", "data": "felt:f=calm:r=setup"})
    assert isinstance(reply, Reply) and "confident" in reply.text.lower()
    data = _all_callback_data(reply)
    assert data == [f"felt:f=calm:r=setup:c={n}" for n in range(1, 6)]
    assert client.inserted == []


def test_callback_confidence_tap_writes_and_acks(monkeypatch):
    client = FakeClient(config_rows=CFG, pending_reads=[[]])  # no existing note
    _use(monkeypatch, client)
    reply = handlers.handle_felt_callback({"id": "1", "data": "felt:f=calm:r=setup:c=3"})
    assert isinstance(reply, Reply) and reply.text.startswith("Recorded ✓")
    assert reply.reply_markup is None  # keyboard dropped on completion
    row = client.inserted[0]
    assert row["symbol"] == "TSLA"  # resolved from config, not a constant
    assert (row["reason"], row["feeling"], row["confidence_1to5"]) == ("setup", "calm", 3)


def test_callback_tampered_field_does_not_write(monkeypatch):
    client = FakeClient(config_rows=CFG, pending_reads=[[]])
    _use(monkeypatch, client)
    reply = handlers.handle_felt_callback({"id": "1", "data": "felt:f=INJECT:r=setup:c=3"})
    assert isinstance(reply, Reply) and "vocabulary changed" in reply.text.lower()
    assert client.inserted == []  # wire value never trusted → no row


def test_callback_out_of_range_confidence_does_not_write(monkeypatch):
    client = FakeClient(config_rows=CFG, pending_reads=[[]])
    _use(monkeypatch, client)
    reply = handlers.handle_felt_callback({"id": "1", "data": "felt:f=calm:r=setup:c=9"})
    assert isinstance(reply, Reply) and "vocabulary changed" in reply.text.lower()
    assert client.inserted == []


def test_callback_non_felt_prefix_returns_none(monkeypatch):
    client = FakeClient(config_rows=CFG, pending_reads=[])
    _use(monkeypatch, client)
    assert handlers.handle_felt_callback({"id": "1", "data": "other:x=1"}) is None


def test_callback_terminal_already_locked_does_not_double_write(monkeypatch):
    existing = {"id": 1, "reason": "momentum", "feeling": "scared", "confidence_1to5": 2}
    client = FakeClient(config_rows=CFG, pending_reads=[[existing]])
    _use(monkeypatch, client)
    reply = handlers.handle_felt_callback({"id": "1", "data": "felt:f=calm:r=setup:c=3"})
    assert isinstance(reply, Reply) and reply.text.startswith("Already logged today ✓")
    assert client.inserted == []  # lock-first up top of _record_annotation


def test_callback_final_tap_race_unique_violation_returns_lock_reply(monkeypatch):
    # Lock read passes (empty), insert loses the race → 23505, re-read returns the winner.
    winner = {"id": 7, "reason": "catalyst", "feeling": "fomo", "confidence_1to5": 5}
    client = FakeClient(
        config_rows=CFG, pending_reads=[[], [winner]], insert_exc=_api_error("23505")
    )
    _use(monkeypatch, client)
    reply = handlers.handle_felt_callback({"id": "1", "data": "felt:f=calm:r=setup:c=3"})
    assert isinstance(reply, Reply) and reply.text.startswith("Already logged today ✓")
    assert "conf 5/5" in reply.text  # rendered from the winning row, not a raise


def test_callback_final_tap_other_db_error_surfaces(monkeypatch):
    # A non-unique-violation error must NOT be swallowed (Law 7) — it propagates.
    client = FakeClient(config_rows=CFG, pending_reads=[[]], insert_exc=_api_error("23503"))
    _use(monkeypatch, client)
    with pytest.raises(APIError):
        handlers.handle_felt_callback({"id": "1", "data": "felt:f=calm:r=setup:c=3"})


# --------------------------------------------------------------------------- #
# Pure callback_data helpers — the stateless scheme + the trust boundary
# --------------------------------------------------------------------------- #
def test_parse_felt_cb_accumulated():
    assert handlers._parse_felt_cb("felt:f=scared:r=gut feel:c=3") == {
        "f": "scared",
        "r": "gut feel",  # a vocab token with a space round-trips intact
        "c": "3",
    }


def test_parse_felt_cb_partial_and_ignores_unknown_keys():
    assert handlers._parse_felt_cb("felt:f=calm") == {"f": "calm"}
    assert handlers._parse_felt_cb("felt:f=calm:x=evil") == {"f": "calm"}


@pytest.mark.parametrize(
    "fields,ok",
    [
        ({"f": "calm"}, True),
        ({"f": "calm", "r": "gut feel"}, True),
        ({"f": "calm", "r": "setup", "c": "5"}, True),
        ({"f": "nope"}, False),  # not in vocab
        ({"f": "calm", "r": "scalp"}, False),  # reason not in vocab
        ({"f": "calm", "r": "setup", "c": "0"}, False),  # out of 1-5
        ({"f": "calm", "r": "setup", "c": "6"}, False),
        ({"f": "calm", "r": "setup", "c": "x"}, False),  # non-digit
        ({"f": "calm", "r": "setup", "c": "٣"}, False),  # non-ASCII digit
    ],
)
def test_felt_cb_valid(fields, ok):
    reasons = list(handlers._DEFAULT_REASONS)
    feelings = list(handlers._DEFAULT_FEELINGS)
    assert handlers._felt_cb_valid(fields, reasons, feelings) is ok
