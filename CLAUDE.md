# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project state

Argus is a personal portfolio-intelligence system (digest + trade journal + quant + fundamental-analysis dossiers) for a single user, Omar. **The MVP (Phases 0–2) is largely built and deployed** — see the per-phase state below. The specs remain the source of truth for *what to build*; for *what is already built*, trust `git log`, never a prose snapshot (this section included — refresh it against the log when it drifts).

- `argus-blueprint.md` (v2.0, FINAL) — master spec. The single source of truth for everything in Phases 0–4.
- `argus-analyst-module.md` (v1.0) — the Phase 5 fundamental-analysis module. Independently buildable; built **after** the MVP.

**Current state (as of 2026-07-05 — verify against `git log`, not this prose):**
- **Phase 0 (spine) — done.** 16-table schema applied to the live DB; wrapped fetchers (`shared/fetcher_base.py`), Supabase client (`shared/db.py`), `fetch_log` writer (`shared/fetch_logger.py`); ingestion for Tiingo, FRED, IBKR Flex, Alpha Vantage / MarketWatch / Reddit news, locally-computed indicators; seeds for instruments, prices, calendar, and `config` (gates seeded + verified against a raw read-back).
- **Phase 1 (digest + bot) — done.** Full pipeline (`digest/`: fetch → URL dedup → Haiku sentiment → local indicators → bundle → Sonnet 5-clause synthesis → numeric grounding gate (`digest/grounding.py`, Law 2 enforced pre-store/pre-send) → store + frozen `bundle_json` → Telegram), plus `--dry-run`. The Vercel webhook (`api/webhook.py`) with fail-closed secret + chat-id auth (`bot/webhook_auth.py`); bot handlers `/book /journal /skip /health /override /pulse /felt` (`bot/handlers.py`); GitHub Actions `daily.yml` (two independent jobs: prices, journal), `digest.yml`, `event_filter.yml` — every scheduled workflow ends with an `if: failure()` Telegram alert (`bot/job_alert.py`).
- **Phase 2 (journal) — largely done.** Round-trip pairing, checkpoint engine + proximity/verdict push (`push_log` fire-once), in-the-moment `/felt` annotations + reconcile.
- **Phase 5 (analyst) — data layer built, dossier engine not.** SEC company-facts mapper (11 concepts incl. capex, `ingestion/sec_facts.py`) → `fundamentals`/`fundamentals_latest`; `corporate_actions` split ledger seeded (TSLA 5:1 2020-08-31, 3:1 2022-08-25; `ingestion/seed_corporate_actions.py`); filed-date-keyed split-adjustment reads (`quant/splits.py`); read-time metrics with per-input provenance — margins, CAGR, consistency, FCF proxy, split-adjusted EPS (`quant/metrics.py`). Not built: `/analyze`, data_pack assembly, framework synthesis.
- **Not built:** Phase 3 (quant models).
- **Funding landed.** The IBKR account is funded and Flex is live: first real fills 2026-06-26 (TSLA 2 sh + SPCX 1 sh, both auto-classified `dca_buy`), so `daily.yml` now hard-fails Flex — the unfunded-era `continue-on-error` soft-fail is retired (Law 7). The live Flex query emits **dd/MM/yyyy** dates (`ingestion/ibkr_flex.py` parses this since 2026-07-05; before that, every statement date resolved to None → positions dropped nightly 06-26→07-04, `exec_time` NULL on both fills, funding deposit never reached `contributions` — all under green fetch_log rows, now hardened to fail loud on dropped rows). **Open backfill (Omar, portal-side):** temporarily widen the Flex query period (e.g. Last 30 Calendar Days), re-pull once to recover the deposit into `contributions`, backfill the two fills' `exec_time` (upserts are `ignore_duplicates`, so that needs a one-off update), then restore LastBusinessDay.

## Non-negotiable invariants (the 8 Operating Laws, as code rules)

These are unusual constraints that govern every line of code. Violating one is a correctness bug, not a style nit. Full text in blueprint §1.

1. **Information, never instruction.** No code path may emit buy/sell/"safe-to-trade"/timing/sizing language. Every synthesis prompt carries an explicit no-recommendation clause. (The Phase 5 analyst module may render *framework verdicts* — cheap/expensive, robust/fragile — but still never "buy now", "enter at $X", or "put 30% in".)
2. **Facts are retrieved, never generated.** The LLM synthesizes prose; it never supplies a date or number from memory. Every figure in any output must render from a stored DB row. Synthesis runs on a frozen input bundle, and each digest persists its exact `bundle_json` (each analysis its `data_pack_json`) so outputs are reproducible forever. If a fact isn't in the DB, the output says "not available" — it never fills the gap.
3. **Free-tier only.** Every external input must be free tier; adding any paid input is a separate explicit decision, never a silent code change. **X/Twitter is permanently excluded.** Respect per-source budgets — Alpha Vantage's 25 req/day is reserved 100% for news sentiment.
4. **Preparedness over prediction.** The forward calendar is first-class and rendered from `calendar_events` — never hallucinated. No single-point forecasts anywhere; forward views are scenario ranges with explicit assumptions.
5. **The core is untouchable.** Core holdings + all DCA contributions never enter the sleeve. This isolation is what makes the sleeve-only Δshares metric 100% trade-attributable — never write logic that mingles DCA buys into sleeve accounting.
6. **The journal is the verdict.** Gate metrics are pre-registered in `config` before trade #1; skipped trades are logged to `skip_log`; gates are never reinterpreted post-hoc. **Phase 2 (journal) must ship before the first round trip.**
7. **Silent failure is misinformation.** Every fetcher is wrapped; every failure is logged to `fetch_log` *and* surfaced in output (Source Health line, staleness flags, critical push alerts). Never swallow an exception that hides missing data.
8. **Boring beats clever.** Deterministic pipeline for v1. No agentic / LLM-driven control flow until Phase 4 at the earliest. Prefer the simplest thing that works.

## Architecture: three surfaces, one spine

The database is the product; every Telegram message is a view of it. Architecture diagram in blueprint §3, hosting rationale in §2 item 11.

- **Supabase Postgres — the spine.** All state lives here. Schema in blueprint §4. Read it directly from the bot; write to it from the pipeline.
- **Vercel (Python serverless) — the webhook ear.** Handles the Telegram webhook. Instant commands (`/book`, `/journal`, `/skip`, `/health`, `/override`) read Supabase directly and reply in ~1s. **Do no heavy work here** — that cold-start lag is the exact failure mode this topology was chosen to kill. `/pulse` and `/analyze` reply with an instant "Generating ⏳" ack and trigger GitHub Actions via `workflow_dispatch`.
- **GitHub Actions — all scheduled and heavy jobs.** The pipeline, Flex pulls, trade detection, event-filter checks, dossier runs. Schedules:
  - **Mon 11:00 UTC** — full weekly digest
  - **Daily 20:30 UTC** (weekdays) — IBKR Flex + prices + trade detection
  - **Daily 12:30 UTC** — event-filter check → morning warning push (15:30 Amman)
  - **workflow_dispatch** — `/pulse` (light pipeline) and `/analyze` (Phase 5 dossier)

**The pipeline (one engine):** `fetch (wrapped) → store raw → URL dedup → Haiku sentiment scoring → compute indicators locally (pandas_ta) → rank vs book → deltas vs last digest → Sonnet synthesis (5-clause contract) → store digest (+ bundle_json) → Telegram`.

## Critical design decisions (easy to get wrong)

- **Trade classification is by quantity proximity.** Sleeve round-trips are ~17 shares; DCA buys are ~0.6–2 shares. `transactions.trade_type` is auto-assigned; `transactions.override_type` (nullable, set via `/override`) **always wins**. Round-trip pairing: same-day sell of qty ≥ `0.8 × sleeve_shares` followed by a similar-qty rebuy.
- **The core metric is sleeve-only Δshares** (more shares = winning) — direction-neutral and contribution-proof. Pre-registered gates live in `config`: trade 10 (early warning), 20 (checkpoint → Phase B), 50 (verdict). See blueprint §8.
- **`config` is JSONB rows + `updated_at`, not schema.** Parameter changes (sleeve_pct, bracket, phase, weekly cap, watchlist) are config-row edits — never migrations, never hardcoded constants. Read these values from `config` at runtime. **Applied-DDL caveat:** `config` is keyed `key text primary key`, so updating a parameter *overwrites* its single row (refreshing `updated_at`) — it does NOT keep in-DB history. Historical auditability of the gates/params therefore rests on their being pinned in the git-tracked blueprint + seed (Law 6), not on config row-versions; a `config_history` table/trigger is deferred. **Re-seed hazard:** `ingestion/seed_config.py` upserts *all* config keys at once, so a full re-run against a live DB silently reverts any drift — an advanced `phase`, a split-adjusted `sleeve_shares` — back to the seed defaults (an L6 corruption). **Never full-re-seed a live DB**; use single-key upserts for one-key changes. The guard is now built: `seed_config` REFUSES a full run once any config row exists, and `--missing-only` upserts only absent seed keys (there is deliberately no `--force`).
- **Sleeve symbol is config-driven and fail-loud.** The single sleeve ticker lives in `config.sleeve_symbol`, resolved at runtime — the twice-defined `_SLEEVE_SYMBOL = "TSLA"` constant (in `bot/handlers.py` and `journal/checkpoint.py`) is retired, and the single config row is the source of truth, closing the cross-surface drift footgun. Resolution **refuses to guess** on a missing/blank/non-string row: the `/felt` webhook returns a refusal without staging a note, the checkpoint engine raises (surfaced via `fetch_log`, Law 7). A wrong ticker is a corrupt journal row / wrong Δshares divisor (Law 6), strictly worse than the old constant — so there is deliberately **no default** (unlike numeric params like `sleeve_shares`, which may fall back). Resolution is **module-local** (`handlers._sleeve_symbol`, `checkpoint._load_sleeve_symbol`) by design; a shared `shared/config` read-helper unification is a separate future effort, not bolted on here. (Ship status: trust `git log`.)
- **Young-ticker indicator suppression.** SPCX listed 2026-06-12 with almost no history. `instruments.first_trade_date` drives suppression — do not compute or display an indicator that lacks enough history (no SMA50 until 50 sessions exist, etc.).
- **`sleeve_shares` is derived, never hardcoded.** At sleeve entry, `sleeve_shares = floor(sleeve_pct × live_portfolio_value ÷ TSLA_price)` (portfolio value = Flex positions market_value + cash). It is then frozen as the registered unit through the 50-trade verdict and re-derived only at a phase gate — this keeps the absolute Δshares thresholds (−1.0, < 0) meaningful (L6). `config` does not seed a `sleeve_shares` constant — it is unset until a sleeve opens (the stale illustration row `17` was deleted from the live DB 2026-07-01); the read path treats "unset" as no active sleeve (graceful), while code that requires it fails loud. Corporate Actions auto-adjust the frozen count on splits (the split ledger + read layer exist; the auto-adjust write fires only once a sleeve is active).
- **SPCX conditional unlock is monitored from `prices_eod`:** close ≥ $175.50 on ≥5 of 10 sessions post-Q2-earnings arms it. SPCX calendar seed in blueprint §14.

## Source & reliability contract

Every external call is wrapped (this is Law 7 in code): **30s timeout, 2 retries 30s apart, then mark unavailable** + add a footer/health line. Log `source / run_id / status / latency_ms / error` to `fetch_log` every run. Staleness: prices >2 trading days or Flex >48h → warn in digest. Critical alerts: Flex fails 2 days (journal goes blind) and Monday digest total failure (auto-retry once at +1h, then fail loud). Details in blueprint §12.

Sources (all free, blueprint §5): **Tiingo** (prices — TSLA/SPCX/SPY/QQQ), **FRED** (6 macro series), **IBKR Flex Web Service** (daily portfolio), **Alpha Vantage NEWS_SENTIMENT** (company news — entire 25/day budget), **Reuters RSS** (wire), **Reddit r/stocks RSS** (retail), **Finnhub → yfinance** (earnings dates), **yfinance** (universal fallback). Indicators are computed **locally** (pandas_ta) and validated against TradingView at build time. Dedup is URL-match only.

## Build phases & ordering rules

Phases (blueprint §10): **0 Spine** → **1 Digest + Bot** → **2 Journal** → **3 Quant** → **4 optional agentic add-on** → **5 Analyst module**.

- **Hard rule: Phase 2 (journal) ships before round-trip #1.** Gates must be pre-registered before any sleeve trade.
- **MVP = Phases 0–2** (~29–40 h). That is where the daily value is.
- **Do not build the Analyst module (Phase 5) first.** It shares the spine but does not block or depend on the MVP; build it on a working spine.

**Phase 0 scope:** Supabase schema (all §4 tables) + Tiingo/FRED/IBKR-Flex ingestion + 200-day historical price seed + SPCX calendar seed (§14) + `fetch_log` and the wrapped fetchers. IBKR Flex query + read-only token setup happens at Phase 0 build time.

## Tech stack & intended layout

Python throughout (Vercel Python serverless + GitHub Actions Python jobs). `pandas_ta` for indicators. Claude API: **Haiku** for sentiment scoring (behind a swappable `score_headlines()` interface) and **Sonnet** for synthesis. Supabase Postgres for state.

Monorepo layout (blueprint §2 item 14) — **scaffolded, nothing implemented yet**: the packages `ingestion/ digest/ journal/ bot/ quant/ shared/` each exist with a docstring-only `__init__.py`, and `.github/workflows/` holds a `.gitkeep` placeholder (workflows arrive in Phase 1).

## Commands

- **Tests:** `.venv\Scripts\python.exe -m pytest -q` from the repo root (pure-logic unit tests; no DB/network). No linter is configured yet.
- **Domain probes (read-only, run against the live DB):** `python -m quant.splits` (adjusted shares series), `python -m quant.metrics` (TSLA metrics table + gross-margin identity), `python -m digest.grounding [digest_id]` (validate a stored digest against its frozen bundle), `python -m digest.pipeline --run-type monday --dry-run` (freeze a bundle, no spend).
- **Seeds:** `python -m ingestion.seed_corporate_actions` (idempotent); `python -m ingestion.seed_config` (bootstrap only — refuses on a live config; `--missing-only` adds absent keys).
- **Domain CLI (spec'd, not yet implemented):** `seed-calendar --year YYYY` — guided annual seeding of FOMC/CPI/NFP dates.
- **Telegram commands:** `/pulse` `/book` `/journal` `/skip` `/health` `/override` (and `/analyze` in Phase 5).

## Cost guardrail

Target is ~$1–2/month total (data $0, hosting $0 on free tiers, Claude API the only spend). Sonnet digest ~$0.07–0.15/run, Haiku scoring ~$0.01–0.03/run. A change that materially raises this needs an explicit decision (Law 3).
