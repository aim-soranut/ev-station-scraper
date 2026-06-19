# Runbook

Step-by-step instructions for deploying, operating, and downloading data.

---

## 1. Set up Supabase

1. Go to [supabase.com](https://supabase.com) → create a new project
2. Open **SQL Editor** → paste the contents of `supabase/schema.sql` → Run
3. Confirm tables `stations` and `snapshots` appear in **Table Editor**
4. Copy your credentials from **Project Settings → API**:
   - **Project URL** → `SUPABASE_URL`
   - **service_role key** → `SUPABASE_SERVICE_KEY` (poller only)
   - **anon public key** → `SUPABASE_ANON_KEY` (dashboard only)

The schema enables row-level security with read-only `anon` policies, so the
dashboard can safely use the anon key. The poller uses the service_role key,
which bypasses RLS for writes.

---

## 2. Deploy to Railway

1. Go to [railway.app](https://railway.app) → New Project → Deploy from GitHub repo
2. Select this repo, set root directory to `monitor/`
3. Set environment variables:
   ```
   SUPABASE_URL=https://xxxx.supabase.co
   SUPABASE_SERVICE_KEY=eyJ...
   POLL_INTERVAL_MIN=30   # optional, default 30
   RETENTION_DAYS=30      # optional, default 30 (see §8)
   ```
4. Railway will detect `Procfile` and run `python main.py` as a Worker
5. Check **Logs** — the first poll runs on startup (takes a few minutes nationwide), then every 30 minutes

---

## 3. Verify data is flowing

In Supabase **Table Editor**:
- `stations` should have ~5,600 rows after the first poll completes
- `snapshots` should gain ~5,600 new rows every 30 minutes

Or run in **SQL Editor**:
```sql
select count(*), max(polled_at) from snapshots;
```

---

## 4. Download data for analysis

### Option A — Quick CSV (Supabase Table Editor)

1. Supabase dashboard → **Table Editor** → `snapshots`
2. Click **Download CSV** (top-right)
3. Open in Excel; use VLOOKUP on `station_id` to join with `stations` CSV

### Option B — Pre-joined CSV (recommended)

1. Supabase dashboard → **SQL Editor**
2. Run this query:
```sql
select
  s.name, s.address, s.province_code, s.source,
  sn.ocpp_status, sn.min_price, sn.max_price,
  sn.n_connectors, sn.n_occupied,
  round(sn.n_occupied::numeric / nullif(sn.n_connectors,0), 2) as occupancy_rate,
  sn.polled_at
from snapshots sn
join stations s on s.id = sn.station_id
order by sn.polled_at desc;
```
3. Click **Download CSV** below the results

---

## 5. Deploy the dashboard (Streamlit Community Cloud)

1. Go to [share.streamlit.io](https://share.streamlit.io) → **New app**
2. Point it at this repo, branch, and main file path `dashboard/app.py`
3. In **Advanced settings → Secrets**, paste (see `dashboard/.streamlit/secrets.toml.example`):
   ```toml
   SUPABASE_URL = "https://xxxx.supabase.co"
   SUPABASE_ANON_KEY = "eyJ...anon..."
   ```
4. Deploy. The sidebar defaults to **Nonthaburi + Pathum Thani**; switch to any
   province or "All Thailand" there.

Run it locally instead:
```bash
cd dashboard
pip install -r requirements.txt
cp .streamlit/secrets.toml.example .streamlit/secrets.toml   # fill in values
streamlit run app.py
```

> Use the **anon** key only. It is read-only via RLS and safe to expose in a
> shared app. Never put `SUPABASE_SERVICE_KEY` in the dashboard or its secrets.

---

## 6. Stop / pause monitoring

In Railway dashboard → your Worker service → **Pause** (no data loss, just stops polling).

---

## 7. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| No rows in `stations` after deploy | Wrong env vars | Check `SUPABASE_URL` / `SUPABASE_SERVICE_KEY` in Railway settings |
| Rows stop appearing | Railway sleep / crash | Check Railway logs; redeploy if needed |
| `RecursionError` in logs | API returned inconsistent counts | Increase `max_depth` in `collect_stations_recursive` (default 20) |
| Price is NULL for some stations | That station has no price data | Normal — some stations lack prices |
| Dashboard shows "No data yet" | Poller not writing, or wrong keys | Confirm snapshots exist; check dashboard `SUPABASE_ANON_KEY` |
| Dashboard empty but poller works | RLS not applied | Re-run `supabase/schema.sql` (creates the `anon` read policies) |
| Database near 500 MB | Free-tier full (nationwide is ~8M rows/mo) | Lower `RETENTION_DAYS`, or upgrade Supabase — see §8 |

---

## 8. Data retention

The poller calls `purge_old_snapshots(RETENTION_DAYS)` (default 30) every cycle,
deleting `snapshots` older than the window. Adjust by setting `RETENTION_DAYS`
in Railway — no redeploy of code needed.

Nationwide @ 30-min ≈ **8M rows/month**, which exceeds the Supabase **free-tier
500 MB** within roughly 1–2 weeks. When it fills up:
- Lower `RETENTION_DAYS` (e.g. `7`), or
- Upgrade to Supabase Pro (8 GB), or
- Export monthly CSVs (§4) and rely on purge to keep the live table small.

---

## 9. Run locally (development)

```bash
cd monitor
pip install -r requirements.txt

export SUPABASE_URL=https://xxxx.supabase.co
export SUPABASE_SERVICE_KEY=eyJ...

python main.py
# Ctrl+C to stop
```

Logs print to stdout. Each poll line looks like:
```
2026-06-19 12:00:01Z INFO polled 5597 stations | 214 api calls | inserted 5597 snapshots | purged 0 | 78.3s
```
