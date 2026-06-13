# ARGUS — Master Blueprint v2.0 (FINAL, post-grill)

**Owner:** Omar · **Updated:** 2026-06-13 (added §5 Retention Policy; SPCX listed 2026-06-12) · **Status:** All 15 grill items resolved. Build-ready.
**Supersedes:** portfolio-intelligence-blueprint.md (v1.0). This document is the single source of truth.
**Name:** Argus — the hundred-eyed watcher of Greek myth; some eyes always open. Repo: `argus`.

---

## 0. Context Capsule

Omar (Data Engineer, Amman; Python/Power BI; has shipped MacrosTracker Telegram bot and ExpenseIQ on Supabase) is building Argus: a personal portfolio-intelligence system. **Primary goal: personal investing utility.** Career/GitHub value is a side effect.

**Investing context:** Long-term DCA investor via IBKR (**cash account — confirmed**), ~$35K portfolio (exact figure to confirm; ~25K is Omar's, treated as all-Omar's for sleeve math at his instruction). Monthly DCA deposits ~200–300+ JD (variable, never assumed fixed) into TSLA and SPCX (listed today, $135 IPO price). Target: $100K, then diversify into ETFs.

**Simultaneously** (not as an alternative — never frame as DCA-vs-trading), Omar runs a weekly intraday round-trip strategy on TSLA under the Trading Protocol (§8). Settled truce: **Argus informs, never recommends; Omar decides; the journal is the verdict.** Do not re-litigate the strategy. DCA and the sleeve run on separate tracks and are never compared against each other; the sleeve is measured only against its own untouched counterfactual.

---

## 1. Operating Laws (unchanged from v1)

1. **Information, never instruction.** No Argus output may say buy/sell/safe-to-trade or recommend timing.
2. **Facts are retrieved, never generated.** Every date/number traces to a stored source row. LLM synthesizes; never supplies facts from memory.
3. **Free-tier only.** Any paid input requires a new explicit decision. (X/Twitter permanently excluded.)
4. **Preparedness over prediction.** Forward calendar is first-class; direction-calling is banned.
5. **The core is untouchable.** Core holdings + all DCA contributions never enter the sleeve.
6. **The journal is the verdict.** Pre-registered metrics; skipped trades logged; no post-hoc reinterpretation.
7. **Silent failure is misinformation.** Every source failure surfaces in output.
8. **Boring beats clever.** Pipeline v1; bounded agentic add-on Phase 4 at earliest.

---

## 2. Final Decisions (all 15 grill items)

| # | Item | Resolution |
|---|------|------------|
| 1 | IBKR account type | **Cash** (confirmed). PDT not applicable. T+1 rule: never sell rebought shares before prior sale settles → enforced by non-consecutive-day trading rule |
| 2a | Bracket | **Accepted.** $1.50 target / $1.50 stop / 15:50 ET time-stop — whichever fires first, rebuy, no discretion. Revisable at 20-trade checkpoint |
| 2b | Sleeve | **20% of $35K = ~$Xk ≈ 17 TSLA shares** (@ ~$405). Phase ladder: A 20% → B 30–40% → C 50%+, each gate = checkpoint pass |
| 2c | Kill criteria | **Metric: sleeve-only Δshares** (DCA never touches sleeve → metric is 100% trade-attributable). 10-trade early warning (Δshares < −1 → pause & examine); 20-trade checkpoint (Δshares < 0 → halt & review; pass → Phase B); 50-trade verdict (Δshares < 0 → permanent stop, sleeve rejoins core) |
| 3 | Schema critical decision | Round-trip vs DCA-buy classification by **quantity proximity** (sleeve-sized ≈17 sh vs DCA ≈0.6–1 sh). Columns: `trade_type` (auto) + `override_type` (nullable, via /override, always wins). Config stored as JSONB rows with `updated_at` — no migrations on parameter change, full historical auditability |
| 4 | Flex Query sections | **Open Positions + Trades + Cash Transactions + Corporate Actions** (split-safety) + Transfer of Positions (temporary, during Jordan-broker transfer). Dividends deferred to Phase 3. IBKR portal setup deferred to build time |
| 5 | Price/indicator sources | **Tiingo primary** (free: ~50 symbols/hr; 4 tickers needed: TSLA, SPCX, SPY, QQQ). **Alpha Vantage reserved 100% for news sentiment** (free tier now 25 req/day). **Indicators computed locally** (pandas_ta from `prices_eod`) — zero API cost. One-time historical seed (200+ days), then daily 1-row updates. **Build-time validation: computed indicators checked against TradingView values before ship.** yfinance = universal fallback. FRED for macro |
| 6 | Calendar sourcing | FOMC/CPI/NFP: **annual seeding via Claude Code CLI prompt** (sources: federalreserve.gov, BLS schedule). Earnings: **Finnhub free endpoint, weekly 30-day lookahead** + yfinance fallback. SPCX first-earnings date appears automatically once announced via 8-K |
| 7 | News sources / clustering | **Reduced to 3 non-overlapping sources** — AV News & Sentiment (company layer: TSLA, SPCX), Reuters RSS (macro/wire layer; CNBC excluded — republishes Reuters + opinion noise), Reddit r/stocks RSS (retail layer). Clustering eliminated → **URL dedup only** (~5 lines) |
| 8 | Sentiment scoring | **Haiku batch scoring** (~$0.01–0.03/run) behind swappable `score_headlines()` interface (FinBERT testable later). Modern LLM > 2019 FinBERT on context; offline irrelevant in network-dependent pipeline |
| 9 | Digest format | **Analyst note: 600–800 words**, short paragraphs, narrative linking macro → holdings. 5 fixed sections. Synthesis prompt = 5 clauses: grounding (Law 2), no-recommendation (Law 1), fixed structure, plain-English interpretation beside every number, uncertainty marking. **Build-time: iterate prompt against one week of real data with Omar reading outputs** |
| 10 | Telegram interface | Commands: /pulse /book /journal /skip /health /override. Pushes: Monday digest; next-morning trade-detection + annotation prompt (confidence 1–5 buttons); checkpoint-proximity warnings; **Option B active morning warnings at 15:30 Amman on event days ("⚠️ CPI tomorrow — event filter active") and cap-reached days ("2/2 this week")** |
| 11 | Hosting topology | **Vercel (Python serverless) replaces Render entirely.** Vercel = webhook ear: instant commands read Supabase directly (~1s cold start, no keep-alive, kills the ExpenseIQ lag problem). /pulse → instant "Generating ⏳" + triggers GitHub Actions via workflow_dispatch. **GitHub Actions = all scheduled/heavy jobs.** Supabase = spine |
| 12 | Reliability | 30s timeout/call; 2 retries 30s apart then mark unavailable + footer line; staleness flags (prices >2 trading days, Flex >48h → warn in digest); critical push alerts (Flex fails 2 days = journal blind; Monday digest total failure). **Monday digest: auto-retry once at +1h, then fail loud** |
| 13 | Secrets | 9 secrets. Never in repo. GH Actions repo secrets (all); Vercel env vars (Telegram token, Supabase, workflow-dispatch-scoped GitHub PAT only); local `.env` gitignored. Flex token: read-only by design, 1-yr expiry, **/health shows days-to-expiry** |
| 14 | Name & structure | **Argus**, monorepo: `ingestion/ digest/ journal/ bot/ quant/ shared/ .github/workflows/` |
| 15 | SPCX calendar | Seeded below (§15). Staggered lockup, not standard 180d. Conditional-unlock monitor: Argus auto-watches the +10% trigger (close ≥ $175.50 on ≥5 of 10 sessions post-Q2-earnings) |

---

## 3. Architecture (final)

```
                    ┌──────────────────────────────────┐
                    │     SUPABASE POSTGRES (spine)     │
                    └──────┬──────────────▲─────────────┘
                           │ read         │ write
   ┌───────────────────────┴──────────────┴────────────────────┐
   │                   PIPELINE (one engine)                    │
   │ fetch (wrapped) → store raw → URL dedup → Haiku scoring →  │
   │ compute indicators → rank vs book → deltas vs last digest →│
   │ Sonnet synthesis (5-clause contract) → store → Telegram    │
   └───────▲────────────────────────────────────▲───────────────┘
           │                                    │
   GITHUB ACTIONS (heavy, scheduled)     VERCEL (light, instant)
   • Mon 11:00 UTC: full digest          • Telegram webhook (Python)
   • Daily 20:30 UTC: Flex + prices      • /book /journal /skip /health
     + trade detection                     /override: direct Supabase reads,
   • Daily 12:30 UTC: event-filter         instant reply
     check → morning warning push        • /pulse: "Generating ⏳" +
   • workflow_dispatch: /pulse runs        workflow_dispatch trigger
```

The database is the product; every Telegram message is a view of it. Every digest stores its exact input bundle (`bundle_json`) — reproducible and auditable forever.

---

## 4. Data Spine — Supabase Schema

| Table | Purpose / key fields |
|---|---|
| `instruments` | symbol, name, first_trade_date (SPCX 2026-06-12 → drives indicator suppression until history exists) |
| `prices_eod` | symbol, date, OHLCV, source. Seeded 200+ days at setup; +1 row/ticker/day after |
| `indicators` | symbol, date, name (sma50, sma200, rsi14, macd…), value — computed locally via pandas_ta |
| `macro_series` | FRED: DFF, CPIAUCSL, UNRATE, DGS10, T10Y2Y, VIXCLS |
| `calendar_events` | type (fomc/cpi/nfp/earnings/lockup/index/quiet_period), date, symbol?, conditional_rule?, materiality |
| `headlines` | source (av/reuters/reddit), url (dedup key), title, published_at, ticker_tags |
| `sentiment` | headline_id, method (av_native/haiku), direction, magnitude |
| `digests` | run_type, sent_at, full_text, bundle_json |
| `positions_snapshot` | daily Flex: symbol, qty, cost_basis, market_value |
| `transactions` | Flex fills: exec_time, symbol, side, qty, price, fees, **trade_type** (auto: round_trip_sell / round_trip_rebuy / dca_buy / dca_sell / unclassified), **override_type** (nullable; always wins) |
| `contributions` | DCA deposits auto-detected from Flex cash transactions: date, amount (variable — never assumed fixed) |
| `round_trips` | paired same-day sell→rebuy: qty, sell_px, rebuy_px, fees, pnl_usd, **delta_shares**, digest_id, day_trades_in_window |
| `trade_annotations` | round_trip_id, confidence_1to5, checklist_passed, notes (via Telegram buttons) |
| `skip_log` | date, reason (event_filter / discretion / other) |
| `fetch_log` | source, run_id, status, latency_ms, error |
| `config` | **JSONB rows + updated_at** (no migrations): sleeve_pct, sleeve_shares, bracket {target, stop, time_stop}, phase, kill_criteria, watchlist, weekly_trade_cap |

Round-trip classifier: same-day sell of qty ≥ 0.8 × sleeve_shares followed by similar-qty buy → pair. Small buys (≈0.6–2 sh) → dca_buy. Misclassification fixed via /override. Corporate Actions feed auto-adjusts sleeve_shares on splits.

---

## 5. Retention Policy (config-driven defaults)

**Principle.** Tables split into two classes: *durable* (financial truth + the distilled
product — kept forever, negligible storage) and *ephemeral* (raw inputs already distilled
into digests — pruned by age). At realistic volume (~40–100 MB/year worst case against the
500 MB free-tier cap), free tier is a permanent home, not a phase — provided the ephemeral
tables are bounded. (Law 3.)

**Hard rule — trades are permanent.** round_trips, transactions, contributions,
positions_snapshot, and trade_annotations are NEVER deleted. The journal verdict (Law 6)
and the trade-10/20/50 checkpoints evaluate *cumulative* sleeve-only Δshares from trade #1
onward; at ~1–2 round trips/week, trade 50 lands ~6–12 months in, so any age-based purge
would erase the early trades the permanent-stop checkpoint depends on. Also required intact
for any future backtest. This is a rule, not a tunable default.

**News is pruned by age, not importance.** Importance is never judged at deletion time.
Each weekly digest is the durable, distilled memory of what mattered; once a digest is
written, the raw headlines it drew from are disposable input. Digests are kept forever
(~0.5 MB/yr).

**Window vs. cadence.** The retention *window* (how old before a row is eligible for
deletion) is the decision that matters and is stored per-table as a config row (JSONB →
changeable with no migration). The cleanup *cadence* (sweep frequency) is not load-bearing
at this volume — a monthly automated sweep is the default; even a yearly sweep stays under
the cap.

**Table map (defaults — every window editable via config):**

Keep forever: instruments, prices_eod, indicators, macro_series, calendar_events, digests,
positions_snapshot, transactions, contributions, round_trips, trade_annotations, config.
(indicators is regenerable from prices_eod if reclamation is ever wanted.)

Prunable (default window):
- headlines — 180 days
- sentiment — pruned in lockstep with its parent headline
- fetch_log — 90 days
- skip_log — 90 days

**Implementation notes (DEFERRED — do not build in Phase 0):**
- Pruning is a future-phase scheduled job (DELETE ... WHERE <timestamp> < now() - window).
  There is no data to prune yet.
- sentiment references headlines; deletion must handle dependents (delete children first or
  ON DELETE CASCADE) — not a naive single-table DELETE.
- Windows live as config rows; the cleanup job reads them at runtime.
- Postgres reclaims deleted space via autovacuum (Supabase-managed), not instantly on DELETE.

Laws engaged: 3 (free-tier-only — math shows it's permanent), 6 (journal-is-verdict — trades
immutable), 8 (boring-beats-clever — age-based DELETE over importance-classification).

---

## 6. Ingestion Sources (final)

| Layer | Source | Cadence | Notes |
|---|---|---|---|
| Prices | Tiingo (free) | Daily, 4 tickers (TSLA, SPCX, SPY, QQQ) | One-time 200-day historical seed at setup |
| Indicators | Local pandas_ta | Daily, computed from prices_eod | Validate vs TradingView at build; suppress on young tickers (no SMA50 until 50 sessions — SPCX) |
| Macro | FRED API | Weekly (Mon run) | 6 series incl. VIXCLS for regime |
| Company news + sentiment | Alpha Vantage NEWS_SENTIMENT | Mon + /pulse | Entire 25/day budget reserved for this |
| Wire/macro news | Reuters RSS | Mon + /pulse | CNBC excluded (derivative) |
| Retail sentiment | Reddit r/stocks RSS | Mon + /pulse | No auth needed |
| Raw-headline scoring | Haiku batch | Per run | Behind swappable score_headlines() |
| Earnings dates | Finnhub (free) → yfinance fallback | Weekly, 30-day lookahead | Upsert to calendar_events |
| FOMC/CPI/NFP dates | Annual CLI seeding (Fed + BLS schedules) | Once/year, prompted | `seed-calendar --year 2027` guided flow |
| Portfolio | IBKR Flex Web Service | Daily 20:30 UTC weekdays | 4+1 sections per §2 item 4; token read-only, 1-yr expiry |
| Fallback prices | yfinance | On Tiingo failure | Unofficial; fallback role only |

Dedup = URL match only. Reliability rules per §2 item 12 apply to every source.

---

## 7. Digest Engine (final spec)

**Format:** analyst note, 600–800 words, 5 fixed sections, readable in 2–3 minutes on phone:
1. **Regime** — SPY/QQQ vs 50/200-day, VIX level + percentile, 10Y + weekly change, days to next FOMC. One paragraph: what kind of market is this.
2. **What Moved** — top items from 3 sources post-dedup, each with sentiment direction; plain-English interpretation beside every number ("RSI 67 — elevated, not extreme; running hot two weeks").
3. **Forward Calendar** — next 7–14 days from calendar_events incl. SPCX lockup tranches and armed/disarmed conditional unlock. Never hallucinated (Law 2); rendered from table.
4. **Your Book** — allocation, distance to $100K, drawdown from peak, contributions this month, sleeve status + phase + trades-to-next-checkpoint, factor-concentration honesty line (TSLA+SPCX ≈ one Musk factor). First Monday of month: extended monthly review.
5. **Source Health** — "7/8 OK; Reddit timed out." + staleness flags + Flex-token days-to-expiry when <30.

**Synthesis contract (Sonnet), 5 clauses:** grounding / no-recommendation / fixed structure / interpretation layer / uncertainty marking. Build-time: run against one real collected week; Omar reads; iterate 2–3 rounds.

**/pulse variant:** delta since last digest + fresh prices + near calendar; light pipeline (no full re-scoring); delivered ~1 min after trigger.

---

## 8. Trading Protocol (FINAL — all parameters locked)

*Not re-litigated. DCA and sleeve run simultaneously on separate tracks; never compared to each other.*

- **Account:** IBKR cash. T+1 settlement rule → **never trade on consecutive days** (Mon/Thu pattern). Never sell rebought shares before prior sale settles.
- **Sleeve:** 20% × ~$XXk = **~$Xk ≈ 17 TSLA shares**. Sleeve only ever round-trips; core + all DCA deposits untouchable (Law 5). Corporate Actions auto-adjusts share count on splits.
- **Bracket (replaces rebuy-at-EOD-no-matter-what):** on selling, three pre-set rebuy triggers — target −$1.50 / stop +$1.50 / time-stop 15:50 ET — whichever first, rebuy, zero discretion. Breakeven ≈ 53–58% win rate at these fees. Net win ≈ +$23.50; net loss ≈ −$27.50.
- **Frequency cap:** max 2 round trips/calendar week, enforced visibly in /journal.
- **Event filter:** no round trips within 24h of calendar_events high-vol items (FOMC, CPI, NFP, earnings of traded ticker, SPCX lockup/index dates). Enforced by 15:30 Amman morning push. Discretionary skips logged via /skip.
- **Orders:** limit only, never market.
- **SPCX:** no round trips during its first weeks — no history, suppressed indicators, IPO volatility (Law 2: Argus cannot inform what it cannot see).
- **Phase ladder:** A = 20% (now) → B = 30–40% (20-trade checkpoint pass) → C = 50%+ (50-trade verdict pass; gap-risk objection stated once at gate, then Omar's call). One variable changes per phase ($2 bracket testable at Phase B, not before).
- **Tax (one line):** non-US person → US capital-gains/wash-sale generally inapplicable; TSLA/SPCX pay no dividends; verify Jordan-side treatment locally once.

---

## 9. Journal & Checkpoints (the verdict — Law 6)

**Core metric: sleeve-only Δshares.** DCA contributions go to core by Law 5, so sleeve share-count changes are 100% trade-attributable. Win example: sell 17 @ 405, rebuy @ 403.50 → ~17.05 sh (+0.06). Loss: rebuy @ 406.50 → ~16.94 (−0.06). Direction-neutral, contribution-proof, goal-aligned (more shares = winning).

**Auto-captured per round trip** (from Flex, zero manual entry): everything in `round_trips` + link to that day's digest. **Prompted** (10s, Telegram buttons): confidence 1–5, checklist confirmation. **Also logged:** skips with reasons.

**Pre-registered gates (in config before trade #1):**
- **Trade 10 (~5 wks):** early warning — if sleeve Δshares < −1.0 → pause & examine. Else continue.
- **Trade 20 (~10 wks):** checkpoint — Δshares < 0 → mandatory halt & review. Pass → Phase B unlock (30–40%).
- **Trade 50 (~6 mo):** verdict — Δshares < 0 → permanent stop, sleeve rejoins core. Pass → Phase C discussion.
- Statistical honesty: 10–20 trades = early signal only; ~40–50 needed before win-rate claims mean anything.

Telegram pushes proximity warnings: "Trade 18 of 20 — checkpoint in 2. Sleeve Δshares: +0.31."

---

## 10. Quant Modules (Phase 3, unchanged)

DCA Monte Carlo to $100K with confidence bands (contributions from observed history, never assumed); drawdown-conviction view; risk view with factor-concentration honesty (TSLA+SPCX ≈ one factor). SPCX joins simulations as history accrues.

---

## 11. Build Phases

| Phase | Scope | Est. |
|---|---|---|
| **0 — Spine** | Supabase schema + Tiingo/FRED/Flex ingestion + historical seed + fetch_log | 12–16 h |
| **1 — Digest + Bot** | 3-source fetch, dedup, Haiku scoring, indicators, synthesis, Telegram via Vercel + GH Actions, morning warnings | 12–16 h |
| **2 — Journal** | Round-trip detection, classifier + /override, annotations, checkpoints, /journal | 5–8 h |
| **3 — Quant** | DCA sim + risk views | 8–12 h |
| **4 — Optional** | Bounded agentic add-on (one web-search round on flagged gaps) | 6–10 h |

**Hard rule: Phase 2 ships before round-trip #1.** MVP (0–2) ≈ 29–40 h. IBKR portal setup (Flex query + token) happens at Phase 0 build time.

## 12. Cost Model

Data $0 · Hosting $0 (Vercel + GH Actions + Supabase free tiers; Render eliminated; no keep-alive services) · Claude API ≈ $1–2/month (Sonnet digest ~$0.07–0.15/run, Haiku scoring ~$0.01–0.03/run, 6–10 runs/mo). **Total ≈ $1–2/month.**

---

## 13–14. Reliability & Secrets

Per decisions table items 12–13: 30s timeouts, 2 retries, staleness flags, footer health line, critical push alerts, Monday auto-retry-once-then-fail-loud; secrets in platform-native stores only, least-privilege scoping, Flex token expiry surfaced in /health.

---

## 15. SPCX Calendar Seed (insert into calendar_events at Phase 0)

IPO: 2026-06-12, $135/share, Nasdaq. Staggered lockup per S-1 (insiders sold nothing in the offering itself; Musk + select major backers excluded from all early release — full 366 days).

| Date | Event | Type |
|---|---|---|
| ~2026-07-02 | Nasdaq-100 fast-entry eligibility (15 sessions) | index |
| 2026-06-22 → mid-Jul | Analyst initiations window (quiet period end; exact timing varies — confirm when banks announce) | research |
| TBD (mid-Jul–Sep) | Q2 earnings — first public report → +2 days: 20% insider unlock | earnings, lockup |
| conditional | +10% unlock if close ≥ **$175.50** (130% × $135) on ≥5 of 10 sessions post-Q2-earnings — **Argus monitors automatically from prices_eod** | lockup (conditional) |
| 2026-08-21 | Day 70 — 7% tranche | lockup |
| 2026-09-10 | Day 90 — 7% | lockup |
| 2026-09-25 | Day 105 — 7% | lockup |
| 2026-10-10 | Day 120 — 7% | lockup |
| 2026-10-25 | Day 135 — 7% | lockup |
| TBD (Oct–Nov) | Q3 earnings → +28% unlock | earnings, lockup |
| **2026-12-09** | Day 180 — full lockup expiry (major supply event) | lockup |
| ~2027-06-13 | Day 366 — Musk + major backers eligible | lockup |

TBD rows auto-resolve via Finnhub once SpaceX files its 8-K earnings announcements.

---

## 16. Next Action

Phase 0 with Claude Code: create `argus` repo (structure per §2 item 14) → Supabase project + schema DDL from §4 → Tiingo key + historical seed → FRED key + series pull → IBKR Flex query + token setup → fetch_log + wrapped fetchers. Then Phase 1.
