# Phase 0 — Deferred Items (resolve when building ingestion)

These were intentionally left OUT of the initial spine DDL (`supabase/migrations/<ts>_init_spine.sql`)
to keep Phase 0 scoped to blueprint §4. They are **not optional** — each must be addressed when the
ingestion layer is built, or a real bug appears. Tracked here so they aren't lost.

## 1. `corporate_actions` table (split-safety)
- **What:** A table logging Corporate Actions from the IBKR Flex feed (splits, etc.).
- **Why deferred:** Not in §4's table list; it belongs to the Flex Corporate Actions section
  (§2 item 4), which arrives with ingestion.
- **Why it matters:** §4 states "Corporate Actions feed auto-adjusts `sleeve_shares` on splits" and
  §7 repeats it. `sleeve_shares` is the frozen registered unit (derived at sleeve entry, §8) — once
  a sleeve is open, without capturing corporate actions that frozen count and historical
  `round_trips.delta_shares` cannot be split-adjusted correctly — the core sleeve metric drifts.
- **Action when building ingestion:** add `corporate_actions` (symbol, ex_date, type, ratio/factor,
  raw_json, source) + the logic that updates `config.sleeve_shares` on a split.
- **Ref:** blueprint §4 (~line 104), §2 item 4, §7.

## 2. `transactions.ext_id` (Flex dedup id)
- **What:** A unique external id from IBKR Flex (execution / trade id) on `transactions`.
- **Why deferred:** Not named in §4's column list; it is an ingestion-idempotency concern.
- **Why it matters:** The daily 20:30 UTC Flex pull re-fetches overlapping windows. Without a unique
  external id, re-ingestion creates **duplicate** transaction rows (or relies on a fragile
  (exec_time, symbol, qty, price) heuristic). Duplicates corrupt round-trip pairing and the sleeve
  Δshares metric.
- **Action when building ingestion:** add `ext_id text` + `unique (ext_id)`, and upsert on it.
- **Ref:** blueprint §4 (transactions), §5 (Flex daily), plan "Design decisions" section.

## 3. `contributions.currency` (JD vs USD)
- **What:** A currency column on `contributions` (plus a normalization rule).
- **Why deferred:** §4 lists only (date, amount).
- **Why it matters:** DCA deposits are described in JD (~200–300+ JD, §0) but portfolio and sleeve
  math are in USD. A bare `amount` with no currency risks mixing JD and USD in the "contributions
  this month" digest line (§6) and the Phase 3 Monte Carlo.
- **Action when building ingestion:** confirm whether Flex cash transactions are already
  USD-converted; add `currency text default 'USD'` (+ an FX rule if JD amounts are ever stored).
- **Ref:** blueprint §0, §4 (contributions), §5.

## 4. `ibkr_flex:config` read-failure is masked in the §5 Source-Health verdict
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
