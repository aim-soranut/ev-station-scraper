# Implementation Plan — 30-min Nationwide Poller + Streamlit Dashboard

> **Status: APPROVED & IMPLEMENTED.** This document is kept as the design record.
> Built: `monitor/main.py` (30-min nationwide poller), `supabase/schema.sql`
> (tables + RLS + `dash_*` RPCs + purge), `dashboard/app.py` (Streamlit), and
> doc updates across `CLAUDE.md`/`docs/`.
>
> **Decisions taken (review answers):**
> 1. AC/DC price split — **adopted**.
> 2. Dashboard read key — **anon key + read-only RLS** (service key stays in the poller).
> 3. Hosting — **Streamlit Community Cloud**.
> 4. Default province filter — **Nonthaburi + Pathum Thani**.
> 5. Retention — **auto-purge at 30 days** (`RETENTION_DAYS`, configurable); note
>    the free-tier caveat below (nationwide realistically holds ~1–2 weeks).

---

## 0. Important context discovered while planning

The repo today is a **prototype + design docs**, not a running system. These
files referenced by `CLAUDE.md`/`docs/` **do not exist yet** and must be created:

- `monitor/main.py`, `monitor/requirements.txt`, `monitor/Procfile`
- `supabase/schema.sql`

So "change polling rate to 30 minutes" is really **"build the poller, at a
30-minute interval."** Going from 5 → 30 min is safe w.r.t. the rate-limit note
in `CLAUDE.md` (it reduces load), but it lowers demand-signal granularity:
occupancy is sampled 48×/day instead of 288×/day. You confirmed the idea is
correct, so the plan uses 30 min.

**Scope change (this revision):** collect **all of Thailand** instead of the
Nonthaburi/Pathum Thani bounding box. The poller no longer filters by province
— it stores every station and tags each with its `province_code`/`province_name`.
The dashboard does the filtering (default view focused on Nonthaburi + Pathum
Thani, but any province selectable).

### Facts verified against the real scraped data (`pugev_stations.txt`)

- A full-country scrape returned **5,597 stations** (the bounding-box approach
  would have been ~457). Nationwide is **~12× more data** — see §2 volume note.
- The full-Thailand bounding box from `pugev.py` is
  `xmin=97.3, xmax=105.7, ymin=5.5, ymax=20.6`.
- Connector JSON shape: `evses[].connectors[]` each have
  `{name, ocpp_status, power, price, type, status_updated_at}`.
- **Prices are per-connector and split by type**: `type` is `AC` or `DC`,
  and DC is generally priced higher. 83 stations expose more than one distinct
  price — collapsing to a single `min/max` loses the AC-vs-DC story that
  matters for pricing analysis. **Recommendation:** store AC and DC prices
  separately (see schema below).
- **0 connectors had a null price** in the sample, but the poller will still
  handle nulls defensively.
- Connector `ocpp_status` includes an **`unknown`** value not listed in the
  current data dictionary (plus `available`, `occupied`, `close`,
  `maintenance`, `specific`). The dictionary will be updated.

---

## 1. The 30-minute nationwide poller — `monitor/main.py`

Port the proven logic from `pugev.py` (do **not** modify `pugev.py`; copy into
`monitor/`). Behaviour per cycle:

1. `collect_stations_recursive(xmin=97.3, xmax=105.7, ymin=5.5, ymax=20.6)`
   — recursive quadrant split over the **whole country** to defeat API
   truncation, deduped by `id`.
   (Bug to fix on copy: `pugev.py:collect_stations_recursive` has its top-level
   `return stations_list` commented out — the ported version will return it.)
2. **No province filter** — keep every returned station.
3. For each station, flatten `evses[].connectors[]` and compute aggregates
   (see snapshot columns below); record `province_code` + `province_name`.
4. **Upsert** into `stations` (`on_conflict="id"`); **insert** into `snapshots`
   in batches (one bulk insert, not 5,597 individual calls).
5. Structured log line per cycle, e.g.
   `[2026-06-19 12:00:01Z] polled 5597 stations, inserted 5597 snapshots, N api calls, 0 errors`.

**API call cost:** recursing the whole country triggers many more quadrant
splits than the old box (dense Bangkok area splits deep). Expect on the order of
**~100–300 API calls/cycle** at 0.3s each → a few minutes of wall-clock time,
still comfortably inside the 30-min window. The dry-run in step 5.3 will measure
the real number before we commit to the interval.

**Scheduling:** APScheduler `BlockingScheduler`, `IntervalTrigger(minutes=30)`,
`max_instances=1`, `coalesce=True`, plus one run immediately on startup.

**Robustness:** per-cycle try/except so one bad poll never kills the worker;
keep the 0.3s inter-request sleep; reuse a `requests.Session`.

**Config (env vars):** `SUPABASE_URL`, `SUPABASE_SERVICE_KEY` (as today).
New optional: `POLL_INTERVAL_MIN` (default `30`) so the interval is tunable
without a code change.

**`monitor/requirements.txt`:** `requests`, `apscheduler`, `supabase`.
(`shapely` is no longer needed — no polygon/box filtering at all.)

**`monitor/Procfile`:** `worker: python main.py` (Railway worker).

---

## 2. Supabase schema — `supabase/schema.sql`

`stations` (static, upserted):

| column | type | notes |
|---|---|---|
| `id` | int PK | pugev station id |
| `name` | text | |
| `address` | text | |
| `latitude` / `longitude` | float8 | |
| `province_code` | text | now **all** provinces (`'12'`, `'13'`, …) |
| `province_name` | text | e.g. `Nonthaburi` — for dashboard filtering/labels |
| `source` | text | network brand (`evolt`, `pttv2`, `tesla`, …) |
| `first_seen_at` | timestamptz default now() | |

`snapshots` (append-only, one row per station per 30-min cycle). **The AC/DC
split is my recommended enhancement over the original single min/max design;
flag if you'd rather keep it minimal:**

| column | type | notes |
|---|---|---|
| `id` | bigserial PK | |
| `station_id` | int FK → stations.id | |
| `polled_at` | timestamptz | UTC |
| `ocpp_status` | text | station-level status |
| `n_connectors` | int | total connectors |
| `n_occupied` | int | connectors with status `occupied` |
| `n_available` | int | connectors with status `available` |
| `min_price` / `max_price` | numeric | across all connectors (back-compat) |
| `ac_min_price` / `ac_max_price` | numeric | AC connectors only, null if none |
| `dc_min_price` / `dc_max_price` | numeric | DC connectors only, null if none |

Indexes: `snapshots(station_id, polled_at desc)`, `snapshots(polled_at)`, and
`stations(province_code)` for fast filtered dashboard queries.

### ⚠️ Data-volume note (nationwide changes this materially)

Nationwide append-only growth ≈ **5,597 × 48 ≈ 269k rows/day ≈ 8M rows/month**
(vs. ~22k/day for the old box). At a rough ~100 bytes/row that approaches the
**Supabase free-tier 500 MB** limit within roughly a month. This makes
**retention/scaling a real decision now**, not "later" — see open question #5.
Mitigations on the table: monthly partitioning + dropping old partitions, a
scheduled purge of snapshots older than N days, or periodic CSV export + truncate.

---

## 3. Streamlit dashboard — new `dashboard/` folder

`dashboard/app.py`, `dashboard/requirements.txt` (`streamlit`, `supabase` or
`psycopg2-binary`, `pandas`, `plotly`, `pydeck`).

**Data access:** read-only queries to Supabase. Use `st.cache_data(ttl=300)` so
the UI doesn't re-query Postgres on every interaction. Uses its own env vars:
`SUPABASE_URL` + a **read** key. Because the dataset is now nationwide, queries
must be **server-side filtered** (by province + date range) before pulling into
pandas — never load all 8M rows into the browser.

**Global filters (sidebar):**
- **Province** multiselect across all Thai provinces — **defaults to Nonthaburi
  + Pathum Thani** (the analysis focus), with an "All Thailand" option.
- Date range, `source`/brand multiselect, connector type (AC/DC/both).

**Views — built to answer "how does competitor price relate to demand?":**
1. **KPI header** — # stations in selection, current occupancy rate,
   median AC price, median DC price, last poll time.
2. **Live map** (`pydeck`) — stations in selection plotted by lat/long, colored
   by current occupancy rate, sized by connector count (nationwide-capable).
3. **Occupancy over time** — line chart of occupancy rate
   (`n_occupied / n_connectors`), split by province and/or brand.
4. **Price over time** — AC and DC price trends; spot competitor price changes.
5. **Price vs. occupancy scatter** ← the core question — each station's avg
   price vs. avg occupancy, to reveal whether cheaper stations run busier.
6. **Per-station detail** — pick a station → its price & occupancy history.

**Deploy:** Streamlit Community Cloud (free, points at this repo) — recommended;
or a second Railway service. Locally: `cd dashboard && streamlit run app.py`.

---

## 4. Documentation updates

- **`CLAUDE.md`**: polling interval 5 → 30 min; scope changed to **nationwide
  collection with dashboard-side province filtering** (the "Target provinces"
  fact and bounding-box fact get reframed as the *analysis focus*, not a
  collection filter); add `dashboard/` to repo layout + env vars + run task.
- **`docs/ARCHITECTURE.md`**: 5 → 30 min; nationwide fetch; recompute rows/day
  (~269k); add the dashboard box + a data-retention section.
- **`docs/DATA_DICTIONARY.md`**: add connector `unknown` status; document
  `province_name`, AC/DC price columns, and `n_available`.
- **`docs/RUNBOOK.md`**: dashboard deploy/run steps; `POLL_INTERVAL_MIN`;
  retention/purge procedure.

---

## 5. Suggested build order (after you approve)

1. `supabase/schema.sql` 2. `monitor/` (poller + requirements + Procfile)
3. Local dry-run of one nationwide poll cycle against the live API — **measure
   station count + API-call count + wall-clock time** before locking the interval
4. `dashboard/` 5. Doc updates 6. Commit + push to `claude/practical-euler-7yjz8f`.

## 6. Open questions for your review

1. **AC/DC price split** — adopt it (recommended), or keep the original single
   `min_price`/`max_price` only?
2. **Dashboard read key** — use the Supabase **anon** key + a read-only RLS
   policy (safer to share), or reuse the **service** key (private dashboards only)?
3. **Dashboard hosting** — Streamlit Community Cloud (free, recommended) or a
   second Railway service?
4. **Default dashboard province filter** — keep the default scoped to
   Nonthaburi + Pathum Thani (recommended), or default to All Thailand?
5. **Retention (now important at nationwide scale)** — keep all history (accept
   eventual free-tier overflow / upgrade Supabase), or auto-purge snapshots
   older than N days (e.g. 90)? If purge, what N?
