-- =============================================================================
-- Signal Lab ledger (Law 1, Amendment #2 — pre-registered experimental signal)
--
-- One row per (signal_version, trading day). The signal is computed at the close of
-- day D-1 and SHADOW-scored against day D; the row is dated D. FAVORABLE days carry a
-- scored outcome (win/loss/no_trigger) and shadow P&L; UNFAVORABLE days log state only
-- (outcome 'no_trigger', shadow_pnl 0). A FAVORABLE day whose day-D OHLC is missing is
-- 'unknown' with NULL P&L (fail-loud, logged signal:inputs_missing).
--
-- PK (signal_version, date): idempotent per-day writes AND a fresh ledger per signal
-- version — editing the rule after registration means signal_v2 with its own clean
-- record, never a mutation of v1 (Law 6). Rows are written ONCE (insert-only-missing),
-- so a later backfill re-run can never overwrite a day scored live.
-- =============================================================================
create table if not exists signal_ledger (
    signal_version  text        not null,
    date            date        not null,
    signal_state    text        not null,
    outcome         text        not null,
    shadow_pnl      numeric,                                 -- NULLABLE (unknown outcome)
    inputs_json     jsonb,
    created_at      timestamptz not null default now(),
    constraint signal_ledger_pkey primary key (signal_version, date),
    constraint signal_ledger_state_chk
        check (signal_state in ('FAVORABLE', 'UNFAVORABLE')),
    constraint signal_ledger_outcome_chk
        check (outcome in ('win', 'loss', 'no_trigger', 'unknown'))
);

comment on table  signal_ledger is
    'Signal Lab shadow track record (Law 1 Amendment #2). Shadow P&L only — never a real order.';
comment on column signal_ledger.shadow_pnl is
    'Simulated P&L from the pre-registered fee model; NULL when outcome is unknown. Not money.';

create index if not exists signal_ledger_version_date_desc_idx
    on signal_ledger (signal_version, date desc);
