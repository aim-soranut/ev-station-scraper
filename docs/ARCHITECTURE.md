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
  - Gzip the raw station list
  - git commit + push to the `scrapes` branch
  - Prune scrape files older than RETENTION_DAYS
     │
     │  git push (https + GITHUB_TOKEN)
     ▼
GitHub repo, `scrapes` branch
  scrapes/<UTC-timestamp>.json.gz   ← one gzipped scrape per poll
     │
     │  download from GitHub UI → gunzip
     ▼
Your analysis tool (Excel, Python, jq, …)
```

## How the pugev.com API works

```
GET /api/v1/stations?xmin={lon_min}&xmax={lon_max}&ymin={lat_min}&ymax={lat_max}
Response: { "data": { "count": N, "stations": [...] } }
```

**Key limitation:** when a bounding box contains more stations than the API
returns, `count > len(stations)`. The scraper detects this and recursively
splits the box into 4 quadrants until every sub-box is fully returned
(`pugev.py:collect_stations_recursive`, ported into `monitor/main.py`).

## Data flow per poll cycle

1. `collect_stations_recursive(xmin=97.3, xmax=105.7, ymin=5.5, ymax=20.6)` —
   fetches **all stations in Thailand**, deduped by id (~100–300 API calls).
2. Write the raw deduped station list to `scrapes/<timestamp>.json.gz`
   (`ensure_ascii=False`, so Thai text stays readable inside the JSON).
3. `git rm` scrape files older than `RETENTION_DAYS`.
4. Commit and push to the `scrapes` branch (with rebase-and-retry on conflict).
5. Log: `scraped 5597 stations | 214 api calls | wrote scrapes/…json.gz (1400 KB) | pruned 2 | 78.3s`

The file content is the **original scraped JSON** — a list of station objects
with nested `evses[].connectors[]`, `province`, `opening_times`, etc. Nothing
is flattened or aggregated.

## Why a separate `scrapes` branch

Railway redeploys on every push to the branch it watches. Pushing scrape data to
the **same** branch would trigger a rebuild every 30 minutes. The scraper pushes
to a dedicated, code-free `scrapes` branch instead, which Railway ignores.

## Why APScheduler (not Railway Cron)

A long-lived Worker keeps one authenticated git clone warm in `SCRAPE_WORKDIR`
and avoids per-run cold starts. Railway Cron would re-clone every run.

## Rate limiting

Each API call sleeps 0.3s. A nationwide poll is ~100–300 calls (deep quadrant
splitting around Bangkok) → a few minutes, well inside the 30-min window.

## Storage growth & retention

Each scrape is ~13 MB raw, ~1–2 MB gzipped. At 48 polls/day that is
**~50–100 MB/day added to git history**. `RETENTION_DAYS` (`git rm`) keeps the
working tree small, but **git history retains every committed blob** — deleting
files does not reclaim space. GitHub will eventually flag repo size.

Mitigations:
- Raise the interval / lower `RETENTION_DAYS`.
- Periodically squash or recreate the `scrapes` branch (orphan + force-push) to
  drop old history.
- Move to object storage (e.g. Supabase Storage / S3) if long history is needed.
