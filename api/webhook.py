"""Argus webhook — Vercel Python serverless entry for the Telegram Bot webhook.

Vercel exposes the module-level ``handler`` class (a stdlib ``BaseHTTPRequestHandler``
— Law 8: no Flask / FastAPI) as the function behind the Telegram webhook URL. Telegram
POSTs each update here; we route the command's first word to a ``bot.handlers``
function, send the reply via ``bot.telegram.send_message``, and ALWAYS answer Telegram
200 — even on error — so a failing update does not trigger Telegram's retry storm.

Heavy work never happens here (blueprint §2 item 11 / §3): the instant commands read
Supabase directly and reply in ~1s; ``/pulse`` only fires a ``workflow_dispatch``. Law
7: an unhandled error is logged to ``fetch_log`` (source ``telegram_webhook``) AND
surfaced to the user as a one-line notice — never swallowed.

Note (schema is truth): ``fetch_log.status`` is CHECK-constrained to
``success|failure|timeout|unavailable`` — ``'error'`` is NOT a valid value, so a
webhook failure is recorded as ``'failure'``.
"""

from __future__ import annotations

import json
import pathlib
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler

# Vercel imports this module from the api/ directory; ensure the repo root is on the
# path so the `bot` and `shared` packages resolve regardless of the runtime's cwd.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from bot.handlers import (  # noqa: E402 — after the sys.path bootstrap above
    handle_book,
    handle_felt,
    handle_health,
    handle_journal,
    handle_override,
    handle_pulse,
    handle_skip,
)
from bot.telegram import send_message  # noqa: E402
from shared.fetch_logger import write_fetch_log  # noqa: E402

_UNKNOWN_REPLY = "Unknown command. Try /book /journal /felt /pulse /skip /health /override"

# Command word → handler. Each handler is (message: dict) -> str.
_COMMANDS = {
    "/pulse": handle_pulse,
    "/book": handle_book,
    "/journal": handle_journal,
    "/felt": handle_felt,
    "/skip": handle_skip,
    "/health": handle_health,
    "/override": handle_override,
}


def _route(message: dict) -> str | None:
    """Route a Telegram message to a handler; return its reply, or None to stay silent.

    None means 'nothing to do' (no text — e.g. a photo or a button callback); the
    webhook then 200s without sending, rather than replying 'unknown command' to every
    non-command update. A text that is not a known command gets the help reply.
    """
    text = (message.get("text") or "").strip()
    if not text:
        return None
    # First word, minus any group-style '@BotName' suffix, lower-cased.
    command = text.split()[0].split("@")[0].lower()
    handler_fn = _COMMANDS.get(command)
    if handler_fn is None:
        return _UNKNOWN_REPLY
    return handler_fn(message)


def _elapsed_ms(start: float) -> int:
    """Whole milliseconds since a ``time.monotonic()`` reading (for fetch_log)."""
    return int((time.monotonic() - start) * 1000)


class handler(BaseHTTPRequestHandler):
    """Vercel serverless handler: Telegram webhook (POST) + uptime health check (GET)."""

    def do_POST(self) -> None:  # noqa: N802 — stdlib API name
        """Handle one Telegram update; ALWAYS answer 200 (failures surface via Law 7)."""
        start = time.monotonic()
        run_id = f"webhook-{uuid.uuid4().hex[:12]}"
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            update = json.loads(body) if body else {}
            message = update.get("message") or update.get("edited_message") or {}
            reply = _route(message)
            if reply:
                send_message(reply)
        except Exception as exc:  # noqa: BLE001 — surface, never swallow (Law 7)
            self._log_and_notify(run_id, _elapsed_ms(start), exc)
        self._respond(200, b"OK")

    def do_GET(self) -> None:  # noqa: N802 — stdlib API name
        """Respond 200 to Vercel / uptime health checks."""
        self._respond(200, b"OK")

    def _log_and_notify(self, run_id: str, latency_ms: int, exc: Exception) -> None:
        """Record the webhook error to fetch_log and notify the user — both best-effort.

        ``fetch_log.status`` has no ``'error'`` value (schema is truth), so the failure
        is logged as ``'failure'``. Both side effects are guarded: a logging or send
        failure must never prevent the mandatory 200 to Telegram.
        """
        try:
            write_fetch_log("telegram_webhook", run_id, "failure", latency_ms, str(exc))
        except Exception:  # noqa: BLE001 — never let logging break the 200 response
            pass
        try:
            send_message("⚠️ Internal error — check /health")
        except Exception:  # noqa: BLE001
            pass

    def _respond(self, code: int, body: bytes) -> None:
        """Write a minimal ``text/plain`` response."""
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:  # noqa: N802 — stdlib API name
        """Silence BaseHTTPRequestHandler's stderr access log (noise in serverless logs)."""
        return
