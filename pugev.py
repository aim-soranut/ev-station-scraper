import time
import json
import requests
from pathlib import Path
from shapely.geometry import shape, Point

thai_xmin = 97.3
thai_xmax = 105.7
thai_ymin = 5.5
thai_ymax = 20.6


def get_url(xmin, xmax, ymin, ymax):
    return (
        f"https://pugev.com/api/v1/stations?xmin={xmin}&xmax={xmax}&ymin="
        f"{ymin}&ymax={ymax}&center=gs%7DrAm%7EpeR&zoom=5"
    )


def fetch_stations_json(xmin, xmax, ymin, ymax, timeout=30, sleep_seconds=0.3):
    url = get_url(xmin, xmax, ymin, ymax)
    response = requests.get(url, timeout=timeout)
    response.raise_for_status()
    time.sleep(sleep_seconds)
    return response.json()


def collect_stations_recursive(
    xmin,
    xmax,
    ymin,
    ymax,
    timeout=30,
    sleep_seconds=0.3,
    stations_list=None,
    seen_ids=None,
    depth=0,
    max_depth=20,
):
    if stations_list is None:
        stations_list = []
    if seen_ids is None:
        seen_ids = set()

    if depth > max_depth:
        raise RecursionError(
            f"Max recursion depth reached at box "
            f"({xmin}, {xmax}, {ymin}, {ymax})"
        )

    data = fetch_stations_json(
        xmin=xmin,
        xmax=xmax,
        ymin=ymin,
        ymax=ymax,
        timeout=timeout,
        sleep_seconds=sleep_seconds,
    )

    count = data["data"]["count"]
    stations = data["data"]["stations"]
    returned_count = len(stations)

    print(
        f"depth={depth} | "
        f"x=({xmin:.4f}, {xmax:.4f}) | "
        f"y=({ymin:.4f}, {ymax:.4f}) | "
        f"count={count} | "
        f"returned={returned_count} | "
        f"collected={len(stations_list)}"
    )

    # Accept this box only if the API returned all stations for it
    if returned_count == count:
        added = 0
        for station in stations:
            station_id = station["id"]
            if station_id not in seen_ids:
                seen_ids.add(station_id)
                stations_list.append(station)
                added += 1

        print(
            f"  accepted leaf: added={added}, "
            f"total_collected={len(stations_list)}"
        )
        return stations_list

    xmid = (xmin + xmax) / 2
    ymid = (ymin + ymax) / 2

    quadrants = [
        (xmin, xmid, ymin, ymid),  # bottom-left
        (xmid, xmax, ymin, ymid),  # bottom-right
        (xmin, xmid, ymid, ymax),  # top-left
        (xmid, xmax, ymid, ymax),  # top-right
    ]

    for i, (qxmin, qxmax, qymin, qymax) in enumerate(quadrants, start=1):
        print(
            f"  splitting -> quadrant {i}: "
            f"x=({qxmin:.4f}, {qxmax:.4f}), "
            f"y=({qymin:.4f}, {qymax:.4f})"
        )
        collect_stations_recursive(
            xmin=qxmin,
            xmax=qxmax,
            ymin=qymin,
            ymax=qymax,
            timeout=timeout,
            sleep_seconds=sleep_seconds,
            stations_list=stations_list,
            seen_ids=seen_ids,
            depth=depth + 1,
            max_depth=max_depth,
        )

    # return stations_list

BOUNDARY_CACHE = "pathum_wan_boundary.geojson"


def get_pathum_wan_polygon(cache_path=BOUNDARY_CACHE):
    cache_file = Path(cache_path)

    # Use cached boundary if present
    if cache_file.exists():
        with cache_file.open("r", encoding="utf-8") as f:
            geojson_obj = json.load(f)

        if geojson_obj["type"] == "FeatureCollection":
            geom = geojson_obj["features"][0]["geometry"]
        elif geojson_obj["type"] == "Feature":
            geom = geojson_obj["geometry"]
        else:
            geom = geojson_obj

        return shape(geom)

    # Fetch from Nominatim and cache locally
    url = "https://nominatim.openstreetmap.org/search"
    params = {
        "q": "Pathum Wan district, Bangkok, Thailand",
        "format": "geojson",
        "polygon_geojson": 1,
        "limit": 1,
    }
    headers = {
        "User-Agent": "station-filter-script/1.0"
    }

    response = requests.get(url, params=params, headers=headers, timeout=30)
    response.raise_for_status()
    data = response.json()

    if not data.get("features"):
        raise ValueError("Could not find Pathum Wan boundary")

    # Cache full response
    with cache_file.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    geom = data["features"][0]["geometry"]
    return shape(geom)


def filter_stations_in_polygon(stations, polygon):
    minx, miny, maxx, maxy = polygon.bounds
    filtered = []

    for station in stations:
        lon = station["longitude"]
        lat = station["latitude"]

        # Cheap bounding-box prefilter first
        if not (minx <= lon <= maxx and miny <= lat <= maxy):
            continue

        # x = longitude, y = latitude
        point = Point(lon, lat)

        # covers() includes points on the boundary
        if polygon.covers(point):
            filtered.append(station)

    return filtered