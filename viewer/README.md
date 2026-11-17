# Viewer

Plain HTML/CSS/JS map page (Leaflet 1.9.4, vendored in `vendor/leaflet`). No build step, no requests to other servers.

| File | Role |
|---|---|
| `index.html` | **Static** page (GitHub Pages / any static host). Reads `data/` directly (`window.NWS.dataDir = 'data/'`). |
| `index.php` + `api.php` + `config.php` | **PHP** page; `api.php` serves `manifest`, `cwa`, `bin` and `event` (`action=...`) from `data/`. |
| `assets/app.js`, `assets/style.css` | The application. Shared by both pages. |
| `data/` | `manifest.json`, `cwa.geojson`, `<season>/<OFFICE>_mean_error.bin`, `<season>/events/<OFFICE>_<id>_error.bin`. Written by `pipeline/update_season.py`. |
| `basemap/` | Own basemap: terrain WebP tiles (zoom 5-9), `water.geojson`, `lines.geojson`, `labels.json`, `meta.json`. Rebuilt with `scripts/build_basemap.py`. |

`index.html` and `index.php` are twins: change one, change the other.

Run locally: `php -S localhost:8000 -t viewer`, or copy `index.html`, `assets`, `vendor`, `data`, `basemap` into one folder and serve it with any static server.

URL state: `#season=2026-27&office=BOX&event=BOX-2026121206&base=terrain&op=0.85`. Esc clears the selection.

`.bin` format: float32 little-endian, row-major, row 0 = north edge, no header; `NaN` = no data, `-999` = radar-excluded cell. Geometry is in `data/manifest.json`.
