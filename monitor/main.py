"""
Nationwide EV charging station poller for pugev.com.

Every POLL_INTERVAL_MIN minutes (default 30) it:
  1. Recursively fetches all stations in Thailand (handling API truncation by
     splitting the bounding box into quadrants -- logic ported from pugev.py).
  2. Computes per-station aggregates (status counts, AC/DC price ranges).
  3. Upserts station info and inserts one snapshot row per station into Supabase.
  4. Purges snapshots older than RETENTION_DAYS.

Province filtering is intentionally NOT done here -- everything is stored and
the dashboard filters by province.

Env vars:
  SUPABASE_URL          (required)
  SUPABASE_SERVICE_KEY  (required, service_role -- bypasses RLS)
  POLL_INTERVAL_MIN     (optional, default 30)
  RETENTION_DAYS        (optional, default 30)
"""

import os
import sys
import time
import logging
from datetime import datetime, timezone

import requests
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger
from supabase import create_client

# Full-Thailand bounding box (from pugev.py).
THAI_XMIN, THAI_XMAX = 97.3, 105.7
THAI_YMIN, THAI_YMAX = 5.5, 20.6

POLL_INTERVAL_MIN = int(os.environ.get("POLL_INTERVAL_MIN", "30"))
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "30"))
SNAPSHOT_CHUNK = 500  # rows per insert call

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)sZ %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("poller")


# ── pugev.com API (ported from pugev.py) ──────────────────────────────────

def get_url(xmin, xmax, ymin, ymax):
    return (
        f"https://pugev.com/api/v1/stations?xmin={xmin}&xmax={xmax}&ymin="
        f"{ymin}&ymax={ymax}&center=gs%7DrAm%7EpeR&zoom=5"
    )


def fetch_stations_json(session, xmin, xmax, ymin, ymax, timeout=30, sleep_seconds=0.3):
    response = session.get(get_url(xmin, xmax, ymin, ymax), timeout=timeout)
    response.raise_for_status()
    time.sleep(sleep_seconds)
    return response.json()


def collect_stations_recursive(
    session, xmin, xmax, ymin, ymax,
    timeout=30, sleep_seconds=0.3,
    stations_list=None, seen_ids=None, stats=None,
    depth=0, max_depth=20,
):
    """Recursively fetch every station in the box, splitting into quadrants
    whenever the API truncates results (count > returned). Returns the full
    deduped list. (pugev.py leaves the top-level return commented out; here we
    return it.)"""
    if stations_list is None:
        stations_list = []
    if seen_ids is None:
        seen_ids = set()
    if stats is None:
        stats = {"api_calls": 0}

    if depth > max_depth:
        raise RecursionError(
            f"Max recursion depth reached at box ({xmin}, {xmax}, {ymin}, {ymax})"
        )

    data = fetch_stations_json(session, xmin, xmax, ymin, ymax, timeout, sleep_seconds)
    stats["api_calls"] += 1

    count = data["data"]["count"]
    stations = data["data"]["stations"]

    # Accept this box only if the API returned all stations for it.
    if len(stations) == count:
        for station in stations:
            sid = station["id"]
            if sid not in seen_ids:
                seen_ids.add(sid)
                stations_list.append(station)
        return stations_list

    xmid = (xmin + xmax) / 2
    ymid = (ymin + ymax) / 2
    quadrants = [
        (xmin, xmid, ymin, ymid),
        (xmid, xmax, ymin, ymid),
        (xmin, xmid, ymid, ymax),
        (xmid, xmax, ymid, ymax),
    ]
    for qxmin, qxmax, qymin, qymax in quadrants:
        collect_stations_recursive(
            session, qxmin, qxmax, qymin, qymax,
            timeout, sleep_seconds,
            stations_list, seen_ids, stats,
            depth + 1, max_depth,
        )
    return stations_list


# ── Aggregation ────────────────────────────────────────────────────────────

def _min_max(values):
    vals = [v for v in values if v is not None]
    if not vals:
        return None, None
    return min(vals), max(vals)


def build_rows(station, polled_at):
    """Return (station_row, snapshot_row) for one raw station dict."""
    connectors = [
        c
        for evse in (station.get("evses") or [])
        for c in (evse.get("connectors") or [])
    ]

    n_occupied = sum(1 for c in connectors if c.get("ocpp_status") == "occupied")
    n_available = sum(1 for c in connectors if c.get("ocpp_status") == "available")

    all_prices = [c.get("price") for c in connectors]
    ac_prices = [c.get("price") for c in connectors if c.get("type") == "AC"]
    dc_prices = [c.get("price") for c in connectors if c.get("type") == "DC"]

    min_price, max_price = _min_max(all_prices)
    ac_min, ac_max = _min_max(ac_prices)
    dc_min, dc_max = _min_max(dc_prices)

    province = station.get("province") or {}

    station_row = {
        "id": station["id"],
        "name": station.get("name"),
        "address": station.get("address"),
        "latitude": station.get("latitude"),
        "longitude": station.get("longitude"),
        "province_code": province.get("code"),
        "province_name": province.get("name_en"),
        "source": station.get("source"),
        # first_seen_at intentionally omitted: DB default sets it on insert and
        # the upsert leaves it untouched on conflict.
    }
    snapshot_row = {
        "station_id": station["id"],
        "polled_at": polled_at,
        "ocpp_status": station.get("ocpp_status"),
        "n_connectors": len(connectors),
        "n_occupied": n_occupied,
        "n_available": n_available,
        "min_price": min_price,
        "max_price": max_price,
        "ac_min_price": ac_min,
        "ac_max_price": ac_max,
        "dc_min_price": dc_min,
        "dc_max_price": dc_max,
    }
    return station_row, snapshot_row


def _chunked(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


# ── Poll cycle ───────────────────────────────────────────────────────────

def poll_once(supabase):
    started = time.monotonic()
    polled_at = datetime.now(timezone.utc).isoformat()

    session = requests.Session()
    stats = {"api_calls": 0}
    stations = collect_stations_recursive(
        session, THAI_XMIN, THAI_XMAX, THAI_YMIN, THAI_YMAX, stats=stats
    )

    station_rows, snapshot_rows = [], []
    for st in stations:
        s_row, snap_row = build_rows(st, polled_at)
        station_rows.append(s_row)
        snapshot_rows.append(snap_row)

    # Upsert stations (preserves first_seen_at on conflict).
    for chunk in _chunked(station_rows, SNAPSHOT_CHUNK):
        supabase.table("stations").upsert(chunk, on_conflict="id").execute()

    # Insert snapshots (append-only).
    inserted = 0
    for chunk in _chunked(snapshot_rows, SNAPSHOT_CHUNK):
        supabase.table("snapshots").insert(chunk).execute()
        inserted += len(chunk)

    # Retention.
    purged = "n/a"
    try:
        res = supabase.rpc(
            "purge_old_snapshots", {"retention_days": RETENTION_DAYS}
        ).execute()
        purged = res.data
    except Exception as exc:  # purge failure must not abort the cycle
        log.warning("purge_old_snapshots failed: %s", exc)

    elapsed = time.monotonic() - started
    log.info(
        "polled %d stations | %d api calls | inserted %d snapshots | "
        "purged %s | %.1fs",
        len(stations), stats["api_calls"], inserted, purged, elapsed,
    )


def run_cycle(supabase):
    """Wrapper so one bad cycle never kills the long-running worker."""
    try:
        poll_once(supabase)
    except Exception:
        log.exception("poll cycle failed")


def main():
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        log.error("SUPABASE_URL and SUPABASE_SERVICE_KEY are required")
        sys.exit(1)

    supabase = create_client(url, key)
    log.info(
        "starting poller: interval=%dmin retention=%dd",
        POLL_INTERVAL_MIN, RETENTION_DAYS,
    )

    # Run immediately, then on the interval.
    run_cycle(supabase)

    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(
        run_cycle, IntervalTrigger(minutes=POLL_INTERVAL_MIN),
        args=[supabase], max_instances=1, coalesce=True,
    )
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("shutting down")


if __name__ == "__main__":
    main()
