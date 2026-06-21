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

``synthesize()`` runs the full §7 five-clause contract (Farm B): it serializes the frozen
bundle into a labeled text block (``digest.serialize``) — never raw JSON — and Sonnet
writes the five sections grounded in that block. The no-recommendation (Law 1) and
grounding (Law 2) clauses are binding in the system prompt.

Run:  python -m digest.pipeline --run-type monday   (or: ... --run-type pulse)
      python -m digest.pipeline --run-type monday --dry-run   (freeze bundle, no
          synthesis/store/Telegram — see ``_dry_run_finish``)
"""

from __future__ import annotations

if __name__ == "__main__" and __package__ in (None, ""):
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone

import httpx
from anthropic import Anthropic
from dotenv import load_dotenv

from digest.bundle import assemble_bundle
from digest.dedup import get_unscored_headline_ids
from digest.sentiment import score_headlines
from digest.serialize import serialize_bundle
from ingestion.fred import fetch_macro
from ingestion.indicators import compute_indicators
from ingestion.news_av import fetch_av_news
from ingestion.news_reddit import fetch_reddit_news
from ingestion.news_wire import fetch_wire_news
from shared.db import get_client
from shared.fetch_logger import write_fetch_log

_SONNET_MODEL = "claude-sonnet-4-6"
_TELEGRAM_LIMIT = 4096  # Telegram sendMessage hard cap (chars)
_DRY_RUN_BUNDLE_PATH = "_bundle_dryrun.json"  # gitignored scratch (see --dry-run)

# Synthesis system prompt — the §7 five-clause contract (grounding / no-recommendation /
# fixed structure / interpretation layer / uncertainty marking). Law 1 and Law 2 are
# binding here on EVERY run. The model is fed the labeled text block from
# ``digest.serialize`` (never raw JSON), so it can only cite labels present in that block.
_SYNTH_SYSTEM = """You are writing this week's Argus digest — a market-intelligence note for one reader who makes his own trading decisions. Synthesize only the DATA below into a fixed five-section note. You do not advise.

Hard rules:
1. Grounding. Every factual claim comes from the DATA block. No outside facts, prices, or events; never invent or estimate a number — and no market-lore thresholds or historical analogies that aren't in the DATA (e.g. don't assert an RSI "overbought above 70" line, a VIX "20" threshold, or that a curve "moved away from inversion" unless the DATA states it). Describe the value and its plain meaning; don't import textbook levels. You MAY locate a value on a genuinely BOUNDED, intrinsic scale (RSI or stochastics on 0-100; a sentiment magnitude on 0-1) — but an UNBOUNDED quantity (VIX, prices, yields, index levels, spreads, MACD) has no such scale, so never put one on an invented "0-100" or "theoretical range." For ANY value, do NOT call it high/low/elevated/contained/normal or set it in a "typical/historical range" unless the DATA supplies the anchor — a stated range, percentile, average, or prior reading (e.g. VIX is given with its trailing range and percentile; use that — and VIX is IMPLIED, forward-looking volatility: never call it "realized" or historical volatility). Likewise do NOT grade the SIZE of an unbounded move with adverbs (slightly, modestly, sharply, strongly) when the DATA gives no magnitude anchor — state the value and its sign/direction (e.g. "MACD below its signal line, histogram negative"), not how large the move is. With no anchor in the DATA, state the value and its direction only. Do not compute new figures from the data — no spreads, differences, premiums, ratios, sums, or percentage changes of your own; cite only numbers the block already provides (it pre-computes the price deltas and the rate spreads you may use). If a comparison you want isn't in the block, describe each value on its own; never derive the gap, and never label a self-made figure "by the data." If something needed is missing, say so.
2. No recommendations. Never tell the reader to buy, sell, enter, exit, trim, add, hold, or wait; never call anything a good/bad entry, "safe to trade," or well-timed — including implicit nudges ("this setup looks attractive," "momentum favors…"). Describe the condition; don't direct the action.
3. Fixed structure. Exactly these five sections, this order, every week: Regime / What Moved / Forward Calendar / Your Book / Source Health. If a section has no data, keep the header and state plainly what's missing.
4. Interpret every number. No bare figures — say what each value means by where it sits on its own scale (e.g. "RSI14 72 — high on its 0-100 range"), not by importing a textbook level. Interpretation describes a condition; it never prescribes an action. Open each section with ONE plain sentence stating its overall current read, drawn only from that section's own figures, then give the per-number detail beneath it. This lead characterizes the present state only — what the numbers collectively say right now (e.g. REGIME: "Equities sit in an uptrend with momentum softening; the macro backdrop is mild and partly stale"). It must never project a trajectory ("set to roll over," "likely to break higher") or imply an action — that remains clause 2. YOUR BOOK's lead sentence leads with the sleeve ticker's (TSLA) current technical condition and sleeve status, the reader's decision focus. In the synthesis, interpret unbounded readings (MACD, spreads, yields, prices, index levels) by sign, direction, and plain meaning — never by magnitude adverbs the data hasn't anchored. "A positive 10Y-2Y spread — the curve is not inverted," not "a modest positive spread"; "momentum softening across all three," not "TSLA most negative." Magnitude or ranking of unbounded values needs a data-supplied anchor (as VIX has its trailing range); without one, sign and direction only. This is clause 1 holding inside the synthesis.
5. Mark uncertainty. Flag stale, missing, or low-confidence data explicitly. Never present stale or absent data as current or complete.

Sections:
- Regime — from INDICATORS and MACRO: trend (price vs SMA50/200), momentum (RSI, MACD), macro backdrop. Interpretation beside each number. State the regime; don't judge whether it's a moment to act.
- What Moved — from HEADLINES only: synthesize the period's themes grouped by theme; do NOT enumerate headline-by-headline. Lead with WATCHLIST & MARKET NEWS. Treat RETAIL CHATTER (Reddit) as unverified retail sentiment — you may note what retail is fixated on, but never present a Reddit claim as established fact. Paraphrase — don't reproduce headline text. No claim without a headline behind it. You may state the overall balance of the reported flow in one phrase — constructive, mixed, or negative, plus the main dissenting item (e.g. "coverage skewed constructive on Q2 fundamentals, with ARK's rotation the main offsetting note"). This describes the tilt of what was REPORTED — the balance of the headlines themselves — never what that flow implies for price or position. "Coverage skewed constructive" is allowed; "positioned to run" is a forecast and forbidden (clause 2).
- Forward Calendar — from CALENDAR: list each event on its own line with its date, type and materiality. The block appends a bracketed rule to any event that arms one (e.g. "[event filter — blocks sleeve round trips within 24h (§8)]"); for a tagged event, state that rule as a plain fact. For an event with NO bracketed tag, give only its date/type/materiality and stop — do not add "no rule on record", "not filtered", "no event filter", or any remark about a rule being absent (an untagged event simply carries none). State rules; never translate them into advice.
- Your Book — from BOOK: positions, round-trips-used vs the weekly cap, sleeve/phase status. If positions are unavailable, say exactly why (account not funded; Flex blind) — do not imply a funded-but-empty book. No verdict on the book.
- Source Health — render the provided summary and staleness flags plainly. Name every failed, unavailable, or stale source. This section exists to surface problems; never soften or omit them.

Length/tone: HARD CAP 800 words (aim ~700) — be economical. In Regime especially, group tickers with similar readings into one statement (e.g. "SPY and QQQ both sit above their 50- and 200-day averages with RSI in the mid-50s") rather than repeating every figure ticker-by-ticker. Plain analyst prose, no hype, no filler hedging, no preamble. Confident about what the data says, explicit about what it doesn't. State each figure and fact once; do not restate the same value or condition twice (VIX's level and range-position in one sentence; the blind/empty book stated once). Do not narrate your own restraint or method ("no directional editorial attached," "not self-computed," "no rule on record") — state the fact or omit it.

Format: plain text only — the reader's client renders raw characters, so any markup prints literally as clutter. Use NO Markdown or structural symbols: no #, *, _, backticks, >, pipes/tables, or "-"/"1." list bullets. Write each section title as a plain CAPITALS line (REGIME, WHAT MOVED, FORWARD CALENDAR, YOUR BOOK, SOURCE HEALTH), separate paragraphs with a blank line, and inside a section use plain sentences or simple "Label: value" lines with no leading symbol."""


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
    """Synthesize the digest prose from the frozen bundle with Sonnet (§7 five-clause contract).

    The bundle is rendered to a labeled text block by :func:`digest.serialize.serialize_bundle`
    (never raw JSON) — the model can only cite labels present in that block, which is how
    grounding (Law 2) and the no-recommendation rule (Law 1) stay enforceable.

    Args:
        bundle: The frozen synthesis input from :func:`digest.bundle.assemble_bundle`.

    Returns:
        The digest text — five sections (Regime / What Moved / Forward Calendar / Your Book
        / Source Health) grounded in the serialized block.
    """
    client = Anthropic(api_key=_anthropic_key())
    message = client.messages.create(
        model=_SONNET_MODEL,
        max_tokens=2000,
        system=_SYNTH_SYSTEM,
        messages=[{"role": "user", "content": f"DATA:\n\n{serialize_bundle(bundle)}"}],
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


def _dry_run_finish(bundle: dict, run_id: str, path: str = _DRY_RUN_BUNDLE_PATH) -> None:
    """Dry-run tail: freeze the assembled bundle to a scratch file + print a completeness summary.

    Replaces the live tail (synthesis -> store -> Telegram) with NO Sonnet call, NO digest
    row, and NO Telegram push. Writes the exact ``bundle_json`` to ``path`` (gitignored) so the
    synthesis prompt can later be iterated against a FIXED bundle without re-fetching or
    spending, and prints per-section counts so the bundle's completeness is verifiable at a
    glance. Iterates the bundle's own dicts (which always carry all 6 macro series and all 4
    tracked symbols as keys), so a MISSING series or a SUPPRESSED young ticker is visible.
    """
    # Windows consoles default to cp1252, which can't encode characters that legitimately
    # appear in Reddit/AV titles (full-width '？', emoji) or the Δ below — and an
    # UnicodeEncodeError mid-summary would abort AFTER the bundle was frozen but BEFORE the
    # "no synthesis/store/Telegram" confirmation prints. Reconfigure to UTF-8 (errors=
    # 'replace' as a floor) so a summary line can never crash the run.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 — non-reconfigurable stream (capture/redirect): best-effort
        pass

    with open(path, "w", encoding="utf-8") as fh:
        json.dump(bundle, fh, ensure_ascii=False, indent=2, default=str)

    macro = bundle.get("macro") or {}
    present = sum(1 for v in macro.values() if v)
    print(f"[dry-run] macro: {present}/{len(macro)} FRED series with a value")
    for series_id, row in macro.items():
        if row:
            print(f"           {series_id:<9} = {row.get('value')}  ({row.get('date')})")
        else:
            print(f"           {series_id:<9} = MISSING")

    by_source: dict[str, dict] = {}
    for h in bundle.get("headlines") or []:
        b = by_source.setdefault(
            h.get("source") or "?", {"n": 0, "methods": set(), "sample": None}
        )
        b["n"] += 1
        for s in h.get("sentiment") or []:
            if s.get("method"):
                b["methods"].add(s["method"])
        if b["sample"] is None and (h.get("title") or "").strip():
            b["sample"] = h["title"].strip()
    total = sum(b["n"] for b in by_source.values())
    print(f"[dry-run] headlines: {total} in last 48h")
    for src in sorted(by_source):
        b = by_source[src]
        methods = ", ".join(sorted(b["methods"])) or "NONE"
        print(f"           {src:<12} {b['n']:>3}   sentiment: {methods}")
        print(f"           {'':<12}       e.g. {(b['sample'] or '')[:90]!r}")

    print("[dry-run] indicators:")
    for symbol, ind in (bundle.get("indicators") or {}).items():
        names = sorted(((ind or {}).get("values") or {}).keys())
        if names:
            print(f"           {symbol:<5} present (as_of {ind.get('as_of')}): {', '.join(names)}")
        else:
            print(f"           {symbol:<5} SUPPRESSED (no indicator rows — young ticker)")

    pos = bundle.get("positions") or {}
    rt = bundle.get("round_trips") or {}
    cal = bundle.get("calendar") or []
    print(
        f"[dry-run] book: positions snapshot {pos.get('date')} — "
        f"{len(pos.get('rows') or [])} row(s); cumulative Δshares = "
        f"{rt.get('cumulative_delta_shares')}"
    )
    nxt = ""
    if cal:
        e = cal[0]
        label = f"{e.get('date')} {e.get('type') or ''}".strip()
        if e.get("symbol"):
            label += f" {e['symbol']}"
        nxt = f" (next: {label})"
    print(f"[dry-run] calendar: {len(cal)} event(s) in next 14d{nxt}")
    print(f"[dry-run] config: {len(bundle.get('config') or {})} key(s)")

    sh = bundle.get("source_health") or {}
    print(f"[dry-run] source health: {sh.get('summary')}")
    for s in sh.get("sources") or []:
        flag = "" if s.get("status") == "success" else "   <-- not OK"
        print(f"           {str(s.get('source')):<16} {s.get('status')}{flag}")
    stale = sh.get("staleness") or {}
    p = stale.get("prices") or {}
    fx = stale.get("flex") or {}
    print(
        f"           prices: latest {p.get('latest_date')} "
        f"({p.get('trading_days_old')} trading-day(s) old) stale={p.get('stale')}"
    )
    print(
        f"           flex:   last success {fx.get('last_success_at')} "
        f"({fx.get('hours_old')}h old) stale={fx.get('stale')}"
    )
    tok = sh.get("flex_token") or {}
    print(
        f"           flex token: days_to_expiry={tok.get('days_to_expiry')} "
        f"known={tok.get('known')} warn={tok.get('warn')}"
    )

    print("[dry-run] NO Sonnet call, NO digest row, NO Telegram — cost = Haiku scoring only.")
    print(f"[dry-run] full bundle written to {path} (run {run_id}).")


def run_pipeline(
    run_type: str = "monday", run_id: str | None = None, dry_run: bool = False
) -> None:
    """Run the full Phase-1 pipeline for ``run_type`` (blueprint §3 / §6 / §7).

    Args:
        run_type: 'monday'/'full' (full weekly digest) or 'pulse' (light run; skips
            news, scoring and indicators and synthesizes from the DB as-is).
        run_id: Optional run identifier; a uuid4-based one is generated if omitted.
        dry_run: If True, run the full data path and assemble the bundle exactly as a
            monday/full run, then write the frozen bundle to ``_bundle_dryrun.json`` and
            print a summary INSTEAD of synthesizing, storing a digest, or sending Telegram.

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

    if dry_run:
        _dry_run_finish(bundle, run_id)
        print(f"[pipeline] done (dry-run, no synthesis/store/telegram) run_id={run_id}")
        return

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
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run the full data path + assemble the bundle, write it to "
        "_bundle_dryrun.json and print a summary — NO synthesis, digest row, or Telegram.",
    )
    args = parser.parse_args()
    run_pipeline(run_type=args.run_type, dry_run=args.dry_run)
