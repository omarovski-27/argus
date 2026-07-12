# Argus

[![Tests](https://github.com/omarovski-27/argus/actions/workflows/tests.yml/badge.svg)](https://github.com/omarovski-27/argus/actions/workflows/tests.yml)

A personal market-intelligence system that turns market, macro, filings and brokerage
data into a weekly grounded **digest**, a pre-registered **trade journal**, and on-demand
fundamental-analysis **dossiers** — all delivered through a single Telegram chat. The
guiding thesis is that the database is the product and every message is a view of it, and
that a language model may write the prose but is never allowed to supply a fact of its
own. It runs for about **$1–2/month** on free-tier infrastructure throughout (the LLM
synthesis is the only spend).

## The demo — `/analyze TICKER`

Text the bot a ticker; ~5 minutes later a dossier lands in the chat. It builds a frozen
data pack (SEC XBRL facts, EDGAR 10-K/proxy excerpts, peers, consensus, news), runs a
deterministic scenario valuation, has an LLM synthesize an 8-stage brief with
Graham / Buffett / Taleb verdicts, and only delivers it if it survives **four gates**.
Any US filer works; nothing numeric reaches you unless it traces to the filings.

A real excerpt from the TSLA brief (delivered plain-text, trimmed):

```
Bottom line: by these frameworks, TSLA is not worth buying at today's price of
$407.76 — Graham reads the price as expensive, above the conservative
intrinsic-value range, and price discipline governs regardless of business quality.
...
Valuation: a conservative, bear-weighted value of about $35.12 a share; against
today's price of $407.76 — no margin of safety. Working backwards from today's
price, the market is assuming about 78.8% revenue growth a year — weigh that
against the filed record above.
```

The **bottom-line rating** is not the model's opinion: it is mapped deterministically by
code from the three framework verdicts, so it can't drift run to run (see the laws below).

## Architecture — three surfaces, one spine

```
                       ┌───────────────────────────────┐
                       │        Supabase Postgres      │
                       │  (the spine — ALL state:      │
                       │  prices, macro, news+sentiment│
                       │  filings facts, transactions, │
                       │  round trips, digests,        │
                       │  analyses, config, fetch_log) │
                       └───────▲──────────────▲────────┘
                        reads/writes       reads/writes
                               │              │
        ┌──────────────────────┴───┐      ┌───┴──────────────────────────┐
        │  GitHub Actions          │      │  Vercel (Python serverless)  │
        │  all scheduled/heavy work│      │  the webhook "ear"           │
        │  • Mon 11:00 UTC digest  │      │  • /book /journal /felt      │
        │  • daily prices+journal  │      │    /skip /health /override   │
        │  • daily event filter    │      │    read the DB, reply ~1s    │
        │  • /pulse, /analyze runs │◄─────┤  • /pulse, /analyze reply    │
        │    (workflow_dispatch)   │      │    instantly, then dispatch  │
        └────────────┬─────────────┘      └──────────────────────────────┘
                     │ sends
                     ▼
                 Telegram (the one interface)
```

State lives in Postgres; the webhook does only instant reads (no heavy work — that
cold-start lag is the failure mode the topology exists to kill); every scheduled or
expensive job runs in GitHub Actions.

**The pipeline (one engine):** fetch (wrapped) → store raw → URL dedup → Haiku sentiment
scoring → indicators computed locally (pandas_ta) → rank vs book → deltas vs last digest
→ Sonnet synthesis under a fixed-clause contract → numeric grounding gate → store digest
with its frozen input bundle → Telegram.

## The 8 Operating Laws

Every line of code answers to these; violating one is a correctness bug, not a style nit.

1. **Information, never instruction.** No output says buy/sell/"safe to trade" or suggests
   timing or sizing. The analyst dossier does render one **bottom-line rating**
   (ATTRACTIVE / MIXED / UNATTRACTIVE *at current price*) — but it is derived by a
   deterministic code mapper from the three framework verdicts, injected *after* the
   instruction lint, and scoped to the dossier alone; every other surface stays purely
   informational, and timing/sizing are never rendered anywhere.
2. **Facts are retrieved, never generated.** The LLM writes prose; every number, date and
   claim must trace to a stored row. Each digest/dossier persists its exact frozen input,
   so outputs are reproducible forever. Missing data renders as "not available".
3. **Free-tier only.** Every external input is free; adding a paid one is an explicit
   decision, never a silent code change.
4. **Preparedness over prediction.** The forward calendar is first-class; forward views
   are scenario ranges with explicit assumptions, never single points.
5. **The core is untouchable.** Core holdings and DCA contributions never mix into the
   trading sleeve — that isolation makes the sleeve metric 100% trade-attributable.
6. **The journal is the verdict.** Gate metrics were pre-registered in config before
   trade #1; skipped trades are logged; gates are never reinterpreted post-hoc.
7. **Silent failure is misinformation.** Every fetch is wrapped (30s timeout, 2 retries,
   then marked unavailable), logged to `fetch_log`, and surfaced in output health lines.
8. **Boring beats clever.** Deterministic control flow; the simplest thing that works.

## Engineering decisions that matter

- **Facts are retrieved, never generated — and reproducibly.** Synthesis runs over a
  single serialized text block, and each output is stored *with the exact frozen bundle
  it was generated from*. A stored digest or dossier re-validates against its own input
  years later — the pipeline is a pure function of frozen state, not of whatever the APIs
  return today.
- **The numeric grounding gate (Law 2's enforcer).** After synthesis and before store or
  send, every number in the output must trace — tolerance- and suffix-aware — to the
  exact block the model saw (`digest/grounding.py`). A figure the model computed or
  remembered fails the whole run. It has caught real violations: a model-derived "80 bps"
  spread in an early digest; model-summed segment revenue in the first dossier runs.
- **The claims-lint (grounded-but-wrong).** The grounding gate proves every *number* is
  real; it cannot see a false *comparative*. "EPS peaked at 3.61 in FY 2022" cites two
  real pack points yet is false (the true peak is 4.30 in FY 2023). `analyst/claims.py`
  pre-computes each series' actual peak and trough into the block and enforces that any
  superlative cites the real extremum — catching the model's habit of promoting a recent
  salient value to an all-time high.
- **Insert-all + `DISTINCT ON` latest-filed view.** The SEC company-facts API surfaces
  only the current value for each period; older, superseded filings simply vanish from
  it. So Argus inserts *every* filing's figure append-style into `fundamentals` (with
  accession + filed date) and reads through a `fundamentals_latest` view (latest `filed`
  wins). Restatement history the source itself discards stays fully queryable.
- **Read-time split adjustment from original filings.** Each filing states share counts
  in the basis in effect at *its* filed date; comparatives in later filings arrive
  already restated — so raw EDGAR series are a basis patchwork (TSLA's diluted share
  count jumps at the 3:1 split). Filings are stored verbatim; splits live in a
  `corporate_actions` ledger; adjustment happens at read time by filed date
  (`quant/splits.py`), so a series can never be double-adjusted.
- **Pre-registered kill criteria.** The journal's success metric (sleeve-only Δshares)
  and its checkpoints (trades 10 / 20 / 50) were seeded into `config` before trade #1. A
  guard refuses full config re-seeds against a live DB, so a registered gate can never be
  silently reverted — the verdict is fixed before the experiment runs.
- **Fail-loud everywhere.** A source that lies is worse than one that's down. IBKR's Flex
  Web Service, for instance, returns **HTTP 200 with a `Status=Fail` body** — so the
  fetcher inspects the body, not the status code, distinguishes the transient
  "generating, try again" case from a real bad-token failure, logs to `fetch_log`, and
  surfaces it in the digest's health line rather than shipping a blank as if it were data.
- **Config is data, not schema.** Tunables (sleeve %, weekly cap, valuation grid,
  synthesis model, dossier length) are JSONB config rows changed by single-key upserts —
  never migrations, never hardcoded constants.

## Stack

Python 3.13 throughout. Supabase Postgres · Vercel Python serverless (stdlib
`BaseHTTPRequestHandler`, no framework) · GitHub Actions · `pandas_ta` · Anthropic API
(Haiku for sentiment scoring, Sonnet for synthesis — model ids are config rows).
Data (all free tier): Tiingo, FRED, IBKR Flex Web Service, Alpha Vantage news sentiment,
Reuters/Reddit RSS, SEC EDGAR (XBRL company-facts + filings text), Finnhub, yfinance.

## Status

**Live:** the spine (16-table schema, wrapped ingestion for all sources above, local
indicators); the weekly digest + pulse behind the grounding gate; the Telegram bot
(webhook with fail-closed auth, instant commands `/book /journal /felt /pulse /analyze
/skip /health /override`); the journal engine (round-trip pairing, checkpoint pushes,
`/felt` annotations, interactive sleeve-entry writer); and the analyst module end to end
(SEC facts mapper → data packs → valuation engine → dossier synthesis behind **four
gates** — instruction lint, numeric grounding, claims-lint, verdict-consistency —
plus the deterministic bottom-line rating and brief/full delivery modes). Live dossiers
span the framework space: GM (cheap value), TSLA and PLTR (expensive glamour, one
mediocre and one wonderful business). **440 tests**, green in CI.

**Funding-gated / not built:** the trading sleeve has no open position yet, so the split
auto-adjustment write and the journal's gate verdicts stay dormant by design until a
sleeve is registered. Phase 3 quant models (Monte Carlo etc.) and Phase 4 agentic
add-ons are not built.

**Honest limitations:** single-user by construction (one book, one chat, one operator);
US SEC filers only; a small class of filers that denominate XBRL facts in thousands is
handled but remains the fiddliest corner of the facts mapper.

## Running it

```bash
# tests (pure logic; no DB, no network)
python -m pytest -q

# read-only live probes
python -m quant.metrics                      # metrics table + identity checks
python -m digest.grounding [digest_id]       # re-validate a stored digest
python -m digest.pipeline --run-type monday --dry-run
python -m analyst.data_pack TSLA             # build + summarize a frozen pack
python -m analyst.dossier TSLA --print-only  # dossier + all four gates, nothing stored
```

Secrets ride in `.env` locally, GitHub repo secrets in Actions, and Vercel env vars in
the webhook (see `.env.example`). No credentials, chat IDs, or account values live in
this repository.
