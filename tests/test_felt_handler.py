"""Tests for handle_felt's DB-orchestration branches (a tiny fake Supabase client — no live DB).

Kept separate from test_annotation_reconcile.py (which is strictly pure logic). Covers the
lock-first read, the happy-path insert, and — the branch the pure tests can't reach — the
read→insert race where the (symbol, trade_date) unique index raises 23505 and the handler must
return the friendly lock reply instead of letting the webhook turn it into an 'internal error'.
"""

from __future__ import annotations

import pytest
from postgrest.exceptions import APIError

import bot.handlers as handlers


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


MSG = {"text": "/felt setup calm 4"}

# config.sleeve_symbol is now a REQUIRED row (§8): /felt fails loud without it (L6) rather than
# stamping a guessed ticker. The happy/lock/race tests seed it so they exercise the insert path;
# the unconfigured test below asserts the refusal. (annotation_* stay unseeded → vocab falls back.)
CFG_SEEDED = [{"key": "sleeve_symbol", "value": "TSLA"}]


# --------------------------------------------------------------------------- #
# handle_felt branches
# --------------------------------------------------------------------------- #
def test_handle_felt_happy_path_inserts_and_acks(monkeypatch):
    client = FakeClient(config_rows=CFG_SEEDED, pending_reads=[[]])  # no existing note today
    _use(monkeypatch, client)
    reply = handlers.handle_felt(MSG)
    assert reply.startswith("Noted ✓")
    assert client.inserted and client.inserted[0]["reason"] == "setup"
    assert client.inserted[0]["confidence_1to5"] == 4
    assert client.inserted[0]["symbol"] == "TSLA"  # stamped from config, not a constant


def test_handle_felt_lock_first_does_not_insert(monkeypatch):
    existing = {"id": 1, "reason": "momentum", "feeling": "calm", "confidence_1to5": 3}
    client = FakeClient(config_rows=CFG_SEEDED, pending_reads=[[existing]])
    _use(monkeypatch, client)
    reply = handlers.handle_felt(MSG)
    assert reply.startswith("Already logged today ✓")
    assert client.inserted == []  # immutable lock — never stage a second


def test_handle_felt_race_unique_violation_returns_lock_reply(monkeypatch):
    # First read: empty (lock check passes). Insert loses the race → 23505. Re-read: the winner.
    winner = {"id": 7, "reason": "catalyst", "feeling": "fomo", "confidence_1to5": 5}
    client = FakeClient(
        config_rows=CFG_SEEDED, pending_reads=[[], [winner]], insert_exc=_api_error("23505")
    )
    _use(monkeypatch, client)
    reply = handlers.handle_felt(MSG)
    assert reply.startswith("Already logged today ✓")
    assert "conf 5/5" in reply  # rendered from the winning row, not a raise


def test_handle_felt_other_db_error_surfaces(monkeypatch):
    # A non-unique-violation error must NOT be swallowed (Law 7) — it propagates.
    client = FakeClient(config_rows=CFG_SEEDED, pending_reads=[[]], insert_exc=_api_error("23503"))
    _use(monkeypatch, client)
    with pytest.raises(APIError):
        handlers.handle_felt(MSG)


def test_handle_felt_unconfigured_symbol_refuses_without_insert(monkeypatch):
    # No config.sleeve_symbol row → fail loud (L6/L7): refuse, never stage a guessed-ticker note.
    client = FakeClient(config_rows=[], pending_reads=[[]])
    _use(monkeypatch, client)
    reply = handlers.handle_felt(MSG)
    assert "not configured" in reply.lower()  # the refusal, not a raise
    assert client.inserted == []  # no row written under a guessed symbol


def test_handle_felt_blank_symbol_refuses_without_insert(monkeypatch):
    # A present-but-blank row is invalid too — must refuse, not stamp "" as the ticker.
    client = FakeClient(config_rows=[{"key": "sleeve_symbol", "value": "  "}], pending_reads=[[]])
    _use(monkeypatch, client)
    reply = handlers.handle_felt(MSG)
    assert "not configured" in reply.lower()
    assert client.inserted == []
