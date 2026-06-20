# Data Dictionary

## Table: `stations`

Populated on first sighting of a station. Updated if name or address changes.

| Column | Type | Description |
|---|---|---|
| `id` | int (PK) | pugev.com station ID — stable identifier |
| `name` | text | Station name (may be Thai or English) |
| `address` | text | Street address (Thai) |
| `latitude` | float8 | WGS84 latitude |
| `longitude` | float8 | WGS84 longitude |
| `province_code` | text | Thai province code (all provinces collected, e.g. `'12'` Nonthaburi, `'13'` Pathum Thani) |
| `province_name` | text | English province name, used for dashboard filtering/labels |
| `source` | text | Charging network brand: `pttv2`, `evolt`, `tesla`, `ea`, etc. |
| `first_seen_at` | timestamptz | When we first recorded this station (preserved across upserts) |

## Table: `connector_snapshots`

**One row per connector per poll cycle** (every 30 minutes), faithful to
`evses[].connectors[]` in the source API. Append-only, never updated.
Station-level metrics (occupancy, price ranges) are **derived** from these rows
by the `dash_*` functions — they are not stored.

| Column | Type | Description |
|---|---|---|
| `id` | bigint (PK) | Auto-increment row ID |
| `station_id` | int (FK → stations.id) | Which station |
| `polled_at` | timestamptz | When this poll cycle ran (UTC); shared by all connectors in a cycle |
| `evse_code` | text | `evses[].code` (the EVSE/charge-point this connector belongs to) |
| `connector_name` | text | `connectors[].name` |
| `connector_type` | text | `AC` or `DC` |
| `power_kw` | numeric | Rated power, parsed from `connectors[].power` |
| `ocpp_status` | text | Connector status at poll time (see values below) |
| `price` | numeric | Price for this connector (THB/kWh). NULL if not published |
| `status_updated_at` | timestamptz | `connectors[].status_updated_at` from the source |

> **Why connector-level?** Price, status, power and AC/DC type all vary *per
> connector* in the source data. Storing each connector row keeps the data
> faithful, makes status counts reconcile exactly (see below), and lets the
> dashboard derive AC vs DC pricing without losing detail. (Earlier versions
> stored one aggregated row per station, which is why `available + occupied`
> didn't sum to the connector count.)

### `ocpp_status` values

| Value | Meaning |
|---|---|
| `available` | Ready to charge |
| `occupied` | Actively charging a vehicle (demand signal) |
| `close` | Closed (e.g. outside opening hours) |
| `maintenance` | Out of service |
| `specific` | Restricted access (e.g. Tesla Supercharger for Tesla vehicles only) |
| `unknown` | Status not reported by the operator |

These are mutually exclusive, so for any station at a given `polled_at`:
`available + occupied + close + maintenance + specific + unknown = n_connectors`.

### Derived metrics (computed in the `dash_*` RPCs, not stored)

| Metric | How it's derived |
|---|---|
| `n_connectors` | `count(*)` of connector rows for the station at that poll |
| `n_occupied` / `n_available` / `n_close` / … | `count(*) filter (where ocpp_status = …)` |
| Occupancy rate | occupied connectors / total connectors |
| `ac_min_price` / `dc_min_price` | `min(price) filter (where connector_type = 'AC' / 'DC')` |
| Station `ocpp_status` | rollup: `occupied` if any connector occupied, else `available`, else … |

## Useful SQL queries

**Latest connector status for all stations:**
```sql
select * from dash_latest_status(null);          -- or pass array['12','13']
```

**Raw connector-level export (for Excel/analysis):**
```sql
select * from dash_export_raw(array['12','13'], now() - interval '7 days', now());
```

**Average occupancy & price by station:**
```sql
select * from dash_station_price_occupancy(null, now() - interval '7 days', now())
order by avg_occupancy desc;
```

> All `dash_*` functions accept `(p_codes text[], p_start, p_end)` and run the
> aggregation in Postgres. Pass `p_codes => null` for all of Thailand.
