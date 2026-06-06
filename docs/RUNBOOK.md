# Runbook

Step-by-step instructions for deploying, operating, and downloading data.

---

## 1. Set up Supabase

1. Go to [supabase.com](https://supabase.com) → create a new project
2. Open **SQL Editor** → paste the contents of `supabase/schema.sql` → Run
3. Confirm tables `stations` and `snapshots` appear in **Table Editor**
4. Copy your credentials from **Project Settings → API**:
   - **Project URL** → `SUPABASE_URL`
   - **service_role key** (not anon key) → `SUPABASE_SERVICE_KEY`

---

## 2. Deploy to Railway

1. Go to [railway.app](https://railway.app) → New Project → Deploy from GitHub repo
2. Select this repo, set root directory to `monitor/`
3. Set environment variables:
   ```
   SUPABASE_URL=https://xxxx.supabase.co
   SUPABASE_SERVICE_KEY=eyJ...
   ```
4. Railway will detect `Procfile` and run `python main.py` as a Worker
5. Check **Logs** — you should see a poll summary line appear within 30 seconds, then every 5 minutes

---

## 3. Verify data is flowing

In Supabase **Table Editor**:
- `stations` should have rows within 30s of first Railway deploy
- `snapshots` should gain ~457 new rows every 5 minutes

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

## 5. Stop / pause monitoring

In Railway dashboard → your Worker service → **Pause** (no data loss, just stops polling).

---

## 6. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| No rows in `stations` after deploy | Wrong env vars | Check `SUPABASE_URL` / `SUPABASE_SERVICE_KEY` in Railway settings |
| Rows stop appearing | Railway sleep / crash | Check Railway logs; redeploy if needed |
| `count > returned` in logs | Normal — quadrant split | Expected behaviour, not an error |
| `RecursionError` in logs | API returned inconsistent counts | Increase `max_depth` in `collect_stations_recursive` (default 20) |
| Price is NULL for some stations | That station has no price data | Normal — ~26% of stations lack prices |

---

## 7. Run locally (development)

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
[2026-06-06 12:00:01 UTC] polled=457 upserted=3 inserted=457 errors=0 duration=9.2s
```
