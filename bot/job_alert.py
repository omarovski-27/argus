"""Argus bot — scheduled-job failure alert (Law 7 / §12: fail loud, in Telegram).

A red GitHub Actions run only produces a GitHub e-mail, but the operating surface of
this system is Telegram — the 2026-07-03 Daily Data failure sat unseen for two days.
Each scheduled workflow therefore ends with an ``if: failure()`` step that runs this
module, so any hard step failure lands as a push with a link to the run.

It is a plain ops alert: fixed text plus the run URL from the default ``GITHUB_*``
env — no market data, no synthesis (Laws 1/2 have nothing to add here). It sends
plain text (no Markdown parse mode), because the alert must never itself fail on an
unescaped entity. If the send fails it raises: the step goes red in the run log and
the GitHub e-mail remains the fallback — never a silent swallow (Law 7).

Run:  python -m bot.job_alert "Daily Data"
"""

from __future__ import annotations

if __name__ == "__main__" and __package__ in (None, ""):
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import os
import re
import sys

from bot.telegram import send_message

# For a dossier run, WHICH gate blocked (and how many findings) turns an opaque
# "Analyze failed" into an actionable line — a grounding/law1/claims/verdicts block
# is a DESIGNED outcome (a bad draft caught), not an infra crash. Surface only the
# gate NAME + the finding COUNT; the raw tokens/context stay in fetch_log (they can
# carry filing internals, so they never ride to the push).
_REASON_NOUN_RE = re.compile(r"(\d+)\s+(figure|superlative|instruction|problem|verdict)", re.IGNORECASE)


def _analyst_gate_reason() -> str | None:
    """The latest analyst gate failure as 'grounding gate (11 figures)', or None.

    Best-effort and fully guarded: any DB error yields None so the base alert still
    sends. Reads the most recent analyst:* failure in a short window (the run that
    just failed); its source suffix is the gate, the leading count its size.
    """
    try:
        from datetime import datetime, timedelta, timezone

        from shared.db import get_client

        since = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
        rows = (
            get_client().table("fetch_log")
            .select("source,error,created_at")
            .like("source", "analyst:%")
            .eq("status", "failure")
            .gte("created_at", since)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
            .data
        ) or []
    except Exception:  # noqa: BLE001 — the alert must never depend on this
        return None
    if not rows:
        return None
    gate = (rows[0].get("source") or "analyst:?").split(":", 1)[-1]
    if gate in ("draft", "repair", "synthesis", "pack"):
        return None  # not a terminal gate block — the base alert is enough
    m = _REASON_NOUN_RE.search(rows[0].get("error") or "")
    detail = f" ({m.group(1)} {m.group(2).lower()}s)" if m else ""
    return f"{gate} gate{detail}"


def send_job_alert(workflow: str) -> None:
    """Push '<workflow> failed' + the Actions run URL to Telegram (raises on failure).

    Args:
        workflow: Human label for the failing workflow/job, e.g. 'Daily Data / prices'.

    The run URL is assembled from the GITHUB_SERVER_URL / GITHUB_REPOSITORY /
    GITHUB_RUN_ID env vars every Actions job defines; outside Actions (a manual local
    run) the URL line says so instead of fabricating a link (Law 2's spirit). For the
    Analyze workflow a gate-reason line is appended when a dossier gate blocked, so a
    designed rejection reads differently from an infra crash — without leaking internals.
    """
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    repo = os.environ.get("GITHUB_REPOSITORY")
    run_id = os.environ.get("GITHUB_RUN_ID")
    url = (
        f"{server}/{repo}/actions/runs/{run_id}"
        if repo and run_id
        else "(no run URL — not running in Actions)"
    )
    reason_line = ""
    if "analyze" in workflow.lower():
        reason = _analyst_gate_reason()
        if reason:
            reason_line = f"\nreason: {reason} — a designed block, not a crash."
    send_message(
        f"🔴 {workflow} failed.\n{url}\n"
        f"fetch_log has the failing source + error (Law 7).{reason_line}",
        parse_mode="",  # plain text: an alert must not fail on entity parsing
    )


if __name__ == "__main__":
    send_job_alert(sys.argv[1] if len(sys.argv) > 1 else "Scheduled job")
