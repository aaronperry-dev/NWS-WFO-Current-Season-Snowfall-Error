#!/usr/bin/env python3
"""Current-season snowfall forecast verification for the NWS offices BOX, GYX, CAR and BTV  -  an incremental, resumable updater.

    pip install -r requirements.txt            # pygrib needs the ECCODES library (the PyPI wheels bundle it)
    python pipeline/update_season.py           # bring the current season (Oct 1 .. May 31 of this winter) up to date

Every run:
  1. finds the newest NOHRSC 6-h snowfall analysis (sfav2) and fetches the periods it does not have yet (plus a 21-day look-back, because
     recent analyses are revised);
  2. detects snow EVENTS for each office from the CWA-mean 6-h series (same rules as the multi-season study: >= 0.08 in per 6 h counts as
     snowing, gaps <= 12 h are merged, event total >= 1.5 in, events longer than 48 h are split at their quietest 6 h);
  3. verifies every finished event against the official WFO-edited NDFD snowfall forecast (NCEI archive, WMO YSUZ98): the latest run issued
     >= 12 h (fallback 6 h) before the event's first 6-h period, regridded to the 0.04-degree NOHRSC grid.  The NCEI archive trails real time
     by about 5 days, so a finished event waits as "pending_forecast" until its forecast is archived - nothing is guessed;
  4. writes the state (state/<season>/...), the viewer data (viewer/data/...) and a CSV of all events (results/).

Event status:  ongoing (still snowing, or less than 12 h since it last did) | pending_forecast (finished; forecast not archived yet) |
               verified | no_forecast (archive covers the day but no run covers the event) | missing_analysis (a NOHRSC period is missing)

State kept in the repository (so a scheduled job needs no cache):
    state/<season>/series_<OFFICE>.csv          6-h CWA-mean NOHRSC snowfall (inches) - the series the events are detected from
    state/<season>/events.json                  every event with its status and statistics
    state/<season>/grids/<OFFICE>_<id>.npz      observed (and, once verified, forecast) event totals on the office's grid, inches
Viewer data (viewer/data/):
    manifest.json, cwa.geojson (static), <season>/<OFFICE>_mean_error.bin  (season-to-date mean error),
    <season>/events/<OFFICE>_<id>_error.bin  (one event's error grid).   .bin = float32 little-endian, row-major, row 0 = north edge,
    NaN = outside the CWA / too few valid events, -999 = radar beam-blockage sector (excluded from every statistic).

Options:  --season 2026-27   --offices BOX GYX   --workdir work   --export-only (rebuild viewer data from state, no network)
          --asof 2026-01-15T12  (pretend it is that moment - for testing; with --lag-days N it also pretends the NCEI archive ends N days earlier)
Environment: NWSNOW_MONTHS=10,11,... (months in the season), NWSNOW_NOHRSC_RATE, NWSNOW_NCEI_RATE, NWSNOW_USER_AGENT.
"""
import argparse, csv, datetime as dt, glob, io, json, math, os, pathlib, random, re, sys, threading, time, types
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor
from urllib.parse import urlparse

try:
    import numpy as np
    import scipy.sparse as sp
    import requests
    from requests.adapters import HTTPAdapter
    import shapely
    from shapely import contains_xy, make_valid
    from shapely.geometry import shape
    from pyproj import Transformer
except ImportError as _e:
    sys.exit(f"Missing Python package ({_e}).  pip install -r requirements.txt")

ROOT = pathlib.Path(__file__).resolve().parents[1]

# ======================================================================================================================================
# SETTINGS
# ======================================================================================================================================
OFFICES = {   # name, map colour, office location (lon, lat)
    "CAR": dict(name="Caribou",       color="#009E73", pt=(-68.02, 46.87)),
    "GYX": dict(name="Gray/Portland", color="#0072B2", pt=(-70.26, 43.89)),
    "BTV": dict(name="Burlington",    color="#CC79A7", pt=(-73.15, 44.47)),
    "BOX": dict(name="Boston/Norton", color="#E69F00", pt=(-71.18, 41.97)),
}
ORDER = list(OFFICES)
MONTHS = tuple(int(m) for m in os.environ.get("NWSNOW_MONTHS", "10,11,12,1,2,3,4,5").split(","))      # months whose 6-h periods belong to the season
NOHRSC_X0, NOHRSC_Y0, NOHRSC_STEP = -126.0, 55.0, 0.04          # NOHRSC sfav2 CONUS grid: 1500 x 850 cells of 0.04 deg, upper-left corner
R0, R1, C0, C1 = 175, 388, 1150, 1500                            # rows 175..387 (48.0N..39.5N), cols 1150..1499 (80W..66W): covers all four CWAs
THR_SERIES, THR_TOTAL, GAP_HOURS, MAX_HOURS = 0.08, 1.5, 12, 48
# LOCALIZED events: snow that misses the CWA-mean threshold but still hit part of the area (lake-effect belts, mountains, a northern edge) also counts as an event
# when at least LOCAL_AREA_PCT % of the CWA's cells received at least LOCAL_MIN_IN inches in total. A 6-h period counts as "snowing" for this when at least
# AREA_SERIES_FRAC of the CWA got AREA_SERIES_IN inches in it.
LOCAL_MIN_IN, LOCAL_AREA_PCT, AREA_SERIES_IN, AREA_SERIES_FRAC = 1.0, 5.0, 0.3, 0.02
LEADS = (12, 6)                                                  # forecast = latest run issued at least this many hours before the event starts
LOOKBACK_DAYS = 21                                               # recent analyses are re-fetched this far back (NOHRSC revises them)
MIN_VALID = 0.9                                                  # a map cell needs valid data in this share of the events
SMALL_SAMPLE = 5                                                 # fewer verified events than this -> flagged in the viewer
CI_MIN_EVENTS = 5                                                # bootstrap interval only from this many events
# radar beam-blockage artifacts of the NOHRSC analysis: office -> (radar, lat, lon, azimuth start, azimuth end, minimum range km)
RADAR_EXCLUSIONS = {"BTV": ("KCXX Burlington", 44.511, -73.166, 30.0, 160.0, 15.0), "GYX": ("KGYX Gray", 43.891, -70.257, 69.0, 82.0, 15.0)}
NET_LIMITS = {"www.nohrsc.noaa.gov": (6, 8.0), "www.ncei.noaa.gov": (4, 3.0)}      # host: (simultaneous connections, requests per second)
NET_DEFAULT = (4, 5.0)
EXCLUDED = -999.0
SIX = dt.timedelta(hours=6)
NDFD_BASE = "https://www.ncei.noaa.gov/thredds/"

cfg = types.SimpleNamespace()
def _init(args):
    env = os.environ.get
    cfg.WORK = pathlib.Path(args.workdir).resolve(); cfg.CUBE = cfg.WORK / "nohrsc_cube"; cfg.CATALOG = cfg.WORK / "ndfd" / "catalog"; cfg.RUNS = cfg.WORK / "ndfd" / "runs"
    cfg.NET = dict(NET_LIMITS); cfg.TODAY = _utcnow().date()
    if env("NWSNOW_NOHRSC_RATE"): cfg.NET["www.nohrsc.noaa.gov"] = (NET_LIMITS["www.nohrsc.noaa.gov"][0], float(env("NWSNOW_NOHRSC_RATE")))
    if env("NWSNOW_NCEI_RATE"): cfg.NET["www.ncei.noaa.gov"] = (NET_LIMITS["www.ncei.noaa.gov"][0], float(env("NWSNOW_NCEI_RATE")))
    cfg.UA = env("NWSNOW_USER_AGENT", "nws-current-season-snowfall-error/1.0 (research use; python-requests)")

# ======================================================================================================================================
# POLITE, RESILIENT HTTP: keep-alive, per-host connection and request-rate caps, retries with backoff, Retry-After, automatic slow-down on 429/503
# ======================================================================================================================================
RETRY_STATUS = {429, 500, 502, 503, 504}
_tl = threading.local(); _hosts = {}; _hl = threading.Lock()

class Host:
    def __init__(self, name):
        conn, rate = cfg.NET.get(name, NET_DEFAULT)
        self.name = name; self.sem = threading.BoundedSemaphore(conn); self.base = 1.0 / rate if rate else 0.0; self.interval = self.base
        self.lock = threading.Lock(); self.next = 0.0; self.pause_until = 0.0; self.streak = 0; self.requests = 0; self.bytes = 0; self.retries = 0; self.throttled = 0
    def pace(self):
        with self.lock:
            now = time.monotonic(); t = max(now, self.next, self.pause_until); self.next = t + self.interval
        if t > now: time.sleep(t - now)
    def ok(self, n):
        with self.lock:
            self.requests += 1; self.bytes += n; self.streak += 1
            if self.streak >= 50 and self.interval > self.base: self.interval = max(self.base, self.interval * 0.8); self.streak = 0
    def slow(self, retry_after=None):
        with self.lock:
            self.throttled += 1; self.streak = 0; self.interval = min(max(self.interval * 2, 0.25), 5.0)
            self.pause_until = max(self.pause_until, time.monotonic() + (retry_after or 5.0))

def _host(name):
    with _hl:
        if name not in _hosts: _hosts[name] = Host(name)
        return _hosts[name]

def _session():
    if not hasattr(_tl, "s"):
        s = requests.Session(); s.headers["User-Agent"] = cfg.UA; a = HTTPAdapter(pool_connections=4, pool_maxsize=8, max_retries=0); s.mount("https://", a); _tl.s = s
    return _tl.s

def http_get(url, headers=None, ok404=False, tries=6, timeout=(15, 120)):
    """GET with pacing and retries -> Response (200/206); None for a 404 when ok404; raises after persistent failure"""
    h = _host(urlparse(url).netloc); last = None
    for k in range(tries):
        h.sem.acquire()
        try:
            h.pace(); r = _session().get(url, headers=headers, timeout=timeout)
            if r.status_code in (200, 206): h.ok(len(r.content)); return r
            if r.status_code == 404 and ok404: h.ok(0); return None
            ra = r.headers.get("Retry-After"); ra = float(ra) if ra and ra.replace(".", "", 1).isdigit() else None; last = f"HTTP {r.status_code}"
            if r.status_code not in RETRY_STATUS: raise RuntimeError(f"{last} for {url}")
            if r.status_code in (429, 503): h.slow(min(ra, 120) if ra else None)
        except (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError) as e:
            last = e.__class__.__name__
        finally:
            h.sem.release()
        h.retries += 1; time.sleep(min(60, 2 ** k) * (0.5 + random.random()))
    raise RuntimeError(f"giving up on {url}: {last}")

def http_download(url, out):
    r = http_get(url, ok404=True)
    if r is None: return False
    tmp = f"{out}.{os.getpid()}.{threading.get_ident()}.part"; open(tmp, "wb").write(r.content); os.replace(tmp, out); return True

def http_summary():
    for h in _hosts.values(): print(f"  net {h.name}: {h.requests} requests, {h.bytes / 1e6:.0f} MB, {h.retries} retries, {h.throttled} throttle responses", flush=True)

# ======================================================================================================================================
# GRID, CWA POLYGONS
# ======================================================================================================================================
CUBE_LON = NOHRSC_X0 + NOHRSC_STEP * (C0 + np.arange(C1 - C0) + 0.5)       # cell-centre longitudes / latitudes (north to south) of the cube
CUBE_LAT = NOHRSC_Y0 - NOHRSC_STEP * (R0 + np.arange(R1 - R0) + 0.5)
LON2, LAT2 = np.meshgrid(CUBE_LON, CUBE_LAT)
_CWA = {}

def load_cwa():
    if not _CWA:
        for ft in json.load(open(ROOT / "reference" / "cwa_new_england.geojson"))["features"]:
            off = ft["properties"]["cwa"].strip()
            if off not in OFFICES: continue
            g = shape(ft["geometry"])
            if not g.is_valid:
                g = make_valid(g)
                if g.geom_type == "GeometryCollection": g = shapely.union_all([p for p in g.geoms if p.geom_type in ("Polygon", "MultiPolygon")])
            _CWA[off] = g
    return _CWA

_GEO = {}
def office_geometry(off):
    """crop slice, CWA mask and radar-exclusion mask of an office on the cube grid (cells whose centre lies inside the CWA)"""
    if off not in _GEO:
        M = contains_xy(load_cwa()[off], LON2, LAT2); ys, xs = np.where(M)
        sl = (slice(max(ys.min() - 2, 0), ys.max() + 3), slice(max(xs.min() - 2, 0), xs.max() + 3)); CM = M[sl].copy(); XM = np.zeros(CM.shape, bool)
        if off in RADAR_EXCLUSIONS:
            nm, la0, lo0, a0, a1, r0 = RADAR_EXCLUSIONS[off]; LON, LAT = np.meshgrid(CUBE_LON[sl[1]], CUBE_LAT[sl[0]])
            dy = (LAT - la0) * 111.0; dx = (LON - lo0) * 111.0 * np.cos(np.radians(la0)); az = (np.degrees(np.arctan2(dx, dy)) + 360) % 360
            XM = CM & (az >= a0) & (az <= a1) & (np.hypot(dx, dy) > r0); CM = CM & ~XM
        _GEO[off] = dict(M=M, sl=sl, CM=CM, XM=XM, lon=CUBE_LON[sl[1]], lat=CUBE_LAT[sl[0]])
    return _GEO[off]

# ======================================================================================================================================
# NOHRSC sfav2 6-h snowfall (inches), named by the END time of the period:  .../snowfall_v2/data/YYYYMM/sfav2_CONUS_6h_YYYYMMDDHH.tif
# Files are small (~0.3-0.4 MB, LZW) and read whole; the New England window is cropped and cached as .npy
# ======================================================================================================================================
def url_of(s): return f"https://www.nohrsc.noaa.gov/snowfall_v2/data/{s[:6]}/sfav2_CONUS_6h_{s}.tif"
def key(t): return t.strftime("%Y%m%d%H")

def nohrsc_exists(t):
    r = http_get(url_of(key(t)), headers={"Range": "bytes=0-99"}, ok404=True); return r is not None and len(r.content) >= 50

def fetch_period(t):
    """'have' | 'ok' | 'missing' | 'err ...'"""
    s = key(t); out = cfg.CUBE / (s + ".npy")
    if out.exists(): return "have"
    try:
        import tifffile
        r = http_get(url_of(s), ok404=True)
        if r is None: return "missing"
        a = np.array(tifffile.imread(io.BytesIO(r.content))[R0:R1, C0:C1], dtype="f4")
        if not bool(np.all((a < -9000) | ((a > -0.01) & (a < 400)))): return "err implausible values"
        a[a < -9000] = np.nan; tmp = str(out) + f".{os.getpid()}.{threading.get_ident()}.tmp.npy"; np.save(tmp, a); os.replace(tmp, out); return "ok"
    except Exception as e:
        return "err " + str(e)[:80]

def _utcnow(): return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
def floor6(t): return t.replace(hour=t.hour // 6 * 6, minute=0, second=0, microsecond=0)

def latest_analysis(now):
    t = floor6(now)
    for _ in range(8):
        if nohrsc_exists(t): return t
        t -= SIX
    raise RuntimeError("no NOHRSC analysis found in the last two days")

def season_periods(season, latest):
    y = int(season[:4]); t = dt.datetime(y, 7, 1, 6); out = []
    while t <= min(latest, dt.datetime(y + 1, 7, 1)):
        if t.month in MONTHS: out.append(t)
        t += SIX
    return out

def cwa_series(off, a):
    """CWA-mean snowfall (in), valid fraction, and the share of valid cells with >= AREA_SERIES_IN of a cube array"""
    M = office_geometry(off)["M"]; v = a[M]; fin = np.isfinite(v); frac = float(fin.mean()) if v.size else 0.0
    return (float(v[fin].mean()) if fin.any() else float("nan")), frac, (float((v[fin] >= AREA_SERIES_IN).mean()) if fin.any() else float("nan"))

# ======================================================================================================================================
# NDFD forecasts: NCEI THREDDS archive, WMO YSUZ98 "Snowfall Accumulation", KWBN, CONUS 2.5 km, 6-h messages
# ======================================================================================================================================
_cat_mem = {}
def day_catalog(day):
    """[(run datetime UTC, urlPath)] of the snowfall files issued on UTC date `day`.  Days within the last 10 days are never cached on disk: they may still be filling up"""
    if day in _cat_mem: return _cat_mem[day]
    f = cfg.CATALOG / f"{day:%Y%m%d}.json"; settled = (cfg.TODAY - day).days > 10
    if settled and f.exists(): out = json.load(open(f))
    else:
        r = http_get(NDFD_BASE + f"catalog/model-ndfd-file_kwbn/access/{day:%Y%m}/{day:%Y%m%d}/catalog.xml", ok404=True)
        s = r.text if r is not None else ""
        it = re.findall(r'<dataset name="Snowfall Accumulation run on (\S+) (\d\d):(\d\d) WMO: YSUZ98 CC: KWBN"[^>]*urlPath="([^"]*)"', s)
        out = sorted((dt.datetime.strptime(d + h + m, "%Y-%m-%d%H%M").strftime("%Y%m%d%H%M"), u) for d, h, m, u in it)
        if settled: f.parent.mkdir(parents=True, exist_ok=True); json.dump(out, open(f, "w"))
    _cat_mem[day] = [(dt.datetime.strptime(a, "%Y%m%d%H%M"), b) for a, b in out]; return _cat_mem[day]

def archive_complete_through(today, lag_days=None):
    """last UTC day whose runs are completely archived: the newest day with a catalog is still filling, so it is the day before.  `lag_days` (testing) pretends the archive ends that many days before `today`"""
    d = today - dt.timedelta(days=lag_days or 0)
    for _ in range(45):
        if day_catalog(d): return d - dt.timedelta(days=1)
        d -= dt.timedelta(days=1)
    return None

def find_run(target, complete_through):
    """('ok', (run time, urlPath)) | ('pending', None) - not archived yet | ('none', None) - archived but no run at or before target"""
    if complete_through is None or target.date() > complete_through: return "pending", None
    for d in (target.date(), target.date() - dt.timedelta(days=1)):
        runs = [r for r in day_catalog(d) if r[0] <= target]
        if runs: return "ok", max(runs)
    return "none", None

def run_path(u): return cfg.RUNS / (os.path.basename(u) + ".grib2")
def fetch_run(u):
    f = run_path(u)
    if not f.exists(): cfg.RUNS.mkdir(parents=True, exist_ok=True); http_download(NDFD_BASE + "fileServer/" + u, f)
    return f

# NDFD 2.5 km Lambert grid -> NOHRSC 0.04 deg cells: each cell is the mean of 5x5 bilinear samples (one sparse-matrix multiply per message)
OFFY = np.array([-0.7, -0.35, 0.0, 0.35, 0.7]); OFFX = np.array([-0.5, -0.25, 0.0, 0.25, 0.5]); _W = {}
def _weights(m):
    k = (m.Ni, m.Nj, m.projString)
    if k in _W: return _W[k]
    lat, lon = m.latlons(); t = Transformer.from_crs("EPSG:4326", m.projString, always_xy=True)
    x, y = t.transform(lon[[0, 0], [0, 1]], lat[[0, 0], [0, 1]]); x1, y1 = t.transform(lon[1, 0], lat[1, 0]); x0, y0 = x[0], y[0]; dx = x[1] - x[0]; dy = y1 - y0
    xs, ys = t.transform(LON2, LAT2); fj = (ys - y0) / dy; fi = (xs - x0) / dx
    j0c = max(int(np.floor(fj.min() - 2)), 0); j1c = min(int(np.ceil(fj.max() + 2)) + 1, m.Nj); i0c = max(int(np.floor(fi.min() - 2)), 0); i1c = min(int(np.ceil(fi.max() + 2)) + 1, m.Ni); nci = i1c - i0c
    n = fj.size; rows = np.arange(n); R_, C_, V_ = [], [], []
    for a in OFFY:
        for b in OFFX:
            j = np.clip(fj + a, 0, m.Nj - 1).ravel(); i = np.clip(fi + b, 0, m.Ni - 1).ravel()
            jb = np.minimum(np.floor(j).astype(int), m.Nj - 2); ib = np.minimum(np.floor(i).astype(int), m.Ni - 2); wj = j - jb; wi = i - ib
            for dj, di, w in ((0, 0, (1 - wj) * (1 - wi)), (0, 1, (1 - wj) * wi), (1, 0, wj * (1 - wi)), (1, 1, wj * wi)):
                R_.append(rows); C_.append((jb + dj - j0c) * nci + (ib + di - i0c)); V_.append(w / 25.0)
    W = sp.csr_matrix((np.concatenate(V_).astype("f4"), (np.concatenate(R_), np.concatenate(C_))), shape=(n, (j1c - j0c) * nci))
    _W[k] = (W, (slice(j0c, j1c), slice(i0c, i1c))); return _W[k]

def regrid_run(path, ends):
    """{period end: float32 array on the cube grid, inches} for the requested period end times found in a run file"""
    import pygrib
    out = {}; want = set(ends); g = pygrib.open(str(path))
    for m in g:
        end = dt.datetime(m.validityDate // 10000, m.validityDate // 100 % 100, m.validityDate % 100, m.validityTime // 100)
        if end in want and end not in out:
            W, (sj, si) = _weights(m); v = np.ma.filled(m.values, 0.0).astype("f4")[sj, si] * np.float32(39.3701)      # m -> in
            out[end] = (W @ v.ravel()).reshape(LON2.shape).astype("f4")
    g.close(); return out

def ends_of(w0, w1): return [w0 + SIX * (i + 1) for i in range(int((w1 - w0).total_seconds() // 21600))]

# ======================================================================================================================================
# EVENT DETECTION from the stored CWA-mean series
# ======================================================================================================================================
def _emit(ts, ser, i0, i1, ongoing):
    """one event from period indices i0..i1, split at the quietest 6-h period when longer than MAX_HOURS"""
    w0 = ts[i0] - SIX; w1 = ts[i1]; ev = []
    if (w1 - w0).total_seconds() / 3600 > MAX_HOURS:
        seg = [(i0, i1)]
        while True:
            j0, j1 = seg[-1]
            if (ts[j1] - ts[j0]).total_seconds() / 3600 + 6 <= MAX_HOURS: break
            k = j0 + 4 + int(np.nanargmin(ser[j0 + 4:j0 + 8])); seg[-1] = (j0, k); seg.append((k + 1, j1))
        for n, (a, b) in enumerate(seg): ev.append((ts[a] - SIX, ts[b], float(np.nansum(ser[a:b + 1])), ongoing and n == len(seg) - 1))
    else: ev.append((w0, w1, float(np.nansum(ser[i0:i1 + 1])), ongoing))
    return ev

def _groups(ts, hot):
    grp = []
    for i in hot:
        if grp and ts[i] - ts[grp[-1][-1]] <= dt.timedelta(hours=GAP_HOURS): grp[-1].append(i)
        else: grp.append([i])
    return grp

def detect(ts, ser, now_end):
    """events [(window start, window end, total in, ongoing)] from the 6-h CWA-mean series (ts = period end times, ser = inches, NaN = no data)"""
    ev = []
    for g in _groups(ts, [i for i in range(len(ts)) if np.isfinite(ser[i]) and ser[i] >= THR_SERIES]):
        i0, i1 = g[0], g[-1]
        if float(np.nansum(ser[i0:i1 + 1])) < THR_TOTAL: continue
        ev += _emit(ts, ser, i0, i1, (now_end - ts[i1]) < dt.timedelta(hours=GAP_HOURS))      # snow could still resume and extend the event
    return ev

def detect_local(ts, ser, area, mean_events, now_end, check):
    """localized events [(start, end, CWA-mean total, ongoing, % of CWA area >= LOCAL_MIN_IN)]: snowing periods by area share, qualified by check(start, end, ongoing) -> (ok, area %).
    Groups that touch an event already found from the CWA-mean series are left to that event."""
    ev = []; gap = dt.timedelta(hours=GAP_HOURS)
    for g in _groups(ts, [i for i in range(len(ts)) if np.isfinite(area[i]) and area[i] >= AREA_SERIES_FRAC]):
        i0, i1 = g[0], g[-1]; w0 = ts[i0] - SIX; w1 = ts[i1]
        if any(w0 <= m1 + gap and w1 >= m0 - gap for m0, m1, _, _ in mean_events): continue
        ongoing = (now_end - w1) < gap; ok, pct = check(w0, w1, ongoing)
        if ok: ev += [(a, b, t, o, pct) for a, b, t, o in _emit(ts, ser, i0, i1, ongoing)]
    return ev

# ======================================================================================================================================
# STATISTICS
# ======================================================================================================================================
def event_stats(fc, obs, CM):
    E = fc - obs; ok = np.isfinite(E) & CM
    if not ok.any(): return None
    tol = np.maximum(2.0, 0.30 * obs); so = float(obs[ok].sum())
    return dict(mean_forecast=float(fc[ok].mean()), mean_observed=float(obs[ok].mean()), mean_error=float(E[ok].mean()), mae=float(np.abs(E[ok]).mean()),
                rmse=float(np.sqrt((E[ok] ** 2).mean())), percent_bias=float(100 * E[ok].sum() / so) if so > 0.05 * ok.sum() else None,
                hit_rate=float(100 * (np.abs(E[ok]) <= tol[ok]).mean()), max_observed=float(np.nanmax(np.where(ok, obs, np.nan))),
                max_forecast=float(np.nanmax(np.where(ok, fc, np.nan))), cells=int(ok.sum()))

rng = np.random.default_rng(2026)
def season_stats(G, evs, grids):
    """season-to-date mean-error grid (NaN where < MIN_VALID of the events are valid, -999 in radar sectors) and statistics, from verified events"""
    CM, XM = G["CM"], G["XM"]; F = np.stack([grids[e["id"]]["fc"] for e in evs]); T = np.stack([grids[e["id"]]["obs"] for e in evs]); E = F - T
    ok = np.isfinite(E) & CM[None]; n = ok.sum(0).astype(float); n[n < MIN_VALID * len(evs)] = np.nan
    with np.errstate(all="ignore"): g = np.nanmean(np.where(ok, E, np.nan), 0)
    g[np.isnan(n)] = np.nan; g[~CM] = np.nan; g = g.astype("<f4"); g[XM] = EXCLUDED
    tol = np.maximum(2.0, 0.30 * T); sm = np.array([E[i][ok[i]].mean() for i in range(len(evs)) if ok[i].any()]); v = g[np.isfinite(g) & (g != EXCLUDED)]
    st = dict(events=len(evs), mean_error=float(E[ok].mean()), mae=float(np.abs(E[ok]).mean()), rmse=float(np.sqrt((E[ok] ** 2).mean())),
              percent_bias=float(100 * E[ok].sum() / T[ok].sum()) if T[ok].sum() > 0 else None, hit_rate=float(100 * (np.abs(E[ok]) <= tol[ok]).mean()),
              mean_forecast=float(F[ok].mean()), mean_observed=float(T[ok].mean()), mean_event_error=float(sm.mean()), mean_error_ci90=None,
              area_pct_gt_p05=float(100 * np.mean(v > 0.5)) if v.size else None, area_pct_lt_m05=float(100 * np.mean(v < -0.5)) if v.size else None,
              cells=int(CM.sum()), cells_excluded_radar=int(XM.sum()), small_sample=bool(len(evs) < SMALL_SAMPLE))
    if len(sm) >= CI_MIN_EVENTS:
        b = np.array([sm[rng.integers(0, len(sm), len(sm))].mean() for _ in range(5000)]); st["mean_error_ci90"] = [float(x) for x in np.percentile(b, [5, 95])]
    return g, st

# ======================================================================================================================================
# STATE (state/<season>/...)
# ======================================================================================================================================
def iso(t): return t.strftime("%Y-%m-%dT%H:%MZ") if t else None
def parse(s): return dt.datetime.strptime(s, "%Y-%m-%dT%H:%MZ") if s else None
def eid(w0): return w0.strftime("%Y%m%d%H")

def load_state(sdir):
    f = sdir / "events.json"; st = json.load(open(f)) if f.exists() else {"version": 1, "offices": {}}
    series = {}
    for off in OFFICES:
        series[off] = {}; p = sdir / f"series_{off}.csv"
        if p.exists():
            for r in csv.DictReader(open(p)): series[off][dt.datetime.strptime(r["end_utc"], "%Y%m%d%H")] = (float(r["mean_in"]) if r["mean_in"] != "" else float("nan"), float(r["valid_frac"]),
                                                                              float(r["area_frac"]) if r.get("area_frac") not in (None, "") else float("nan"))
    return st, series

def save_series(sdir, off, s):
    sdir.mkdir(parents=True, exist_ok=True)
    with open(sdir / f"series_{off}.csv", "w", newline="") as f:
        w = csv.writer(f); w.writerow(["end_utc", "mean_in", "valid_frac", "area_frac"])
        for t in sorted(s): w.writerow([key(t), "" if not np.isfinite(s[t][0]) else f"{s[t][0]:.4f}", f"{s[t][1]:.3f}", "" if not np.isfinite(s[t][2]) else f"{s[t][2]:.4f}"])

def grid_path(sdir, off, i): return sdir / "grids" / f"{off}_{i}.npz"
def load_grids(sdir, off, i):
    p = grid_path(sdir, off, i)
    if not p.exists(): return None
    z = np.load(p); return dict(obs=z["obs"], fc=z["fc"] if "fc" in z.files else None)
def save_grids(sdir, off, i, obs, fc=None):
    p = grid_path(sdir, off, i); p.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(p, obs=obs.astype("f4"), **({"fc": fc.astype("f4")} if fc is not None else {}))

# ======================================================================================================================================
# THE UPDATE
# ======================================================================================================================================
def local_check(sdir, state, off, w0, w1, ongoing, look):
    """does a snowing stretch qualify as a localized event? -> (ok, % of CWA cells with >= LOCAL_MIN_IN in total). Settled answers (older than the look-back) are kept in the state."""
    lc = state.setdefault("local_checks", {}); k = f"{off}|{iso(w0)}|{iso(w1)}"
    if not ongoing and w1 < look and k in lc: return lc[k] >= LOCAL_AREA_PCT, lc[k]
    ends = ends_of(w0, w1)
    for t in ends:
        if not (cfg.CUBE / (key(t) + ".npy")).exists(): fetch_period(t)
    if not all((cfg.CUBE / (key(t) + ".npy")).exists() for t in ends): return False, None          # not evaluable now; asked again on the next run
    G = office_geometry(off); tot = np.sum([np.nan_to_num(np.load(cfg.CUBE / (key(t) + ".npy"))[G["sl"]]) for t in ends], axis=0); v = tot[G["CM"]]
    pct = float(100 * (v >= LOCAL_MIN_IN).mean()) if v.size else 0.0
    if not ongoing: lc[k] = round(pct, 2)
    return pct >= LOCAL_AREA_PCT, pct

def update(args):
    season = args.season; y0 = int(season[:4]); sdir = ROOT / "state" / season; offices = args.offices
    now = dt.datetime.strptime(args.asof, "%Y-%m-%dT%H") if args.asof else _utcnow().replace(minute=0, second=0, microsecond=0)
    for d in (cfg.CUBE, cfg.CATALOG, cfg.RUNS): d.mkdir(parents=True, exist_ok=True)
    cfg.TODAY = now.date(); state, series = load_state(sdir)
    print(f"season {season}   now {now:%Y-%m-%d %H}Z   offices {' '.join(offices)}", flush=True)

    # ---- 1. NOHRSC: newest analysis, fetch what is new (and the look-back window)
    latest = latest_analysis(now) if not args.asof else floor6(now)
    periods = season_periods(season, latest)
    if not periods: print("no season periods yet"); return None
    have_all = {o: set(series[o]) for o in offices}; look = latest - dt.timedelta(days=LOOKBACK_DAYS)
    todo = [t for t in periods if t >= look or any(t not in have_all[o] or (series[o][t][1] > 0 and not np.isfinite(series[o][t][2])) for o in offices)]
    cached = sum((cfg.CUBE / (key(t) + ".npy")).exists() for t in todo)
    print(f"NOHRSC through {latest:%Y-%m-%d %H}Z: {len(periods)} season periods, {len(todo)} to (re)read ({cached} cached)", flush=True)
    # a look-back period is re-fetched even when the cache holds it, because the analysis may have been revised since: drop cached files inside the look-back
    if not args.asof:
        for t in todo:
            if t >= look and (cfg.CUBE / (key(t) + ".npy")).exists() and (cfg.CUBE / (key(t) + ".npy")).stat().st_mtime < time.time() - 3600: (cfg.CUBE / (key(t) + ".npy")).unlink()
    with ThreadPoolExecutor(6) as ex: got = dict(zip(todo, ex.map(fetch_period, todo)))
    c = Counter(r.split()[0] for r in got.values()); print("  NOHRSC periods:", dict(c), flush=True)
    if c["err"]: print("  WARNING: some periods failed and will be retried on the next run", flush=True)
    for t in todo:
        if got[t].startswith("err"): continue                                  # transient failure: not recorded, so the next run tries again
        f = cfg.CUBE / (key(t) + ".npy")
        if not f.exists():
            for o in offices: series[o][t] = (float("nan"), 0.0, float("nan"))           # missing analysis: recorded so that detection and status know
            continue
        a = np.load(f)
        for o in offices: series[o][t] = cwa_series(o, a)
    fetch_start = look - dt.timedelta(hours=2 * MAX_HOURS)                     # events starting before this are frozen (their grids are in state)

    # ---- 2. events per office
    archive_day = archive_complete_through(now.date(), args.lag_days)
    print(f"NCEI forecast archive complete through {archive_day}", flush=True)
    out_events = {}; need_fc = []; pset = set(periods)
    for off in offices:
        s = series[off]; ts = sorted(t for t in s if t in pset); ser = np.array([s[t][0] if s[t][1] > 0.5 else np.nan for t in ts])
        area = np.array([s[t][2] if s[t][1] > 0.5 else np.nan for t in ts])
        det = [(*d, None) for d in detect(ts, ser, latest)]
        det += detect_local(ts, ser, area, [(d[0], d[1], 0, 0) for d in det], latest, lambda w0, w1, ongoing, off=off: local_check(sdir, state, off, w0, w1, ongoing, look))
        det.sort(key=lambda d: d[0]); old = {e["id"]: e for e in state["offices"].get(off, [])}; evs = []
        for w0, w1, tot, ongoing, apct in det:
            i = eid(w0); prev = old.get(i); e = dict(id=i, start=iso(w0), end=iso(w1), hours=int((w1 - w0).total_seconds() // 3600), obs_cwa_mean=round(tot, 3))
            if apct is not None: e["basis"] = "local"; e["area_pct"] = round(apct, 1)
            if prev and prev.get("status") == "verified" and w0 < fetch_start and prev.get("end") == e["end"]:
                evs.append(prev); continue                                     # frozen, already verified
            ends = ends_of(w0, w1); miss = [t for t in ends if t not in s or not np.isfinite(s[t][0])]
            if ongoing: e["status"] = "ongoing"
            elif miss: e["status"] = "missing_analysis"
            else:
                obs = None
                if all((cfg.CUBE / (key(t) + ".npy")).exists() for t in ends):
                    G = office_geometry(off); obs = np.sum([np.load(cfg.CUBE / (key(t) + ".npy"))[G["sl"]] for t in ends], axis=0)
                else:
                    g0 = load_grids(sdir, off, i); obs = g0["obs"] if g0 else None
                if obs is None: e["status"] = "missing_analysis"
                else:
                    g_old = load_grids(sdir, off, i); save_grids(sdir, off, i, obs, g_old["fc"] if g_old else None)      # keeps a stored forecast
                    e["status"] = "pending_forecast"; need_fc.append((off, e, w0, w1))
            evs.append(e)
        out_events[off] = evs
        print(f"{off}: {len(evs)} events  " + str(dict(Counter(e['status'] for e in evs))), flush=True)

    # ---- 3. forecasts for finished events (reuse stored forecasts when the same event was already verified)
    todo_fc = []
    for off, e, w0, w1 in need_fc:
        prev = {x["id"]: x for x in state["offices"].get(off, [])}.get(e["id"]); g0 = load_grids(sdir, off, e["id"])
        if prev and prev.get("run") and g0 and g0.get("fc") is not None and prev.get("end") == e["end"]:
            e.update({k: prev[k] for k in ("run", "lead", "lead_hours") if k in prev}); e["_fc"] = g0["fc"]
        else: todo_fc.append((off, e, w0, w1))
    pend = list(todo_fc)
    for lead in LEADS:
        if not pend: break
        pick = []; nxt = []                                                    # pick: [((office, event, start, end), (run time, urlPath))]
        for k in pend:
            off, e, w0, w1 = k; st_, run = find_run(w0 - dt.timedelta(hours=lead), archive_day)
            if st_ == "ok": pick.append((k, run))
            elif st_ == "none": nxt.append(k)                                  # archived, but no run before that time: try the shorter lead
            # "pending": the archive does not reach this event yet; it stays pending_forecast and is retried on later runs
        needs = {}
        for (off, e, w0, w1), (rt, u) in pick: needs.setdefault(u, set()).update(ends_of(w0, w1))
        print(f"lead {lead} h: {len(needs)} NDFD run files for {len(pick)} events", flush=True)
        decoded = {}
        if needs:
            with ThreadPoolExecutor(min(len(needs), 4)) as ex: list(ex.map(lambda u: _safe(fetch_run, u), needs))
            paths = {u: run_path(u) for u in needs if run_path(u).exists()}
            if args.jobs > 1 and len(paths) > 1:
                with ProcessPoolExecutor(min(args.jobs, len(paths))) as ex: res = list(ex.map(regrid_run, paths.values(), [sorted(needs[u]) for u in paths], chunksize=1))
            else: res = [regrid_run(p, sorted(needs[u])) for u, p in paths.items()]
            decoded = dict(zip(paths, res))
        for k, (rt, u) in pick:
            off, e, w0, w1 = k; ends = ends_of(w0, w1)
            if u not in decoded: continue                                      # download failed this time: stays pending_forecast, retried on the next run
            if all(t in decoded[u] for t in ends):
                fc = sum(decoded[u][t] for t in ends)[office_geometry(off)["sl"]]
                e["_fc"] = fc; e["run"] = iso(rt); e["lead"] = lead; e["lead_hours"] = round((w0 - rt).total_seconds() / 3600, 1)
            else: nxt.append(k)                                                # the run does not cover the whole event: try the shorter lead
        pend = nxt
    for off, e, w0, w1 in pend: e["status"] = "no_forecast"                    # archived, but no run covers the event

    # ---- 4. statistics, grids, state
    for off in offices:
        G = office_geometry(off)
        for e in out_events[off]:
            if "_fc" in e:
                g0 = load_grids(sdir, off, e["id"]); obs = g0["obs"]; fc = e.pop("_fc"); st_ = event_stats(fc, obs, G["CM"])
                if st_ is None: e["status"] = "missing_analysis"; continue
                e["status"] = "verified"; e["stats"] = st_; save_grids(sdir, off, e["id"], obs, fc)
        save_series(sdir, off, series[off])
    state["offices"].update(out_events); state["updated"] = iso(_utcnow().replace(second=0, microsecond=0)); state["asof"] = iso(now)
    state["nohrsc_through"] = iso(latest); state["ndfd_archive_complete_through"] = str(archive_day) if archive_day else None; state["season"] = season
    json.dump(state, open(sdir / "events.json", "w"), indent=1)
    http_summary(); return state

def _safe(fn, *a):
    try: return fn(*a)
    except Exception as e: print("  warning:", str(e)[:120], flush=True)

# ======================================================================================================================================
# EXPORT: viewer data + events CSV
# ======================================================================================================================================
def season_label(season): y = int(season[:4]); return f"{y}–{str(y + 1)[2:]}"

def export(season, offices):
    sdir = ROOT / "state" / season; out = ROOT / "viewer" / "data"; (out / season / "events").mkdir(parents=True, exist_ok=True)
    state = json.load(open(sdir / "events.json")); mf = out / "manifest.json"; M = json.load(open(mf)) if mf.exists() else {}
    ofc = {}; rows = []
    for off in ORDER:
        if off not in offices: continue
        G = office_geometry(off); evs = state["offices"].get(off, []); ny, nx = G["CM"].shape
        dx = abs(float(G["lon"][1] - G["lon"][0])); dy = abs(float(G["lat"][1] - G["lat"][0]))
        _, ser = load_state(sdir); tot = sum(v[0] for v in ser[off].values() if np.isfinite(v[0]) and v[1] > 0.5)       # CWA-mean snowfall observed so far this season (all periods, events or not)
        ent = dict(nx=nx, ny=ny, west=float(G["lon"][0] - dx / 2), north=float(G["lat"][0] + dy / 2), dx=dx, dy=dy, cells=int(G["CM"].sum()), cells_excluded_radar=int(G["XM"].sum()),
                   observed_total_in=round(float(tot), 3), file=None, stats=None, events=[])
        ver = [e for e in evs if e["status"] == "verified"]; grids = {e["id"]: load_grids(sdir, off, e["id"]) for e in ver}
        for p in (out / season / "events").glob(f"{off}_*_error.bin"): p.unlink()                       # rewritten below; drops events that no longer exist
        for e in sorted(evs, key=lambda e: e["start"], reverse=True):
            rec = {k: e[k] for k in ("id", "start", "end", "hours", "obs_cwa_mean", "status", "basis", "area_pct") if k in e}
            if e["status"] == "verified":
                g = (grids[e["id"]]["fc"] - grids[e["id"]]["obs"]).astype("<f4"); g[~G["CM"]] = np.nan; g[~np.isfinite(grids[e["id"]]["fc"] - grids[e["id"]]["obs"])] = np.nan; g[G["XM"]] = EXCLUDED
                fn = f"{season}/events/{off}_{e['id']}_error.bin"; g.tofile(out / fn); rec.update(file=fn, run=e.get("run"), lead_hours=e.get("lead_hours"), **e["stats"])
            ent["events"].append(rec)
            rows.append(dict(season=season, office=off, event_start_utc=e["start"], event_end_utc=e["end"], hours=e["hours"], status=e["status"], basis=e.get("basis", "cwa_mean"), area_ge_min_pct=e.get("area_pct", ""), observed_cwa_mean_in=e["obs_cwa_mean"],
                             **{k: (round(e["stats"][k], 3) if e.get("stats") and e["stats"].get(k) is not None else "") for k in ("mean_forecast", "mean_observed", "mean_error", "mae", "rmse", "percent_bias", "hit_rate", "max_observed", "max_forecast")},
                             forecast_run_utc=e.get("run") or "", lead_hours=e.get("lead_hours") or ""))
        for p in (out / season).glob(f"{off}_mean_error.bin"): p.unlink()
        if ver:
            g, st = season_stats(G, ver, grids); fn = f"{season}/{off}_mean_error.bin"; g.tofile(out / fn); ent.update(file=fn, stats=st)
        st_counts = Counter(e["status"] for e in evs); ent["counts"] = dict(st_counts); ofc[off] = ent
        print(f"{off}: {len(evs)} events, {len(ver)} verified" + (f", season mean error {ent['stats']['mean_error']:+.2f} in" if ent["stats"] else ""))
    M.update(version=2, order=ORDER, units="in", byte_order="little-endian float32, row-major, row 0 = north edge, no header", nodata="NaN", excluded=EXCLUDED,
             layers=dict(season=dict(label="Season-to-date mean error (forecast − observed)", units="inches of snow per event", vmin=-2.0, vmax=2.0),
                         event=dict(label="Event error (forecast − observed)", units="inches of snow in the event", vmin=-6.0, vmax=6.0)),
             offices={o: dict(name=OFFICES[o]["name"], color=OFFICES[o]["color"], point=list(OFFICES[o]["pt"])) for o in ORDER},
             radar_exclusions={o: dict(radar=v[0], azimuth=[v[3], v[4]], min_range_km=v[5]) for o, v in RADAR_EXCLUSIONS.items() if o in OFFICES},
             min_valid_event_fraction=MIN_VALID, small_sample_events=SMALL_SAMPLE,
             thresholds=dict(snowing_in_per_6h=THR_SERIES, event_total_in=THR_TOTAL, local_min_in=LOCAL_MIN_IN, local_area_pct=LOCAL_AREA_PCT, gap_hours=GAP_HOURS, max_hours=MAX_HOURS, lead_hours=list(LEADS)))
    M.setdefault("seasons", {})[season] = dict(label=f"{season_label(season)} season", window=f"Oct 1 {season[:4]} – May 31 {int(season[:4]) + 1}", offices=ofc,
                                               updated=state.get("updated"), nohrsc_through=state.get("nohrsc_through"), ndfd_archive_complete_through=state.get("ndfd_archive_complete_through"))
    M["default_season"] = max(M["seasons"]); json.dump(M, open(mf, "w"), indent=1)
    (ROOT / "results").mkdir(exist_ok=True)
    with open(ROOT / "results" / f"events_{season}.csv", "w", newline="") as f:
        cols = list(rows[0]) if rows else ["season", "office", "event_start_utc", "event_end_utc", "hours", "status"]
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader(); w.writerows(sorted(rows, key=lambda r: (r["event_start_utc"], r["office"]), reverse=True))
    print("wrote", mf, f"and results/events_{season}.csv")

def season_of(t): return f"{t.year if t.month >= 7 else t.year - 1}-{str((t.year if t.month >= 7 else t.year - 1) + 1)[2:]}"

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--season", help="e.g. 2026-27 (default: the current season)"); ap.add_argument("--offices", nargs="+", default=ORDER, choices=ORDER)
    ap.add_argument("--workdir", default="work"); ap.add_argument("--jobs", type=int, default=min(4, os.cpu_count() or 1), help="parallel GRIB decoders")
    ap.add_argument("--export-only", action="store_true", help="rebuild the viewer data from state without any network access")
    ap.add_argument("--asof", help="testing: pretend it is YYYY-MM-DDTHH (UTC)"); ap.add_argument("--lag-days", type=int, help="testing: pretend the NCEI archive ends this many days before --asof")
    args = ap.parse_args(); _init(args)
    if args.asof and args.lag_days is None: args.lag_days = 5
    args.season = args.season or season_of(dt.datetime.strptime(args.asof, "%Y-%m-%dT%H") if args.asof else _utcnow())
    if not args.export_only:
        if update(args) is None and not (ROOT / "state" / args.season / "events.json").exists(): return
    export(args.season, args.offices)

if __name__ == "__main__":
    main()
