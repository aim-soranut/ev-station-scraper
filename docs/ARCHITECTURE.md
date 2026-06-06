# Architecture

## System overview

```
pugev.com API
     │
     │  HTTP GET every 5 min
     ▼
Railway Worker (Python)
  monitor/main.py
  - APScheduler triggers poll
  - Recursive quadrant fetch
  - Province filter (code 12, 13)
  - Upsert stations
  - Insert snapshots
     │
     │  supabase-py (service key)
     ▼
Supabase (Postgres)
  stations       ← static info, upserted
  snapshots      ← time series, insert-only
     │
     │  Table Editor → Download CSV
     ▼
Excel / analysis tool
```

## How the pugev.com API works

The API returns EV stations within a geographic bounding box:
```
GET /api/v1/stations?xmin={lon_min}&xmax={lon_max}&ymin={lat_min}&ymax={lat_max}
Response: { "data": { "count": N, "stations": [...] } }
```

**Key limitation:** when a bounding box contains more stations than the API returns, `count > len(stations)`. The poller detects this and recursively splits the box into 4 quadrants until every sub-box is fully returned. This is implemented in `pugev.py:collect_stations_recursive`.

## Data flow per poll cycle

1. `collect_stations_recursive(xmin=100.30, xmax=100.85, ymin=13.75, ymax=14.25)` — fetches all stations in the combined bounding box
2. Filter: `station["province"]["code"] in ("12", "13")`
3. For each station:
   - Flatten `evses[].connectors[]` → extract all prices and statuses
   - Compute `min_price`, `max_price` from connector prices (skip nulls)
   - Compute `n_connectors`, `n_occupied` (connectors with `ocpp_status == "occupied"`)
   - Upsert into `stations` (on conflict update name/address in case they change)
   - Insert one row into `snapshots`
4. Log: `[2026-06-06 12:00:01] polled 457 stations, 0 errors`

## Why APScheduler (not Railway Cron)

Railway Cron spins up a fresh container per run, which means cold-start overhead (~5–10s) and no shared state. APScheduler runs inside a long-lived Worker process — simpler, no container overhead, and easier to debug via streaming logs.

## Rate limiting

Each API call sleeps 0.3s (`sleep_seconds=0.3` in `fetch_stations_json`). A full poll of the bounding box typically requires ~10–20 API calls after quadrant splitting stabilises. Total poll time: ~10s. Well within the 5-min window.

## Supabase write strategy

- `stations`: `upsert` with `on_conflict="id"` — safe to run repeatedly; only updates if name/address changed
- `snapshots`: plain `insert` — append-only, never updated or deleted; grows ~457 rows every 5 min (~131k rows/day)

## Scaling notes (not needed now)

If the snapshot table grows too large for free-tier Supabase:
- Add a Postgres partition by month (`PARTITION BY RANGE (polled_at)`)
- Or export monthly CSVs and truncate old rows
