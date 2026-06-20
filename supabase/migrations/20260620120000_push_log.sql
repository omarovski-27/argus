-- push_log — fire-once ledger for the Phase-2 checkpoint push (journal/checkpoint_push.py).
--
-- Why: the daily job evaluates the checkpoint state every run (and a manual
-- workflow_dispatch re-runs it). Without a persisted "already sent" record, every run
-- would re-push the same "Trade 18 of 20 — checkpoint in 2" proximity warning and re-fire
-- a gate verdict. push_log is the single fire-once truth: a row per (kind, dedup_key) that
-- has been delivered. The push checks it before sending and inserts AFTER a successful
-- send (at-least-once — losing a verdict is worse than a rare duplicate).
--
-- Dedup keys (kind always 'checkpoint'):
--   • 'verdict:{N}'            — the gate at trade N (10/20/50) has fired its verdict
--   • 'verdict:10:undefined'   — gate-10 magnitude undefined (zero/None rebuy_px): the loud
--                                integrity notice yells ONCE, without consuming 'verdict:10'
--   • 'proximity:{count}'      — the proximity warning at trade_count was sent
--
-- body keeps the literal message sent (auditability — same spirit as digests.bundle_json).
--
-- Idempotency style mirrors 20260619120000_round_trips_sell_ext_id.sql: additive
-- (CREATE / ADD COLUMN IF NOT EXISTS), DROP-then-ADD the UNIQUE constraint so a re-run with
-- an altered definition re-applies cleanly. NOT destructive: it never drops the table, so
-- re-running never wipes the dedup history (which would cause re-sends).
--
-- GRANT (the reason this migration is REQUIRED, not optional): the init migration
-- (20260612175007) grants service_role on the tables that existed AT grant time and
-- deliberately does NOT set default privileges for future tables. A table created after
-- that — like this one — gets NO grant and every backend call fails 42501
-- "permission denied for table push_log". So the explicit per-table grant below is what
-- makes push_log usable by the only role Argus runs as.

CREATE TABLE IF NOT EXISTS push_log (
    id          bigint generated always as identity primary key,
    kind        text not null,          -- 'checkpoint'
    dedup_key   text not null,          -- 'verdict:20' | 'proximity:9' | 'verdict:10:undefined'
    body        text,                   -- the literal message sent (audit)
    sent_at     timestamptz not null default now()
);

-- Reconcile a pre-existing (out-of-band) push_log to this spec without dropping it.
ALTER TABLE push_log ADD COLUMN IF NOT EXISTS kind      text;
ALTER TABLE push_log ADD COLUMN IF NOT EXISTS dedup_key text;
ALTER TABLE push_log ADD COLUMN IF NOT EXISTS body      text;
ALTER TABLE push_log ADD COLUMN IF NOT EXISTS sent_at   timestamptz default now();

-- One delivery per (kind, dedup_key). DROP then ADD so a re-run re-applies cleanly.
ALTER TABLE push_log DROP CONSTRAINT IF EXISTS push_log_kind_dedup_key_key;
ALTER TABLE push_log ADD  CONSTRAINT push_log_kind_dedup_key_key UNIQUE (kind, dedup_key);

COMMENT ON TABLE  push_log IS
    'Fire-once ledger for checkpoint pushes (proximity + verdict). One row per delivered '
    '(kind, dedup_key). §9 / journal/checkpoint_push.py.';
COMMENT ON COLUMN push_log.dedup_key IS
    'verdict:{N} | verdict:10:undefined | proximity:{count}. UNIQUE with kind — the push '
    'checks it before sending and inserts after a successful send (at-least-once).';

-- RLS on, no policy (matches all spine tables; service_role bypasses RLS).
ALTER TABLE push_log ENABLE ROW LEVEL SECURITY;

-- The required grant (see header). Per-table, not a blanket re-grant — avoids the
-- SQL-Editor implicit-transaction rollback the init migration's GRANTS block warns about.
GRANT SELECT, INSERT, UPDATE, DELETE ON push_log TO service_role;
