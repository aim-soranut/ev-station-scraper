# ev-station-scraper

## What this project does

Scrapes EV charging station data **nationwide across Thailand** from the
pugev.com API every 30 minutes and saves each scrape as a gzipped JSON file
(the original scraped format) committed to the **`scrapes` branch** of this
repo, so every snapshot is downloadable from GitHub.

No database, no dashboard — just downloadable JSON snapshots over time.

## Repo layout

```
ev-station-scraper/
  pugev.py                  ← original one-shot scraper (reference, do not modify)
  pugev_stations.txt        ← one-time full-Thailand scrape (reference data)
  pathum_wan_boundary.geojson

  monitor/
    main.py                 ← the scraper/poller (entry point)
    requirements.txt
    Procfile

  docs/
    ARCHITECTURE.md         ← system design and data flow
    DATA_DICTIONARY.md      ← the scraped JSON structure explained
    RUNBOOK.md              ← how to deploy, operate, and download scrapes
```

Scrape files land on the `scrapes` branch under `scrapes/<UTC-timestamp>.json.gz`
(one per poll). The `scrapes` branch is data-only and separate from code branches.

## Key facts about the data source

- API: `https://pugev.com/api/v1/stations?xmin=...&xmax=...&ymin=...&ymax=...`
- The API **truncates results** when too many stations fit a bounding box — the scraper handles this via recursive quadrant splitting (see `pugev.py:collect_stations_recursive`)
- The scraper collects **all of Thailand** using the bounding box `xmin=97.3, xmax=105.7, ymin=5.5, ymax=20.6` (~5,600 stations); it does **not** filter by province
- Each scrape is the raw deduped list of station objects, exactly as returned by the API (nested `evses[].connectors[]`, `province`, `opening_times`, etc.)
- Price, status, power and AC/DC type are all **connector-level** (`evses[].connectors[]`)

## Stack

| Layer | Technology | Where |
|---|---|---|
| Scraper | Python + APScheduler | Railway Worker |
| Storage | Gzipped JSON committed to the `scrapes` branch | GitHub |

## Environment variables

| Variable | Description |
|---|---|
| `GITHUB_TOKEN` | **Required.** Token with write access to the repo (to push scrapes) |
| `SCRAPE_REPO` | Optional, `owner/name`, default `aim-soranut/ev-station-scraper` |
| `SCRAPE_BRANCH` | Optional, default `scrapes` |
| `SCRAPE_WORKDIR` | Optional local clone path, default `/tmp/scrape-repo` |
| `POLL_INTERVAL_MIN` | Optional, default `30` |
| `RETENTION_DAYS` | Optional, default `14`; older scrape files are `git rm`ed from the branch |

## Common tasks

**Run the scraper locally:**
```bash
cd monitor
pip install -r requirements.txt
GITHUB_TOKEN=ghp_... python main.py
```

**Download a scrape:**
GitHub → switch to the `scrapes` branch → `scrapes/` → pick a file → Download →
`gunzip <file>.json.gz` to get the original JSON.

## What not to change

- `pugev.py` — reference implementation, do not edit; copy logic into `monitor/main.py` as needed
- The polling interval (30 min, `POLL_INTERVAL_MIN`) balances freshness against pugev.com rate limits; do not lower it without testing
- The scraper pushes to the **`scrapes` branch only** — never the code branch (that would trigger a redeploy loop on Railway)

## Caveat: git history growth

Committing a scrape every 30 minutes grows git history permanently (gzipped
~1–2 MB each; deleting old files does not reclaim history). Expect GitHub size
pressure over time; mitigations are in `docs/RUNBOOK.md`.
