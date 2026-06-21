"""Tests for the webhook auth gate (bot/webhook_auth.py) — pure, no HTTP / env / DB.

secret_ok: fail-closed when unset, constant-time match. chat_ok: owner-only, reading the chat id
with the same message→edited_message precedence the router uses, comparing JSON int id to str env.
"""

from __future__ import annotations

from bot.webhook_auth import chat_ok, secret_ok

SECRET = "s3cr3t-token_value"
CHAT = "123456789"  # env values are strings; JSON delivers chat.id as an int


# --------------------------------------------------------------------------- #
# secret_ok
# --------------------------------------------------------------------------- #
def test_secret_unset_rejects_everything():
    # Fail-closed: no configured secret → reject regardless of the header presented.
    assert secret_ok(SECRET, None) is False
    assert secret_ok(SECRET, "") is False
    assert secret_ok(None, None) is False


def test_secret_missing_header_rejected():
    assert secret_ok(None, SECRET) is False
    assert secret_ok("", SECRET) is False


def test_secret_mismatch_rejected():
    assert secret_ok("wrong-token", SECRET) is False


def test_secret_exact_match_accepted():
    assert secret_ok(SECRET, SECRET) is True


# --------------------------------------------------------------------------- #
# chat_ok
# --------------------------------------------------------------------------- #
def test_chat_unset_rejects():
    assert chat_ok({"message": {"chat": {"id": 123456789}}}, None) is False
    assert chat_ok({"message": {"chat": {"id": 123456789}}}, "") is False


def test_chat_no_message_rejected():
    assert chat_ok({}, CHAT) is False
    # my_chat_member / channel_post / inline_message_id tap — no message chat anywhere → ignored.
    assert chat_ok({"my_chat_member": {"chat": {"id": 123456789}}}, CHAT) is False
    assert chat_ok({"callback_query": {"id": "x", "data": "felt:f=calm"}}, CHAT) is False


def test_chat_match_via_callback_query():
    # A button tap authenticates on callback_query.message.chat (the bot's own keyboard message),
    # by the IDENTICAL chat-id compare as a typed command — owner chat → accepted.
    tap = {"callback_query": {"id": "x", "data": "felt:f=calm", "message": {"chat": {"id": 123456789}}}}
    assert chat_ok(tap, CHAT) is True


def test_chat_callback_query_wrong_chat_rejected():
    tap = {"callback_query": {"id": "x", "data": "felt:f=calm", "message": {"chat": {"id": 999999}}}}
    assert chat_ok(tap, CHAT) is False


def test_chat_missing_id_rejected():
    assert chat_ok({"message": {"chat": {}}}, CHAT) is False
    assert chat_ok({"message": {}}, CHAT) is False


def test_chat_mismatch_rejected():
    assert chat_ok({"message": {"chat": {"id": 999999}}}, CHAT) is False


def test_chat_match_int_id_vs_str_env():
    assert chat_ok({"message": {"chat": {"id": 123456789}}}, CHAT) is True


def test_chat_match_via_edited_message():
    assert chat_ok({"edited_message": {"chat": {"id": 123456789}}}, CHAT) is True


def test_chat_message_takes_precedence_over_edited_message():
    # message wins over edited_message (same precedence as the router) — the winning chat decides.
    ok = {"message": {"chat": {"id": 123456789}}, "edited_message": {"chat": {"id": 999}}}
    bad = {"message": {"chat": {"id": 999}}, "edited_message": {"chat": {"id": 123456789}}}
    assert chat_ok(ok, CHAT) is True
    assert chat_ok(bad, CHAT) is False
