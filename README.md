# NWS WFO Current-Season Snowfall Forecast Error

A self-updating tracker of how the official **NWS snowfall forecasts** did, **event by event, this winter**, for four New England forecast offices:

| Office | Name |
|---|---|
| **CAR** | Caribou, ME |
| **GYX** | Gray/Portland, ME |
| **BTV** | Burlington, VT |
| **BOX** | Boston/Norton, MA |

A scheduled GitHub Actions job finds each new snow event, verifies the WFO-edited forecast against the observed snowfall analysis, and republishes an interactive map. The map shows the **season-to-date mean error** for each county warning area (CWA), and, when you pick an event, **that event's error map**, all at the native ~4 km grid of the observations.

> Forecast − observed, in inches of snow. **Red** = the forecast was too high, **blue** = too low.

The method is the one used in the multi-season study this tracker is based on ([NWS-WFO-Snowfall-Error-Viewer](https://github.com/aaronperry-dev/NWS-WFO-Snowfall-Error-Viewer)); on a past winter the code reproduces that study's forecast and observed grids exactly (see [Testing](#testing)).

## What you see

* **Offices table:** per office, the number of verified events (and how many are detected in all), the season-to-date mean error, MAE and hit rate. A ⚠ marks fewer than 5 verified events.
* **Events list, newest first:** each event with its UTC window, CWA-mean observed and forecast snowfall, mean error and MAE. Finished events whose NWS NDFD forecast is not available yet show **Awaiting NWS NDFD forecast**; snow still falling (or fallen less than 12 h ago) shows **In progress**. Click a verified event to map its error; click an office to list only its events.
* **Map:** terrain basemap, CWA outlines, and the error grid. A tooltip follows the cursor with the cell's value. Radar beam-blockage sectors in the observations (BTV, GYX) are hatched and excluded from every statistic.
* **Snowfall log:** for every office, every stretch of *measurable* snow this season (at least 0.1 in on 1 % of the CWA in a 6-hour period; the NWS definition of measurable), with its CWA-mean total, highest cell and share of the CWA at 1 in or more. Episodes that qualify as events say so (a verified one opens its error map); the rest are marked *not an event*. Also in `results/snowfall_log_<season>.csv`.
* **Status line:** when the data were last updated, how recent the observed analysis is, and how far the NWS NDFD forecast data reach.
* Before the first qualifying event of a season, the page says so and shows each office's CWA-mean snowfall so far.

The view is kept in the URL (`#season=2026-27&office=BOX&event=BOX-2026121206&base=terrain&op=0.85`), so any view can be shared.

## How it works

`pipeline/update_season.py` runs the same steps every time and is safe to re-run:

1. **Observed.** NOHRSC National Snowfall Analysis (`sfav2`), 6-hourly, 0.04° (~4 km). New periods are read, and the last 21 days are re-read because recent analyses are revised.
2. **Events** are detected per office in two ways, both from the 6-hour series:
   * **CWA-mean events** (the rule of the multi-season study): a period with a CWA-mean ≥ 0.08 in counts as snowing, snowing periods less than 12 h apart are merged, and a cluster is an event if its CWA-mean total is ≥ 1.5 in.
   * **Local events** (tagged *localized* in the viewer): snow that stays in part of the CWA (a northern edge, mountains, a lake-effect belt) never reaches a CWA-mean of 1.5 in. A cluster that is not already a CWA-mean event also counts when **at least 5 % of the CWA's cells received 1 in or more** in total (a period counts as snowing for this when ≥ 2 % of the CWA got ≥ 0.3 in in it).
   Events longer than 48 h are split at their quietest 6 hours.
3. **Forecast.** The official WFO-edited NDFD *Snowfall Accumulation* grids (WMO header YSUZ98, 6-hour, 2.5 km) from the NCEI archive: the **latest run issued at least 12 h before the event began** (6 h if no run is available that early). It is regridded to the 0.04° cells by averaging 5 × 5 bilinear samples per cell. This is the forecasters' product, not NBM.
4. **Error** = forecast − observed per cell, over the cells inside the CWA. Per event: mean forecast and observed, mean error, MAE, RMSE, percent bias, hit rate (|error| ≤ max(2 in, 30 % of observed)), and the highest cell. Season to date: the mean over verified events (a cell needs valid data in ≥ 90 % of them), with a 90 % bootstrap interval from 5 events on.
5. **Event status:** `ongoing` → `pending_forecast` → `verified`. Others: `no_forecast` (the archive covers the day but no run covers the event) and `missing_analysis` (a NOHRSC period is missing).

> **The NWS NDFD forecast data trail real time by about 5 days.** NCEI publishes the NDFD files with a delay, so a finished event is first listed as *Awaiting NWS NDFD forecast* and is verified automatically on a later run. Nothing is guessed in the meantime, and a forecast is only used once its whole day is available.

## Set it up on GitHub

1. Create the repository, push this folder to `main`.
2. **Settings → Pages → Build and deployment → Source: GitHub Actions.**
3. **Settings → Actions → General → Workflow permissions: Read and write** (the job commits the updated data; the workflow file already requests `contents: write`).
4. **Actions → "Update season data and deploy viewer" → Run workflow** once. After that it runs by itself **every day at about 18:17 UTC** from October through May (change the `cron` line in `.github/workflows/update.yml`; GitHub may start scheduled runs a few minutes late), and on every push that changes `viewer/` or `pipeline/`. Each run reads the NOHRSC analyses published since the last run and verifies every finished event the NCEI forecast archive now covers. The site is at `https://<user>.github.io/<repo>/`.

Notes:
* Each run commits the changed `state/`, `viewer/data/` and `results/` files (nothing is committed when nothing changed), then deploys the page.
* GitHub pauses scheduled workflows in a repository with no activity for 60 days. If the job has stopped (for example after the summer), re-enable it in the Actions tab; the pipeline then catches up on the whole season in one run.
* The first run of a season starts on 1 October; the season is chosen from the date (July–June, e.g. `2026-27`). Earlier seasons stay in the viewer's season menu.

## Run it locally

```bash
pip install -r requirements.txt            # pygrib needs the ECCODES library (the PyPI wheels bundle it)
python pipeline/update_season.py           # updates state/, viewer/data/, results/
php -S localhost:8000 -t viewer            # PHP viewer (8.1+)   ...or serve viewer/ with any static server after copying index.html
```

`--season 2025-26`, `--offices BOX GYX`, `--workdir work` (download cache), `--export-only` (rebuild the viewer data from `state/` with no network), `--asof 2026-01-15T12 --lag-days 5` (pretend it is that moment, for testing). Environment: `NWSNOW_MONTHS`, `NWSNOW_NOHRSC_RATE`, `NWSNOW_NCEI_RATE`, `NWSNOW_USER_AGENT`. Downloads are paced politely per host, with retries and automatic slow-down on 429/503.

The viewer is plain HTML/JS with Leaflet 1.9.4 (vendored). `viewer/index.php` + `api.php` serve it from PHP hosting; `viewer/index.html` is the same page for static hosting (GitHub Pages) and reads `viewer/data/` directly. They are twins: edit both. The page makes no requests to other servers.

## Files

```
pipeline/update_season.py     the whole pipeline (observed, events, forecast, error, state, viewer data)
reference/                    CWA polygons used to cut the grids
state/<season>/               series_<OFFICE>.csv (6-h CWA means), events.json, grids/<OFFICE>_<event>.npz (observed + forecast totals)
results/events_<season>.csv   every event with its status and statistics (also handy for a spreadsheet)
results/snowfall_log_<season>.csv   every measurable snowfall episode per office, event or not
viewer/                       the web page; data/ (manifest, outlines, .bin grids), basemap/ (own terrain + boundaries), vendor/leaflet
scripts/build_basemap.py      rebuilds viewer/basemap (AWS Terrain Tiles + Natural Earth 1:50m); needs requirements-basemap.txt
.github/workflows/update.yml  scheduled update + Pages deploy
```

### `.bin` grids

Raw **float32, little-endian, row-major, no header**; row 0 is the **north** edge. Geometry (`nx`, `ny`, `west`, `north`, `dx`, `dy`, in degrees) is in `viewer/data/manifest.json`. `NaN` = outside the CWA or too few valid events; `-999` = radar beam-blockage sector, excluded from every statistic.

* `<season>/<OFFICE>_mean_error.bin`: season-to-date mean error, inches per event.
* `<season>/events/<OFFICE>_<yyyymmddhh>_error.bin`: one verified event, inches in that event (`yyyymmddhh` = start of the event, UTC).

```python
import json, numpy as np
m = json.load(open("viewer/data/manifest.json")); o = m["seasons"]["2026-27"]["offices"]["BOX"]
ev = next(e for e in o["events"] if e["status"] == "verified")
grid = np.fromfile("viewer/data/" + ev["file"], dtype="<f4").reshape(o["ny"], o["nx"])
```

## Settings

At the top of `pipeline/update_season.py`: the offices, the months of a season (`MONTHS`, October–May), the event rules (`THR_SERIES`, `THR_TOTAL`, `GAP_HOURS`, `MAX_HOURS`), the forecast lead (`LEADS`), the look-back for revised analyses (`LOOKBACK_DAYS`), the radar exclusions (`RADAR_EXCLUSIONS`), the sample-size flags and the network limits. `LOCAL_MIN_IN` and `LOCAL_AREA_PCT` (1 in, 5 %) decide which localized snowfalls count; set `LOCAL_AREA_PCT` to 100 to track only CWA-mean events as in the study. Local events are smaller on average, so they pull a season's mean error toward zero (for winter 2022–23 they roughly doubled the event count, e.g. BOX 5 → 16).

## Testing

* **Same numbers as the multi-season study.** For winter 2022–23, the CWA-mean events of all four offices are the same events (IDs, windows, totals) as in the original script, and the BTV and GYX forecast and observed grids are bit-for-bit identical (17 and 15 events, maximum difference 0). The local events are additional.
* **Incremental equals one-shot.** Running the updater as the season unfolded (mid-December, year end, May) gives the same events, statistics and grids as one full run. Events that were in progress or awaiting the archive at an earlier step were verified on later runs.
* **Viewer.** Checked in headless Chrome for an empty season, for a populated season (event list, event map, deep link, Esc, tooltip, static and PHP builds) and for requests to other servers (none).
* Not tested here: the GitHub Actions run itself (it needs your repository).

## Caveats

* NOHRSC is an analysis, not ground truth, and is itself least certain where observations are sparse. Recent values can change; they are re-read for 21 days.
* Few events say little: a season mean from a handful of events is indicative only (⚠ below 5 events).
* Events use one fixed forecast issuance (≥ 12 h before the event began), not each forecaster's last update before the snow.
* The NWS NDFD forecast data can have gaps; an event they cannot cover is marked `no_forecast`, not dropped.

## Data and credits

Forecasts: NWS NDFD via the [NCEI archive](https://www.ncei.noaa.gov/products/weather-climate-models/national-digital-forecast-database). Observed: [NOHRSC National Snowfall Analysis](https://www.nohrsc.noaa.gov/snowfall_v2/). CWA boundaries: NWS. Terrain: [Terrain Tiles on AWS](https://registry.opendata.aws/terrain-tiles/) (sources: <https://github.com/tilezen/joerd/blob/master/docs/attribution.md>). Boundaries and names: [Natural Earth](https://www.naturalearthdata.com/) 1:50m. Map library: [Leaflet](https://leafletjs.com/) (BSD-2-Clause). No license is set for this repository's own code; choose one before publishing.
