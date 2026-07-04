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
import sys

from bot.telegram import send_message


def send_job_alert(workflow: str) -> None:
    """Push '<workflow> failed' + the Actions run URL to Telegram (raises on failure).

    Args:
        workflow: Human label for the failing workflow/job, e.g. 'Daily Data / prices'.

    The run URL is assembled from the GITHUB_SERVER_URL / GITHUB_REPOSITORY /
    GITHUB_RUN_ID env vars every Actions job defines; outside Actions (a manual local
    run) the URL line says so instead of fabricating a link (Law 2's spirit).
    """
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    repo = os.environ.get("GITHUB_REPOSITORY")
    run_id = os.environ.get("GITHUB_RUN_ID")
    url = (
        f"{server}/{repo}/actions/runs/{run_id}"
        if repo and run_id
        else "(no run URL — not running in Actions)"
    )
    send_message(
        f"🔴 {workflow} failed.\n{url}\nfetch_log has the failing source + error (Law 7).",
        parse_mode="",  # plain text: an alert must not fail on entity parsing
    )


if __name__ == "__main__":
    send_job_alert(sys.argv[1] if len(sys.argv) > 1 else "Scheduled job")
