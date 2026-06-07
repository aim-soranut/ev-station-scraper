-- ev-station-scraper schema
-- Run once in the Supabase SQL editor (see docs/RUNBOOK.md §1).
-- Safe to re-run: every statement is idempotent.

-- Static station info. Upserted on conflict (id) by the poller; the poller
-- omits first_seen_at from the upsert payload so it survives updates.
create table if not exists stations (
    id            integer primary key,            -- pugev.com station ID (stable)
    name          text,
    address       text,
    latitude      double precision,
    longitude     double precision,
    province_code text,                            -- '12' = Nonthaburi, '13' = Pathum Thani
    source        text,                            -- network brand: pttv2, evolt, tesla, ea, ...
    first_seen_at timestamptz not null default now()
);

-- Append-only time series: one row per station per poll cycle (~every 5 min).
-- Never updated or deleted after insert.
create table if not exists snapshots (
    id           bigserial primary key,
    station_id   integer not null references stations(id),
    ocpp_status  text,                             -- station-level status at poll time
    min_price    numeric,                          -- THB/kWh; NULL if no price data
    max_price    numeric,                          -- THB/kWh; NULL if no price data
    n_connectors integer,
    n_occupied   integer,
    polled_at    timestamptz not null default now()
);

-- "Latest per station" and per-station history scans (the hot path; see
-- the distinct-on query in docs/DATA_DICTIONARY.md).
create index if not exists snapshots_station_polled_idx
    on snapshots (station_id, polled_at desc);

-- Time-window scans and time-ordered exports.
create index if not exists snapshots_polled_idx
    on snapshots (polled_at desc);
