"""/analyze handler tests (bot/handlers.handle_analyze) — the /pulse mirror.

Pure-logic with a monkeypatched ``httpx.post``: no network, no DB. The contract
under test: shape-gate the ticker BEFORE any dispatch (garbage and shell-hostile
strings never reach the workflow payload), mirror /pulse's env handling and
failure surface, and ack with the exact instant-reply text. ``load_dotenv`` is
no-op'd so a developer's local ``.env`` cannot leak into the env-var tests.
"""

import httpx
import pytest

import bot.handlers as handlers


class _Response:
    def raise_for_status(self) -> None:
        return None


@pytest.fixture()
def dispatch_env(monkeypatch):
    """Configured env + captured dispatch calls; returns the capture list."""
    calls: list[dict] = []

    def _post(url, headers=None, json=None, timeout=None):
        calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return _Response()

    monkeypatch.setattr(handlers, "load_dotenv", lambda **_: None)
    monkeypatch.setenv("GH_REPO", "omarovski-27/argus")
    monkeypatch.setenv("GH_DISPATCH_PAT", "test-pat")
    monkeypatch.setattr(handlers.httpx, "post", _post)
    return calls


def test_dispatches_analyze_workflow_with_ticker_and_acks(dispatch_env):
    reply = handlers.handle_analyze({"text": "/analyze tsla"})
    assert reply == "Building dossier for TSLA, ~5 min ⏳"
    assert len(dispatch_env) == 1
    call = dispatch_env[0]
    assert call["url"].endswith("/repos/omarovski-27/argus/actions/workflows/analyze.yml/dispatches")
    assert call["json"] == {"ref": "main", "inputs": {"ticker": "TSLA"}}
    assert call["headers"]["Authorization"] == "Bearer test-pat"


def test_class_share_tickers_normalize_to_the_sec_dash_form(dispatch_env):
    # SEC's ticker map and yfinance use BRK-B; the dot form users type is normalized
    # so a class-share /analyze doesn't produce a near-empty reduced-depth dossier.
    reply = handlers.handle_analyze({"text": "/analyze brk.b"})
    assert reply == "Building dossier for BRK-B, ~5 min ⏳"
    assert dispatch_env[0]["json"]["inputs"]["ticker"] == "BRK-B"


def test_missing_argument_returns_usage_without_dispatch(dispatch_env):
    reply = handlers.handle_analyze({"text": "/analyze"})
    assert reply == "Usage: /analyze TICKER (e.g. /analyze TSLA)"
    assert dispatch_env == []


@pytest.mark.parametrize("bad", ["7up!", "TOOLONGG", "TS;LA", "$(rm)", "..", "1TSLA", "x__y*"])
def test_malformed_ticker_is_rejected_without_dispatch_or_echo(dispatch_env, bad):
    reply = handlers.handle_analyze({"text": f"/analyze {bad}"})
    assert "doesn't look like a ticker" in reply
    # The refusal must not echo user text: it rides a Markdown-parsed send, where a
    # stray '_'/'*' 400s and turns the refusal into "Internal error".
    assert bad not in reply
    assert dispatch_env == [], f"shape gate let {bad!r} through to the dispatch"


def test_unconfigured_env_degrades_without_dispatch(monkeypatch):
    monkeypatch.setattr(handlers, "load_dotenv", lambda **_: None)
    monkeypatch.delenv("GH_REPO", raising=False)
    monkeypatch.delenv("GH_DISPATCH_PAT", raising=False)
    monkeypatch.setattr(
        handlers.httpx, "post",
        lambda *a, **k: pytest.fail("must not dispatch without GH_REPO/GH_DISPATCH_PAT"),
    )
    reply = handlers.handle_analyze({"text": "/analyze TSLA"})
    assert reply == "⚠️ Analyze unavailable — GH_REPO / GH_DISPATCH_PAT not configured."


def test_dispatch_failure_surfaces_type_but_never_the_pat(monkeypatch):
    monkeypatch.setattr(handlers, "load_dotenv", lambda **_: None)
    monkeypatch.setenv("GH_REPO", "omarovski-27/argus")
    monkeypatch.setenv("GH_DISPATCH_PAT", "secret-pat")

    def _boom(*a, **k):
        raise httpx.ConnectError("kaboom")

    monkeypatch.setattr(handlers.httpx, "post", _boom)
    reply = handlers.handle_analyze({"text": "/analyze TSLA"})
    assert "Couldn't trigger the dossier run (ConnectError)" in reply
    assert "secret-pat" not in reply
