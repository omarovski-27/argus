"""Argus journal — checkpoint proximity/verdict Telegram push (Phase 2b; blueprint §9).

Runs after ``journal.pairing`` in the daily job. Evaluates the checkpoint state off the
freshly paired ``round_trips`` and pushes — at most once each — the §9 warnings:

  • proximity — "Trade 18 of 20 — checkpoint in 2. Sleeve Δshares: +0.31." when the next
    gate is within ``config.proximity_window`` trades (default 2);
  • verdict   — the gate's pre-registered verdict (pause / halt / stop, or pass → Phase
    B/C) for every gate the trade count has REACHED.

It REPORTS; it never ACTS (Law 1). The gate math is NOT reimplemented here — it reuses
``journal.checkpoint.checkpoint_state`` wholesale.

Fire-once (Law 6) is owned by the ``push_log`` ledger, not by control flow: a candidate
is keyed (``verdict:{N}`` / ``proximity:{count}`` / ``verdict:10:undefined``), checked
against push_log, and — only after a SUCCESSFUL send — recorded. Send-first-then-record is
deliberate at-least-once: a rare duplicate beats silently losing a verdict. Evaluating on
``trade_count >= gate`` (not ``== gate``) means a re-dispatch or a caught-up missed run can
never step silently over a gate — the ledger is the single source of "already sent".

Two failure modes, kept distinct (Law 7):
  • Telegram transport fails → ``fetch_log`` failure under ``journal:checkpoint_push`` and
    re-raise (fail loud). The dedup row is NOT written, so the next run retries.
  • Gate-10 magnitude UNDEFINED (zero/None rebuy_px → the −1.0 threshold can't be
    evaluated) → push a loud integrity notice, dedup it under ``verdict:10:undefined`` so it
    yells once (never consuming ``verdict:10``), and make the run's terminal fetch_log row a
    FAILURE so /health goes red — but do NOT crash: sibling messages still send and the real
    verdict still fires once the data is fixed. A data finding is loud, not fatal.

Run:  python -m journal.checkpoint_push
"""

from __future__ import annotations

if __name__ == "__main__" and __package__ in (None, ""):
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import time
import uuid

from bot.telegram import send_message
from journal.checkpoint import _latest_close, _load_sleeve_symbol, checkpoint_state
from shared.db import get_client
from shared.fetch_logger import write_fetch_log

# Every push_log row this module writes carries kind 'checkpoint'; fetch_log rows carry
# this source so a push (transport / integrity) failure is separable from a compute one.
_PUSH_KIND = "checkpoint"
_SOURCE = "journal:checkpoint_push"

# Fallback when config.proximity_window is unseeded (mirrors handlers.py _DEFAULT_*). §9.
_DEFAULT_PROXIMITY_WINDOW = 2

# Verdict word → severity glyph. Reporting only — the glyph never implies an instruction.
_VERDICT_GLYPH = {
    "pause": "⚠️",
    "halt": "⛔",
    "stop": "🛑",
    "continue": "✅",
    "pass": "✅",
}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _elapsed_ms(start: float) -> int:
    """Whole milliseconds since a ``time.monotonic()`` reading (for fetch_log)."""
    return int((time.monotonic() - start) * 1000)


def _proximity_window(client) -> int:
    """config.proximity_window as an int; fallback 2 when the row is unseeded (§9)."""
    rows = (
        client.table("config").select("value").eq("key", "proximity_window").limit(1).execute().data
    ) or []
    try:
        return int(rows[0]["value"]) if rows else _DEFAULT_PROXIMITY_WINDOW
    except (TypeError, ValueError):
        return _DEFAULT_PROXIMITY_WINDOW


def _already_sent(client, dedup_key: str) -> bool:
    """True when (kind, dedup_key) is already in push_log — the fire-once check."""
    rows = (
        client.table("push_log")
        .select("id")
        .eq("kind", _PUSH_KIND)
        .eq("dedup_key", dedup_key)
        .limit(1)
        .execute()
        .data
    ) or []
    return bool(rows)


def _record_sent(client, dedup_key: str, body: str) -> None:
    """Record a delivered push (after a successful send). body kept for audit."""
    client.table("push_log").insert(
        {"kind": _PUSH_KIND, "dedup_key": dedup_key, "body": body}
    ).execute()


def _verdict_text(gate: dict) -> str:
    """The verdict push for a reached gate — reports the pre-registered verdict on its
    IMMUTABLE as-of-gate basis (Law 1 / Law 6).

    The figures are the gate's OWN frozen basis (the first-N ledger), never the live state:
    on a caught-up run (trade_count already past the gate) the verdict must still read the
    numbers the gate decided on, or it misreports the verdict beside live figures it never
    used (Law 2). Live standing lives in the proximity message — the two surfaces split
    cleanly: verdict = what the gate decided and on what; proximity = where you stand now.
    """
    glyph = _VERDICT_GLYPH.get(gate["verdict"], "🔔")
    cum = gate["cumulative_pnl_at_gate"]
    if gate["metric_delta_shares"] is not None:
        # Magnitude gate (early_warning) — decided once at trip #N's fixed rebuy_px (Law 6).
        basis = (
            f"Δshares {gate['metric_delta_shares']:+.3f} @ fixed {gate['price_used']:.2f} "
            f"(cumulative ${cum:+.2f})"
        )
    else:
        # Sign gate (checkpoint / verdict) — decided on the dollar sign over the first N.
        basis = f"cumulative ${cum:+.2f} (sign gate)"
    # Headline carries the action phrase only — it already leads with the disposition, so
    # re-stating the bare verdict word ("pause. pause & examine") just stutters. The glyph
    # encodes severity; gate['verdict'] still drives that glyph and the push_log dedup key.
    return (
        f"{glyph} Gate {gate['trade']} — {gate['name']}: {gate['action']}.\n"
        f"As of trade {gate['trade']}: {basis}."
    )


def _integrity_text(gate: dict) -> str:
    """The loud gate-10 integrity notice (magnitude undefined — bad/zero rebuy_px)."""
    return (
        f"🧨 INTEGRITY — gate {gate['trade']} ({gate['name']}) verdict is UNDEFINED: "
        f"trade #{gate['trade']}'s rebuy price is missing or zero, so the "
        f"{gate['threshold_delta_shares']} Δshares magnitude can't be evaluated. Fix the "
        "round_trips row; the real verdict fires once the data is valid. (Law 7)"
    )


# --------------------------------------------------------------------------- #
# Runner (wrapped + logged; mirrors bot.event_filter_check conventions)
# --------------------------------------------------------------------------- #
def check_and_push() -> int:
    """Push due proximity/verdict checkpoint warnings, fire-once via push_log (§9).

    Returns the number of messages sent this run. Wrapped + logged (Law 7): a transport
    failure logs ``journal:checkpoint_push`` failure and re-raises; a gate-10 undefined
    makes the terminal log a failure but does not crash.
    """
    run_id = f"checkpoint-push-{uuid.uuid4().hex[:12]}"
    start = time.monotonic()
    try:
        client = get_client()
        round_trips = (
            client.table("round_trips")
            .select("id,date,pnl_usd,rebuy_px")
            .order("date")
            .order("id")
            .execute()
            .data
        ) or []
        kc_rows = (
            client.table("config").select("value").eq("key", "kill_criteria").execute().data
        ) or []
        kill_criteria = kc_rows[0]["value"] if kc_rows else {}
        sleeve_symbol = _load_sleeve_symbol(client)  # fail loud if unseeded (L6/L7)
        current_price = _latest_close(client, sleeve_symbol)
        window = _proximity_window(client)

        state = checkpoint_state(round_trips, kill_criteria, current_price)
        trade_count = state["trade_count"]

        sent = 0
        integrity = False

        # Verdict candidates: every REACHED gate (fire on >=, dedup per gate).
        for gate in state["gates"]:
            if not gate["reached"]:
                continue
            if gate["verdict"] is None:
                # Gate-10 magnitude undefined — loud, deduped separately, never fatal.
                integrity = True
                key = f"verdict:{gate['trade']}:undefined"
                if not _already_sent(client, key):
                    text = _integrity_text(gate)
                    send_message(text, parse_mode=None)
                    _record_sent(client, key, text)
                    sent += 1
                continue
            key = f"verdict:{gate['trade']}"
            if _already_sent(client, key):
                continue
            text = _verdict_text(gate)
            send_message(text, parse_mode=None)
            _record_sent(client, key, text)
            sent += 1

        # Proximity candidate: next gate within the window; deduped on the current count.
        next_gate = state["next_gate"]
        if next_gate and next_gate["trades_to_go"] <= window:
            key = f"proximity:{trade_count}"
            if not _already_sent(client, key):
                text = state["proximity_text"]
                send_message(text, parse_mode=None)
                _record_sent(client, key, text)
                sent += 1

        # Terminal fetch_log row. Integrity (a data finding) makes it a FAILURE so /health
        # surfaces it red — but the run already did its work and did not crash (Law 7).
        if integrity:
            write_fetch_log(
                _SOURCE, run_id, "failure", _elapsed_ms(start),
                "gate-10 verdict undefined (zero/None rebuy_px)",
            )
            print(f"[checkpoint_push] sent {sent} message(s); GATE-10 INTEGRITY UNDEFINED (logged failure, not fatal).")
        else:
            write_fetch_log(_SOURCE, run_id, "success", _elapsed_ms(start))
            print(f"[checkpoint_push] sent {sent} message(s).")
        return sent
    except Exception as exc:  # noqa: BLE001 — surface, never swallow (Law 7)
        write_fetch_log(_SOURCE, run_id, "failure", _elapsed_ms(start), str(exc))
        print(f"[checkpoint_push] FAILED — {exc}")
        raise


if __name__ == "__main__":
    import sys

    # Messages carry 'Δ' / '→' / emoji; force UTF-8 stdout so a Windows cp1252 console
    # prints them instead of raising (Telegram/DB consumers always see proper UTF-8).
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 — best-effort; ASCII-only terminals still run fine
        pass
    check_and_push()
