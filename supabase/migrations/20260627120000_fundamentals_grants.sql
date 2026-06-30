-- fundamentals three-layer schema — service_role GRANTs (the missing privilege).
--
-- The fundamentals stack (fundamentals table -> fundamentals_latest view ->
-- corporate_actions table) was applied to the live DB directly, AFTER the init
-- migration (20260612175007). As that migration's header warns, its
-- `grant ... on all tables in schema public` covers only tables that existed AT
-- grant time and deliberately sets NO default privileges for future tables — so
-- these three relations got NO grant. Every service_role call against them fails
-- 42501 "permission denied for table fundamentals" (hit on the very first write
-- from ingestion/sec_facts.py). This is the same footgun push_log
-- (20260620120000) and pending_annotations (20260620130000) each had to fix with
-- an explicit per-table grant — this migration does the same for fundamentals.
--
-- GRANT-only by design: the tables/view already exist and are verified against the
-- live DB, and their DDL was not authored here — so this migration does NOT
-- create or alter them (no risk of clobbering the live shape). It only grants the
-- privileges PostgREST checks before RLS, and (idempotently) enables RLS on the two
-- base tables to match every other spine table. Re-running is safe: grants and
-- `ENABLE ROW LEVEL SECURITY` are idempotent.
--
-- Views are not RLS targets and inherit no grant from their base table, so
-- fundamentals_latest needs its own GRANT SELECT for service_role to read it.

-- RLS on, no policy (matches all spine tables; service_role bypasses RLS). Base
-- tables only — a view has no row-level security.
ALTER TABLE fundamentals       ENABLE ROW LEVEL SECURITY;
ALTER TABLE corporate_actions  ENABLE ROW LEVEL SECURITY;

-- The required grants (see header). Per-table/-view, not a blanket re-grant —
-- avoids the SQL-Editor implicit-transaction rollback the init migration warns of.
GRANT SELECT, INSERT, UPDATE, DELETE ON fundamentals      TO service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON corporate_actions TO service_role;
GRANT SELECT                          ON fundamentals_latest TO service_role;
