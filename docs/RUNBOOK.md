# Runbook

Deploy, operate, and download scrapes. No database or dashboard — the scraper
commits gzipped JSON snapshots to the `scrapes` branch.

---

## 1. Create a GitHub token

The scraper needs write access to push scrape files.

1. GitHub → **Settings → Developer settings → Personal access tokens**
2. Create a token (fine-grained, scoped to this repo, **Contents: Read & write**;
   or a classic token with `repo`).
3. Copy it — this is `GITHUB_TOKEN`.

---

## 2. Deploy to Railway

1. [railway.app](https://railway.app) → New Project → Deploy from GitHub repo →
   select this repo.
2. Service **Settings**:
   - **Root Directory**: `monitor`
   - **Branch** (source): your code branch (e.g. `claude/practical-euler-7yjz8f`)
     — **not** `scrapes`.
3. **Variables**:
   ```
   GITHUB_TOKEN=ghp_...           # required
   POLL_INTERVAL_MIN=30           # optional
   RETENTION_DAYS=14              # optional
   # SCRAPE_REPO / SCRAPE_BRANCH / SCRAPE_WORKDIR only if changing defaults
   ```
4. Railway detects `Procfile` and runs `python main.py` as a Worker.
5. First scrape runs on startup (a few minutes nationwide), then every 30 min.

The first run auto-creates the `scrapes` branch if it doesn't exist.

---

## 3. Verify it's working

In Railway **Logs**, look for:
```
... INFO scraped 5597 stations | 214 api calls | wrote scrapes/20260619T160000Z.json.gz (1400 KB) | pruned 0 | 78.3s
```
On GitHub, switch to the **`scrapes`** branch → `scrapes/` should show the file.

---

## 4. Download a scrape

**From GitHub UI:** switch to the `scrapes` branch → open `scrapes/` → click a
file → **Download** → `gunzip <file>.json.gz`.

**With git:**
```bash
git fetch origin scrapes
git show origin/scrapes:scrapes/20260619T160000Z.json.gz > scrape.json.gz
gunzip scrape.json.gz
```

**With the GitHub raw URL:**
```
https://raw.githubusercontent.com/aim-soranut/ev-station-scraper/scrapes/scrapes/<file>.json.gz
```

See `docs/DATA_DICTIONARY.md` for the JSON structure and a Python read example.

---

## 5. Stop / pause

Railway → service → **Pause** (no data loss; just stops scraping).

---

## 6. Retention & repo size

The scraper `git rm`s scrape files older than `RETENTION_DAYS` each cycle, so
the working tree stays small. **But git history keeps every committed blob** —
deleting files does not shrink the repo. At ~1–2 MB gzipped × 48/day, history
grows ~50–100 MB/day; GitHub will eventually flag the repo size.

When that happens, recreate the branch to drop old history:
```bash
git checkout --orphan scrapes-new origin/scrapes
git commit -m "reset scrapes history"
git push -f origin scrapes-new:scrapes
```
Or raise `POLL_INTERVAL_MIN` / lower `RETENTION_DAYS`, or move to object storage
if you need long history cheaply.

---

## 7. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Worker exits immediately | `GITHUB_TOKEN` missing | Set it in Railway Variables |
| `git push failed` in logs | Token lacks write, or wrong repo | Check token scope / `SCRAPE_REPO` |
| Railway redeploys every 30 min | Scraper pushing to the code branch | Ensure `SCRAPE_BRANCH=scrapes` (default) and code branch differs |
| `RecursionError` in logs | API returned inconsistent counts | Increase `max_depth` in `collect_stations_recursive` |
| No `scrapes` branch appears | First run hasn't finished/failed | Check logs; it's created on the first successful push |

---

## 8. Run locally (development)

```bash
cd monitor
pip install -r requirements.txt
export GITHUB_TOKEN=ghp_...
python main.py        # Ctrl+C to stop
```
Each scrape clones into `SCRAPE_WORKDIR` (default `/tmp/scrape-repo`) and pushes
to the `scrapes` branch. To test without pushing to the shared repo, point
`SCRAPE_REPO` at a fork.
