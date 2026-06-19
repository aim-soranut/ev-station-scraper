-- ev-station-scraper schema
-- Run once in the Supabase SQL Editor.
-- Collects EV charging stations nationwide (Thailand) every 30 minutes.
-- Province filtering is done in the dashboard, not at collection time.

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

-- Time series. One row per station per poll cycle. Append-only.
create table if not exists public.snapshots (
    id            bigint generated always as identity primary key,
    station_id    integer not null references public.stations (id),
    polled_at     timestamptz not null,
    ocpp_status   text,                           -- station-level status
    n_connectors  integer,
    n_occupied    integer,
    n_available   integer,
    min_price     numeric,                        -- across all connectors (THB/kWh)
    max_price     numeric,
    ac_min_price  numeric,                        -- AC connectors only (null if none)
    ac_max_price  numeric,
    dc_min_price  numeric,                        -- DC connectors only (null if none)
    dc_max_price  numeric
);

create index if not exists snapshots_station_polled_idx on public.snapshots (station_id, polled_at desc);
create index if not exists snapshots_polled_idx on public.snapshots (polled_at);

-- ─────────────────────────────────────────────────────────────────────────
-- Row Level Security
-- The poller uses the service_role key, which bypasses RLS.
-- The dashboard uses the anon key and only needs read access.
-- ─────────────────────────────────────────────────────────────────────────

alter table public.stations  enable row level security;
alter table public.snapshots enable row level security;

drop policy if exists "anon read stations"  on public.stations;
drop policy if exists "anon read snapshots" on public.snapshots;

create policy "anon read stations"  on public.stations  for select to anon using (true);
create policy "anon read snapshots" on public.snapshots for select to anon using (true);

-- ─────────────────────────────────────────────────────────────────────────
-- Retention: drop snapshots older than N days. Called by the poller.
-- ─────────────────────────────────────────────────────────────────────────

create or replace function public.purge_old_snapshots(retention_days integer default 30)
returns bigint
language plpgsql
as $$
declare
    deleted bigint;
begin
    delete from public.snapshots
    where polled_at < now() - make_interval(days => retention_days);
    get diagnostics deleted = row_count;
    return deleted;
end;
$$;

-- ─────────────────────────────────────────────────────────────────────────
-- Dashboard RPC functions (aggregation happens in Postgres, not the browser).
-- All are SECURITY INVOKER so the anon read policies above apply.
-- Pass p_codes = NULL for "All Thailand", or an array like ARRAY['12','13'].
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

-- Latest snapshot per station in the selected provinces (map + KPIs).
create or replace function public.dash_latest_status(p_codes text[] default null)
returns table (
    id integer, name text, latitude double precision, longitude double precision,
    province_name text, source text, ocpp_status text,
    n_connectors integer, n_occupied integer,
    min_price numeric, max_price numeric, ac_min_price numeric, dc_min_price numeric,
    polled_at timestamptz
)
language sql stable
as $$
    select distinct on (s.id)
        s.id, s.name, s.latitude, s.longitude, s.province_name, s.source,
        sn.ocpp_status, sn.n_connectors, sn.n_occupied,
        sn.min_price, sn.max_price, sn.ac_min_price, sn.dc_min_price, sn.polled_at
    from public.stations s
    join public.snapshots sn on sn.station_id = s.id
    where p_codes is null or s.province_code = any (p_codes)
    order by s.id, sn.polled_at desc;
$$;

-- Hourly fleet occupancy rate over time, per province.
create or replace function public.dash_occupancy_timeseries(
    p_codes text[] default null,
    p_start timestamptz default now() - interval '7 days',
    p_end   timestamptz default now()
)
returns table (ts timestamptz, province_name text, occupancy_rate numeric, stations bigint)
language sql stable
as $$
    select
        date_trunc('hour', sn.polled_at) as ts,
        s.province_name,
        round(sum(sn.n_occupied)::numeric / nullif(sum(sn.n_connectors), 0), 4) as occupancy_rate,
        count(distinct s.id)::bigint as stations
    from public.snapshots sn
    join public.stations s on s.id = sn.station_id
    where sn.polled_at between p_start and p_end
      and (p_codes is null or s.province_code = any (p_codes))
    group by 1, 2
    order by 1;
$$;

-- Hourly average AC/DC price over time, per province.
create or replace function public.dash_price_timeseries(
    p_codes text[] default null,
    p_start timestamptz default now() - interval '7 days',
    p_end   timestamptz default now()
)
returns table (ts timestamptz, province_name text, avg_ac_price numeric, avg_dc_price numeric)
language sql stable
as $$
    select
        date_trunc('hour', sn.polled_at) as ts,
        s.province_name,
        round(avg(sn.ac_min_price), 2) as avg_ac_price,
        round(avg(sn.dc_min_price), 2) as avg_dc_price
    from public.snapshots sn
    join public.stations s on s.id = sn.station_id
    where sn.polled_at between p_start and p_end
      and (p_codes is null or s.province_code = any (p_codes))
    group by 1, 2
    order by 1;
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
        round(avg(sn.n_occupied::numeric / nullif(sn.n_connectors, 0)), 4) as avg_occupancy,
        round(avg(coalesce(sn.dc_min_price, sn.ac_min_price, sn.min_price)), 2) as avg_price,
        count(*)::bigint as samples
    from public.snapshots sn
    join public.stations s on s.id = sn.station_id
    where sn.polled_at between p_start and p_end
      and (p_codes is null or s.province_code = any (p_codes))
    group by s.id, s.name, s.source, s.province_name;
$$;

-- Full snapshot history for a single station (per-station detail view).
create or replace function public.dash_station_history(p_station_id integer)
returns table (
    polled_at timestamptz, ocpp_status text,
    n_connectors integer, n_occupied integer,
    min_price numeric, max_price numeric, ac_min_price numeric, dc_min_price numeric
)
language sql stable
as $$
    select sn.polled_at, sn.ocpp_status, sn.n_connectors, sn.n_occupied,
           sn.min_price, sn.max_price, sn.ac_min_price, sn.dc_min_price
    from public.snapshots sn
    where sn.station_id = p_station_id
    order by sn.polled_at;
$$;

-- Raw per-poll snapshot rows joined with station info, for CSV export.
-- One row per station per poll cycle. Filtered by province + time window.
create or replace function public.dash_export_raw(
    p_codes text[] default null,
    p_start timestamptz default now() - interval '7 days',
    p_end   timestamptz default now()
)
returns table (
    station_id integer, name text, address text,
    province_code text, province_name text, source text,
    latitude double precision, longitude double precision,
    polled_at timestamptz, ocpp_status text,
    n_connectors integer, n_occupied integer, n_available integer,
    min_price numeric, max_price numeric,
    ac_min_price numeric, ac_max_price numeric,
    dc_min_price numeric, dc_max_price numeric
)
language sql stable
as $$
    select s.id, s.name, s.address, s.province_code, s.province_name, s.source,
           s.latitude, s.longitude,
           sn.polled_at, sn.ocpp_status, sn.n_connectors, sn.n_occupied, sn.n_available,
           sn.min_price, sn.max_price, sn.ac_min_price, sn.ac_max_price,
           sn.dc_min_price, sn.dc_max_price
    from public.snapshots sn
    join public.stations s on s.id = sn.station_id
    where sn.polled_at between p_start and p_end
      and (p_codes is null or s.province_code = any (p_codes))
    order by sn.polled_at, s.id;
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
    from public.snapshots sn
    join public.stations s on s.id = sn.station_id
    where sn.polled_at between p_start and p_end
      and (p_codes is null or s.province_code = any (p_codes));
$$;

