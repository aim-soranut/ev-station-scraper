# Data Dictionary

Each scrape file (`scrapes/<UTC-timestamp>.json.gz`) is a gzipped JSON **array
of station objects**, exactly as returned by the pugev.com API. Decompress with
`gunzip` (or read directly with `gzip` in code). No transformation is applied.

## Station object

| Field | Type | Notes |
|---|---|---|
| `id` | int | pugev station id (stable) |
| `name` | text | Station name (Thai or English) |
| `address` | text | Street address (usually Thai) |
| `latitude` / `longitude` | float | WGS84 |
| `source` | text | Charging network brand (`evolt`, `pttv2`, `tesla`, `ea`, …) |
| `ocpp_status` | text | Station-level rollup status |
| `types` | string[] | e.g. `["DC"]`, `["AC","DC"]` |
| `province` | object | `{ code, name_th, name_en }` (e.g. `12` = Nonthaburi, `13` = Pathum Thani) |
| `opening_times` | object[] | Per-weekday opening periods |
| `updated_at` | timestamp | Source's last-updated time |
| `icon_scale`, `distance`, `distance_per_time_kilo` | — | Map/UI hints from the API |
| `evses` | object[] | The charge points — see below |

## `evses[]` (charge point)

| Field | Type | Notes |
|---|---|---|
| `brand` | text | |
| `code` | text | EVSE id |
| `connectors` | object[] | The plugs — see below |

## `evses[].connectors[]` (connector — where price/status actually live)

| Field | Type | Notes |
|---|---|---|
| `name` | text | Connector id |
| `ocpp_status` | text | `available`, `occupied`, `close`, `maintenance`, `specific`, `unknown` |
| `power` | text | Rated power in kW (e.g. `"150"`) |
| `price` | number | THB/kWh for this connector (may be null) |
| `type` | text | `AC` or `DC` |
| `status_updated_at` | timestamp | When this connector's status last changed |

**Price, status, power and AC/DC type are per connector** — a station can have
several connectors at different prices/states. Connector statuses are mutually
exclusive, so per station they sum to the total connector count.

## Reading a scrape (Python)

```python
import gzip, json
with gzip.open("scrapes/20260619T160000Z.json.gz", "rt", encoding="utf-8") as f:
    stations = json.load(f)

for s in stations:
    for evse in s.get("evses", []):
        for c in evse.get("connectors", []):
            print(s["id"], s["province"]["name_en"], c["type"], c["ocpp_status"], c["price"])
```
