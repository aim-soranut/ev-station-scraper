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
| `province_code` | text | `'12'` = Nonthaburi, `'13'` = Pathum Thani |
| `source` | text | Charging network brand: `pttv2`, `evolt`, `tesla`, `ea`, etc. |
| `first_seen_at` | timestamptz | When we first recorded this station |

## Table: `snapshots`

One row per station per poll cycle (every 5 minutes). Never updated after insert.

| Column | Type | Description |
|---|---|---|
| `id` | bigserial (PK) | Auto-increment row ID |
| `station_id` | int (FK → stations.id) | Which station |
| `ocpp_status` | text | Station-level status at poll time (see values below) |
| `min_price` | numeric | Lowest price across all connectors (THB/kWh). NULL if no price data. |
| `max_price` | numeric | Highest price across all connectors (THB/kWh). NULL if no price data. |
| `n_connectors` | int | Total number of connectors at this station |
| `n_occupied` | int | Connectors with `ocpp_status = 'occupied'` at poll time |
| `polled_at` | timestamptz | When this snapshot was taken (UTC) |

### `ocpp_status` values

| Value | Meaning |
|---|---|
| `available` | Ready to charge |
| `occupied` | Actively charging a vehicle (demand signal) |
| `close` | Station closed (outside opening hours) |
| `maintenance` | Out of service |
| `specific` | Restricted access (e.g. Tesla Supercharger for Tesla vehicles only) |

### Derived analysis columns

These are not stored but computed from the raw columns:

| Derived metric | Formula |
|---|---|
| Occupancy rate | `n_occupied / n_connectors` |
| Is busy | `ocpp_status = 'occupied'` |
| Price change | Compare `min_price` across consecutive rows for same `station_id` |

## Useful SQL queries

**Latest status for all stations:**
```sql
select distinct on (station_id)
  s.name, s.province_code, s.source,
  sn.ocpp_status, sn.min_price, sn.n_occupied, sn.n_connectors, sn.polled_at
from snapshots sn
join stations s on s.id = sn.station_id
order by station_id, polled_at desc;
```

**Full history export (for Excel):**
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

**Average occupancy rate by station:**
```sql
select
  s.name, s.source, s.province_code,
  round(avg(sn.n_occupied::numeric / nullif(sn.n_connectors,0)), 3) as avg_occupancy,
  round(avg(sn.min_price), 2) as avg_min_price,
  count(*) as samples
from snapshots sn
join stations s on s.id = sn.station_id
group by s.id, s.name, s.source, s.province_code
order by avg_occupancy desc;
```
