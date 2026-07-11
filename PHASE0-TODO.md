# Phase 0 — Deferred Items (resolve when building ingestion)

These were intentionally left OUT of the initial spine DDL (`supabase/migrations/<ts>_init_spine.sql`)
to keep Phase 0 scoped to blueprint §4. They are **not optional** — each must be addressed when the
ingestion layer is built, or a real bug appears. Tracked here so they aren't lost.

## 1. `corporate_actions` table (split-safety) — ✅ table + seed + read layer shipped; sleeve auto-adjust still deferred
- **What:** A table logging Corporate Actions (splits, etc.) so share-basis math is correct.
- **Shipped (2026-06/07):** the table exists live with grants
  (`20260627120000_fundamentals_grants.sql`); TSLA's two splits seeded idempotently on
  (symbol, action_type, effective_date) — 5:1 eff. 2020-08-31, 3:1 eff. 2022-08-25
  (`ingestion/seed_corporate_actions.py`, 5197d91); the filed-date-keyed split-adjustment
  read layer (`quant/splits.py`, 4b7710c) and the read-time metrics consuming it
  (`quant/metrics.py`, dd3a448). Adjusted `shares_diluted` verified smooth:
  1.923B (FY2015) → 3.528B (FY2025), no ×5/×15 artifacts.
- **Still deferred (gated on funding / an active sleeve):** the IBKR Flex Corporate-Actions
  feed ingestion and the "auto-adjust `config.sleeve_shares` on a split" write (§4/§7) —
  moot while `sleeve_shares` is unset (no active sleeve; the stale illustration row was
  deleted 2026-07-01).
- **Ref:** blueprint §4 (~line 104), §2 item 4, §7.

## 2. `transactions.ext_id` (Flex dedup id) — ✅ SHIPPED
- **What:** A unique external id from IBKR Flex (execution / trade id) on `transactions`.
- **Shipped:** `transactions.ext_id` + `unique(ext_id)` applied
  (`20260619120000_round_trips_sell_ext_id.sql`); `ingestion/ibkr_flex._store_trades`
  upserts on `ext_id` (`ibExecID`), and `_store_cash` on `contributions.ext_id`
  (`transactionID`) — the daily overlapping-window re-pull is idempotent (verified live:
  repeated pulls store 0 duplicate rows). The two 2026-06-26 fills carry their ibExecIDs.
- **Ref:** blueprint §4 (transactions), §5 (Flex daily).

## 3. `contributions.currency` (JD vs USD)
- **What:** A currency column on `contributions` (plus a normalization rule).
- **Why deferred:** §4 lists only (date, amount).
- **Why it matters:** DCA deposits are described in JD (~200–300+ JD, §0) but portfolio and sleeve
  math are in USD. A bare `amount` with no currency risks mixing JD and USD in the "contributions
  this month" digest line (§6) and the Phase 3 Monte Carlo.
- **Action when building ingestion:** confirm whether Flex cash transactions are already
  USD-converted; add `currency text default 'USD'` (+ an FX rule if JD amounts are ever stored).
- **Ref:** blueprint §0, §4 (contributions), §5.

## 4. `ibkr_flex:config` read-failure is masked in the §5 Source-Health verdict — ✅ relabeled (completion run 2026-07-06)
- **Shipped:** the filed design, verbatim — the config read now logs as
  `config_read:sleeve_shares` (its own logical source), and `config_read` joined
  `shared.sources.NON_DATA_SOURCES`, so the row can neither be superseded inside the
  `ibkr_flex` verdict slot (the mask) nor redden a healthy feed later (the 56314e2
  stale-red class). Both surfaces (digest §5 verdict + `/health`) inherit via the shared
  frozenset. Regression tests: `tests/test_source_health.py`
  (`test_config_read_failure_cannot_be_masked_by_flex_section_successes`,
  `..._cannot_redden_a_healthy_flex_feed_later`, and the old-label seam kept as
  executable documentation). No migration needed (label-only change).
- **Where config-read failures surface (the deferred decision, decided):** `fetch_log`
  is the forensic channel — the row is always written; classification degrades to
  "no active sleeve" by design (advisory, /override wins). A dedicated infra-health
  line (distinct from §5 data health) remains future work, tracked below as Wave-3
  flag-only #1's sibling.

### (original filing, for context)
## ~~4. `ibkr_flex:config` read-failure is masked in the §5 Source-Health verdict~~
- **What:** When `_load_sleeve_shares` (ingestion/ibkr_flex.py) hits a config-read *exception*, it
  writes a `fetch_log` row `ibkr_flex:config = failure` (Law 7: logged), then degrades to "no active
  sleeve". But that row is **masked in the §7/§5 verdict**: `_aggregate_sources` (digest/bundle.py)
  collapses granular labels to the logical `ibkr_flex` source most-recent-`at`-wins, and the config
  read fires *before* the section stores — so the later `ibkr_flex:positions/trades/cash` successes
  supersede it and the verdict shows `ibkr_flex success`. The failure persists in `fetch_log`
  (forensics intact) but does not surface on the digest/`/health` summary line.
- **Why deferred (not a reconcile regression):** the `write_fetch_log("ibkr_flex:config","failure")`
  call is byte-identical to pre-reconcile code — this seam predates the sleeve_shares change and is
  broader than it (any partial Flex section failure can be masked the same most-recent-wins way).
- **Why NOT a one-line "failure-wins" aggregator change:** `ibkr_flex:config` only logs on failure,
  so its latest row is its last failure — failure-wins would redden `ibkr_flex` until that row
  scrolls out of `_FETCH_LOG_SCAN`, reintroducing the stale-red class fixed in 56314e2.
- **Action (design-consistent fix):** relabel the config read to its own non-§5-data logical source
  (kept in `fetch_log`, excluded from the verdict via `shared.sources.NON_DATA_SOURCES` — the
  `pipeline`/`telegram_webhook` pattern), and decide where config-read failures *should* surface
  (infra-health, not §5 data health). Moot until funding (no live trades to misclassify).
- **Ref:** digest/bundle.py `_aggregate_sources`, shared/sources.py, [[argus-source-health-webhook-stale-red]].

## 5. Flex rows stored `unclassified` are not auto-reclassified on re-pull
- **What:** `_store_trades` upserts with `on_conflict="ext_id", ignore_duplicates=True`. A trade row
  written `trade_type='unclassified'` (e.g. classified during a config-read blip with no active
  sleeve_shares) is therefore **never updated** by a later healthy re-pull — `/override` is the only
  remedy.
- **Why deferred:** pre-existing property of the dedup design (item 2's ext_id idempotency), not the
  sleeve_shares reconcile; `/override` is the designed escape hatch; moot until funding (no trades).
- **Action (if ever needed):** an opt-in reclassify path that re-runs `_classify` on existing
  `unclassified` rows when config becomes healthy, or a manual reclassify command.
- **Ref:** ingestion/ibkr_flex.py `_store_trades`, §2 item 3, §4 (transactions).

## 6. Earnings event-filter scope (gated on the Finnhub resolver — not yet built)
- **What:** §8's event filter must arm on "earnings of a *traded* ticker" only. Today this is a
  non-issue: no `earnings` row ever reaches `calendar_events` — `seed_calendar.py` seeds none
  (Law 2/4: no inventing unannounced dates), and the Finnhub earnings resolver (§5, §15) doesn't
  exist. The `shared.event_filter` predicate arms by event *type* with no symbol gate, so its
  `earnings` arm is currently **dormant** (cannot fire, cannot over-block).
- **Why deferred (do NOT build the gate now):** a symbol gate would guard a code path with no live
  code behind it (L8). The hazard only materializes when the resolver lands.
- **Action when the resolver is built — satisfy §8 one of two ways:**
  - **(primary)** seed earnings **book-scoped** at the fetch — config-watchlist symbols only.
    Finnhub's `earnings_calendar` returns every company; scoping at the fetch keeps
    type-membership ≡ §8 with **no predicate change**.
  - **(fallback, only if a broader fetch ever lands)** add the gate the `event_filter.py` docstring
    already anticipates: `event['symbol'] in book` inside `triggers_event_filter` — added there
    once, both surfaces (morning push + digest Forward Calendar) inherit it.
- **Ref:** shared/event_filter.py (predicate + docstring), ingestion/seed_calendar.py:27–33,
  blueprint §5, §8, §15.

---

## Wave 3 tail — reseeder-cure remainder (deferred)

Remaining items from the Wave 3 review (the `/felt`-hardening + config-drift cure, of which the
`sleeve_symbol` config-ification was the last code change). Recorded here so the tail is tracked,
not only in session memory. Labels are the review's own, **not** PHASE0 item numbers.

- **#2 — `config.ibkr_token_expiry_date` seed (unseeded).** The Flex read-only token's expiry is
  not seeded, so `/health` shows **"expiry unknown"** (the §7 days-to-expiry line can't compute).
  Needs the real Flex-token expiry date, single-key upserted (never a full re-seed — see CLAUDE.md
  re-seed hazard).
- **#5 — stale-audit refinement.** Deferred to pre-funding (the unmatched-note audit design
  question carried over from quarantine #2).
- **flag-only #1 — `/health` raw vs collapsed.** `/health` renders raw source rows while the digest
  shows the collapsed §5 verdict; reconcile the two presentations.
- **flag-only #2 — `checkpoint_push` failure-mode split.** `journal/checkpoint_push.py` logs two
  distinct failure modes (gate-10 integrity-undefined vs transport delivery) under one
  `journal:checkpoint_push` source string; splittable at the logging site for cleaner Source Health
  attribution.

---
_When an item is resolved, add the migration filename that addresses it and check it off._
