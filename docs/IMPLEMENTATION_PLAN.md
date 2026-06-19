# Implementation Plan — 30-min Poller + Streamlit Dashboard

> **Status: PROPOSAL — awaiting review.** Nothing below has been built yet.
> Two deliverables: (1) switch polling to **30 minutes** and build the poller
> that the docs describe but that does not yet exist, and (2) add a **Streamlit**
> dashboard to visualize price vs. occupancy.

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

### Facts verified against the real scraped data (`pugev_stations.txt`, 5,597 stations)

- Target box (`xmin=100.30, xmax=100.85, ymin=13.75, ymax=14.25`) holds the
  documented provinces: **Nonthaburi (code 12) = 274**, **Pathum Thani (code 13) = 183** → ~**457 stations/poll**.
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

## 1. The 30-minute poller — `monitor/main.py`

Port the proven logic from `pugev.py` (do **not** modify `pugev.py`; copy into
`monitor/`). Behaviour per cycle:

1. `collect_stations_recursive(xmin=100.30, xmax=100.85, ymin=13.75, ymax=14.25)`
   — recursive quadrant split to defeat API truncation, deduped by `id`.
   (Bug to fix on copy: `pugev.py:collect_stations_recursive` has its top-level
   `return stations_list` commented out — the ported version will return it.)
2. Keep only `station["province"]["code"] in ("12", "13")`.
3. For each station, flatten `evses[].connectors[]` and compute aggregates
   (see snapshot columns below).
4. **Upsert** into `stations` (`on_conflict="id"`); **insert** into `snapshots`.
5. Structured log line per cycle, e.g.
   `[2026-06-19 12:00:01Z] polled 457 stations, inserted 457 snapshots, 0 errors`.

**Scheduling:** APScheduler `BlockingScheduler`, `IntervalTrigger(minutes=30)`,
`max_instances=1`, `coalesce=True`, plus one run immediately on startup.

**Robustness:** per-cycle try/except so one bad poll never kills the worker;
keep the 0.3s inter-request sleep; reuse a `requests.Session`.

**Config (env vars):** `SUPABASE_URL`, `SUPABASE_SERVICE_KEY` (as today).
New optional: `POLL_INTERVAL_MIN` (default `30`) so the interval is tunable
without a code change.

**`monitor/requirements.txt`:** `requests`, `apscheduler`, `supabase`, `shapely`*
(*only if we keep polygon filtering — not needed for province-code filtering,
so it will be omitted unless you want it).

**`monitor/Procfile`:** `worker: python main.py` (Railway worker).

---

## 2. Supabase schema — `supabase/schema.sql`

`stations` (static, upserted) — as documented:

| column | type | notes |
|---|---|---|
| `id` | int PK | pugev station id |
| `name` | text | |
| `address` | text | |
| `latitude` / `longitude` | float8 | |
| `province_code` | text | `'12'` / `'13'` |
| `source` | text | network brand (`evolt`, `pttv2`, `tesla`, …) |
| `first_seen_at` | timestamptz default now() | |

`snapshots` (append-only, one row per station per 30-min cycle). **Proposed
columns — the AC/DC split is my recommended enhancement over the original
single min/max design; flag if you'd rather keep it minimal:**

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

Indexes: `snapshots(station_id, polled_at desc)` and `snapshots(polled_at)`
for fast dashboard queries. Append-only growth ≈ **457 × 48 ≈ 22k rows/day**
(far lighter than the old 5-min estimate of ~131k/day).

---

## 3. Streamlit dashboard — new `dashboard/` folder

`dashboard/app.py`, `dashboard/requirements.txt` (`streamlit`, `supabase` or
`psycopg2-binary`, `pandas`, `plotly`, `pydeck`).

**Data access:** read-only queries to Supabase. Use `st.cache_data(ttl=300)` so
the UI doesn't re-query Postgres on every interaction. Uses its own env vars:
`SUPABASE_URL` + a **read** key (anon key with RLS, or service key if private).

**Global filters (sidebar):** date range, province (Nonthaburi/Pathum Thani),
`source`/brand multiselect, connector type (AC/DC/both).

**Views — built to answer "how does competitor price relate to demand?":**
1. **KPI header** — # stations tracked, current overall occupancy rate,
   median AC price, median DC price, last poll time.
2. **Live map** (`pydeck`) — stations plotted by lat/long, colored by current
   occupancy rate, sized by connector count.
3. **Occupancy over time** — line chart of fleet occupancy rate
   (`n_occupied / n_connectors`), split by province and/or brand.
4. **Price over time** — AC and DC price trends; spot competitor price changes.
5. **Price vs. occupancy scatter** ← the core question — each station's avg
   price vs. avg occupancy, to reveal whether cheaper stations run busier.
6. **Per-station detail** — pick a station → its price & occupancy history.

**Deploy:** Streamlit Community Cloud (free, points at this repo) — recommended;
or a second Railway service. Locally: `cd dashboard && streamlit run app.py`.

---

## 4. Documentation updates

- **`CLAUDE.md`**: polling interval 5 → 30 min (and the "what not to change"
  rationale); add `dashboard/` to repo layout; add dashboard env vars; add the
  "run the dashboard" common task.
- **`docs/ARCHITECTURE.md`**: 5 → 30 min; recompute rows/day (~22k); add the
  dashboard box to the diagram and a Streamlit section.
- **`docs/DATA_DICTIONARY.md`**: add connector `unknown` status; document the
  new AC/DC price + `n_available` columns.
- **`docs/RUNBOOK.md`**: dashboard deploy/run steps; note interval is now
  `POLL_INTERVAL_MIN` (default 30).

---

## 5. Suggested build order (after you approve)

1. `supabase/schema.sql` 2. `monitor/` (poller + requirements + Procfile)
3. Local dry-run of one poll cycle against the live API (verify counts/inserts)
4. `dashboard/` 5. Doc updates 6. Commit + push to `claude/practical-euler-7yjz8f`.

## 6. Open questions for your review

1. **AC/DC price split** — adopt it (recommended), or keep the original single
   `min_price`/`max_price` only?
2. **Dashboard read key** — use the Supabase **anon** key + a read-only RLS
   policy (safer to share), or reuse the **service** key (private dashboards only)?
3. **Polygon filtering** — drop it (province-code filter is enough), or keep
   `shapely` so we can also restrict to specific districts later?
4. **Dashboard hosting** — Streamlit Community Cloud (free, recommended) or a
   second Railway service?
