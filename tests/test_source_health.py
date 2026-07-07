"""Tests for §5 Source-Health source exclusion — on BOTH surfaces (pure logic + fake client).

Regression target: ``telegram_webhook`` (the inbound command ear) and ``pipeline:*`` (step
outcomes) write fetch_log only on failure / dupe a source, so their last rows are stale failures.
They are NOT §5 data feeds — including them reddened Source Health while the data was fine. The
exclusion taxonomy lives in ``shared.sources`` so the digest verdict (``_aggregate_sources``) and
the ``/health`` command (``handle_health``) can't drift. Surviving rows must render unchanged, and
the webhook's failure rows must still be WRITTEN (this only changes the read-side verdict).
"""

from __future__ import annotations

import bot.handlers as handlers
from digest.bundle import _aggregate_sources
from shared.sources import NON_DATA_SOURCES, is_non_data_source, logical_source


def _fetch(source: str, status: str, at: str, error: str | None = None) -> dict:
    return {"source": source, "status": status, "at": at, "error": error}


def _sources(rows: list[dict]) -> set[str]:
    return {s["source"] for s in _aggregate_sources(rows)}


# --------------------------------------------------------------------------- #
# shared.sources — the taxonomy both surfaces share
# --------------------------------------------------------------------------- #
def test_logical_source_collapses_prefix():
    assert logical_source("tiingo:TSLA") == "tiingo"
    assert logical_source("pipeline:av_news") == "pipeline"
    assert logical_source("telegram_webhook") == "telegram_webhook"  # bare label is its own source


def test_is_non_data_source_matches_by_prefix():
    assert is_non_data_source("telegram_webhook")
    assert is_non_data_source("pipeline:av_news")
    assert is_non_data_source("pipeline:telegram")
    assert is_non_data_source("config_read:sleeve_shares")
    # The dossier pipeline: analyst:draft logs only on failure BY DESIGN (a repaired
    # draft is a normal outcome) — included, it would redden /health forever, the
    # exact telegram_webhook bug. Dossier failures surface via the red analyze.yml
    # run + Telegram alert, not §5.
    assert is_non_data_source("analyst:draft")
    assert is_non_data_source("analyst:law1")
    assert is_non_data_source("sec_facts:PLTR")
    assert not is_non_data_source("tiingo:TSLA")
    assert not is_non_data_source("ibkr_flex:positions")
    assert not is_non_data_source("journal:checkpoint_push")  # a real job, not excluded
    assert NON_DATA_SOURCES == {
        "pipeline", "telegram_webhook", "config_read", "analyst", "sec_facts"
    }


# --------------------------------------------------------------------------- #
# Surface 1 — the digest §5 verdict (_aggregate_sources)
# --------------------------------------------------------------------------- #
def test_digest_verdict_excludes_webhook_regardless_of_status():
    # Exclusion is by source identity (not a data feed), not by status.
    assert _sources([_fetch("telegram_webhook", "success", "2026-06-21T07:00:00+00:00")]) == set()
    assert _sources([_fetch("telegram_webhook", "failure", "2026-06-21T07:00:00+00:00")]) == set()


def test_digest_verdict_excludes_pipeline_and_keeps_real_feeds():
    rows = [
        _fetch("fred:DFF", "success", "2026-06-20T20:30:00+00:00"),
        _fetch("pipeline:synthesis", "failure", "2026-06-20T20:35:00+00:00"),
        _fetch("telegram_webhook", "failure", "2026-06-21T07:04:00+00:00", "404"),
        _fetch("ibkr_flex:get", "success", "2026-06-20T19:08:00+00:00"),
        _fetch("ibkr_flex:positions", "failure", "2026-06-20T19:12:00+00:00"),  # 19:12 > 19:08 wins
    ]
    out = {s["source"]: s["status"] for s in _aggregate_sources(rows)}
    assert out == {"fred": "success", "ibkr_flex": "failure"}  # no pipeline, no telegram_webhook


# --------------------------------------------------------------------------- #
# The config-read masking seam (PHASE0-TODO #4) — regression for the relabel
# --------------------------------------------------------------------------- #
def test_config_read_failure_cannot_be_masked_by_flex_section_successes():
    """Same run: config read fails BEFORE the section stores succeed (the real order).

    Under the old 'ibkr_flex:config' label the three later successes superseded the
    failure most-recent-wins inside the ibkr_flex verdict slot — the failure was in
    fetch_log but invisible on §5. Under 'config_read:*' the row is its own non-data
    source: it no longer competes in (or pollutes) the ibkr_flex slot at all, and the
    data verdict reports what is true — the data sections DID store.
    """
    rows = [
        _fetch("config_read:sleeve_shares", "failure", "2026-07-06T20:30:01+00:00", "read timeout"),
        _fetch("ibkr_flex:positions", "success", "2026-07-06T20:30:05+00:00"),
        _fetch("ibkr_flex:trades", "success", "2026-07-06T20:30:06+00:00"),
        _fetch("ibkr_flex:cash", "success", "2026-07-06T20:30:07+00:00"),
    ]
    out = {s["source"]: s["status"] for s in _aggregate_sources(rows)}
    assert out == {"ibkr_flex": "success"}  # data verdict true; no config_read slot invented


def test_config_read_failure_cannot_redden_a_healthy_flex_feed_later():
    """The inverse masking: a config failure FRESHER than the section rows (e.g. the
    sections' next scheduled run hasn't happened yet) must not flip ibkr_flex red —
    the old label did exactly that until the row scrolled out of the scan window."""
    rows = [
        _fetch("ibkr_flex:positions", "success", "2026-07-05T20:30:05+00:00"),
        _fetch("config_read:sleeve_shares", "failure", "2026-07-06T09:00:00+00:00", "blip"),
    ]
    out = {s["source"]: s["status"] for s in _aggregate_sources(rows)}
    assert out == {"ibkr_flex": "success"}


def test_old_label_documents_the_masking_seam_this_relabel_closes():
    """With the retired 'ibkr_flex:config' label, the same-run rows DID mask the
    failure — kept as executable documentation of why the relabel exists."""
    rows = [
        _fetch("ibkr_flex:config", "failure", "2026-07-06T20:30:01+00:00", "read timeout"),
        _fetch("ibkr_flex:positions", "success", "2026-07-06T20:30:05+00:00"),
    ]
    out = {s["source"]: s["status"] for s in _aggregate_sources(rows)}
    assert out == {"ibkr_flex": "success"}  # the failure vanished from the verdict — the seam


# --------------------------------------------------------------------------- #
# Surface 2 — the /health command (handle_health), via a minimal fake client
# --------------------------------------------------------------------------- #
class _Result:
    def __init__(self, data):
        self.data = data


class _Builder:
    def __init__(self, data):
        self._data = data

    def select(self, *a, **k):
        return self

    def order(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def execute(self):
        return _Result(self._data)


class FakeHealthClient:
    def __init__(self, fetch_rows, digests=None, config_rows=None):
        self._t = {"fetch_log": fetch_rows, "digests": digests or [], "config": config_rows or []}

    def table(self, name):
        return _Builder(self._t.get(name, []))


def test_health_command_excludes_non_data_but_keeps_raw_survivors(monkeypatch):
    rows = [
        {"source": "tiingo:TSLA", "status": "success", "created_at": "2026-06-21T14:10:00+00:00", "error": None},
        {"source": "telegram_webhook", "status": "failure", "created_at": "2026-06-21T07:04:00+00:00", "error": "404"},
        {"source": "pipeline:av_news", "status": "failure", "created_at": "2026-06-14T09:35:00+00:00", "error": "x"},
        {"source": "config_read:sleeve_shares", "status": "failure", "created_at": "2026-06-21T20:30:00+00:00", "error": "blip"},
    ]
    monkeypatch.setattr(handlers, "get_client", lambda: FakeHealthClient(rows))
    out = handlers.handle_health({})
    assert "telegram_webhook" not in out          # the false-red is gone from /health
    assert "pipeline" not in out                  # pipeline:* dropped too (same category)
    assert "config_read" not in out               # infra category, not §5 data (PHASE0-TODO #4)
    assert "tiingo:TSLA" in out                   # surviving source still rendered RAW (no collapse)
