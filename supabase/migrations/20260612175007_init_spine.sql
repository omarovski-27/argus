-- =============================================================================
-- ARGUS — Phase 0 Data Spine DDL  (Supabase / Postgres 15)
-- Maps to blueprint §4 (schema), with detail from §2 item 3, §7, §8, §12, §14.
--
-- Design ethos: "no migrations." Enumerations are TEXT + named CHECK constraints
-- (not native ENUM); tunable parameters live as JSONB rows in `config`.
--
-- Conventions:
--   * Surrogate id PKs  : bigint generated always as identity
--   * Natural composite : prices_eod, indicators, macro_series
--   * Natural single    : instruments (symbol), config (key)
--   * Money/price/qty/delta_shares = numeric ; volume = bigint ;
--     latency_ms/confidence/day_trades = integer
--   * created_at on every surrogate-id table (audit) ; config keeps updated_at
--   * Run order is dependency-ordered (targets before their FKs)
--
-- Idempotent: CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS throughout.
-- NOTE: re-running with an altered CHECK/UNIQUE will NOT retro-edit an existing
-- table (IF NOT EXISTS is a no-op when the table already exists) — use
-- `supabase db reset` in a fresh project, or ALTER explicitly, if a constraint changes.
--
-- DEFERRED to the ingestion phase (intentionally NOT created here — see PHASE0-TODO.md):
--   * corporate_actions table  (split-safety; arrives with Flex Corporate Actions feed)
--   * transactions.ext_id       (Flex execution id for idempotent upserts)
--   * contributions.currency    (JD vs USD normalization)
-- =============================================================================


-- -----------------------------------------------------------------------------
-- 1. instruments                                                         (§4, §2)
--    The 4 tracked tickers. first_trade_date drives young-ticker indicator
--    suppression (SPCX first_trade_date = 2026-06-12 → no SMA50 until 50 sessions).
-- -----------------------------------------------------------------------------
create table if not exists instruments (
    symbol            text primary key,
    name              text,
    first_trade_date  date
);

comment on table  instruments is 'Tracked tickers (TSLA, SPCX, SPY, QQQ). §4.';
comment on column instruments.first_trade_date is
    'Listing date; drives indicator suppression for young tickers (SPCX = 2026-06-12).';


-- -----------------------------------------------------------------------------
-- 2. prices_eod                                                          (§4, §5)
--    Daily OHLCV. Seeded 200+ days at setup, then +1 row/ticker/day.
--    source: tiingo (primary) | yfinance (fallback).
-- -----------------------------------------------------------------------------
create table if not exists prices_eod (
    symbol   text   not null references instruments (symbol),
    date     date   not null,
    open     numeric,
    high     numeric,
    low      numeric,
    close    numeric,
    volume   bigint,
    source   text,
    constraint prices_eod_pkey       primary key (symbol, date),
    constraint prices_eod_source_chk check (source in ('tiingo', 'yfinance')),
    constraint prices_eod_volume_chk check (volume is null or volume >= 0)
);

comment on table prices_eod is 'Daily OHLCV per ticker. §4 / §5. PK (symbol, date).';

create index if not exists prices_eod_symbol_date_desc_idx
    on prices_eod (symbol, date desc);


-- -----------------------------------------------------------------------------
-- 3. indicators                                                          (§4, §5)
--    Locally computed (pandas_ta) from prices_eod. SPARSE by design: rows are
--    OMITTED for young tickers until enough history exists (absence encodes
--    suppression). value is NULLABLE. name is an OPEN set (no CHECK) so adding a
--    new indicator needs no DDL change.
-- -----------------------------------------------------------------------------
create table if not exists indicators (
    symbol   text   not null references instruments (symbol),
    date     date   not null,
    name     text   not null,
    value    numeric,
    constraint indicators_pkey primary key (symbol, date, name)
);

comment on table  indicators is
    'Locally computed indicators (pandas_ta). Sparse: omitted for young tickers. §4 / §5.';
comment on column indicators.value is
    'Nullable; row omitted entirely (not null-valued) when history is insufficient.';

create index if not exists indicators_symbol_date_desc_idx
    on indicators (symbol, date desc);


-- -----------------------------------------------------------------------------
-- 4. macro_series                                                        (§4, §5)
--    FRED series. series_id: DFF | CPIAUCSL | UNRATE | DGS10 | T10Y2Y | VIXCLS.
-- -----------------------------------------------------------------------------
create table if not exists macro_series (
    series_id  text    not null,
    date       date    not null,
    value      numeric,
    constraint macro_series_pkey    primary key (series_id, date),
    constraint macro_series_id_chk  check (series_id in
        ('DFF', 'CPIAUCSL', 'UNRATE', 'DGS10', 'T10Y2Y', 'VIXCLS'))
);

comment on table macro_series is 'FRED macro series (6). §4 / §5. PK (series_id, date).';

create index if not exists macro_series_id_date_desc_idx
    on macro_series (series_id, date desc);


-- -----------------------------------------------------------------------------
-- 5. calendar_events                                                    (§4, §14)
--    Forward calendar (first-class; rendered, never hallucinated — Law 4).
--    UNIQUE NULLS NOT DISTINCT (PG15) so re-seeded / re-fetched events dedupe
--    even when symbol is NULL (macro events). SPCX lockup rows seeded here (§14).
-- -----------------------------------------------------------------------------
create table if not exists calendar_events (
    id                bigint generated always as identity primary key,
    date              date   not null,
    type              text   not null,
    symbol            text   references instruments (symbol),          -- NULLABLE (macro)
    conditional_rule  jsonb,                                           -- nullable
    materiality       text,                                            -- nullable
    created_at        timestamptz not null default now(),
    constraint calendar_events_type_chk check (type in
        ('fomc', 'cpi', 'nfp', 'earnings', 'lockup', 'index',
         'quiet_period', 'research')),
    constraint calendar_events_materiality_chk check
        (materiality is null or materiality in ('high', 'medium', 'low')),
    constraint calendar_events_dedup_uk
        unique nulls not distinct (type, date, symbol)
);

comment on table  calendar_events is
    'Forward calendar; rendered from table never hallucinated (Law 4). §4 / §14.';
comment on column calendar_events.symbol is
    'NULL for macro events (fomc/cpi/nfp); set for ticker-specific (earnings/lockup).';
comment on column calendar_events.conditional_rule is
    'JSONB armed-trigger spec, e.g. {"metric":"close","op":">=","threshold":175.50,'
    '"sessions_required":5,"window":10,"anchor":"post_q2_earnings"} '
    '(SPCX +10% conditional unlock, §14). NULL for unconditional events.';

create index if not exists calendar_events_date_idx
    on calendar_events (date);
create index if not exists calendar_events_symbol_date_idx
    on calendar_events (symbol, date);


-- -----------------------------------------------------------------------------
-- 6. headlines                                                           (§4, §7)
--    3 non-overlapping sources. url is the UNIQUE dedup key (§7).
-- -----------------------------------------------------------------------------
create table if not exists headlines (
    id            bigint generated always as identity primary key,
    url           text not null,
    source        text,
    title         text,
    published_at  timestamptz,
    ticker_tags   text[],
    created_at    timestamptz not null default now(),
    constraint headlines_url_uk     unique (url),                       -- dedup key
    constraint headlines_source_chk check (source in ('av', 'reuters', 'reddit'))
);

comment on table  headlines is 'Raw headlines; URL is the dedup key (§7). §4.';
comment on column headlines.url is 'UNIQUE — the only dedup mechanism (URL match only, §7).';

create index if not exists headlines_published_at_desc_idx
    on headlines (published_at desc);
create index if not exists headlines_ticker_tags_gin_idx
    on headlines using gin (ticker_tags);


-- -----------------------------------------------------------------------------
-- 7. sentiment                                                           (§4, §8)
--    Derived from headlines. method: av_native | haiku (swappable scorer).
--    direction: bullish | neutral | bearish. magnitude is UNCONSTRAINED numeric
--    (see comment) to avoid rejecting signed AV scores. ON DELETE CASCADE.
-- -----------------------------------------------------------------------------
create table if not exists sentiment (
    id           bigint generated always as identity primary key,
    headline_id  bigint not null references headlines (id) on delete cascade,
    method       text,
    direction    text,
    magnitude    numeric,
    created_at   timestamptz not null default now(),
    constraint sentiment_method_chk    check (method    in ('av_native', 'haiku')),
    constraint sentiment_direction_chk check (direction in ('bullish', 'neutral', 'bearish'))
);

comment on table  sentiment is
    'Per-headline sentiment from swappable scorer (av_native|haiku). §4 / §8.';
comment on column sentiment.magnitude is
    'Signal strength. Scale is a build-time decision (ambiguity to resolve in Phase 1). '
    'Intentionally unconstrained: AV native scores are signed (~-1..+1), so a >=0 CHECK '
    'would silently reject bearish rows. Recommended convention: magnitude = strength, '
    'sign carried by `direction`.';

create index if not exists sentiment_headline_id_idx
    on sentiment (headline_id);


-- -----------------------------------------------------------------------------
-- 8. digests                                                             (§4, §6)
--    Each digest persists its EXACT input bundle (bundle_json) → reproducible
--    forever (Law 2). run_type: full (Mon) | pulse (/pulse light run).
-- -----------------------------------------------------------------------------
create table if not exists digests (
    id           bigint generated always as identity primary key,
    run_type     text,
    sent_at      timestamptz,
    full_text    text,
    bundle_json  jsonb,
    created_at   timestamptz not null default now(),
    constraint digests_run_type_chk check (run_type in ('full', 'pulse'))
);

comment on table  digests is 'Generated digests + frozen input bundle (Law 2). §4 / §6.';
comment on column digests.bundle_json is
    'Exact frozen synthesis input for reproducibility, e.g. '
    '{"prices":{...},"indicators":{...},"macro":{...},"headlines":[...],'
    '"calendar":[...],"book":{...},"config_snapshot":{...},"source_health":{...}}.';


-- -----------------------------------------------------------------------------
-- 9. positions_snapshot                                                  (§4)
--    Daily IBKR Flex Open-Positions snapshot.
-- -----------------------------------------------------------------------------
create table if not exists positions_snapshot (
    id            bigint generated always as identity primary key,
    date          date not null,
    symbol        text not null references instruments (symbol),
    qty           numeric,
    cost_basis    numeric,
    market_value  numeric,
    created_at    timestamptz not null default now()
);

comment on table positions_snapshot is 'Daily Flex Open-Positions snapshot. §4.';

create index if not exists positions_snapshot_date_desc_symbol_idx
    on positions_snapshot (date desc, symbol);


-- -----------------------------------------------------------------------------
-- 10. transactions                                                  (§4, §2 item 3)
--     Flex fills. side: buy | sell.
--     trade_type   (auto, quantity-proximity classifier):
--                  round_trip_sell | round_trip_rebuy | dca_buy | dca_sell | unclassified
--     override_type(nullable, via /override, ALWAYS WINS over trade_type) — same enum.
-- -----------------------------------------------------------------------------
create table if not exists transactions (
    id             bigint generated always as identity primary key,
    exec_time      timestamptz,
    symbol         text not null references instruments (symbol),
    side           text,
    qty            numeric,
    price          numeric,
    fees           numeric,
    trade_type     text not null default 'unclassified',
    override_type  text,                                              -- nullable; wins
    created_at     timestamptz not null default now(),
    constraint transactions_side_chk  check (side in ('buy', 'sell')),
    constraint transactions_price_chk check (price is null or price >= 0),
    constraint transactions_trade_type_chk check (trade_type in
        ('round_trip_sell', 'round_trip_rebuy', 'dca_buy', 'dca_sell', 'unclassified')),
    constraint transactions_override_type_chk check
        (override_type is null or override_type in
        ('round_trip_sell', 'round_trip_rebuy', 'dca_buy', 'dca_sell', 'unclassified'))
);

comment on table  transactions is 'IBKR Flex fills + classification. §4 / §2 item 3.';
comment on column transactions.trade_type is
    'Auto-assigned by quantity-proximity classifier (sleeve-sized vs DCA-sized).';
comment on column transactions.override_type is
    'Manual /override; when present ALWAYS WINS over trade_type. NULL = no override.';

create index if not exists transactions_exec_time_idx
    on transactions (exec_time);
create index if not exists transactions_symbol_exec_time_idx
    on transactions (symbol, exec_time);


-- -----------------------------------------------------------------------------
-- 11. contributions                                                      (§4)
--     DCA deposits auto-detected from Flex cash transactions.
--     amount is VARIABLE — never assumed fixed (Law 5 keeps these out of the sleeve).
-- -----------------------------------------------------------------------------
create table if not exists contributions (
    id          bigint generated always as identity primary key,
    date        date not null,
    amount      numeric,
    created_at  timestamptz not null default now()
);

comment on table  contributions is 'Variable DCA deposits (never assumed fixed). §4.';
comment on column contributions.amount is
    'Deposit amount; intentionally NOT >0-constrained (allow corrections/reversals).';


-- -----------------------------------------------------------------------------
-- 12. round_trips                                                     (§4, §7, §8)
--     Paired same-day sell→rebuy (the sleeve unit of work).
--     delta_shares = CORE sleeve metric (more shares = winning); CAN be negative
--     and near-zero — NOT constrained. pnl_usd CAN be negative — NOT constrained.
--     digest_id NULLABLE (FK ON DELETE SET NULL): daily detection precedes the
--     weekly Monday digest, so a round trip is often detected before its digest exists.
-- -----------------------------------------------------------------------------
create table if not exists round_trips (
    id                    bigint generated always as identity primary key,
    date                  date not null,
    symbol                text not null references instruments (symbol),
    qty                   numeric,
    sell_px               numeric,
    rebuy_px              numeric,
    fees                  numeric,
    pnl_usd               numeric,                                    -- may be negative
    delta_shares          numeric,                                    -- may be negative / ~0
    digest_id             bigint references digests (id) on delete set null,  -- NULLABLE
    day_trades_in_window  integer,
    created_at            timestamptz not null default now(),
    constraint round_trips_sell_px_chk  check (sell_px  is null or sell_px  >= 0),
    constraint round_trips_rebuy_px_chk check (rebuy_px is null or rebuy_px >= 0),
    constraint round_trips_day_trades_chk
        check (day_trades_in_window is null or day_trades_in_window >= 0)
);

comment on table  round_trips is 'Paired same-day sell→rebuy; the sleeve unit. §4 / §7 / §8.';
comment on column round_trips.delta_shares is
    'Core sleeve-only metric (more shares = winning). Signed; near-zero is normal. Not constrained.';
comment on column round_trips.digest_id is
    'Reporting digest; NULLABLE — daily detection precedes the weekly Monday digest. SET NULL on delete.';

create index if not exists round_trips_date_idx
    on round_trips (date);


-- -----------------------------------------------------------------------------
-- 13. trade_annotations                                                  (§4, §8)
--     Telegram-button capture per round trip. confidence_1to5 in 1..5.
--     ON DELETE CASCADE: annotation dies with its round trip.
-- -----------------------------------------------------------------------------
create table if not exists trade_annotations (
    id                bigint generated always as identity primary key,
    round_trip_id     bigint not null references round_trips (id) on delete cascade,
    confidence_1to5   integer,
    checklist_passed  boolean,
    notes             text,
    created_at        timestamptz not null default now(),
    constraint trade_annotations_confidence_chk
        check (confidence_1to5 is null or confidence_1to5 between 1 and 5)
);

comment on table trade_annotations is 'Per-round-trip annotations via Telegram buttons. §4 / §8.';

create index if not exists trade_annotations_round_trip_id_idx
    on trade_annotations (round_trip_id);


-- -----------------------------------------------------------------------------
-- 14. skip_log                                                       (§4, §7, §8)
--     Discretionary / filtered skips. reason: event_filter | discretion | other.
-- -----------------------------------------------------------------------------
create table if not exists skip_log (
    id          bigint generated always as identity primary key,
    date        date not null,
    reason      text,
    notes       text,
    created_at  timestamptz not null default now(),
    constraint skip_log_reason_chk check (reason in ('event_filter', 'discretion', 'other'))
);

comment on table skip_log is 'Logged skipped trades + reasons (Law 6). §4 / §7 / §8.';


-- -----------------------------------------------------------------------------
-- 15. fetch_log                                                     (§4, §12)
--     Every wrapped fetch (Law 7: silent failure = misinformation).
--     status: success | failure | timeout | unavailable.
-- -----------------------------------------------------------------------------
create table if not exists fetch_log (
    id          bigint generated always as identity primary key,
    source      text,
    run_id      text,
    status      text,
    latency_ms  integer,
    error       text,
    created_at  timestamptz not null default now(),
    constraint fetch_log_status_chk check (status in
        ('success', 'failure', 'timeout', 'unavailable')),
    constraint fetch_log_latency_chk check (latency_ms is null or latency_ms >= 0)
);

comment on table fetch_log is 'Wrapped-fetch outcomes; every failure surfaces (Law 7). §4 / §12.';

create index if not exists fetch_log_source_created_at_desc_idx
    on fetch_log (source, created_at desc);


-- -----------------------------------------------------------------------------
-- 16. config                                                       (§4, §2 item 3)
--     JSONB rows, NO fixed per-parameter columns → parameter changes need no
--     migration and keep full history. updated_at (not created_at) per spec.
-- -----------------------------------------------------------------------------
create table if not exists config (
    key         text primary key,
    value       jsonb,
    updated_at  timestamptz not null default now()
);

comment on table  config is
    'Tunable parameters as JSONB rows (no migrations on change). §4 / §2 item 3.';
comment on column config.value is
    'Parameter payload by key, e.g. sleeve_pct -> 0.20 ; sleeve_shares -> 17 ; '
    'bracket -> {"target":1.50,"stop":1.50,"time_stop":"15:50 ET"} ; '
    'phase -> "A" ; weekly_trade_cap -> 2 ; watchlist -> ["TSLA","SPCX","SPY","QQQ"] ; '
    'kill_criteria -> {"early_warning":{"trade":10,"delta_shares_lt":-1.0},'
    '"checkpoint":{"trade":20,"delta_shares_lt":0},'
    '"verdict":{"trade":50,"delta_shares_lt":0}}.';


-- =============================================================================
-- ROW LEVEL SECURITY  —  deny-by-default for anon/public
--
-- RLS ENABLED on all 16 tables with NO policies. With RLS on and zero policies,
-- the anon/authenticated (PostgREST) roles can read/write NOTHING. Argus is
-- accessed exclusively server-side via the Supabase SERVICE_ROLE key (which
-- BYPASSES RLS) from GitHub Actions and Vercel. No browser client exists, so this
-- hard-denies the public PostgREST endpoint while trusted backends are unaffected.
-- =============================================================================
alter table instruments         enable row level security;
alter table prices_eod          enable row level security;
alter table indicators          enable row level security;
alter table macro_series        enable row level security;
alter table calendar_events     enable row level security;
alter table headlines           enable row level security;
alter table sentiment           enable row level security;
alter table digests             enable row level security;
alter table positions_snapshot  enable row level security;
alter table transactions        enable row level security;
alter table contributions       enable row level security;
alter table round_trips         enable row level security;
alter table trade_annotations   enable row level security;
alter table skip_log            enable row level security;
alter table fetch_log           enable row level security;
alter table config              enable row level security;
-- END — 16 tables, dependency-ordered.
