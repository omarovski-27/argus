"""Argus bot — Telegram webhook authentication (pure; the gate for api/webhook.py).

Two stacked checks the Vercel webhook applies before doing any work:
  • secret_ok — the ``X-Telegram-Bot-Api-Secret-Token`` header (echoed by Telegram from the
    ``secret_token`` set at setWebhook time) must equal ``TELEGRAM_WEBHOOK_SECRET``. Blocks anyone
    who isn't Telegram-carrying-our-secret. FAIL-CLOSED: an unset secret rejects everything (never
    run unprotected, Law 7) rather than silently leaving the door open.
  • chat_ok — even a genuine Telegram delivery must come from ``TELEGRAM_CHAT_ID``. Blocks anyone
    who isn't the owner.

Pure (no HTTP / env / DB) so the gate is unit-tested without a server, and it lives in bot/ — not
api/, which Vercel scans for serverless functions — so it imports cleanly in tests.
"""

from __future__ import annotations

import hmac


def secret_ok(header: str | None, configured: str | None) -> bool:
    """True when the webhook secret header matches the configured secret (constant-time).

    Fail-closed: when ``configured`` is unset/empty, return False — the webhook rejects every
    request rather than run unprotected. The header is coerced None→"" so a missing header is just
    a mismatch, and the compare is constant-time (``hmac.compare_digest``) so the secret can't leak
    through timing. Telegram's secret_token is ASCII, so a str/str compare is correct.
    """
    if not configured:
        return False
    return hmac.compare_digest(header or "", configured)


def chat_ok(update: dict, configured: str | None) -> bool:
    """True when the update's chat id equals the configured owner chat id.

    Reads the chat id with the SAME precedence ``api.webhook`` uses to act on the update —
    ``message`` → ``edited_message`` → ``callback_query.message`` — so auth and routing always read
    the same chat (no authenticate-on-one-field / act-on-another gap). A button tap (callback_query)
    carries its chat under ``callback_query.message.chat`` — the bot's OWN keyboard message, which
    only ever lives in the owner's chat — so it is authenticated by the IDENTICAL chat-id compare as
    a typed command (chat-id only; no separate ``from`` rule — one auth invariant). Updates with no
    such chat (my_chat_member, channel_post, an inline_message_id tap Argus never sends, …) yield no
    id → False → silently ignored, fail-closed. The id is a JSON int and the env value is a str, so
    the compare is on ``str``.
    """
    if not configured:
        return False
    cq = update.get("callback_query") or {}
    msg = (
        update.get("message")
        or update.get("edited_message")
        or cq.get("message")
        or {}
    )
    chat_id = (msg.get("chat") or {}).get("id")
    return chat_id is not None and str(chat_id) == str(configured)
