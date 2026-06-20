-- round_trips idempotency key for the Phase-2 pairing engine (journal/pairing.py).
--
-- Why: the pairing engine re-reads ALL transactions and re-derives round trips on
-- every run (the daily 20:30 UTC job re-covers an overlapping window). Without a
-- stable key + UNIQUE, each run would re-insert duplicate round_trips rows. A single
-- duplicated round trip corrupts the sleeve verdict (the cumulative pnl_usd the
-- Δshares view is computed from) — and the journal is the verdict (Law 6).
--
-- Key choice: the SELL leg's ext_id (IBKR ibExecID, carried on transactions.ext_id).
-- A round trip is uniquely identified by its opening sell fill; that id never changes,
-- so re-pairing the same fills yields the same key and the upsert is a no-op.
-- Paired with .upsert(..., on_conflict="sell_ext_id", ignore_duplicates=True) in the
-- engine: append-only, never a destructive update (Law 6).
--
-- Mirrors the transactions/contributions ext_id pattern from
-- 20260614120000_add_dedup_constraints.sql. NOT in the original §4 DDL, so additive.

ALTER TABLE round_trips
  ADD COLUMN IF NOT EXISTS sell_ext_id text;

-- DROP then ADD so a re-run with an altered definition re-applies cleanly (the init
-- migration's "no migrations" note: CREATE TABLE IF NOT EXISTS won't retro-edit).
ALTER TABLE round_trips
  DROP CONSTRAINT IF EXISTS round_trips_sell_ext_id_key;
ALTER TABLE round_trips
  ADD CONSTRAINT round_trips_sell_ext_id_key UNIQUE (sell_ext_id);

COMMENT ON COLUMN round_trips.sell_ext_id IS
  'Idempotency key: the opening sell leg''s IBKR ibExecID (= transactions.ext_id). '
  'UNIQUE so pairing re-runs upsert, never duplicate (Law 6).';
