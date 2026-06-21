"""Argus bot — Telegram outbound push (send-only; the webhook ear is api/webhook.py).

This is the single outbound channel for every Telegram message Argus emits: the
Monday / pulse digest, the morning event-filter warnings (``bot.event_filter_check``),
and the instant command replies routed by ``api.webhook``. It POSTs to the Bot API
``sendMessage`` endpoint with ``httpx`` directly — Telegram is an outbound *sink*, not
a data source, so it deliberately does NOT go through ``shared.fetcher_base`` (that
wraps inbound fetches for ``fetch_log``).

Law 7 (silent failure is misinformation): a failed send RAISES so the caller can
surface it — the webhook logs it to ``fetch_log`` and replies with an error notice;
the event-filter job fails loud. A push that silently vanished would hide exactly the
warnings this system exists to deliver. Law 13 (§13): the bot token rides in the URL,
so it is masked out of any error this module raises, never leaking to a caller's logs.
"""

from __future__ import annotations

import os

import httpx
from dotenv import load_dotenv

TELEGRAM_API_BASE = "https://api.telegram.org"

# Telegram's hard per-message limit; longer text is split across multiple sends.
TELEGRAM_MAX_CHARS = 4096

# Outbound send timeout. Matches the §12 per-call contract and still returns well
# within Telegram's webhook timeout when invoked from api.webhook.
_SEND_TIMEOUT_SECONDS = 30.0


def _split_message(text: str) -> list[str]:
    """Split ``text`` into <=4096-char chunks on line boundaries.

    Splitting on newlines avoids cutting through a Markdown entity (e.g. ``*bold*``
    or a ``code`` span) far more often than a blind character slice would. A single
    line longer than the limit is hard-split as a last resort.
    """
    if len(text) <= TELEGRAM_MAX_CHARS:
        return [text]

    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        if len(line) > TELEGRAM_MAX_CHARS:
            # Pathological single line: flush, then hard-split it.
            if current:
                chunks.append(current)
                current = ""
            for start in range(0, len(line), TELEGRAM_MAX_CHARS):
                chunks.append(line[start : start + TELEGRAM_MAX_CHARS])
            continue
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > TELEGRAM_MAX_CHARS:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def _post(method: str, payload: dict, token: str) -> None:
    """POST ``payload`` to Bot API ``/<method>``; raise on failure with the token masked (§13).

    Single transport choke point for every outbound Bot API call (sendMessage,
    answerCallbackQuery, editMessageText) so the Law-7 raise and the §13 token-mask are
    written once and can't drift between callers.
    """
    url = f"{TELEGRAM_API_BASE}/bot{token}/{method}"
    try:
        response = httpx.post(url, json=payload, timeout=_SEND_TIMEOUT_SECONDS)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        # The token is in the URL and httpx echoes the URL in its error text; mask it
        # before the error can reach a caller's logs / fetch_log (§13).
        raise RuntimeError(f"Telegram {method} failed: {str(exc).replace(token, '***')}") from None


def _require(*names: str) -> list[str]:
    """Return the requested env values, raising once if any is unset (load .env first)."""
    load_dotenv(override=True)
    values = [os.environ.get(n) for n in names]
    if not all(values):
        raise RuntimeError(f"Missing {' / '.join(names)} (see .env.example).")
    return values  # type: ignore[return-value]


def send_message(
    text: str, parse_mode: str = "Markdown", reply_markup: dict | None = None
) -> None:
    """POST ``text`` to the Telegram Bot API ``sendMessage`` (Law 7: raise on failure).

    Args:
        text: Message body. Empty text is a no-op (Telegram rejects empty messages).
            Text longer than 4096 chars is split into multiple sequential sends.
        parse_mode: Telegram parse mode ('Markdown' default). A falsy value sends as
            plain text (the ``parse_mode`` field is omitted from the request).
        reply_markup: optional inline-keyboard markup (the /felt button flow). Attached to
            the LAST chunk only, so a split message shows the keyboard under its final part.

    Raises:
        RuntimeError: if TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are not set, or if a
            send fails — the underlying httpx error is re-raised with the bot token
            masked (§13), never swallowed, so the caller can surface the outage.
    """
    if not text:
        return

    token, chat_id = _require("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
    chunks = _split_message(text)
    for i, chunk in enumerate(chunks):
        payload: dict = {"chat_id": chat_id, "text": chunk}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup is not None and i == len(chunks) - 1:
            payload["reply_markup"] = reply_markup
        _post("sendMessage", payload, token)


def answer_callback_query(callback_query_id: str | None) -> None:
    """Acknowledge a button tap so Telegram stops the client's loading spinner (Law 7: raise).

    No text — v1 surfaces state by editing the message in place (``edit_message_text``), not a
    toast. A falsy id is a no-op. Mirrors ``send_message``'s token-masked raise (§13).
    """
    if not callback_query_id:
        return
    (token,) = _require("TELEGRAM_BOT_TOKEN")
    _post("answerCallbackQuery", {"callback_query_id": callback_query_id}, token)


def edit_message_text(
    chat_id: object,
    message_id: object,
    text: str,
    parse_mode: str = "Markdown",
    reply_markup: dict | None = None,
) -> None:
    """Edit an existing message in place — morph the /felt flow to its next stage (Law 7: raise).

    ``reply_markup=None`` clears the keyboard (the terminal 'Recorded ✓' / error messages drop it).
    Empty text is a no-op (Telegram rejects it). Token masked on failure (§13).
    """
    if not text:
        return
    (token,) = _require("TELEGRAM_BOT_TOKEN")
    payload: dict = {"chat_id": chat_id, "message_id": message_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    _post("editMessageText", payload, token)
