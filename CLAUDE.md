# ev-station-scraper

## What this project does

Monitors EV charging station status and prices **nationwide across Thailand** by polling the pugev.com API every 30 minutes. Historical data is stored in Supabase and explored through a Streamlit dashboard (or downloaded as CSV) for pricing analysis.

Collection is country-wide; **province filtering happens in the dashboard**, which defaults to our analysis focus of **Nonthaburi** and **Pathum Thani**.

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
    schema.sql              ← run once in Supabase SQL editor: tables, RLS, dash_* RPCs

  dashboard/
    app.py                  ← Streamlit dashboard (price vs. occupancy)
    requirements.txt
    .streamlit/secrets.toml.example

  docs/
    ARCHITECTURE.md         ← system design and data flow
    DATA_DICTIONARY.md      ← every field in Supabase explained
    RUNBOOK.md              ← how to deploy, operate, and download data
```

## Key facts about the data source

- API: `https://pugev.com/api/v1/stations?xmin=...&xmax=...&ymin=...&ymax=...`
- The API **truncates results** when too many stations fit a bounding box — the poller handles this via recursive quadrant splitting (see `pugev.py:collect_stations_recursive`)
- The poller collects **all of Thailand** using the bounding box `xmin=97.3, xmax=105.7, ymin=5.5, ymax=20.6` (~5,600 stations); it does **not** filter by province
- Analysis focus (default dashboard filter): Nonthaburi (`code=12`, ~274 stations) and Pathum Thani (`code=13`, ~183 stations)
- Station `ocpp_status` values: `available`, `occupied`, `close`, `maintenance`, `specific`. Connector-level status may also be `unknown`
- Price is per kWh in THB, stored at connector level inside `evses[].connectors[].price`

## Stack

| Layer | Technology | Where |
|---|---|---|
| Poller | Python + APScheduler | Railway Worker |
| Database | Supabase (Postgres) | Supabase cloud |
| Dashboard | Streamlit + Plotly + pydeck | Streamlit Community Cloud |
| Data download | Supabase Table Editor → CSV | Manual |

## Environment variables

| Variable | Used by | Description |
|---|---|---|
| `SUPABASE_URL` | monitor/main.py, dashboard/app.py | Project URL from Supabase settings |
| `SUPABASE_SERVICE_KEY` | monitor/main.py | Service role key (write access; bypasses RLS) |
| `SUPABASE_ANON_KEY` | dashboard/app.py | Anon public key (read-only via RLS) |
| `POLL_INTERVAL_MIN` | monitor/main.py | Optional, default `30` |
| `RETENTION_DAYS` | monitor/main.py | Optional, default `30`; snapshots older than this are purged |

## Common tasks

**Run the poller locally:**
```bash
cd monitor
pip install -r requirements.txt
SUPABASE_URL=... SUPABASE_SERVICE_KEY=... python main.py
```

**Run the dashboard locally:**
```bash
cd dashboard
pip install -r requirements.txt
cp .streamlit/secrets.toml.example .streamlit/secrets.toml   # fill in URL + anon key
streamlit run app.py
```

**Download data:**
Supabase dashboard → SQL Editor → run the pre-joined query from `docs/RUNBOOK.md` → Download CSV

**Set up database:**
Supabase dashboard → SQL Editor → paste and run `supabase/schema.sql`

## What not to change

- `pugev.py` — reference implementation, do not edit; copy logic into `monitor/main.py` as needed
- The polling interval (30 min, `POLL_INTERVAL_MIN`) is a balance between data freshness and pugev.com rate limits; do not lower it without testing
- The dashboard uses the **anon** key only; never put `SUPABASE_SERVICE_KEY` in the dashboard or its secrets
