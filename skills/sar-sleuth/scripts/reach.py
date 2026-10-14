# /// script
# requires-python = ">=3.10"
# dependencies = ["numpy", "scipy", "pillow", "rasterio", "scikit-image", "duckdb", "pandas"]
# ///
"""How far could they have got? Walking-time map from the last seen point over real terrain.

  reach.py --at 40.70,29.96 --since 2026-10-13T14:00+03:00 --hours 24 --age 72 --sex f --mobility cane --out case1/

Terrain: AWS Terrarium elevation, ESA WorldCover land cover, OSM roads / paths / cliffs (no API keys).
Speed: Wood et al. 2023 walking model (slope in the direction of travel + hill slope + path type),
scaled to the person by person.py. Shortest walking time on a 16-neighbour grid, uphill and downhill
costed separately.

Writes to --out:
  reach.json     inputs, every parameter used, budgets, area and distance per contour
  reach.geojson  contour lines: likely limit, max limit, and hourly walking-time lines
  reach.png      map: hillshade, land cover, paths, contours, last seen point
  time.npy       walking hours from the point for every grid cell (+ grid.json to georeference it)
"""
from __future__ import annotations

import argparse
import io
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from dataclasses import asdict
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).parent))
import person as P  # noqa: E402

UA = "sar-sleuth/0.1 (https://github.com/mstkyvz)"
TERRARIUM = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
WORLDCOVER = "https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/ESA_WorldCover_10m_2021_v200_{tile}_Map.tif"
OVERPASS = ["https://maps.mail.ru/osm/tools/overpass/api/interpreter", "https://overpass-api.de/api/interpreter",
            "https://overpass.kumi.systems/api/interpreter", "https://overpass.private.coffee/api/interpreter"]
OSM_CHUNK_DEG = 0.08
OVERTURE_RELEASES = "https://overturemaps-us-west-2.s3.us-west-2.amazonaws.com/?list-type=2&prefix=release/&delimiter=/"
OVERTURE_SEGMENTS = "s3://overturemaps-us-west-2/release/{rel}/theme=transportation/type=segment/*"
M_LAT = 110_574.0
M_LON_EQ = 111_320.0

# 16-neighbour stencil; knight moves need both cells they cut through to be passable
NEIGH = [(0, 1), (1, 0), (0, -1), (-1, 0), (1, 1), (1, -1), (-1, 1), (-1, -1),
         (1, 2), (2, 1), (-1, 2), (-2, 1), (1, -2), (2, -1), (-1, -2), (-2, -1)]


def log(*a):
    print(*a, file=sys.stderr, flush=True)


class NotFound(Exception):
    pass


def http(url: str, data: bytes | None = None, timeout: int = 60, tries: int = 4) -> bytes:
    """GET (or POST form data) with curl: uses the system certificate store, which some mirrors need."""
    import subprocess
    import tempfile
    err = None
    for k in range(tries):
        if k:
            time.sleep(2 ** k)
        with tempfile.NamedTemporaryFile(suffix=".body") as body:
            cmd = ["curl", "-q", "-sS", "-L", "-m", str(timeout), "-A", UA, "-o", body.name, "-w", "%{http_code}"]
            if data is not None:
                cmd += ["--data-binary", "@-", "-H", "Content-Type: application/x-www-form-urlencoded"]
            r = subprocess.run(cmd + [url], input=data, capture_output=True, check=False)
            code = r.stdout.decode().strip()
            if r.returncode == 0 and code == "200":
                return Path(body.name).read_bytes()
            if code == "404":
                raise NotFound(url)
            err = f"curl exit {r.returncode}, HTTP {code} {r.stderr.decode().strip()[:80]}"
    raise RuntimeError(f"{url}: {err}")


# ---------------------------------------------------------------- grid

class Grid:
    """Local metric grid centred on the last seen point (equirectangular; fine for < ~150 km)."""

    def __init__(self, lat0: float, lon0: float, half_m: float, res: float):
        self.lat0, self.lon0, self.res = lat0, lon0, res
        self.n = int(math.ceil(half_m / res))
        self.size = 2 * self.n + 1
        self.m_lon = M_LON_EQ * math.cos(math.radians(lat0))
        ax = (np.arange(self.size) - self.n) * res
        self.x = ax[None, :].repeat(self.size, 0)            # east, m
        self.y = -ax[:, None].repeat(self.size, 1)           # north, m (row 0 = north)
        self.lat = lat0 + self.y / M_LAT
        self.lon = lon0 + self.x / self.m_lon

    def bbox(self):
        return (float(self.lat.min()), float(self.lon.min()), float(self.lat.max()), float(self.lon.max()))

    def to_rc(self, lat, lon):
        c = (np.asarray(lon) - self.lon0) * self.m_lon / self.res + self.n
        r = -(np.asarray(lat) - self.lat0) * M_LAT / self.res + self.n
        return r, c

    def to_ll(self, r, c):
        return (self.lat0 - (np.asarray(r) - self.n) * self.res / M_LAT,
                self.lon0 + (np.asarray(c) - self.n) * self.res / self.m_lon)

    def meta(self):
        return {"lat0": self.lat0, "lon0": self.lon0, "res_m": self.res, "n": self.n, "size": self.size,
                "projection": "local equirectangular: lat = lat0 - (row-n)*res/110574, lon = lon0 + (col-n)*res/(111320*cos(lat0))"}


# ---------------------------------------------------------------- elevation

def _tile_xy(lat, lon, z):
    s = 2 ** z
    x = (np.asarray(lon) + 180) / 360 * s * 256
    la = np.radians(np.asarray(lat))
    y = (1 - np.log(np.tan(la) + 1 / np.cos(la)) / math.pi) / 2 * s * 256
    return x, y


def elevation(g: Grid, cache: Path) -> np.ndarray:
    mpp = 156543.03 * math.cos(math.radians(g.lat0))
    z = int(min(14, max(8, math.ceil(math.log2(mpp / (g.res / 1.5))))))
    px, py = _tile_xy(g.lat, g.lon, z)
    tx0, tx1 = int(px.min() // 256), int(px.max() // 256)
    ty0, ty1 = int(py.min() // 256), int(py.max() // 256)
    jobs = [(x, y) for y in range(ty0, ty1 + 1) for x in range(tx0, tx1 + 1)]
    log(f"elevation: {len(jobs)} Terrarium tiles at z{z}")
    cache.mkdir(parents=True, exist_ok=True)

    def one(t):
        p = cache / f"terrarium_{z}_{t[0]}_{t[1]}.png"
        if not p.exists():
            b = http(TERRARIUM.format(z=z, x=t[0], y=t[1]))
            p.write_bytes(b)
        a = np.asarray(Image.open(p).convert("RGB"), dtype=np.float32)
        return a[..., 0] * 256 + a[..., 1] + a[..., 2] / 256 - 32768

    mos = np.zeros(((ty1 - ty0 + 1) * 256, (tx1 - tx0 + 1) * 256), np.float32)
    with ThreadPoolExecutor(8) as ex:
        for (x, y), a in zip(jobs, ex.map(one, jobs)):
            mos[(y - ty0) * 256:(y - ty0 + 1) * 256, (x - tx0) * 256:(x - tx0 + 1) * 256] = a
    fx, fy = px - tx0 * 256 - 0.5, py - ty0 * 256 - 0.5
    x0 = np.clip(np.floor(fx).astype(int), 0, mos.shape[1] - 2)
    y0 = np.clip(np.floor(fy).astype(int), 0, mos.shape[0] - 2)
    wx, wy = np.clip(fx - x0, 0, 1), np.clip(fy - y0, 0, 1)
    return ((mos[y0, x0] * (1 - wx) + mos[y0, x0 + 1] * wx) * (1 - wy)
            + (mos[y0 + 1, x0] * (1 - wx) + mos[y0 + 1, x0 + 1] * wx) * wy)


# ---------------------------------------------------------------- land cover

def landcover(g: Grid, cache: Path) -> np.ndarray:
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.windows import from_bounds
    os.environ.setdefault("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
    out = np.zeros(g.lat.shape, np.uint8)
    s, w, n, e = g.bbox()
    tiles = {(la, lo) for la in range(int(math.floor(s / 3)) * 3, int(math.floor(n / 3)) * 3 + 1, 3)
             for lo in range(int(math.floor(w / 3)) * 3, int(math.floor(e / 3)) * 3 + 1, 3)}
    for la, lo in sorted(tiles):
        name = f"{'N' if la >= 0 else 'S'}{abs(la):02d}{'E' if lo >= 0 else 'W'}{abs(lo):03d}"
        bs, bw, bn, be = max(s, la), max(w, lo), min(n, la + 3), min(e, lo + 3)
        key = cache / f"wc_{name}_{bs:.4f}_{bw:.4f}_{bn:.4f}_{be:.4f}_{g.res:.0f}.npy"
        if key.exists():
            arr = np.load(key)
        else:
            url = "/vsicurl/" + WORLDCOVER.format(tile=name)
            try:
                with rasterio.open(url) as r:
                    win = from_bounds(bw, bs, be, bn, r.transform)
                    h = max(1, int(round((bn - bs) * M_LAT / (g.res / 2))))
                    wd = max(1, int(round((be - bw) * g.m_lon / (g.res / 2))))
                    arr = r.read(1, window=win, out_shape=(h, wd), resampling=Resampling.mode)
            except Exception as ex:  # noqa: BLE001  (no tile = open sea)
                log(f"land cover tile {name}: {str(ex)[:120]} -> treated as water")
                arr = np.full((2, 2), 80, np.uint8)
            cache.mkdir(parents=True, exist_ok=True)
            np.save(key, arr)
        m = (g.lat >= bs) & (g.lat <= bn) & (g.lon >= bw) & (g.lon <= be)
        rr = np.clip(((bn - g.lat[m]) / (bn - bs) * arr.shape[0]).astype(int), 0, arr.shape[0] - 1)
        cc = np.clip(((g.lon[m] - bw) / (be - bw) * arr.shape[1]).astype(int), 0, arr.shape[1] - 1)
        out[m] = arr[rr, cc]
    log("land cover: " + ", ".join(f"{k}:{v}" for k, v in zip(*np.unique(out, return_counts=True))))
    return out


# ---------------------------------------------------------------- current conditions (Sentinel-2)

EARTH_SEARCH = "https://earth-search.aws.element84.com/v1/search"
SCL_SNOW, SCL_WATER = 11, 6
SCL_CLEAR = {2, 4, 5, 6, 7, 11}            # dark/shadow, vegetation, bare, water, unclassified, snow; not cloud or no data


def conditions(g: Grid, since, days: int, cache: Path) -> tuple[np.ndarray | None, dict]:
    """Sentinel-2 L2A scene classification (SCL, 20 m) from the clearest scene in the `days` before `since`.

    Keyless: Element84 Earth Search STAC + COGs on AWS. Returns an SCL array on the grid (0 = no clear data)."""
    import rasterio
    from rasterio.warp import transform
    from datetime import timedelta, timezone as tz
    s, w, n, e = g.bbox()
    t1 = since.astimezone(tz.utc)
    t0 = t1 - timedelta(days=days)
    body = json.dumps({"collections": ["sentinel-2-l2a"], "bbox": [w, s, e, n], "limit": 50,
                       "datetime": f"{t0:%Y-%m-%dT%H:%M:%SZ}/{t1:%Y-%m-%dT%H:%M:%SZ}"}).encode()
    try:
        import subprocess
        r = subprocess.run(["curl", "-sS", "-m", "60", "-X", "POST", EARTH_SEARCH, "-H", "Content-Type: application/json",
                            "--data-binary", "@-"], input=body, capture_output=True, check=True)
        feats = json.loads(r.stdout)["features"]
    except Exception as ex:  # noqa: BLE001
        return None, {"error": f"STAC search failed: {str(ex)[:120]}"}
    if not feats:
        return None, {"error": f"no Sentinel-2 scene in the {days} days before"}
    # newest dates first, and within a date the clearest; fill the grid tile by tile
    feats.sort(key=lambda f: (f["properties"]["datetime"][:10], -f["properties"]["eo:cloud_cover"]), reverse=True)
    out = np.zeros(g.lat.shape, np.uint8)
    used = []
    os.environ.setdefault("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
    for f in feats:
        if (out > 0).mean() > 0.98:
            break
        href = f["assets"]["scl"]["href"]
        try:
            with rasterio.open("/vsicurl/" + href) as src:
                xs, ys = transform("EPSG:4326", src.crs, g.lon.ravel().tolist(), g.lat.ravel().tolist())
                xs, ys = np.array(xs).reshape(g.lat.shape), np.array(ys).reshape(g.lat.shape)
                inside = (xs >= src.bounds.left) & (xs < src.bounds.right) & (ys > src.bounds.bottom) & (ys <= src.bounds.top)
                todo = inside & (out == 0)
                if not todo.any():
                    continue
                from rasterio.windows import from_bounds
                win = from_bounds(xs[todo].min() - 40, ys[todo].min() - 40, xs[todo].max() + 40, ys[todo].max() + 40,
                                  src.transform).round_offsets().round_lengths()
                fac = max(1, int(g.res // 20))
                h, wd = max(1, int(win.height // fac)), max(1, int(win.width // fac))
                arr = src.read(1, window=win, out_shape=(h, wd), boundless=True, fill_value=0)
                wt = src.window_transform(win)
                cc = ((xs - wt.c) / (wt.a * win.width / wd)).astype(int)
                rr = ((ys - wt.f) / (wt.e * win.height / h)).astype(int)
                ok = todo & (rr >= 0) & (rr < h) & (cc >= 0) & (cc < wd)
                v = np.zeros(out.shape, np.uint8)
                v[ok] = arr[rr[ok], cc[ok]]
                clear = ok & np.isin(v, list(SCL_CLEAR))
                out[clear] = v[clear]
                if clear.any():
                    used.append({"id": f["id"], "date": f["properties"]["datetime"][:10],
                                 "cloud_pct": round(f["properties"]["eo:cloud_cover"], 1)})
        except Exception as ex:  # noqa: BLE001
            log(f"conditions: {f['id']}: {str(ex)[:100]}")
    info = {"scenes": used, "clear_share": round(float((out > 0).mean()), 3),
            "snow_share": round(float((out == SCL_SNOW).mean()), 4), "water_share": round(float((out == SCL_WATER).mean()), 4),
            "source": "Sentinel-2 L2A SCL via Element84 Earth Search (Copernicus data, free and open)"}
    return out, info


# ---------------------------------------------------------------- OSM

def _overpass(q: str) -> list:
    last = None
    for k, url in enumerate(OVERPASS * 3):
        if k >= len(OVERPASS):
            time.sleep(15 * (k // len(OVERPASS)))              # busy servers: back off before the next round
        try:
            b = http(url, data=urllib.parse.urlencode({"data": q}).encode(), timeout=120, tries=1)
            return json.loads(b)["elements"]
        except Exception as ex:  # noqa: BLE001
            last = ex
            log(f"  OSM: {url.split('/')[2]} failed ({str(ex)[-60:]}), trying next")
    raise RuntimeError(f"Overpass failed on every mirror: {last}. Re-run later (finished chunks are cached) or pass --no-osm")


def osm_ways(g: Grid, cache: Path, cfg: dict) -> list[dict]:
    """Highways and cliffs in the grid bbox, fetched in OSM_CHUNK_DEG chunks so busy mirrors don't time out."""
    s, w, n, e = g.bbox()
    hw = "|".join(cfg["paved"] + cfg["unpaved"])
    ny, nx = max(1, math.ceil((n - s) / OSM_CHUNK_DEG)), max(1, math.ceil((e - w) / OSM_CHUNK_DEG))
    cache.mkdir(parents=True, exist_ok=True)
    seen, ways = set(), []
    log(f"OSM: {ny * nx} chunk(s) of roads, paths and cliffs")
    for i in range(ny):
        for j in range(nx):
            cs, cn = s + (n - s) * i / ny, s + (n - s) * (i + 1) / ny
            cw, ce = w + (e - w) * j / nx, w + (e - w) * (j + 1) / nx
            bb = f"{cs:.5f},{cw:.5f},{cn:.5f},{ce:.5f}"
            key = cache / f"osm_{bb.replace(',', '_')}.json"
            if key.exists():
                els = json.loads(key.read_text())
            else:
                q = (f"[out:json][timeout:180];(way[highway~\"^({hw})$\"]({bb});way[natural=cliff]({bb}););"
                     f"out tags geom;")
                els = _overpass(q)
                key.write_text(json.dumps(els))
            for x in els:
                if x["id"] in seen:
                    continue
                seen.add(x["id"])
                ways.append({"t": x.get("tags", {}), "g": [(p["lat"], p["lon"]) for p in x.get("geometry", [])]})
    return ways


def overture_ways(g: Grid, cache: Path) -> list[dict]:
    """Roads and paths from Overture Maps (OSM-derived, served as GeoParquet on public S3: no Overpass, no key)."""
    import re
    s, w, n, e = g.bbox()
    key = cache / f"overture_{s:.4f}_{w:.4f}_{n:.4f}_{e:.4f}.json"
    if key.exists():
        return json.loads(key.read_text())
    rels = re.findall(r"<Prefix>release/([^<]+)/</Prefix>", http(OVERTURE_RELEASES, timeout=30).decode())
    if not rels:
        raise RuntimeError("no Overture release listed")
    rel = sorted(rels)[-1]
    import duckdb
    log(f"roads: Overture {rel} (one query, ~1 min)")
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs; INSTALL spatial; LOAD spatial; SET s3_region='us-west-2';")
    rows = con.execute(f"""
        SELECT class, ST_AsText(geometry) FROM read_parquet('{OVERTURE_SEGMENTS.format(rel=rel)}', hive_partitioning=1)
        WHERE subtype = 'road' AND bbox.xmax > {w} AND bbox.xmin < {e} AND bbox.ymax > {s} AND bbox.ymin < {n}""").fetchall()
    ways = []
    for cls, wkt in rows:
        nums = re.findall(r"(-?[\d.]+) (-?[\d.]+)", wkt or "")
        if len(nums) >= 2:
            ways.append({"t": {"highway": cls or "unknown"}, "g": [(float(b), float(a)) for a, b in nums]})
    cache.mkdir(parents=True, exist_ok=True)
    key.write_text(json.dumps(ways))
    return ways


def road_data(g: Grid, cache: Path, cfg: dict, source: str) -> tuple[list[dict], str, list[str]]:
    """Ways from the first source that works; never fails: no roads + a warning as the last resort."""
    order = {"auto": ["overture", "overpass"], "overture": ["overture"], "overpass": ["overpass"], "none": []}[source]
    errs = []
    for src in order:
        try:
            ways = overture_ways(g, cache) if src == "overture" else osm_ways(g, cache, cfg)
            return ways, src, errs
        except Exception as ex:  # noqa: BLE001
            errs.append(f"{src}: {str(ex)[:150]}")
            log(f"roads: {src} failed, {'trying next' if src != order[-1] else 'continuing WITHOUT roads'}")
    if order:
        errs.append("no road data: reach along roads and paths is underestimated (terrain and land cover still used)")
    return [], "none", errs


def burn(g: Grid, ways: list[dict], cfg: dict) -> tuple[np.ndarray, np.ndarray]:
    """road class per cell (0 none, 1 paved, 2 unpaved) and cliff mask."""
    from skimage.draw import line
    road = np.zeros(g.lat.shape, np.uint8)
    cliff = np.zeros(g.lat.shape, bool)
    paved, unpaved = set(cfg["paved"]), set(cfg["unpaved"]) | {"unknown"}
    ps, us = set(cfg["paved_surfaces"]), set(cfg["unpaved_surfaces"])
    lim = g.size - 1
    for wy in ways:
        t = wy["t"]
        if t.get("natural") == "cliff":
            cls = -1
        elif t.get("highway") in paved or t.get("highway") in unpaved:
            cls = 1 if t["highway"] in paved else 2
            sf = t.get("surface")
            if sf in ps:
                cls = 1
            elif sf in us:
                cls = 2
            if t.get("access") in ("private", "no") and t["highway"] in ("motorway", "trunk"):
                pass  # a lost person still walks along them
        else:
            continue
        pts = wy["g"]
        if len(pts) < 2:
            continue
        r, c = g.to_rc([p[0] for p in pts], [p[1] for p in pts])
        r, c = np.round(r).astype(int), np.round(c).astype(int)
        for i in range(len(r) - 1):
            if (min(r[i], r[i + 1]) > lim or max(r[i], r[i + 1]) < 0 or min(c[i], c[i + 1]) > lim
                    or max(c[i], c[i + 1]) < 0):
                continue
            rr, cc = line(r[i], c[i], r[i + 1], c[i + 1])
            ok = (rr >= 0) & (rr <= lim) & (cc >= 0) & (cc <= lim)
            rr, cc = rr[ok], cc[ok]
            if cls < 0:
                cliff[rr, cc] = True
            else:
                cur = road[rr, cc]
                road[rr, cc] = np.where((cur == 0) | (cls < cur), cls, cur)   # paved wins over unpaved
    return road, cliff


# ---------------------------------------------------------------- cost

def hill_slope(z: np.ndarray, res: float) -> np.ndarray:
    gy, gx = np.gradient(z, res)
    return np.degrees(np.arctan(np.hypot(gx, gy)))


def cell_classes(lc: np.ndarray, road: np.ndarray, cliff: np.ndarray, model: dict):
    """Per cell: index into the terrain class list, extra speed factor, passable flag."""
    names = list(model["terrain"]["classes"])
    cls = np.full(lc.shape, names.index("offroad"), np.int8)
    extra = np.ones(lc.shape, np.float32)
    passable = np.ones(lc.shape, bool)
    for code, d in model["landcover"].items():
        if code.startswith("_"):
            continue
        m = lc == int(code)
        if d["class"] is None:
            passable[m] = False
        else:
            cls[m] = names.index(d["class"])
            extra[m] = d["extra"]
    passable[cliff] = False
    on = road > 0
    cls[road == 1] = names.index("paved")
    cls[road == 2] = names.index("unpaved")
    extra[on] = 1.0
    passable[on] = True                         # bridges, causeways, cliff-side paths
    return cls, extra, passable


def travel_hours(g: Grid, z, cls, extra, passable, pr: P.Profile, model: dict, limit_h: float) -> np.ndarray:
    """Shortest walking time (h, daytime, this person) from the centre cell to every cell."""
    from scipy.sparse import csr_matrix
    from scipy.sparse.csgraph import dijkstra
    coef = np.array([[c["a"], c["b"], c["c"], c["d"]] for c in model["terrain"]["classes"].values()], np.float32)
    phi = hill_slope(z, g.res).astype(np.float32)
    passable = passable & ((phi <= pr.max_hill) | (cls <= 1))        # cls 0/1 = paved/unpaved: paths cross steep ground
    S = g.size
    idx = np.arange(S * S).reshape(S, S)
    src_all, dst_all, w_all = [], [], []
    hs = pr.sens
    max_up = pr.max_climb

    def speed(ci, theta, ph, ex):
        a, b, c, d = coef[ci, 0], coef[ci, 1], coef[ci, 2], coef[ci, 3]
        return pr.speed_factor * np.exp(a + hs * (b * theta + c * theta ** 2 + d * ph)) * ex ** hs

    for dr, dc in NEIGH:
        r0, r1 = max(0, -dr), S - max(0, dr)
        c0, c1 = max(0, -dc), S - max(0, dc)
        a = (slice(r0, r1), slice(c0, c1))
        b = (slice(r0 + dr, r1 + dr), slice(c0 + dc, c1 + dc))
        ok = passable[a] & passable[b]
        if abs(dr) + abs(dc) == 3:               # knight move: the two cells it cuts through
            sr, sc = int(np.sign(dr)), int(np.sign(dc))
            m1 = (dr - sr, dc) if abs(dr) == 2 else (dr, dc - sc)
            m2 = (sr, 0) if abs(dr) == 2 else (0, sc)
            for mr, mc in (m1, m2):
                ok &= passable[r0 + mr:r1 + mr, c0 + mc:c1 + mc]
        dist = g.res * math.hypot(dr, dc)
        theta = np.degrees(np.arctan((z[b] - z[a]) / dist))
        ok &= np.abs(theta) <= max_up
        ph = (phi[a] + phi[b]) / 2
        va = speed(cls[a], theta, ph, extra[a])
        vb = speed(cls[b], theta, ph, extra[b])
        hours = (dist / 2000) / va + (dist / 2000) / vb
        src_all.append(idx[a][ok])
        dst_all.append(idx[b][ok])
        w_all.append(hours[ok].astype(np.float64))
    src, dst, w = np.concatenate(src_all), np.concatenate(dst_all), np.concatenate(w_all)
    log(f"graph: {S}x{S} cells, {len(w):,} edges; shortest paths up to {limit_h:.1f} h")
    G = csr_matrix((w, (src, dst)), shape=(S * S, S * S))
    t, pred = dijkstra(G, directed=True, indices=idx[g.n, g.n], limit=limit_h, return_predecessors=True)
    travel_hours.graph = G                     # kept for transit (multi-source walking after alighting)
    return t.reshape(S, S), pred


# ---------------------------------------------------------------- public transport

def transit_reach(g: Grid, t: np.ndarray, since, hours: float, pr, T_like: float, T_max: float, extra_urls: list[str],
                  cache: Path) -> tuple[np.ndarray | None, dict]:
    """Walking time map when the person may also ride buses / trams / metro (timetables from GTFS)."""
    import transit as TR
    from scipy.sparse import csr_matrix, vstack, hstack
    from scipy.sparse.csgraph import dijkstra
    feeds = TR.find_feeds(g.bbox(), cache) + [{"id": f"user-{i}", "provider": "user", "url": u} for i, u in enumerate(extra_urls)]
    info = {"feeds": [], "source": "Mobility Database catalog + GTFS feeds (licences per feed in 'feeds')"}
    if not feeds:
        info["note"] = "no GTFS feed covers this area; pass --gtfs <zip url> if the city publishes one"
        return None, info
    S = g.size
    src_cells, src_cost, reached = [], [], []
    for f in feeds:
        try:
            zp = TR._get(f["url"], cache / f"{f['id']}.zip", max_age_days=14)
            d = TR.load_connections(zp, since, hours, g.bbox())
        except Exception as ex:  # noqa: BLE001
            info["feeds"].append({**f, "error": str(ex)[:150]})
            continue
        st, conn = d["stops"], d["conn"]
        info["feeds"].append({**f, "stops_in_area": len(st), "connections": int(len(conn))})
        if len(st) == 0 or len(conn) == 0:
            continue
        r, c = g.to_rc(st.stop_lat.to_numpy(), st.stop_lon.to_numpy())
        r, c = np.round(r).astype(int), np.round(c).astype(int)
        inside = (r >= 0) & (r < S) & (c >= 0) & (c < S)
        walk = np.full(len(st), np.inf)
        walk[inside] = t[r[inside], c[inside]]
        arr, used = TR.scan(st, conn, walk, pr.walk_kmh, T_max)
        ok = np.isfinite(arr) & inside & (used <= T_max)
        for i in np.nonzero(ok)[0]:
            src_cells.append(r[i] * S + c[i])
            src_cost.append(used[i])
            reached.append({"stop": st.stop_name.iloc[i] if "stop_name" in st else st.stop_id.iloc[i],
                            "lat": round(float(st.stop_lat.iloc[i]), 6), "lon": round(float(st.stop_lon.iloc[i]), 6),
                            "arrival": (since + timedelta(seconds=float(arr[i]))).strftime("%Y-%m-%d %H:%M"),
                            "walk_used_h": round(float(used[i]), 2), "rode": bool(arr[i] < walk[i] * 3600 - 1),
                            "km": round(float(np.hypot(g.x[r[i], c[i]], g.y[r[i], c[i]])) / 1000, 2), "feed": f["id"]})
    if not src_cells:
        info["note"] = "no stop reachable on foot within the walking budget, or no service in the period"
        return None, info
    G = travel_hours.graph
    N = S * S
    cols = np.array(src_cells)
    row = csr_matrix((np.array(src_cost) + 1e-9, (np.zeros(len(cols), int), cols)), shape=(1, N + 1))
    G2 = vstack([hstack([G, csr_matrix((N, 1))]), row]).tocsr()
    tt = dijkstra(G2, directed=True, indices=N, limit=T_max * 1.001)[:N].reshape(S, S)
    rode = [x for x in reached if x["rode"]]
    rode.sort(key=lambda x: -x["km"])
    info.update({"boardable_or_reached_stops": len(reached), "stops_reached_by_vehicle": len(rode),
                 "farthest_stops_by_vehicle": rode[:15],
                 "farthest_stops_likely": [x for x in rode if x["walk_used_h"] <= T_like][:10],
                 "stops_by_vehicle_likely": sorted(({k: x[k] for k in ("stop", "lat", "lon", "arrival", "km")}
                                                    for x in rode if x["walk_used_h"] <= T_like), key=lambda x: x["arrival"])})
    return np.minimum(t, tt), info


# ---------------------------------------------------------------- distance statistics (rings)

STATS_PATH = Path(__file__).resolve().parent.parent / "data" / "distance_stats.json"
CATEGORY_ALIASES = {"autism": "intellectual_disability", "forager": "hunter_forager", "hunter": "hunter_forager",
                    "shepherd": "hunter_forager"}


def auto_category(age: float, dementia: bool, mobility: str = "normal", injured: bool = False) -> str:
    if dementia:
        return "dementia"
    for hi, c in ((3, "child_1_3"), (6, "child_4_6"), (12, "child_7_12"), (15, "child_13_15")):
        if age <= hi:
            return c
    if age >= 65 or (age >= 50 and (mobility != "normal" or injured)):
        return "elderly"                       # a cane or limp moves like the elderly, not like a hiker
    return "hiker"


def terrain_type(g: Grid, z: np.ndarray, lc: np.ndarray) -> str:
    d = np.hypot(g.x, g.y)
    near5, near1 = d <= 5000, d <= 1000
    if (lc[near1] == 50).mean() > 0.5:
        return "urban"
    mountain = float(z[near5].max() - z[near5].min()) > 305
    dry = (lc[near5] == 60).mean() > 0.3
    return ("dry_" if dry else "") + ("mountain" if mountain else "flat")


def pick_table(cat: str, terrain: str) -> dict:
    tables = json.loads(STATS_PATH.read_text())["categories"][cat]
    for want in (terrain, terrain.replace("dry_", ""), "any"):
        for t in tables:
            if t["terrain"] == want:
                return t
    return tables[0]


def percentile_km(p: dict, q: float) -> float | None:
    pts = sorted((float(k), float(v)) for k, v in p.items())
    pts = [(0.0, 0.0)] + pts
    if q > pts[-1][0]:
        return None
    for (q0, v0), (q1, v1) in zip(pts, pts[1:]):
        if q <= q1:
            return v0 + (v1 - v0) * (q - q0) / (q1 - q0)
    return None


# ---------------------------------------------------------------- outputs

def contours(g: Grid, t: np.ndarray, level: float) -> list[list[list[float]]]:
    from skimage import measure
    tt = np.where(np.isfinite(t), t, level * 10 + 1)
    lines = []
    for cline in measure.find_contours(tt, level):
        la, lo = g.to_ll(cline[:, 0], cline[:, 1])
        lines.append([[round(float(x), 6), round(float(y), 6)] for x, y in zip(lo, la)])
    return lines


DIRS = {"N": 0, "NE": 45, "E": 90, "SE": 135, "S": 180, "SW": 225, "W": 270, "NW": 315}


def parse_bearing(s: str) -> float:
    s = s.strip().upper()
    tr = {"K": "N", "G": "S", "D": "E", "B": "W", "KD": "NE", "KB": "NW", "GD": "SE", "GB": "SW"}   # Turkish
    s = tr.get(s, s)
    return float(DIRS[s]) if s in DIRS else float(s) % 360


def route(g: Grid, pred: np.ndarray, r: int, c: int) -> list[list[float]]:
    """Walking route from the last seen point to cell (r, c), as [lon, lat] pairs."""
    S, cells, k = g.size, [], r * g.size + c
    while k >= 0 and len(cells) < S * S:
        cells.append(k)
        k = pred[k]
    cells = np.array(cells[::-1])
    la, lo = g.to_ll(cells // S, cells % S)
    return [[round(float(x), 6), round(float(y), 6)] for x, y in zip(lo, la)]


def farthest(g: Grid, t: np.ndarray, pred: np.ndarray, level: float, bearing: float, half: float = 22.5) -> dict | None:
    """Farthest reachable cell (straight-line) within bearing ± half, with its walking route."""
    br = np.degrees(np.arctan2(g.x, g.y)) % 360
    m = (t <= level) & (np.abs((br - bearing + 180) % 360 - 180) <= half)
    if not m.any():
        return None
    d = np.where(m, np.hypot(g.x, g.y), -1)
    r, c = np.unravel_index(int(np.argmax(d)), d.shape)
    rt = route(g, pred, r, c)
    seg = np.array(rt)
    rkm = float(np.sum(np.hypot(np.diff(seg[:, 0]) * g.m_lon, np.diff(seg[:, 1]) * M_LAT))) / 1000 if len(seg) > 1 else 0
    la, lo = g.to_ll(r, c)
    return {"bearing": bearing, "lat": round(float(la), 6), "lon": round(float(lo), 6),
            "straight_km": round(float(d[r, c]) / 1000, 2), "route_km": round(rkm, 2),
            "walking_hours": round(float(t[r, c]), 2), "route": rt}


def stats(g: Grid, t: np.ndarray, level: float) -> dict:
    m = t <= level
    if not m.any():
        return {"area_km2": 0}
    d = np.hypot(g.x, g.y)[m] / 1000
    br = (np.degrees(np.arctan2(g.x, g.y)) % 360)[m]
    far = {}
    for i, name in enumerate(["N", "NE", "E", "SE", "S", "SW", "W", "NW"]):
        sel = np.abs(((br - i * 45) + 180) % 360 - 180) <= 22.5
        far[name] = round(float(d[sel].max()), 2) if sel.any() else 0
    edge = m[0, :].any() or m[-1, :].any() or m[:, 0].any() or m[:, -1].any()
    return {"area_km2": round(float(m.sum()) * g.res ** 2 / 1e6, 2), "max_km": round(float(d.max()), 2),
            "median_km": round(float(np.median(d)), 2), "farthest_km_by_direction": far,
            "touches_grid_edge": bool(edge)}


def hillshade(z: np.ndarray, res: float) -> np.ndarray:
    gy, gx = np.gradient(z, res)
    slope = np.arctan(np.hypot(gx, gy))
    aspect = np.arctan2(-gx, gy)
    az, alt = math.radians(315), math.radians(45)
    hs = np.sin(alt) * np.cos(slope) + np.cos(alt) * np.sin(slope) * np.cos(az - aspect)
    return np.clip(hs, 0, 1)


LC_RGB = {10: (60, 120, 60), 20: (150, 140, 70), 30: (170, 200, 110), 40: (220, 210, 140), 50: (180, 90, 90),
          60: (200, 190, 170), 70: (240, 240, 250), 80: (90, 140, 210), 90: (110, 170, 170), 95: (40, 110, 90),
          100: (190, 200, 160), 0: (200, 200, 200)}


def render(g: Grid, z, lc, road, cliff, t, levels: dict, path: Path, title: str, points: list | None = None,
           rings: list | None = None, t_tr: np.ndarray | None = None, stops: list | None = None) -> None:
    hs = hillshade(z, g.res)
    base = np.zeros(z.shape + (3,), np.float32)
    for k, rgb in LC_RGB.items():
        base[lc == k] = rgb
    img = base * (0.45 + 0.55 * hs[..., None])
    img[road == 1] = (40, 40, 40)
    img[road == 2] = (130, 60, 20)
    img[cliff] = (200, 0, 0)
    out = img.copy()
    like, mx = levels["likely"], levels["max"]
    inmax = np.isfinite(t) & (t <= mx)
    inlike = np.isfinite(t) & (t <= like)
    out[inmax & ~inlike] = out[inmax & ~inlike] * 0.65 + np.array([255, 170, 0]) * 0.35
    out[inlike] = out[inlike] * 0.55 + np.array([230, 30, 30]) * 0.45
    im = Image.fromarray(out.clip(0, 255).astype(np.uint8))
    scale = max(1, int(round(1100 / g.size)))
    if scale > 1:
        im = im.resize((g.size * scale, g.size * scale), Image.NEAREST)
    elif g.size > 1400:
        im = im.resize((1400, 1400), Image.LANCZOS)
    k = im.size[0] / g.size
    d = ImageDraw.Draw(im)
    from skimage import measure
    for lv, col, wdt in ((like, (160, 0, 0), 2), (mx, (200, 110, 0), 2)):
        tt = np.where(np.isfinite(t), t, 1e9)
        for cl in measure.find_contours(tt, lv):
            d.line([(p[1] * k, p[0] * k) for p in cl], fill=col, width=wdt)
    if t_tr is not None:                            # extra area reachable only by riding (within the likely walking)
        extra_m = np.isfinite(t_tr) & (t_tr <= like) & ~(np.isfinite(t) & (t <= like))
        ov = np.zeros(extra_m.shape + (4,), np.uint8)
        ov[extra_m] = (150, 0, 200, 70)
        ovi = Image.fromarray(ov, "RGBA").resize(im.size, Image.NEAREST)
        im.paste(ovi, (0, 0), ovi)
        d = ImageDraw.Draw(im)
        for x in stops or []:
            rr, cc = g.to_rc(x["lat"], x["lon"])
            px, py = float(cc) * k + k / 2, float(rr) * k + k / 2
            d.ellipse([px - 3, py - 3, px + 3, py + 3], fill=(150, 0, 200), outline=(255, 255, 255))
    for q, km in rings or []:                      # plain circles; the reach-clipped rings are in reach.geojson
        rad = km * 1000 / g.res * k
        cx = g.n * k + k / 2
        d.ellipse([cx - rad, cx - rad, cx + rad, cx + rad], outline=(30, 60, 220), width=2 if q in (50, 95) else 1)
        d.text((cx + rad * 0.71 + 3, cx - rad * 0.71 - 16), f"{q}%", fill=(30, 60, 220))
    for h in levels["hourly"]:
        tt = np.where(np.isfinite(t), t, 1e9)
        for cl in measure.find_contours(tt, h):
            d.line([(p[1] * k, p[0] * k) for p in cl], fill=(255, 255, 255), width=1)
    for p in points or []:
        rr, cc = g.to_rc(p["lat"], p["lon"])
        px, py = float(cc) * k + k / 2, float(rr) * k + k / 2
        d.ellipse([px - 4, py - 4, px + 4, py + 4], fill=(255, 255, 255), outline=(0, 0, 0))
    c = g.n * k + k / 2
    d.ellipse([c - 7, c - 7, c + 7, c + 7], fill=(255, 255, 0), outline=(0, 0, 0), width=2)
    # scale bar
    km = max(1, int(round(g.size * g.res / 1000 / 5)))
    L = km * 1000 / g.res * k
    y0 = im.size[1] - 30
    d.rectangle([20, y0 - 4, 30 + L + 60, y0 + 18], fill=(255, 255, 255))
    d.line([(25, y0 + 8), (25 + L, y0 + 8)], fill=(0, 0, 0), width=4)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 15)
    except Exception:  # noqa: BLE001
        font = ImageFont.load_default()
    d.text((30 + L, y0), f"{km} km", fill=(0, 0, 0), font=font)
    d.text((im.size[0] - 380, im.size[1] - 22), "estimate for search planning - decision support only",
           fill=(255, 255, 255), font=font, stroke_width=2, stroke_fill=(0, 0, 0))
    lines = [title, f"red: likely limit ({like:.1f} h walking)", f"orange: max limit ({mx:.1f} h walking)",
             "white: walking time 1, 2, 4, 8 ... h", "blue: found-distance rings 25/50/75/95 % (statistics)",
             *(["purple: extra area / stops if they rode a bus, tram or metro"] if t_tr is not None else []),
             "N up; black = paved, brown = track/path, red = cliff"]
    d.rectangle([10, 10, 470, 18 + 19 * len(lines)], fill=(255, 255, 255))
    for i, s in enumerate(lines):
        d.text((16, 14 + 19 * i), s, fill=(0, 0, 0), font=font)
    im.save(path, quality=90)


def render_route(g: Grid, z, lc, road, excess, info: dict, path: Path, title: str) -> None:
    hs = hillshade(z, g.res)
    base = np.zeros(z.shape + (3,), np.float32)
    for k_, rgb in LC_RGB.items():
        base[lc == k_] = rgb
    img = base * (0.45 + 0.55 * hs[..., None])
    img = img * 0.55 + 255 * 0.45 * hs[..., None]               # wash out so the corridor stands out
    img[road == 1] = (60, 60, 60)
    img[road == 2] = (120, 80, 50)
    ex = np.where(np.isfinite(excess), excess, 9)
    for b, col, al in ((0.75, (255, 220, 0), 0.30), (0.35, (255, 140, 0), 0.45), (0.15, (220, 30, 30), 0.55)):
        m = ex <= b
        img[m] = img[m] * (1 - al) + np.array(col) * al
    im = Image.fromarray(img.clip(0, 255).astype(np.uint8))
    scale = max(1, int(round(1000 / g.size)))
    im = im.resize((g.size * scale, g.size * scale), Image.NEAREST) if scale > 1 else im.resize((1000, 1000), Image.LANCZOS)
    k = im.size[0] / g.size
    d = ImageDraw.Draw(im)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 15)
        big = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 16)
    except Exception:  # noqa: BLE001
        font = big = ImageFont.load_default()
    pts = []
    for lo_, la_ in info["route"]:
        r_, c_ = g.to_rc(la_, lo_)
        pts.append((float(c_) * k + k / 2, float(r_) * k + k / 2))
    d.line(pts, fill=(20, 60, 230), width=5)
    for i in range(1, 6):                                      # arrowheads along the route
        j = int(len(pts) * i / 6)
        if 0 < j < len(pts):
            (x0, y0), (x1, y1) = pts[max(0, j - 3)], pts[j]
            ang = math.atan2(y1 - y0, x1 - x0)
            tip = (x1, y1)
            l_, w_ = 16, 9
            a1 = (x1 - l_ * math.cos(ang) + w_ * math.sin(ang), y1 - l_ * math.sin(ang) - w_ * math.cos(ang))
            a2 = (x1 - l_ * math.cos(ang) - w_ * math.sin(ang), y1 - l_ * math.sin(ang) + w_ * math.cos(ang))
            d.polygon([tip, a1, a2], fill=(20, 60, 230))
    for b in info["branches"][:6]:
        r_, c_ = g.to_rc(b["far_point"]["lat"], b["far_point"]["lon"])
        x, y = float(c_) * k, float(r_) * k
        d.text((x - 8, y - 10), b["id"], fill=(255, 255, 255), font=big, stroke_width=3, stroke_fill=(120, 40, 0))
    for nm, col in (("position_likely", (255, 255, 255)), ("position_max", (0, 0, 0))):
        p_ = info[nm]
        r_, c_ = g.to_rc(p_["lat"], p_["lon"])
        x, y = float(c_) * k + k / 2, float(r_) * k + k / 2
        d.rectangle([x - 6, y - 6, x + 6, y + 6], fill=col, outline=(20, 60, 230), width=2)
    x0, y0 = pts[0]
    d.ellipse([x0 - 8, y0 - 8, x0 + 8, y0 + 8], fill=(255, 255, 0), outline=(0, 0, 0), width=2)
    x1, y1 = pts[-1]
    d.polygon([(x1, y1), (x1, y1 - 26), (x1 + 18, y1 - 20), (x1, y1 - 14)], fill=(20, 160, 60), outline=(0, 0, 0))
    km = max(1, int(round(g.size * g.res / 1000 / 5)))
    L = km * 1000 / g.res * k
    yb = im.size[1] - 30
    d.rectangle([20, yb - 4, 30 + L + 60, yb + 18], fill=(255, 255, 255))
    d.line([(25, yb + 8), (25 + L, yb + 8)], fill=(0, 0, 0), width=4)
    d.text((30 + L, yb), f"{km} km", fill=(0, 0, 0), font=font)
    lines = [title + " - heading for a destination",
             f"blue arrow: best route {info['best_route_km']} km, {info['best_route_walking_h']} h walking",
             "red / orange / yellow: routes up to 15 / 35 / 75 % longer (where a lost person drifts)",
             "B1, B2 ...: branches that leave the best route; squares: position by now (white likely, black max)"]
    d.rectangle([10, 10, 700, 18 + 19 * len(lines)], fill=(255, 255, 255))
    for i, s_ in enumerate(lines):
        d.text((16, 14 + 19 * i), s_, fill=(0, 0, 0), font=font)
    d.text((im.size[0] - 380, im.size[1] - 22), "estimate for search planning - decision support only",
           fill=(255, 255, 255), font=font, stroke_width=2, stroke_fill=(0, 0, 0))
    im.save(path, quality=90)


def view_crop(g: Grid, view_km: float | None):
    """A smaller grid around the same centre for drawing, and a function that cuts arrays to it."""
    if not view_km or view_km * 1000 >= g.n * g.res:
        return g, lambda arr: arr
    v = int(math.ceil(view_km * 1000 / g.res))
    vg = Grid(g.lat0, g.lon0, v * g.res, g.res)
    sl = slice(g.n - vg.n, g.n + vg.n + 1)
    return vg, lambda arr: arr[sl, sl]


def render_priority(g, z, lc, road, P, secs, t, T_like, T_max, path: Path, title: str, top: int = 15) -> None:
    hs = hillshade(z, g.res)
    gray = (60 + 150 * hs)[..., None].repeat(3, -1)
    gray[road == 1] = (30, 30, 30)
    gray[road == 2] = (110, 70, 40)
    pos = P[P > 0]
    p99 = np.percentile(pos, 99) if pos.size else 1.0
    v = np.clip(P / p99, 0, 1) ** 0.6                          # scale to the 99th percentile so the tail stays visible
    heat = np.stack([255 * np.clip(v * 2, 0, 1), 255 * np.clip(v * 2 - 0.6, 0, 1), 40 * (1 - v)], -1)
    a = np.clip(v * 1.6, 0, 0.85)[..., None]
    img = gray * (1 - a) + heat * a
    im = Image.fromarray(img.clip(0, 255).astype(np.uint8))
    scale = max(1, int(round(1000 / g.size)))
    im = im.resize((g.size * scale, g.size * scale), Image.NEAREST) if scale > 1 else im.resize((1000, 1000), Image.LANCZOS)
    k = im.size[0] / g.size
    d = ImageDraw.Draw(im)
    from skimage import measure
    tt = np.where(np.isfinite(t), t, 1e9)
    for lv, col in ((T_like, (200, 0, 0)), (T_max, (230, 130, 0))):
        for cl in measure.find_contours(tt, lv):
            d.line([(p[1] * k, p[0] * k) for p in cl], fill=col, width=2)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 14)
        big = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 15)
    except Exception:  # noqa: BLE001
        font = big = ImageFont.load_default()
    for s_ in secs[:top]:
        la0, lo0, la1, lo1 = s_["box"]
        r0, c0 = g.to_rc(la1, lo0)
        r1, c1 = g.to_rc(la0, lo1)
        box = [float(c0) * k, float(r0) * k, float(c1) * k, float(r1) * k]
        d.rectangle(box, outline=(0, 90, 255), width=2)
        d.text((box[0] + 3, box[1] + 2), str(s_["rank"]), fill=(255, 255, 255), font=big, stroke_width=2, stroke_fill=(0, 0, 0))
    c = g.n * k + k / 2
    d.ellipse([c - 7, c - 7, c + 7, c + 7], fill=(255, 255, 0), outline=(0, 0, 0), width=2)
    km = max(1, int(round(g.size * g.res / 1000 / 5)))
    L = km * 1000 / g.res * k
    y0 = im.size[1] - 30
    d.rectangle([20, y0 - 4, 30 + L + 60, y0 + 18], fill=(255, 255, 255))
    d.line([(25, y0 + 8), (25 + L, y0 + 8)], fill=(0, 0, 0), width=4)
    d.text((30 + L, y0), f"{km} km", fill=(0, 0, 0), font=font)
    d.text((im.size[0] - 380, im.size[1] - 22), "estimate for search planning - decision support only",
           fill=(255, 255, 255), font=font, stroke_width=2, stroke_fill=(0, 0, 0))
    cum = secs[min(top, len(secs)) - 1]["cum_poa"] if secs else 0
    lines = [title, "colour: probability per km² (yellow = highest)",
             f"blue boxes: top {min(top, len(secs))} search sectors, numbered in order ({cum:.0%} of the probability)",
             "red: likely limit, orange: physical limit"]
    d.rectangle([10, 10, 620, 18 + 19 * len(lines)], fill=(255, 255, 255))
    for i, s_ in enumerate(lines):
        d.text((16, 14 + 19 * i), s_, fill=(0, 0, 0), font=font)
    im.save(path, quality=90)


# ---------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--at", required=True, help="last seen point lat,lon")
    P.add_args(ap)
    ap.add_argument("--out", required=True, help="output directory")
    ap.add_argument("--res", type=float, help="grid cell size in m (default: chosen from the area, 25-250 m)")
    ap.add_argument("--max-cells", type=int, default=1000, help="grid side limit when choosing --res (default 1000)")
    ap.add_argument("--roads", choices=["auto", "overture", "overpass", "none"], default="auto",
                    help="road/path source: auto = Overture (S3), then OSM Overpass, then none with a warning")
    ap.add_argument("--no-osm", action="store_true", help="same as --roads none")
    ap.add_argument("--toward", action="append", default=[],
                    help="direction to report the farthest point and route for: N NE E SE S SW W NW (or K G D B KD KB GD GB) "
                         "or a bearing in degrees; repeatable. Default: all eight")
    ap.add_argument("--sector", type=float, default=22.5, help="half-width of the direction sector in degrees (default 22.5)")
    ap.add_argument("--conditions", type=int, default=14, metavar="DAYS",
                    help="use the clearest Sentinel-2 scene of the DAYS before --since for snow and new water (0 = off; default 14)")
    ap.add_argument("--category", choices=["auto", "child_1_3", "child_4_6", "child_7_12", "child_13_15", "dementia",
                                           "elderly", "hiker", "hunter", "forager", "shepherd", "despondent",
                                           "mental_illness", "intellectual_disability", "autism", "any"], default="auto",
                    help="behaviour category for the distance rings (default: from age and --dementia)")
    ap.add_argument("--no-render", action="store_true", help="skip reach.png (faster; used by calibrate.py)")
    ap.add_argument("--transit", choices=["auto", "yes", "no"], default="auto",
                    help="public transport (GTFS timetables): auto = on in built-up areas for ages 7+ when a feed covers them")
    ap.add_argument("--gtfs", action="append", default=[], help="extra GTFS zip URL (city not in the Mobility Database catalog)")
    ap.add_argument("--transit-km", type=float, default=20, help="area radius for transit (default 20 km)")
    ap.add_argument("--to", help="destination lat,lon the person was heading for: draws the best route and the corridor "
                                  "of near-best routes instead of only circles (route.png)")
    ap.add_argument("--grid-km", type=float, help="compute only within this radius (finer cells for the likely zone; "
                                                 "the physical limit is then cut at the edge and flagged)")
    ap.add_argument("--view-km", type=float, help="draw the maps only out to this radius (computation still covers everything)")
    ap.add_argument("--no-priority", action="store_true", help="skip the probability map, sectors and weather")
    ap.add_argument("--sector-km", type=float, help="search sector size in km (default: chosen so ~40 sectors hold 80 %% of the probability)")
    ap.add_argument("--gpx-top", type=int, default=15, help="write GPX files for the top N sectors (default 15)")
    ap.add_argument("--cache", default=".sar-cache")
    a = ap.parse_args()

    model = P.load_model()
    lat0, lon0 = map(float, a.at.split(","))
    since, warn = P.parse_time(a.since, lon0)
    if warn:
        log("WARNING:", warn)
    pr = P.build_profile(P.person_from_args(a), model)
    bud = P.budget(pr, since, a.hours, lat0, lon0)
    T_like, T_max = bud["likely"]["walk_equiv_hours"], bud["max"]["walk_equiv_hours"]
    # farthest possible: flat paved road at the person's best speed (Wood peaks ~1.1x flat on a gentle descent)
    reach_m = pr.walk_kmh * 1.15 * T_max * 1000 + 1000
    cache = Path(a.cache)
    use_transit, transit_why = False, "off (--transit no)"
    if a.transit != "no":
        if a.age < 7 and a.transit == "auto":
            transit_why = "off: under 7 (unlikely to board alone; --transit yes to force)"
        else:
            g0 = Grid(lat0, lon0, 1000, 50)
            built = float((landcover(g0, cache / "worldcover") == 50).mean())
            if a.transit == "yes" or built >= 0.25:
                use_transit, transit_why = True, f"on (built-up {built:.0%} within 1 km)"
                reach_m = max(reach_m, a.transit_km * 1000)
            else:
                transit_why = f"off: not urban (built-up {built:.0%} within 1 km; --transit yes to force)"
    log(f"transit: {transit_why}")
    if a.to:
        tl, to_ = map(float, a.to.split(","))
        d_xy = math.hypot((tl - lat0) * M_LAT, (to_ - lon0) * M_LON_EQ * math.cos(math.radians(lat0)))
        reach_m = max(reach_m, 1.6 * d_xy + 1500)       # room for the destination and detours around it
    if a.grid_km:
        reach_m = a.grid_km * 1000                    # user-limited area: the physical limit may run off the edge
    res = a.res or float(min(250, max(25, math.ceil(2 * reach_m / a.max_cells / 5) * 5)))
    g = Grid(lat0, lon0, reach_m, res)
    log(f"person: {pr.walk_kmh:.2f} km/h flat, slope sensitivity {pr.sens:.2f}, max climb {pr.max_climb:.0f} deg")
    log(f"budget: likely {T_like:.2f} h, max {T_max:.2f} h of daytime walking; grid {g.size}x{g.size} at {res:.0f} m "
        f"({2 * reach_m / 1000:.0f} km across)")

    z = elevation(g, cache / "terrain")
    lc = landcover(g, cache / "worldcover")
    ways, road_src, road_warn = road_data(g, cache / "osm", model["osm_ways"], "none" if a.no_osm else a.roads)
    road, cliff = burn(g, ways, model["osm_ways"])
    nways = len(ways)
    log(f"roads: {nways} ways from {road_src}")
    cond_info = {"off": True}
    if a.conditions > 0:
        scl, cond_info = conditions(g, since, a.conditions, cache / "s2")
        if scl is not None:
            snow = (scl == SCL_SNOW) & (lc != 70)
            if snow.sum() < 0.005 * max(1, int((scl > 0).sum())):
                cond_info["snow_ignored"] = f"{int(snow.sum())} snow cells (<0.5% of clear cells): treated as misclassified bright ground"
                snow[:] = False
            water = (scl == SCL_WATER) & (lc != 80)
            lc = lc.copy()
            lc[snow] = 70                                   # current snow: slow like snow and ice
            lc[water] = 80                                  # water now where WorldCover had land: flooded, impassable
            cond_info["applied"] = {"snow_cells": int(snow.sum()), "new_water_cells": int(water.sum())}
            log(f"conditions: {cond_info['scenes'][:3]} clear {cond_info['clear_share']:.0%}, "
                f"snow {snow.sum()} cells, new water {water.sum()} cells")
        else:
            log(f"conditions: {cond_info.get('error')}")
    cls, extra, passable = cell_classes(lc, road, cliff, model)
    if not passable[g.n, g.n]:
        log("WARNING: the last seen point falls on water or a cliff cell; moving it to the nearest passable cell")
        rr, cc = np.nonzero(passable)
        k = np.argmin((rr - g.n) ** 2 + (cc - g.n) ** 2)
        passable[g.n, g.n] = True
        cls[g.n, g.n], extra[g.n, g.n] = cls[rr[k], cc[k]], extra[rr[k], cc[k]]
    limit_h = T_max * 1.001
    if a.to:
        limit_h = max(limit_h, 6 * (d_xy / 1000) / max(pr.walk_kmh, 0.5) + 1)   # the destination must be reached
    t, pred = travel_hours(g, z, cls, extra, passable, pr, model, limit_h)
    t_tr, transit_info = None, {"status": transit_why}
    if use_transit:
        t_tr, transit_info = transit_reach(g, t, since, a.hours, pr, T_like, T_max, a.gtfs, cache / "gtfs")
        transit_info["status"] = transit_why

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    route_info, route_arr = None, {}
    if a.to:
        import route as RT
        route_info, route_arr = RT.corridor(g, travel_hours.graph, t, pred, (tl, to_), T_like, T_max, z_arr=z)
        if "excess" in route_arr:
            np.save(out / "excess.npy", route_arr["excess"].astype(np.float32))
    hourly = [h for h in (0.5, 1, 2, 4, 8, 12, 16, 24) if h < T_max]
    levels = {"likely": T_like, "max": T_max, "hourly": hourly}
    feats = []
    for name, lv in [("likely", T_like), ("max", T_max)] + [(f"{h:g} h", h) for h in hourly]:
        ls = contours(g, t, lv)
        if ls:
            feats.append({"type": "Feature", "properties": {"name": name, "walking_hours": round(lv, 2)},
                          "geometry": {"type": "MultiLineString", "coordinates": ls}})
    cat = auto_category(a.age, a.dementia, a.mobility, a.injured) if a.category == "auto" else CATEGORY_ALIASES.get(a.category, a.category)
    terr = terrain_type(g, z, lc)
    tab = pick_table(cat, terr)
    dist_km = np.hypot(g.x, g.y) / 1000
    reach_max = np.isfinite(t) & (t <= T_max)
    rings = {"category": cat, "terrain": terr, "table_terrain": tab["terrain"], "source": tab["src"], "n": tab["n"],
             "quality": tab["quality"], "km": {}, "area_km2": {}, "reachable_share": {}}
    ring_lines = []
    for q in (25, 50, 75, 95):
        km = percentile_km(tab["p"], q)
        if km is None:
            continue
        circ = dist_km <= km
        m = circ & reach_max
        rings["km"][str(q)] = round(km, 2)
        rings["area_km2"][str(q)] = round(float(m.sum()) * g.res ** 2 / 1e6, 2)
        rings["reachable_share"][str(q)] = round(float(m.sum() / max(1, circ.sum())), 3)
        ls = contours(g, np.where(m, 0.0, 1.0), 0.5)
        if ls:
            feats.append({"type": "Feature", "properties": {"name": f"{q}% ring ({cat}, {terr})", "km": round(km, 2),
                                                            "source": tab["src"]},
                          "geometry": {"type": "MultiLineString", "coordinates": ls}})
        ring_lines.append((q, km))
    if t_tr is not None:
        transit_info["likely"] = stats(g, t_tr, T_like)
        transit_info["max"] = stats(g, t_tr, T_max)
        for name, lv in (("with transit: likely", T_like), ("with transit: max", T_max)):
            ls = contours(g, t_tr, lv)
            if ls:
                feats.append({"type": "Feature", "properties": {"name": name, "walking_hours": round(lv, 2)},
                              "geometry": {"type": "MultiLineString", "coordinates": ls}})
        for x in transit_info.get("farthest_stops_by_vehicle", []):
            feats.append({"type": "Feature", "properties": {"name": f"stop reachable by vehicle: {x['stop']}", **x},
                          "geometry": {"type": "Point", "coordinates": [x["lon"], x["lat"]]}})
        np.save(out / "time_transit.npy", t_tr.astype(np.float32))
    if route_info and "route" in route_info:
        feats.append({"type": "Feature", "properties": {"name": "best route to destination",
                                                        "walking_h": route_info["best_route_walking_h"], "km": route_info["best_route_km"]},
                      "geometry": {"type": "LineString", "coordinates": route_info["route"]}})
        for b in (0.15, 0.35, 0.75):
            ex = route_arr["excess"]
            ls = contours(g, np.where(np.isfinite(ex), ex, 9.0), b)
            if ls:
                feats.append({"type": "Feature", "properties": {"name": f"corridor: routes up to {int(b * 100)} % longer"},
                              "geometry": {"type": "MultiLineString", "coordinates": ls}})
        for b in route_info["branches"]:
            feats.append({"type": "Feature", "properties": {"name": f"branch {b['id']}", **{k: v for k, v in b.items() if k != 'far_point'}},
                          "geometry": {"type": "Point", "coordinates": [b["far_point"]["lon"], b["far_point"]["lat"]]}})
        for nm in ("position_likely", "position_max"):
            p_ = route_info[nm]
            feats.append({"type": "Feature", "properties": {"name": nm.replace("_", " "), **p_},
                          "geometry": {"type": "Point", "coordinates": [p_["lon"], p_["lat"]]}})
    bearings = [(x, parse_bearing(x)) for x in a.toward] or list(DIRS.items())
    toward = {}
    for name, b in bearings:
        toward[str(name)] = {}
        for sc, lv in (("likely", T_like), ("max", T_max)):
            f = farthest(g, t, pred, lv, b, a.sector)
            toward[str(name)][sc] = f and {k: v for k, v in f.items() if k != "route"}
            if f:
                feats.append({"type": "Feature", "properties": {"name": f"farthest {name} ({sc})", **{k: v for k, v in f.items() if k != "route"}},
                              "geometry": {"type": "Point", "coordinates": [f["lon"], f["lat"]]}})
                feats.append({"type": "Feature", "properties": {"name": f"route {name} ({sc})", "route_km": f["route_km"]},
                              "geometry": {"type": "LineString", "coordinates": f["route"]}})
    feats.append({"type": "Feature", "properties": {"name": "last seen"},
                  "geometry": {"type": "Point", "coordinates": [lon0, lat0]}})
    (out / "reach.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": feats}))
    np.save(out / "time.npy", t.astype(np.float32))
    (out / "grid.json").write_text(json.dumps(g.meta(), indent=1))
    rep = {
        "notice": ("Estimate for search planning (decision support). Zones show where the person could be, calibrated on "
                   "a limited set of solved cases; local knowledge and field information take precedence."),
        "last_seen": {"lat": lat0, "lon": lon0, "time": since.isoformat(), "hours_since": a.hours},
        "person": asdict(P.person_from_args(a)), "profile": asdict(pr), "budget": bud,
        "grid": g.meta(), "conditions": cond_info, "roads": {"source": road_src, "ways": nways},
        "start_cell": {"elevation_m": round(float(z[g.n, g.n]), 1),
                       "landcover": int(lc[g.n, g.n]), "on_road": int(road[g.n, g.n])},
        "rings": rings, "transit": transit_info, "heading": route_info,
        "likely": stats(g, t, T_like), "max": stats(g, t, T_max), "toward": toward,
        "hourly": {f"{h:g}": stats(g, t, h) for h in hourly},
        "warnings": [w for w in [warn] if w] + road_warn,
        "model": "data/speed_model.json (see the _source of each block; 'ASSUMPTION' blocks are uncalibrated)",
    }
    if rep["max"].get("touches_grid_edge"):
        rep["warnings"].append("max contour touches the grid edge: raise --max-cells or --res")
    (out / "reach.json").write_text(json.dumps(rep, indent=1, ensure_ascii=False))
    title = f"age {a.age:g} {a.sex} {a.mobility}{' injured' if a.injured else ''}{' dementia' if a.dementia else ''}, {a.hours:g} h since last seen"
    if not a.no_priority:
        import priority as PR
        prob, pinfo = PR.probability(g, z, lc, road, t, T_like, T_max, tab, cat, model["priority"], percentile_km)
        np.save(out / "prob.npy", prob.astype(np.float32))
        secs, skm = PR.sectors(g, prob, t, T_max, a.sector_km, 40)
        PR.write_sectors(out, secs, a.gpx_top)
        wx = PR.weather(lat0, lon0, since, a.hours)
        rep["priority"] = {**pinfo, "sector_km": skm, "sectors": len(secs),
                           "top": [{k: s_[k] for k in ("rank", "poa", "cum_poa", "area_km2", "box")} for s_ in secs[:10]]}
        rep["weather"] = wx
        (out / "reach.json").write_text(json.dumps(rep, indent=1, ensure_ascii=False))
        n50 = next((s_["rank"] for s_ in secs if s_["cum_poa"] >= 0.5), None)
        print(f"priority: {len(secs)} sectors of {skm} km; top 5 hold {secs[min(4, len(secs) - 1)]['cum_poa']:.0%}, "
              f"{n50} sectors reach 50 % -> sectors.geojson, sectors.csv, gpx/")
        if "flags" in wx:
            print(f"weather: {wx['temp_min_c']}..{wx['temp_max_c']} C, rain {wx['rain_mm']} mm, snow {wx['snow_cm']} cm"
                  + (" | " + "; ".join(wx["flags"]) if wx["flags"] else ""))
        if not a.no_render:
            vg, cut = view_crop(g, a.view_km)
            render_priority(vg, cut(z), cut(lc), cut(road), cut(prob), secs, cut(t), T_like, T_max, out / "priority.png",
                            f"{title} - search priority ({cat})")
    if route_info and "route" in route_info and not a.no_render:
        vg, cut = view_crop(g, a.view_km)
        render_route(vg, cut(z), cut(lc), cut(road), cut(route_arr["excess"]), route_info, out / "route.png", title)
        hi = route_info
        print(f"heading: best route {hi['best_route_km']} km, {hi['best_route_walking_h']} h walking; by now likely "
              f"{hi['position_likely']['km_along_route']} km along ({'arrived' if hi['position_likely']['arrived'] else 'not arrived'}); "
              f"{len(hi['branches'])} branches -> route.png")
    if not a.no_render:
        vg, cut = view_crop(g, a.view_km)
        render(vg, cut(z), cut(lc), cut(road), cut(cliff), cut(t), levels, out / "reach.png", title,
               [v["max"] for v in toward.values() if v["max"]] + [v["likely"] for v in toward.values() if v["likely"]],
               ring_lines, None if t_tr is None else cut(t_tr), transit_info.get("stops_by_vehicle_likely", []))
    print(json.dumps({k: rep[k] for k in ("budget", "likely", "max", "warnings")}, indent=1, ensure_ascii=False))
    if t_tr is not None:
        ti = transit_info
        print(f"transit: {ti.get('stops_reached_by_vehicle', 0)} stops reachable by vehicle; with transit the likely zone reaches "
              f"{ti['likely'].get('max_km', 0)} km, max {ti['max'].get('max_km', 0)} km")
        for x in ti.get("farthest_stops_likely", [])[:5]:
            print(f"   {x['km']:5.1f} km  {x['stop']}  arrive {x['arrival']}  (walked {x['walk_used_h']} h)")
    print(f"found-distance rings ({cat}, {terr} terrain, {tab['src']}, n={tab['n']}): "
          + ", ".join(f"{q}% {v} km" for q, v in rings["km"].items()))
    print("farthest point by direction (straight km / walking-route km):")
    for name, v in toward.items():
        cells = [f"{sc} {v[sc]['straight_km']:.1f}/{v[sc]['route_km']:.1f} km @ {v[sc]['lat']:.5f},{v[sc]['lon']:.5f}"
                 if v[sc] else f"{sc} -" for sc in ("likely", "max")]
        print(f"  {name:>4}: " + " | ".join(cells))
    print(f"-> {out}/reach.png, reach.geojson, reach.json, time.npy")


if __name__ == "__main__":
    main()
