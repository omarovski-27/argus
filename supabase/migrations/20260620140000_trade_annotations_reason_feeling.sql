-- trade_annotations.reason / feeling / captured_at + one-annotation-per-trip uniqueness (§8 Step 4).
--
-- Why: this step records WHY a trade was made and HOW it felt, alongside the existing
-- confidence_1to5. reason / feeling are FREE TEXT — the vocabulary lives in config
-- (annotation_reasons / annotation_feelings) and is validated in the /felt handler, so it grows
-- by editing a config row, NEVER a migration ("config is JSONB rows, not schema"). That is the
-- deliberate reason there is no CHECK constraint on reason / feeling.
--
-- captured_at: the row's own created_at defaults to RECONCILE time (next morning, when
-- journal.annotation_reconcile files it). The honest "when did I feel this" timestamp is the
-- /felt moment — carried from pending_annotations.created_at into captured_at at reconcile.
--
-- UNIQUE(round_trip_id): one annotation per trip. It makes the reconcile upsert idempotent
-- (on_conflict=round_trip_id, ignore_duplicates), so a daily re-run — or a crash-orphaned note
-- re-matching its already-annotated trip — is a harmless no-op, never a duplicate row.
--
-- NO grant needed: trade_annotations existed at the init migration's `grant ... on all tables`
-- (20260612175007, line 471), so it is already granted to service_role and new columns inherit
-- table-level privileges. (Unlike pending_annotations, a NEW table, which carries its own grant.)
-- Additive only; mirrors the round_trips.sell_ext_id / push_log DROP-then-ADD constraint style.

alter table trade_annotations add column if not exists reason      text;
alter table trade_annotations add column if not exists feeling     text;
alter table trade_annotations add column if not exists captured_at timestamptz;

-- DROP then ADD so a re-run with an altered definition re-applies cleanly.
alter table trade_annotations drop constraint if exists trade_annotations_round_trip_id_key;
alter table trade_annotations add  constraint trade_annotations_round_trip_id_key
    unique (round_trip_id);

comment on column trade_annotations.reason is
    'Why the trade was made (free text; vocabulary validated in the /felt handler from '
    'config.annotation_reasons, not a DB CHECK — vocab grows by config edit).';
comment on column trade_annotations.feeling is
    'How it felt (free text; validated from config.annotation_feelings in the /felt handler).';
comment on column trade_annotations.captured_at is
    'The in-the-moment /felt timestamp (from pending_annotations.created_at), NOT the '
    'reconcile/file time held in created_at.';
