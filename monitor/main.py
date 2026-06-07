"""EV charging-station poller for Nonthaburi & Pathum Thani.

Polls the pugev.com API every 5 minutes, reduces each station to a
price/occupancy snapshot, and writes the result to Supabase:

  - stations  : static info, upserted (only new/changed rows)
  - snapshots : append-only time series, one row per station per cycle

See docs/ARCHITECTURE.md for the data flow. The recursive fetch logic is
copied (not imported) from pugev.py per CLAUDE.md; the only change is that
the commented-out `return stations_list` is restored here.
"""
import os
import sys
import time
from datetime import datetime, timezone

import requests
from apscheduler.schedulers.blocking import BlockingScheduler
from supabase import create_client

# --- Configuration ---------------------------------------------------------

# Combined bounding box covering Nonthaburi + Pathum Thani (see CLAUDE.md).
BBOX = {"xmin": 100.30, "xmax": 100.85, "ymin": 13.75, "ymax": 14.25}

# Province codes we keep: 12 = Nonthaburi, 13 = Pathum Thani.
TARGET_PROVINCES = ("12", "13")

# Polling cadence. Do NOT lower without testing pugev.com rate limits (CLAUDE.md).
POLL_INTERVAL_MINUTES = 5

# Per-request settings, matching pugev.py defaults.
REQUEST_TIMEOUT = 30
SLEEP_SECONDS = 0.3

# Verbose per-quadrant fetch logging is noisy; enable with DEBUG=1.
VERBOSE = os.environ.get("DEBUG", "").lower() in ("1", "true", "yes")


# --- Logging ---------------------------------------------------------------

def log(message):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts} UTC] {message}", flush=True)


# --- pugev.com fetch (copied from pugev.py) --------------------------------

def get_url(xmin, xmax, ymin, ymax):
    return (
        f"https://pugev.com/api/v1/stations?xmin={xmin}&xmax={xmax}&ymin="
        f"{ymin}&ymax={ymax}&center=gs%7DrAm%7EpeR&zoom=5"
    )


def fetch_stations_json(xmin, xmax, ymin, ymax):
    response = requests.get(get_url(xmin, xmax, ymin, ymax), timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    time.sleep(SLEEP_SECONDS)
    return response.json()


def collect_stations_recursive(
    xmin, xmax, ymin, ymax,
    stations_list=None, seen_ids=None, depth=0, max_depth=20,
):
    """Fetch every station in the box, splitting into quadrants when the API
    truncates the result (count > returned). De-dupes via seen_ids."""
    if stations_list is None:
        stations_list = []
    if seen_ids is None:
        seen_ids = set()
    if depth > max_depth:
        raise RecursionError(
            f"Max recursion depth reached at box ({xmin}, {xmax}, {ymin}, {ymax})"
        )

    data = fetch_stations_json(xmin, xmax, ymin, ymax)
    count = data["data"]["count"]
    stations = data["data"]["stations"]

    if VERBOSE:
        log(
            f"depth={depth} x=({xmin:.4f},{xmax:.4f}) y=({ymin:.4f},{ymax:.4f}) "
            f"count={count} returned={len(stations)} collected={len(stations_list)}"
        )

    # Accept this box only if the API returned all of its stations.
    if len(stations) == count:
        for station in stations:
            station_id = station["id"]
            if station_id not in seen_ids:
                seen_ids.add(station_id)
                stations_list.append(station)
        return stations_list

    # Otherwise split into four quadrants and recurse.
    xmid = (xmin + xmax) / 2
    ymid = (ymin + ymax) / 2
    quadrants = (
        (xmin, xmid, ymin, ymid),  # bottom-left
        (xmid, xmax, ymin, ymid),  # bottom-right
        (xmin, xmid, ymid, ymax),  # top-left
        (xmid, xmax, ymid, ymax),  # top-right
    )
    for qxmin, qxmax, qymin, qymax in quadrants:
        collect_stations_recursive(
            qxmin, qxmax, qymin, qymax,
            stations_list, seen_ids, depth + 1, max_depth,
        )
    return stations_list


# --- Snapshot reduction ----------------------------------------------------

def reduce_station(station):
    """Flatten evses[].connectors[] into the snapshot metrics."""
    prices = []
    n_connectors = 0
    n_occupied = 0
    for evse in station.get("evses") or []:
        for connector in evse.get("connectors") or []:
            n_connectors += 1
            if connector.get("ocpp_status") == "occupied":
                n_occupied += 1
            price = connector.get("price")
            if price is not None:
                try:
                    prices.append(float(price))
                except (TypeError, ValueError):
                    pass  # skip non-numeric prices
    return {
        "min_price": min(prices) if prices else None,
        "max_price": max(prices) if prices else None,
        "n_connectors": n_connectors,
        "n_occupied": n_occupied,
    }


# --- Supabase --------------------------------------------------------------

# id -> (name, address). Drives the "upserted" count: a station is re-upserted
# only when its name or address differs from what we last wrote.
_station_cache = {}


def prime_station_cache(client):
    """Load known stations so we don't re-upsert all ~457 after a restart."""
    try:
        result = client.table("stations").select("id,name,address").execute()
        for row in result.data or []:
            _station_cache[row["id"]] = (row.get("name"), row.get("address"))
        log(f"primed station cache with {len(_station_cache)} known stations")
    except Exception as exc:  # noqa: BLE001 - non-fatal; first poll re-upserts all
        log(f"warning: could not prime station cache ({exc}); first poll will re-upsert all")


# --- Poll cycle ------------------------------------------------------------

def poll(client):
    started = time.monotonic()
    polled_at = datetime.now(timezone.utc).isoformat()
    errors = 0

    try:
        raw_stations = collect_stations_recursive(**BBOX)
    except Exception as exc:  # noqa: BLE001 - skip this cycle, keep scheduler alive
        log(f"poll aborted: fetch failed ({exc})")
        return

    station_rows = []   # new/changed stations only
    snapshot_rows = []
    for station in raw_stations:
        try:
            province_code = (station.get("province") or {}).get("code")
            if province_code not in TARGET_PROVINCES:
                continue

            station_id = station["id"]
            metrics = reduce_station(station)
            snapshot_rows.append({
                "station_id": station_id,
                "ocpp_status": station.get("ocpp_status"),
                "min_price": metrics["min_price"],
                "max_price": metrics["max_price"],
                "n_connectors": metrics["n_connectors"],
                "n_occupied": metrics["n_occupied"],
                "polled_at": polled_at,
            })

            key = (station.get("name"), station.get("address"))
            if _station_cache.get(station_id) != key:
                station_rows.append({
                    "id": station_id,
                    "name": station.get("name"),
                    "address": station.get("address"),
                    "latitude": station.get("latitude"),
                    "longitude": station.get("longitude"),
                    "province_code": province_code,
                    "source": station.get("source"),
                })
        except Exception as exc:  # noqa: BLE001 - one bad station shouldn't drop the poll
            errors += 1
            if VERBOSE:
                log(f"station error (id={station.get('id')}): {exc}")

    upserted = 0
    inserted = 0
    try:
        # Upsert stations before snapshots so the snapshots FK is satisfied.
        if station_rows:
            client.table("stations").upsert(station_rows, on_conflict="id").execute()
            upserted = len(station_rows)
            for row in station_rows:
                _station_cache[row["id"]] = (row["name"], row["address"])
        if snapshot_rows:
            client.table("snapshots").insert(snapshot_rows).execute()
            inserted = len(snapshot_rows)
    except Exception as exc:  # noqa: BLE001 - log and move on to next cycle
        errors += 1
        log(f"supabase write failed: {exc}")

    duration = time.monotonic() - started
    log(
        f"polled={len(snapshot_rows)} upserted={upserted} "
        f"inserted={inserted} errors={errors} duration={duration:.1f}s"
    )


# --- Entry point -----------------------------------------------------------

def main():
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        print(
            "ERROR: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set.",
            file=sys.stderr,
        )
        sys.exit(1)

    client = create_client(url, key)
    log("ev-station-scraper poller starting")
    prime_station_cache(client)

    # One poll immediately on boot, then every POLL_INTERVAL_MINUTES.
    poll(client)

    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(
        poll, "interval",
        minutes=POLL_INTERVAL_MINUTES,
        args=[client],
        max_instances=1,   # never overlap polls
        coalesce=True,     # collapse missed runs into one
        misfire_grace_time=60,
    )
    log(f"scheduled poll every {POLL_INTERVAL_MINUTES} min; Ctrl+C to stop")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log("poller stopped")


if __name__ == "__main__":
    main()
