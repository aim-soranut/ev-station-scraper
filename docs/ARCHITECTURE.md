# Architecture

## System overview

```
pugev.com API
     │
     │  HTTP GET every 30 min
     ▼
Railway Worker (Python)
  monitor/main.py
  - APScheduler triggers poll
  - Recursive quadrant fetch (whole country)
  - NO province filter (collect all of Thailand)
  - Upsert stations
  - Insert snapshots
  - Purge snapshots older than RETENTION_DAYS
     │
     │  supabase-py (service key, bypasses RLS)
     ▼
Supabase (Postgres)
  stations       ← static info, upserted
  snapshots      ← time series, insert-only
  dash_* RPCs    ← server-side aggregation for the dashboard
     │
     ├─ anon key + read-only RLS ──▶ Streamlit dashboard (dashboard/app.py)
     │                                province filter, charts, map
     │
     └─ Table Editor → Download CSV ─▶ Excel / analysis tool
```

## How the pugev.com API works

The API returns EV stations within a geographic bounding box:
```
GET /api/v1/stations?xmin={lon_min}&xmax={lon_max}&ymin={lat_min}&ymax={lat_max}
Response: { "data": { "count": N, "stations": [...] } }
```

**Key limitation:** when a bounding box contains more stations than the API returns, `count > len(stations)`. The poller detects this and recursively splits the box into 4 quadrants until every sub-box is fully returned. This is implemented in `pugev.py:collect_stations_recursive` (and ported into `monitor/main.py`).

## Data flow per poll cycle

1. `collect_stations_recursive(xmin=97.3, xmax=105.7, ymin=5.5, ymax=20.6)` — fetches **all stations in Thailand** (the dense Bangkok area triggers deep quadrant splitting, ~100–300 API calls/cycle)
2. **No province filter** — every station is kept, tagged with `province_code` + `province_name`
3. For each station:
   - Flatten `evses[].connectors[]` → extract all prices/statuses
   - Compute `min_price`/`max_price` over all connectors, plus `ac_min/ac_max` and `dc_min/dc_max` split by connector `type` (skip nulls)
   - Compute `n_connectors`, `n_occupied`, `n_available`
   - Upsert into `stations` (batched; `first_seen_at` preserved on conflict)
   - Insert one row into `snapshots` (batched)
4. Purge snapshots older than `RETENTION_DAYS` via the `purge_old_snapshots` RPC
5. Log: `polled 5597 stations | 214 api calls | inserted 5597 snapshots | purged 0 | 78.3s`

## Why APScheduler (not Railway Cron)

Railway Cron spins up a fresh container per run, which means cold-start overhead (~5–10s) and no shared state. APScheduler runs inside a long-lived Worker process — simpler, no container overhead, and easier to debug via streaming logs.

## Rate limiting

Each API call sleeps 0.3s (`sleep_seconds=0.3` in `fetch_stations_json`). A nationwide poll requires ~100–300 API calls after quadrant splitting stabilises (the box covers all of Thailand). Total poll time: a few minutes — well within the 30-min window.

## Supabase write strategy

- `stations`: `upsert` with `on_conflict="id"`, batched — safe to run repeatedly; `first_seen_at` is preserved on conflict
- `snapshots`: batched `insert` — append-only; grows ~5,600 rows every 30 min (**~269k rows/day, ~8M rows/month**)

## Dashboard read path

The dashboard (`dashboard/app.py`) connects with the **anon** key. Read-only RLS policies on both tables allow `select` for `anon`. Heavy aggregation (time bucketing, occupancy/price rollups, the price-vs-occupancy scatter) runs server-side in the `dash_*` SQL functions, so the browser only ever receives already-aggregated rows — never the full 8M-row snapshot table. Results are cached in Streamlit for 5 minutes (`st.cache_data(ttl=300)`).

## Retention & scaling

Nationwide collection at 30-min is ~8M rows/month, so the poller calls `purge_old_snapshots(RETENTION_DAYS)` (default 30) each cycle to drop old rows.

**Free-tier caveat:** at ~150–250 bytes/row incl. indexes, 30 days nationwide is well over Supabase's 500 MB free-tier limit — in practice the free tier holds roughly 1–2 weeks of nationwide history. Options when it fills up:
- Lower `RETENTION_DAYS` (a one-line env change)
- Upgrade to Supabase Pro (8 GB)
- Add monthly partitioning (`PARTITION BY RANGE (polled_at)`) and drop old partitions
- Export monthly CSVs and truncate
