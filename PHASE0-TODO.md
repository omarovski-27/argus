# Phase 0 — Deferred Items (resolve when building ingestion)

These were intentionally left OUT of the initial spine DDL (`supabase/migrations/<ts>_init_spine.sql`)
to keep Phase 0 scoped to blueprint §4. They are **not optional** — each must be addressed when the
ingestion layer is built, or a real bug appears. Tracked here so they aren't lost.

## 1. `corporate_actions` table (split-safety)
- **What:** A table logging Corporate Actions from the IBKR Flex feed (splits, etc.).
- **Why deferred:** Not in §4's table list; it belongs to the Flex Corporate Actions section
  (§2 item 4), which arrives with ingestion.
- **Why it matters:** §4 states "Corporate Actions feed auto-adjusts `sleeve_shares` on splits" and
  §7 repeats it. Without capturing corporate actions, `config.sleeve_shares` (≈17) and historical
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

---
_When an item is resolved, add the migration filename that addresses it and check it off._
