# ev-station-scraper

## What this project does

Monitors EV charging station status and prices in **Nonthaburi** and **Pathum Thani**, Thailand, by polling the pugev.com API every 5 minutes. Historical data is stored in Supabase and downloaded as CSV for pricing analysis.

The goal is to understand the relationship between competitor prices and demand (occupancy) to inform pricing decisions for our own station.

## Repo layout

```
ev-station-scraper/
  pugev.py                  ← original one-shot scraper (reference, do not modify)
  pugev_stations.txt        ← one-time full-Thailand scrape (reference data)
  pathum_wan_stations.txt   ← one-time Pathum Wan scrape (reference data)
  pathum_wan_boundary.geojson

  monitor/
    main.py                 ← the poller (entry point)
    requirements.txt
    Procfile

  supabase/
    schema.sql              ← run once in Supabase SQL editor to set up tables

  docs/
    ARCHITECTURE.md         ← system design and data flow
    DATA_DICTIONARY.md      ← every field in Supabase explained
    RUNBOOK.md              ← how to deploy, operate, and download data
```

## Key facts about the data source

- API: `https://pugev.com/api/v1/stations?xmin=...&xmax=...&ymin=...&ymax=...`
- The API **truncates results** when too many stations fit a bounding box — the poller handles this via recursive quadrant splitting (see `pugev.py:collect_stations_recursive`)
- Target provinces: Nonthaburi (`code=12`, ~274 stations) and Pathum Thani (`code=13`, ~183 stations)
- Bounding box used: `xmin=100.30, xmax=100.85, ymin=13.75, ymax=14.25`
- Station `ocpp_status` values: `available`, `occupied`, `close`, `maintenance`, `specific`
- Price is per kWh in THB, stored at connector level inside `evses[].connectors[].price`

## Stack

| Layer | Technology | Where |
|---|---|---|
| Poller | Python + APScheduler | Railway Worker |
| Database | Supabase (Postgres) | Supabase cloud |
| Data download | Supabase Table Editor → CSV | Manual |

## Environment variables

| Variable | Used by | Description |
|---|---|---|
| `SUPABASE_URL` | monitor/main.py | Project URL from Supabase settings |
| `SUPABASE_SERVICE_KEY` | monitor/main.py | Service role key (has write access) |

## Common tasks

**Run the poller locally:**
```bash
cd monitor
pip install -r requirements.txt
SUPABASE_URL=... SUPABASE_SERVICE_KEY=... python main.py
```

**Download data:**
Supabase dashboard → SQL Editor → run the pre-joined query from `docs/RUNBOOK.md` → Download CSV

**Set up database:**
Supabase dashboard → SQL Editor → paste and run `supabase/schema.sql`

## What not to change

- `pugev.py` — reference implementation, do not edit; copy logic into `monitor/main.py` as needed
- The polling interval (5 min) is a balance between data freshness and pugev.com rate limits; do not lower it without testing
