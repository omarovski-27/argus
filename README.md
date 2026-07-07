# Argus

A personal portfolio-intelligence system for one user, built to a simple thesis: **the
database is the product; every Telegram message is a view of it.** Argus ingests market,
macro, filings and brokerage data into a Postgres spine, synthesizes a weekly digest and
on-demand fundamental-analysis dossiers with an LLM that is never allowed to supply a
fact of its own, and keeps a trade journal whose success gates were registered before
the first trade.

It is deliberately boring: a deterministic pipeline, free-tier data sources, and
~$1–2/month of total running cost (LLM synthesis is the only spend).

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

**The pipeline (one engine):** fetch (wrapped) → store raw → URL dedup → Haiku sentiment
scoring → indicators computed locally (pandas_ta) → rank vs book → deltas vs last digest
→ Sonnet synthesis under a 5-clause contract → numeric grounding gate → store digest with
its frozen input bundle → Telegram.

**The analyst module (Phase 5):** `/analyze TICKER` → frozen data pack (SEC XBRL facts,
EDGAR 10-K/proxy excerpts, peers, consensus, news) → deterministic scenario valuation
(bear/base/bull range, sensitivity, reverse-DCF — never a point forecast) → Sonnet
dossier under an 8-stage structure with Graham / Buffett / Taleb framework verdicts →
Law-1 lint + grounding gate → stored in `analyses` with its exact pack → Telegram.

## The 8 Operating Laws

Every line of code answers to these; violating one is a correctness bug, not a style nit.

1. **Information, never instruction.** No output ever says buy/sell/"safe to trade" or
   suggests timing or sizing. The analyst module renders *framework verdicts*
   (cheap/expensive, fragile/antifragile) — analysis, never direction.
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

## Design decisions that matter

- **Insert-all + latest-filed view.** SEC XBRL facts are stored append-style in
  `fundamentals` (every filing's figure, with accession + filed date); reads go through
  the `fundamentals_latest` view (`DISTINCT ON`, latest `filed` wins). Restatements
  update the analytical view while the full audit trail stays queryable.
- **Split adjustment is a read-time layer.** Splits live in a `corporate_actions`
  ledger; share counts are adjusted at read time by filed date (`quant/splits.py`), so
  raw filings stay verbatim and adjusted series can never be double-applied.
- **The grounding gate (Law 2's enforcer).** After synthesis and before store/send,
  every number in the output must trace — tolerance- and suffix-aware — to the exact
  serialized block the model saw (`digest/grounding.py`). A figure the model computed
  or remembered fails the run. It has caught real violations: a model-derived "80 bps"
  spread in an early digest; model-summed segment revenue in the first dossier runs.
- **The Law-1 lint.** A regex gate over instruction *shapes* ("you should buy",
  "allocate 30%", "stop-loss at…") blocks a dossier from store/send, while analytical
  vocabulary ("share buybacks", "sell-side consensus", "exit multiple") passes — each
  pattern ships with counter-example tests.
- **Pre-registered gates.** The journal's success metric (sleeve-only Δshares) and its
  checkpoints (trades 10/20/50) live in `config`, seeded before the first trade. A
  guard refuses full config re-seeds against a live DB so registered parameters cannot
  be silently reverted.
- **Config is data, not schema.** Tunables (sleeve %, weekly cap, valuation grid,
  synthesis model) are JSONB config rows changed by single-key upserts — never
  migrations, never hardcoded constants.

## Current state (honest)

**Built and live:** the spine (16-table schema, wrapped ingestion for Tiingo / FRED /
IBKR Flex / Alpha Vantage / Reuters & Reddit RSS, local indicators); the weekly digest +
pulse with grounding gate; the Telegram bot (webhook with fail-closed auth, instant
commands); the journal engine (round-trip pairing, checkpoint pushes, `/felt`
annotations); the analyst module end to end (SEC facts mapper, data packs, valuation
engine, dossier synthesis with both gates, `/analyze` wiring).

**Not built:** Phase 3 quant models (Monte Carlo etc.); Phase 4 agentic add-ons. The
sleeve itself has no open position yet, so sleeve-entry derivation and split
auto-adjustment writes are dormant by design.

## Stack

Python 3.13 throughout. Supabase Postgres · Vercel Python serverless (stdlib
`BaseHTTPRequestHandler`, no framework) · GitHub Actions · `pandas_ta` · Anthropic API
(Haiku for sentiment scoring, Sonnet for synthesis — model ids are config rows).
Data: Tiingo, FRED, IBKR Flex Web Service, Alpha Vantage news sentiment, Reuters/Reddit
RSS, SEC EDGAR (XBRL company-facts + filings text), Finnhub, yfinance. All free tier.

## Running it

```bash
# tests (pure logic; no DB, no network)
python -m pytest -q

# read-only live probes
python -m quant.metrics                      # metrics table + identity checks
python -m digest.grounding [digest_id]       # re-validate a stored digest
python -m digest.pipeline --run-type monday --dry-run
python -m analyst.data_pack TSLA             # build + summarize a frozen pack
python -m analyst.dossier TSLA --print-only  # dossier + gates, nothing stored
```

Secrets ride in `.env` locally, GitHub repo secrets in Actions, and Vercel env vars in
the webhook (see `.env.example`).
