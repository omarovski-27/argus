"""Argus digest — the one pipeline engine (blueprint §3 / §6 / §7).

Wires the Phase-1 data layer into a single deterministic run (Law 8: boring, no
agentic control flow):

    fetch (av/marketwatch/reddit) -> compute indicators -> Haiku score new headlines ->
    assemble frozen bundle -> Sonnet synthesis -> store digest (+bundle_json) -> Telegram

Two run types:
    'monday' / 'full'  — the full weekly digest (refresh sources, indicators, scoring).
    'pulse'            — light run: skip news+scoring+indicators, synthesize from the DB
                         as-is (the /pulse delta uses last_digest_sent_at in the bundle).

Law 7 in code: every data step is wrapped — its failure is logged to ``fetch_log`` and
surfaced, but does NOT abort the run, so one source outage still yields a digest with a
Source-Health gap rather than no digest at all. The critical tail (bundle -> synthesis
-> store -> Telegram) logs and RE-RAISES on failure; the +1h Monday auto-retry (§12)
lives in the GitHub Actions schedule, not here.

LLM calls use the ``anthropic`` SDK and the Telegram push uses ``httpx`` directly —
neither is a REST data source, so neither goes through ``shared.fetcher_base`` (which is
a GET-only fetcher for the §5 sources). Both are still wrapped and logged (Law 7).

NOTE: ``synthesize()`` is a STUB — a minimal Sonnet prompt that still carries the binding
no-recommendation (Law 1) and grounding (Law 2) clauses. Farm B replaces it with the
full §7 five-clause contract.

Run:  python -m digest.pipeline --run-type monday   (or: ... --run-type pulse)
"""

from __future__ import annotations

if __name__ == "__main__" and __package__ in (None, ""):
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import json
import os
import time
import uuid
from datetime import datetime, timezone

import httpx
from anthropic import Anthropic
from dotenv import load_dotenv

from digest.bundle import assemble_bundle
from digest.dedup import get_unscored_headline_ids
from digest.sentiment import score_headlines
from ingestion.fred import fetch_macro
from ingestion.indicators import compute_indicators
from ingestion.news_av import fetch_av_news
from ingestion.news_reddit import fetch_reddit_news
from ingestion.news_wire import fetch_wire_news
from shared.db import get_client
from shared.fetch_logger import write_fetch_log

_SONNET_MODEL = "claude-sonnet-4-6"
_TELEGRAM_LIMIT = 4096  # Telegram sendMessage hard cap (chars)

# Synthesis stub system prompt. Minimal, but Law 1 (no recommendation) and Law 2
# (grounding) are binding on EVERY synthesis prompt — they stay even in the stub.
_SYNTH_SYSTEM = (
    "You are Argus, a portfolio-intelligence analyst writing a weekly market digest for "
    "a single reader. STRICT, NON-NEGOTIABLE RULES:\n"
    "1. INFORMATION, NEVER INSTRUCTION: never tell the reader to buy, sell, hold, enter, "
    "exit, trim, add, size a position, or whether anything is 'safe to trade' or "
    "well-timed. Describe and interpret the data; never recommend or advise an action.\n"
    "2. GROUNDING: use ONLY the numbers and facts in the provided JSON bundle. Never "
    "invent or recall a price, date, or figure from memory. If something is missing, say "
    "'not available' rather than filling the gap.\n"
    "3. Put a plain-English interpretation beside every number and mark uncertainty "
    "honestly.\n"
    "Write a 600-800 word analyst note in five short sections: Regime, What Moved, "
    "Forward Calendar, Your Book, and Source Health."
)


def _elapsed_ms(start: float) -> int:
    """Whole milliseconds since a ``time.monotonic()`` reading (for fetch_log)."""
    return int((time.monotonic() - start) * 1000)


def _anthropic_key() -> str:
    """Read ANTHROPIC_API_KEY from the env (loading .env in dev); fail loud if absent."""
    load_dotenv(override=True)
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("Missing ANTHROPIC_API_KEY (see .env.example).")
    return key


def _step(run_id: str, name: str, fn) -> None:
    """Run a best-effort data step: log+surface a failure, but never abort the run (Law 7)."""
    start = time.monotonic()
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 — surface + continue; the digest still ships
        write_fetch_log(f"pipeline:{name}", run_id, "failure", _elapsed_ms(start), str(exc))
        print(f"[pipeline] step '{name}' FAILED (continuing) — {exc}")


def _critical(run_id: str, name: str, fn):
    """Run a critical step: log a failure to fetch_log and RE-RAISE (no digest without it)."""
    start = time.monotonic()
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 — log, then propagate (Law 7)
        write_fetch_log(f"pipeline:{name}", run_id, "failure", _elapsed_ms(start), str(exc))
        print(f"[pipeline] CRITICAL step '{name}' FAILED — {exc}")
        raise


def synthesize(bundle: dict) -> str:
    """STUB: synthesize the digest prose from the frozen bundle with Sonnet.

    Args:
        bundle: The frozen synthesis input from :func:`digest.bundle.assemble_bundle`.

    Returns:
        The digest text. Farm B replaces this stub with the full §7 five-clause contract;
        the no-recommendation (Law 1) and grounding (Law 2) clauses are already enforced.
    """
    client = Anthropic(api_key=_anthropic_key())
    message = client.messages.create(
        model=_SONNET_MODEL,
        max_tokens=2000,
        system=_SYNTH_SYSTEM,
        messages=[
            {
                "role": "user",
                "content": (
                    "Here is the frozen data bundle — the ONLY source of facts for this "
                    "digest. Write the analyst note.\n\n"
                    + json.dumps(bundle, ensure_ascii=False, default=str)
                ),
            }
        ],
    )
    text = next((b.text for b in message.content if b.type == "text"), "")
    if not text.strip():
        raise RuntimeError("Sonnet synthesis returned empty text")
    return text


def _digest_run_type(run_type: str) -> str:
    """Map a pipeline run_type to a ``digests.run_type`` CHECK value.

    The CHECK permits only ('full', 'pulse'); the weekly 'monday' run is stored as
    'full' (storing 'monday' would violate the constraint).
    """
    return "pulse" if run_type == "pulse" else "full"


def _store_digest(run_type: str, full_text: str, bundle: dict, run_id: str) -> None:
    """Insert the digest row, persisting its exact frozen ``bundle_json`` (Law 2 / §6)."""
    row = {
        "run_type": _digest_run_type(run_type),
        "sent_at": datetime.now(timezone.utc).isoformat(),
        "full_text": full_text,
        "bundle_json": bundle,
    }
    get_client().table("digests").insert(row).execute()
    print(f"[pipeline] stored digest (run_type={row['run_type']}, run {run_id}).")


def _split_message(text: str, limit: int) -> list[str]:
    """Split ``text`` into <=``limit``-char chunks, preferring newline boundaries."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:  # a single over-long line: hard-slice it
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = line if not current else f"{current}\n{line}"
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def _send_telegram(text: str) -> None:
    """Push the digest to Telegram (outbound, not a data source). Raise on failure (Law 7).

    Splits on the 4096-char limit. The bot token rides in the URL, so any httpx error is
    re-raised with the token redacted (Law 13: secrets never leak, even to logs).
    """
    load_dotenv(override=True)
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID (see .env.example).")

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    chunks = _split_message(text, _TELEGRAM_LIMIT)
    for chunk in chunks:
        try:
            response = httpx.post(
                url, json={"chat_id": chat_id, "text": chunk}, timeout=30.0
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise RuntimeError(
                f"Telegram send failed: {str(exc).replace(token, '***')}"
            ) from None
    print(f"[pipeline] sent digest to Telegram in {len(chunks)} message(s).")


def run_pipeline(run_type: str = "monday", run_id: str | None = None) -> None:
    """Run the full Phase-1 pipeline for ``run_type`` (blueprint §3 / §6 / §7).

    Args:
        run_type: 'monday'/'full' (full weekly digest) or 'pulse' (light run; skips
            news, scoring and indicators and synthesizes from the DB as-is).
        run_id: Optional run identifier; a uuid4-based one is generated if omitted.

    Data steps are best-effort (logged + surfaced, never aborting the run); the critical
    tail (bundle -> synthesis -> store -> Telegram) logs and re-raises on failure (Law 7).
    """
    load_dotenv(override=True)
    run_id = run_id or f"pipeline-{uuid.uuid4().hex[:12]}"
    if run_type not in ("monday", "full", "pulse"):
        raise ValueError(f"unknown run_type {run_type!r}; expected 'monday', 'full' or 'pulse'")
    print(f"[pipeline] start run_type={run_type} run_id={run_id}")

    if run_type != "pulse":
        _step(run_id, "av_news", lambda: fetch_av_news(run_id))
        _step(run_id, "wire_news", lambda: fetch_wire_news(run_id))
        _step(run_id, "reddit_news", lambda: fetch_reddit_news(run_id))
        _step(run_id, "macro", lambda: fetch_macro(run_id))
        _step(run_id, "indicators", lambda: compute_indicators(run_id))
        _step(run_id, "scoring", lambda: score_headlines(get_unscored_headline_ids(run_id), run_id))

    bundle_run_type = "pulse" if run_type == "pulse" else "monday"
    bundle = _critical(run_id, "bundle", lambda: assemble_bundle(bundle_run_type))
    full_text = _critical(run_id, "synthesis", lambda: synthesize(bundle))
    _critical(run_id, "store_digest", lambda: _store_digest(run_type, full_text, bundle, run_id))
    _critical(run_id, "telegram", lambda: _send_telegram(full_text))
    print(f"[pipeline] done run_id={run_id}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run the Argus digest pipeline.")
    parser.add_argument(
        "--run-type",
        default="monday",
        choices=["monday", "full", "pulse"],
        help="'monday'/'full' for the weekly digest, 'pulse' for a light run.",
    )
    args = parser.parse_args()
    run_pipeline(run_type=args.run_type)
