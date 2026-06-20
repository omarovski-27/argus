-- pending_annotations — staging buffer for in-the-moment trade annotations (/felt; §8 / §9).
--
-- Why: /felt is fired at TRADE time, but the round_trip it describes does not exist until
-- journal.pairing detects it the next morning (the 20:30 UTC Flex pull). And
-- trade_annotations.round_trip_id is NOT NULL — there is no trip to point at yet. So the note
-- (reason / feeling / confidence) is STAGED here at /felt time, then attached to its trip by
-- journal.annotation_reconcile right after pairing. created_at is the HONEST in-the-moment
-- timestamp (carried onto trade_annotations.captured_at at reconcile) — the whole point of
-- capturing in the moment rather than at next-morning file time.
--
-- Match basis: a note attaches to a round_trip with the SAME symbol and SAME UTC date
-- (pending_annotations.trade_date == round_trips.date). trade_date is the UTC calendar day,
-- stamped explicitly by the handler — NOT derived from created_at at match time — so the match
-- key is a plain date column on both sides (no timezone-offset slicing). The sleeve session never
-- crosses UTC midnight, so a fill's UTC date is its trading date and a /felt typed during the
-- trade shares it. A note with no trade that day simply never matches — it can never mis-attach
-- to a later trip.
--
-- One-per-day is a SCHEMA invariant, not just a handler read-then-write: a FULL unique index on
-- (symbol, trade_date) — consumed or not — enforces the lock-first "one note per day, forever"
-- rule in the database, so no code path (not just a concurrent /felt) can ever stage a second
-- note for the same day. The handler's same-day check is the friendly "already logged" path; this
-- index is the guarantee behind it. created_at (the honest moment) is kept separate and carried
-- onto trade_annotations.captured_at at reconcile.
--
-- consumed_round_trip_id: NULL while pending; set to the trip id once reconciled. ON DELETE SET
-- NULL (not cascade) — deleting a trip reverts its note to unconsumed, never destroys it.
--
-- GRANT (required, not optional): this table is created AFTER the init migration's
-- `grant ... on all tables` (20260612175007, line 471), which covers only tables that existed at
-- grant time. A table added later gets NO grant and every backend call fails 42501
-- "permission denied for table pending_annotations" — the same lesson as push_log. The explicit
-- per-table grant below is what makes it usable by the only role Argus runs as.

create table if not exists pending_annotations (
    id                     bigint generated always as identity primary key,
    created_at             timestamptz not null default now(),  -- the honest in-moment timestamp (→ captured_at)
    trade_date             date not null default (timezone('utc', now()))::date,  -- UTC calendar day: match + lock key
    symbol                 text not null default 'TSLA',
    reason                 text,            -- free text; validated in the /felt handler, no DB CHECK
    feeling                text,            -- free text; validated in the /felt handler, no DB CHECK
    confidence_1to5        integer,
    consumed_round_trip_id bigint references round_trips (id) on delete set null,
    constraint pending_annotations_confidence_chk
        check (confidence_1to5 is null or confidence_1to5 between 1 and 5)
);

-- Reconcile a pre-existing (out-of-band) pending_annotations to this spec without dropping it.
alter table pending_annotations add column if not exists created_at             timestamptz default now();
alter table pending_annotations add column if not exists trade_date             date;
alter table pending_annotations add column if not exists symbol                 text;
alter table pending_annotations add column if not exists reason                 text;
alter table pending_annotations add column if not exists feeling                text;
alter table pending_annotations add column if not exists confidence_1to5        integer;
alter table pending_annotations add column if not exists consumed_round_trip_id bigint;

-- One note per (symbol, UTC trade_date), CONSUMED OR NOT — the full lock-first "one per day,
-- forever" invariant, enforced in the schema (this project locks its invariants in the DB:
-- round_trips.sell_ext_id, push_log's unique key). It also serves as the lookup index for the
-- /felt lock check. Built on the plain trade_date column — NOT an expression over created_at —
-- because a timestamptz->date cast is only STABLE, not IMMUTABLE, and Postgres forbids a
-- non-IMMUTABLE expression in an index.
create unique index if not exists pending_annotations_one_per_day_idx
    on pending_annotations (symbol, trade_date);

comment on table pending_annotations is
    'Staging buffer for in-the-moment /felt annotations; attached to a round_trip by '
    'journal.annotation_reconcile on same symbol + UTC date. §8 / §9.';
comment on column pending_annotations.created_at is
    'The in-the-moment /felt timestamp; carried onto trade_annotations.captured_at at reconcile.';
comment on column pending_annotations.trade_date is
    'UTC calendar day the note was logged — the match key against round_trips.date and the '
    'lock-first key (UNIQUE per symbol/day, consumed or not). Stamped by the handler, not derived.';
comment on column pending_annotations.consumed_round_trip_id is
    'NULL while pending; the attached trip id once reconciled. ON DELETE SET NULL — a deleted '
    'trip reverts the note to unconsumed, never destroys it.';

-- RLS on, no policy (matches all spine tables; service_role bypasses RLS).
alter table pending_annotations enable row level security;

-- The required grant (see header). Per-table, not a blanket re-grant.
grant select, insert, update, delete on pending_annotations to service_role;
