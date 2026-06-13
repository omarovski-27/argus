-- Dedup constraints for idempotent IBKR Flex re-runs (pre-Phase-2 requirement).
--
-- Why: ingestion/ibkr_flex.py re-runs (manual re-pulls, the daily 20:30 UTC job
-- re-covering an overlapping window) would otherwise re-insert duplicate rows.
-- Plain .insert() is not idempotent. Journal integrity depends on clean
-- transactions: the journal is the verdict (Law 6) and every gate metric —
-- including the sleeve-only Δshares count — is computed off `transactions`, so a
-- single duplicated fill corrupts the verdict. Phase 2 must not ship on dirty data.
--
-- ext_id carries IBKR's own unique IDs (Trades -> ibExecID, CashTransactions ->
-- transactionID); positions_snapshot dedups on its natural key (date, symbol).
-- Paired with the .upsert(..., ignore_duplicates=True) switch in ibkr_flex.py.

-- transactions: ext_id column + unique constraint
ALTER TABLE transactions
  ADD COLUMN IF NOT EXISTS ext_id text;
ALTER TABLE transactions
  DROP CONSTRAINT IF EXISTS transactions_ext_id_key;
ALTER TABLE transactions
  ADD CONSTRAINT transactions_ext_id_key UNIQUE (ext_id);

-- contributions: same pattern
ALTER TABLE contributions
  ADD COLUMN IF NOT EXISTS ext_id text;
ALTER TABLE contributions
  DROP CONSTRAINT IF EXISTS contributions_ext_id_key;
ALTER TABLE contributions
  ADD CONSTRAINT contributions_ext_id_key UNIQUE (ext_id);

-- positions_snapshot: natural key (date, symbol) — no new column needed
ALTER TABLE positions_snapshot
  DROP CONSTRAINT IF EXISTS positions_snapshot_date_symbol_key;
ALTER TABLE positions_snapshot
  ADD CONSTRAINT positions_snapshot_date_symbol_key UNIQUE (date, symbol);
