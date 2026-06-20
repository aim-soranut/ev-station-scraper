"""
Nationwide EV charging station scraper for pugev.com.

Every POLL_INTERVAL_MIN minutes (default 30) it:
  1. Recursively fetches every station in Thailand (handling API truncation by
     splitting the bounding box into quadrants -- logic from pugev.py).
  2. Saves the raw station list (original scraped JSON format) as a gzipped file.
  3. Commits that file to the `scrapes` branch of the GitHub repo and pushes,
     so each scrape is downloadable from GitHub.
  4. Prunes scrape files older than RETENTION_DAYS from the branch.

No database, no dashboard -- just downloadable JSON snapshots.

Env vars:
  GITHUB_TOKEN       (required) PAT/token with write access to the repo
  SCRAPE_REPO        (optional) "owner/name", default aim-soranut/ev-station-scraper
  SCRAPE_BRANCH      (optional) branch to commit scrapes to, default "scrapes"
  SCRAPE_WORKDIR     (optional) local clone path, default /tmp/scrape-repo
  POLL_INTERVAL_MIN  (optional) default 30
  RETENTION_DAYS     (optional) default 14; older scrape files are removed
"""

import os
import sys
import gzip
import json
import time
import logging
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger

# Full-Thailand bounding box (from pugev.py).
THAI_XMIN, THAI_XMAX = 97.3, 105.7
THAI_YMIN, THAI_YMAX = 5.5, 20.6

POLL_INTERVAL_MIN = int(os.environ.get("POLL_INTERVAL_MIN", "30"))
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "14"))
REPO_SLUG = os.environ.get("SCRAPE_REPO", "aim-soranut/ev-station-scraper").strip()
BRANCH = os.environ.get("SCRAPE_BRANCH", "scrapes").strip()
WORKDIR = Path(os.environ.get("SCRAPE_WORKDIR", "/tmp/scrape-repo"))
SCRAPE_SUBDIR = "scrapes"
FILE_FMT = "%Y%m%dT%H%M%SZ"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)sZ %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("scraper")

_TOKEN = (os.environ.get("GITHUB_TOKEN") or "").strip()


# ── pugev.com API (from pugev.py) ──────────────────────────────────────────

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
    deduped list of raw station dicts."""
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

    if len(stations) == count:
        for station in stations:
            sid = station["id"]
            if sid not in seen_ids:
                seen_ids.add(sid)
                stations_list.append(station)
        return stations_list

    xmid = (xmin + xmax) / 2
    ymid = (ymin + ymax) / 2
    for qxmin, qxmax, qymin, qymax in [
        (xmin, xmid, ymin, ymid),
        (xmid, xmax, ymin, ymid),
        (xmin, xmid, ymid, ymax),
        (xmid, xmax, ymid, ymax),
    ]:
        collect_stations_recursive(
            session, qxmin, qxmax, qymin, qymax,
            timeout, sleep_seconds,
            stations_list, seen_ids, stats,
            depth + 1, max_depth,
        )
    return stations_list


# ── git helpers ────────────────────────────────────────────────────────────

def _remote_url():
    return f"https://x-access-token:{_TOKEN}@github.com/{REPO_SLUG}.git"


def _scrub(text):
    return (text or "").replace(_TOKEN, "***") if _TOKEN else (text or "")


def git(*args, cwd=WORKDIR, check=True):
    """Run a git command, scrubbing the token from any output we log."""
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd) if cwd else None,
        capture_output=True, text=True,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed: {_scrub(proc.stderr.strip())}"
        )
    return proc


def ensure_repo():
    """Make sure WORKDIR is a clone of the repo on the scrapes branch."""
    if (WORKDIR / ".git").exists():
        # Re-sync to remote (this worker is the only writer).
        git("fetch", "origin", BRANCH, check=False)
        git("checkout", BRANCH, check=False)
        git("reset", "--hard", f"origin/{BRANCH}", check=False)
        return

    WORKDIR.parent.mkdir(parents=True, exist_ok=True)
    url = _remote_url()

    # Try to clone the existing scrapes branch.
    cloned = git("clone", "--single-branch", "--branch", BRANCH, url, str(WORKDIR),
                 cwd=None, check=False)
    if cloned.returncode == 0:
        _config_identity()
        return

    # Branch doesn't exist yet: clone default branch, then make an orphan branch.
    git("clone", "--depth", "1", url, str(WORKDIR), cwd=None)
    _config_identity()
    git("checkout", "--orphan", BRANCH)
    git("rm", "-rf", ".", check=False)
    (WORKDIR / SCRAPE_SUBDIR).mkdir(parents=True, exist_ok=True)
    (WORKDIR / "README.md").write_text(
        "# pugev scrapes\n\nGzipped raw JSON snapshots of pugev.com stations, "
        "one per poll. Download a file and `gunzip` it to get the original "
        "scraped JSON.\n"
    )
    git("add", "-A")
    git("commit", "-m", "init scrapes branch")
    git("push", "-u", "origin", BRANCH)


def _config_identity():
    git("config", "user.email", "scraper@ev-station-scraper.local")
    git("config", "user.name", "ev-station-scraper")


def prune_old():
    """git rm scrape files older than RETENTION_DAYS (history still retains them)."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    folder = WORKDIR / SCRAPE_SUBDIR
    removed = 0
    for f in folder.glob("*.json.gz"):
        try:
            ts = datetime.strptime(f.stem.replace(".json", ""), FILE_FMT).replace(
                tzinfo=timezone.utc)
        except ValueError:
            continue
        if ts < cutoff:
            git("rm", "-q", f"{SCRAPE_SUBDIR}/{f.name}", check=False)
            removed += 1
    return removed


def push_with_retry(attempts=4):
    for i in range(attempts):
        if git("push", "origin", BRANCH, check=False).returncode == 0:
            return
        # Someone/something moved the branch -- rebase and retry.
        git("fetch", "origin", BRANCH, check=False)
        git("rebase", f"origin/{BRANCH}", check=False)
        time.sleep(2 ** i)
    raise RuntimeError("git push failed after retries")


# ── Poll cycle ───────────────────────────────────────────────────────────

def poll_once():
    started = time.monotonic()
    stamp = datetime.now(timezone.utc).strftime(FILE_FMT)

    session = requests.Session()
    stats = {"api_calls": 0}
    stations = collect_stations_recursive(
        session, THAI_XMIN, THAI_XMAX, THAI_YMIN, THAI_YMAX, stats=stats
    )

    ensure_repo()
    rel = f"{SCRAPE_SUBDIR}/{stamp}.json.gz"
    path = WORKDIR / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump(stations, fh, ensure_ascii=False)
    size_kb = path.stat().st_size / 1024

    pruned = prune_old()
    git("add", "-A")
    git("commit", "-m", f"scrape {stamp}: {len(stations)} stations")
    push_with_retry()

    elapsed = time.monotonic() - started
    log.info(
        "scraped %d stations | %d api calls | wrote %s (%.0f KB) | pruned %d | %.1fs",
        len(stations), stats["api_calls"], rel, size_kb, pruned, elapsed,
    )


def run_cycle():
    """Wrapper so one bad cycle never kills the long-running worker."""
    try:
        poll_once()
    except Exception:
        log.exception("scrape cycle failed")


def main():
    if not _TOKEN:
        log.error("GITHUB_TOKEN is required (write access to %s)", REPO_SLUG)
        sys.exit(1)

    log.info(
        "starting scraper: repo=%s branch=%s interval=%dmin retention=%dd",
        REPO_SLUG, BRANCH, POLL_INTERVAL_MIN, RETENTION_DAYS,
    )

    run_cycle()  # run immediately, then on the interval

    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(
        run_cycle, IntervalTrigger(minutes=POLL_INTERVAL_MIN),
        max_instances=1, coalesce=True,
    )
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("shutting down")


if __name__ == "__main__":
    main()
