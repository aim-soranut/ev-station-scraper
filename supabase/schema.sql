-- ev-station-scraper schema (connector-level)
-- Run once in the Supabase SQL Editor (safe to re-run: tables use
-- "if not exists", functions use "create or replace").
--
-- Collects EV charging stations nationwide (Thailand) every 30 minutes and
-- stores CONNECTOR-LEVEL snapshots, faithful to the pugev API. Station-level
-- aggregates (occupancy, price ranges) are derived in the dash_* functions.
-- Province filtering is done in the dashboard, not at collection time.
--
-- MIGRATING from the old station-level schema? After running this, drop the
-- obsolete table:  drop table if exists public.snapshots cascade;

-- ─────────────────────────────────────────────────────────────────────────
-- Tables
-- ─────────────────────────────────────────────────────────────────────────

-- Static-ish info about each station. Upserted on every poll.
create table if not exists public.stations (
    id            integer primary key,            -- pugev.com station id (stable)
    name          text,
    address       text,
    latitude      double precision,
    longitude     double precision,
    province_code text,                           -- e.g. '12' (Nonthaburi), '13' (Pathum Thani)
    province_name text,                           -- e.g. 'Nonthaburi'
    source        text,                           -- charging network brand (evolt, pttv2, tesla, ...)
    first_seen_at timestamptz not null default now()
);

create index if not exists stations_province_code_idx on public.stations (province_code);

-- Time series at CONNECTOR granularity. One row per connector per poll cycle.
-- Append-only. Mirrors evses[].connectors[] from the source API.
create table if not exists public.connector_snapshots (
    id                bigint generated always as identity primary key,
    station_id        integer not null references public.stations (id),
    polled_at         timestamptz not null,       -- one timestamp per poll cycle
    evse_code         text,                       -- evses[].code
    connector_name    text,                       -- connectors[].name
    connector_type    text,                       -- 'AC' / 'DC'
    power_kw          numeric,                     -- connectors[].power
    ocpp_status       text,                       -- connector-level status (see values below)
    price             numeric,                     -- THB/kWh, per connector
    status_updated_at timestamptz                  -- connectors[].status_updated_at
);

create index if not exists connector_snapshots_station_polled_idx
    on public.connector_snapshots (station_id, polled_at desc);
create index if not exists connector_snapshots_polled_idx
    on public.connector_snapshots (polled_at);

-- ─────────────────────────────────────────────────────────────────────────
-- Row Level Security
-- Poller uses the service_role key (bypasses RLS); dashboard uses anon (read).
-- ─────────────────────────────────────────────────────────────────────────

alter table public.stations            enable row level security;
alter table public.connector_snapshots enable row level security;

drop policy if exists "anon read stations"   on public.stations;
drop policy if exists "anon read connectors" on public.connector_snapshots;

create policy "anon read stations"   on public.stations            for select to anon using (true);
create policy "anon read connectors" on public.connector_snapshots for select to anon using (true);

-- ─────────────────────────────────────────────────────────────────────────
-- Retention: drop connector snapshots older than N days. Called by the poller.
-- ─────────────────────────────────────────────────────────────────────────

create or replace function public.purge_old_snapshots(retention_days integer default 30)
returns bigint
language plpgsql
as $$
declare
    deleted bigint;
begin
    delete from public.connector_snapshots
    where polled_at < now() - make_interval(days => retention_days);
    get diagnostics deleted = row_count;
    return deleted;
end;
$$;

-- ─────────────────────────────────────────────────────────────────────────
-- Dashboard RPC functions. Station-level metrics are DERIVED from connector
-- rows here, so status counts always reconcile to n_connectors.
-- All SECURITY INVOKER; pass p_codes = NULL for all of Thailand.
-- ─────────────────────────────────────────────────────────────────────────

-- Provinces present in the data, with station counts (for the filter UI).
create or replace function public.dash_provinces()
returns table (province_code text, province_name text, station_count bigint)
language sql stable
as $$
    select province_code, province_name, count(*)::bigint
    from public.stations
    where province_code is not null
    group by province_code, province_name
    order by province_name;
$$;

-- Latest snapshot per station, aggregated from its connectors (map + KPIs).
create or replace function public.dash_latest_status(p_codes text[] default null)
returns table (
    id integer, name text, latitude double precision, longitude double precision,
    province_name text, source text, ocpp_status text,
    n_connectors integer, n_occupied integer, n_available integer,
    n_close integer, n_maintenance integer, n_specific integer, n_unknown integer,
    min_price numeric, max_price numeric, ac_min_price numeric, dc_min_price numeric,
    polled_at timestamptz
)
language sql stable
as $$
    with latest as (
        select station_id, max(polled_at) as mp
        from public.connector_snapshots
        group by station_id
    )
    select
        s.id, s.name, s.latitude, s.longitude, s.province_name, s.source,
        case
            when count(*) filter (where cs.ocpp_status = 'occupied')    > 0 then 'occupied'
            when count(*) filter (where cs.ocpp_status = 'available')   > 0 then 'available'
            when count(*) filter (where cs.ocpp_status = 'maintenance') > 0 then 'maintenance'
            when count(*) filter (where cs.ocpp_status = 'close')       > 0 then 'close'
            when count(*) filter (where cs.ocpp_status = 'specific')    > 0 then 'specific'
            else 'unknown'
        end as ocpp_status,
        count(*)::int                                                    as n_connectors,
        count(*) filter (where cs.ocpp_status = 'occupied')::int         as n_occupied,
        count(*) filter (where cs.ocpp_status = 'available')::int        as n_available,
        count(*) filter (where cs.ocpp_status = 'close')::int            as n_close,
        count(*) filter (where cs.ocpp_status = 'maintenance')::int      as n_maintenance,
        count(*) filter (where cs.ocpp_status = 'specific')::int         as n_specific,
        count(*) filter (where cs.ocpp_status = 'unknown')::int          as n_unknown,
        min(cs.price)                                                    as min_price,
        max(cs.price)                                                    as max_price,
        min(cs.price) filter (where cs.connector_type = 'AC')            as ac_min_price,
        min(cs.price) filter (where cs.connector_type = 'DC')            as dc_min_price,
        max(cs.polled_at)                                                as polled_at
    from public.connector_snapshots cs
    join latest l on l.station_id = cs.station_id and cs.polled_at = l.mp
    join public.stations s on s.id = cs.station_id
    where p_codes is null or s.province_code = any (p_codes)
    group by s.id, s.name, s.latitude, s.longitude, s.province_name, s.source;
$$;

-- Hourly fleet occupancy rate over time (occupied connectors / total), per province.
create or replace function public.dash_occupancy_timeseries(
    p_codes text[] default null,
    p_start timestamptz default now() - interval '7 days',
    p_end   timestamptz default now()
)
returns table (ts timestamptz, province_name text, occupancy_rate numeric, stations bigint)
language sql stable
as $$
    select
        date_trunc('hour', cs.polled_at) as ts,
        s.province_name,
        round(count(*) filter (where cs.ocpp_status = 'occupied')::numeric
              / nullif(count(*), 0), 4) as occupancy_rate,
        count(distinct cs.station_id)::bigint as stations
    from public.connector_snapshots cs
    join public.stations s on s.id = cs.station_id
    where cs.polled_at between p_start and p_end
      and (p_codes is null or s.province_code = any (p_codes))
    group by 1, 2
    order by 1, 2;
$$;

-- Hourly average AC/DC connector price over time, per province.
create or replace function public.dash_price_timeseries(
    p_codes text[] default null,
    p_start timestamptz default now() - interval '7 days',
    p_end   timestamptz default now()
)
returns table (ts timestamptz, province_name text, avg_ac_price numeric, avg_dc_price numeric)
language sql stable
as $$
    select
        date_trunc('hour', cs.polled_at) as ts,
        s.province_name,
        round(avg(cs.price) filter (where cs.connector_type = 'AC'), 2) as avg_ac_price,
        round(avg(cs.price) filter (where cs.connector_type = 'DC'), 2) as avg_dc_price
    from public.connector_snapshots cs
    join public.stations s on s.id = cs.station_id
    where cs.polled_at between p_start and p_end
      and (p_codes is null or s.province_code = any (p_codes))
    group by 1, 2
    order by 1, 2;
$$;

-- Per-station average price vs. average occupancy (the core scatter).
create or replace function public.dash_station_price_occupancy(
    p_codes text[] default null,
    p_start timestamptz default now() - interval '7 days',
    p_end   timestamptz default now()
)
returns table (
    id integer, name text, source text, province_name text,
    avg_occupancy numeric, avg_price numeric, samples bigint
)
language sql stable
as $$
    select
        s.id, s.name, s.source, s.province_name,
        round(avg(case when cs.ocpp_status = 'occupied' then 1.0 else 0 end), 4) as avg_occupancy,
        round(avg(cs.price), 2) as avg_price,
        count(distinct cs.polled_at)::bigint as samples
    from public.connector_snapshots cs
    join public.stations s on s.id = cs.station_id
    where cs.polled_at between p_start and p_end
      and (p_codes is null or s.province_code = any (p_codes))
    group by s.id, s.name, s.source, s.province_name;
$$;

-- Per-poll aggregates for a single station (per-station detail view).
create or replace function public.dash_station_history(p_station_id integer)
returns table (
    polled_at timestamptz, ocpp_status text,
    n_connectors integer, n_occupied integer, n_available integer,
    min_price numeric, max_price numeric, ac_min_price numeric, dc_min_price numeric
)
language sql stable
as $$
    select
        cs.polled_at,
        case
            when count(*) filter (where cs.ocpp_status = 'occupied')  > 0 then 'occupied'
            when count(*) filter (where cs.ocpp_status = 'available') > 0 then 'available'
            else 'other'
        end as ocpp_status,
        count(*)::int                                              as n_connectors,
        count(*) filter (where cs.ocpp_status = 'occupied')::int   as n_occupied,
        count(*) filter (where cs.ocpp_status = 'available')::int  as n_available,
        min(cs.price)                                              as min_price,
        max(cs.price)                                              as max_price,
        min(cs.price) filter (where cs.connector_type = 'AC')      as ac_min_price,
        min(cs.price) filter (where cs.connector_type = 'DC')      as dc_min_price
    from public.connector_snapshots cs
    where cs.station_id = p_station_id
    group by cs.polled_at
    order by cs.polled_at;
$$;

-- Raw connector-level rows joined with station info, for CSV/JSON export.
-- One row per connector per poll — faithful to the source granularity.
create or replace function public.dash_export_raw(
    p_codes text[] default null,
    p_start timestamptz default now() - interval '7 days',
    p_end   timestamptz default now()
)
returns table (
    station_id integer, name text, address text,
    province_code text, province_name text, source text,
    latitude double precision, longitude double precision,
    polled_at timestamptz, evse_code text, connector_name text,
    connector_type text, power_kw numeric, ocpp_status text,
    price numeric, status_updated_at timestamptz
)
language sql stable
as $$
    select s.id, s.name, s.address, s.province_code, s.province_name, s.source,
           s.latitude, s.longitude,
           cs.polled_at, cs.evse_code, cs.connector_name, cs.connector_type,
           cs.power_kw, cs.ocpp_status, cs.price, cs.status_updated_at
    from public.connector_snapshots cs
    join public.stations s on s.id = cs.station_id
    where cs.polled_at between p_start and p_end
      and (p_codes is null or s.province_code = any (p_codes))
    order by cs.polled_at, s.id, cs.connector_name;
$$;

-- Row count for a raw export selection, so the dashboard can warn before pulling.
create or replace function public.dash_export_count(
    p_codes text[] default null,
    p_start timestamptz default now() - interval '7 days',
    p_end   timestamptz default now()
)
returns bigint
language sql stable
as $$
    select count(*)::bigint
    from public.connector_snapshots cs
    join public.stations s on s.id = cs.station_id
    where cs.polled_at between p_start and p_end
      and (p_codes is null or s.province_code = any (p_codes));
$$;
