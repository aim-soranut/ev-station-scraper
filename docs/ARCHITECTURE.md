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
  - Insert connector_snapshots (one row per connector)
  - Purge snapshots older than RETENTION_DAYS
     │
     │  supabase-py (service key, bypasses RLS)
     ▼
Supabase (Postgres)
  stations            ← static info, upserted
  connector_snapshots ← connector-level time series, insert-only
  dash_* RPCs         ← derive station aggregates for the dashboard
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
   - Upsert station info into `stations` (batched; `first_seen_at` preserved on conflict)
   - Emit **one row per connector** from `evses[].connectors[]` (status, price, power, AC/DC type, `status_updated_at`) and batch-insert into `connector_snapshots` — no aggregation at write time
4. Purge connector snapshots older than `RETENTION_DAYS` via the `purge_old_snapshots` RPC
5. Log: `polled 5597 stations | 214 api calls | inserted 15937 connectors | purged 0 | 78.3s`

Station-level metrics (occupancy, price ranges, status counts) are **derived at read time** in the `dash_*` functions, so they always reconcile to the connector data.

## Why APScheduler (not Railway Cron)

Railway Cron spins up a fresh container per run, which means cold-start overhead (~5–10s) and no shared state. APScheduler runs inside a long-lived Worker process — simpler, no container overhead, and easier to debug via streaming logs.

## Rate limiting

Each API call sleeps 0.3s (`sleep_seconds=0.3` in `fetch_stations_json`). A nationwide poll requires ~100–300 API calls after quadrant splitting stabilises (the box covers all of Thailand). Total poll time: a few minutes — well within the 30-min window.

## Supabase write strategy

- `stations`: `upsert` with `on_conflict="id"`, batched — safe to run repeatedly; `first_seen_at` is preserved on conflict
- `connector_snapshots`: batched `insert` — append-only; grows ~16k rows every 30 min (**~766k rows/day, ~23M rows/month** nationwide)

## Dashboard read path

The dashboard (`dashboard/app.py`) connects with the **anon** key. Read-only RLS policies on both tables allow `select` for `anon`. The `dash_*` SQL functions derive station-level aggregates (occupancy/price rollups, status counts, the price-vs-occupancy scatter) from the connector rows server-side, so the browser receives small aggregated results — never the full connector table. Because PostgREST caps responses at 1000 rows, the dashboard pages through results (`limit`/`offset` with a stable sort). Results are cached in Streamlit for 5 minutes (`st.cache_data(ttl=300)`).

## Retention & scaling

Connector-level nationwide collection at 30-min is **~23M rows/month**, so the poller calls `purge_old_snapshots(RETENTION_DAYS)` each cycle to drop old rows.

**Free-tier caveat (now significant):** at ~150–250 bytes/row incl. indexes, the Supabase 500 MB free tier holds only a **few days** of connector-level nationwide history. Realistic options:
- Lower `RETENTION_DAYS` aggressively (e.g. `2`–`3`)
- Scope collection to the focus provinces (Nonthaburi + Pathum Thani) — ~1/12th the volume
- Upgrade to Supabase Pro (8 GB)
- Add monthly partitioning (`PARTITION BY RANGE (polled_at)`) and drop old partitions
