# /// script
# requires-python = ">=3.10"
# dependencies = ["numpy", "scipy", "pillow", "rasterio", "scikit-image", "duckdb", "pandas"]
# ///
"""Access after an earthquake: which roads are blocked, and how long it now takes to reach each neighbourhood.

  access.py --ems EMSR648 --aoi 18 --out out/kirikhan                 # Copernicus EMS grading product, AOI 18
  access.py --ems-zip product_vector.zip --from 36.51,36.36 --out out/x  # a downloaded package, own entry points

Data (no key): Copernicus Emergency Management Service rapid-mapping grading products (vector packages on the public
CEMS S3 bucket): every building graded (no visible damage / possibly damaged / damaged / destroyed) and every road
segment graded, mapped from post-event imagery.

Model (all numbers in data/speed_model.json -> "access", with sources/assumptions):
  - road graph from the product's own road layer; speed by road class
  - a road segment graded damaged / destroyed is closed; possibly damaged is slowed
  - debris: a destroyed building closes road within a distance that depends on the road class (narrow streets fill,
    wide roads keep a lane); a damaged building slows the road next to it
  - entry points: where main roads leave the mapped area (help arrives from outside), plus any --from points
  - travel time from the nearest entry point to every building, before and after; buildings that can no longer be
    reached by vehicle, and the extra time for the rest

Writes: access.png, access.json (summary, cut-off clusters), access.geojson (closed and slowed roads, cut-off clusters),
buildings.csv (every building with damage, time before/after).
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import subprocess
import sys
import zipfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import person as P  # noqa: E402

BUCKET = "https://cems-mapping-website.s3.eu-west-1.amazonaws.com/static/activations/{e}/{e}_AOI{a:02d}_GRA_PRODUCT_r1_{t}_{v}_vector.zip"
GRADES = ["No visible damage", "Possibly damaged", "Damaged", "Destroyed"]


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- data

def fetch_ems(ems: str, aoi: int, cache: Path) -> Path:
    cache.mkdir(parents=True, exist_ok=True)
    for t in ("RTP01", "VECTORS", "RTP02"):
        for v in ("v1", "v2", "v3"):
            url = BUCKET.format(e=ems, a=aoi, t=t, v=v)
            dst = cache / Path(url).name
            if dst.exists() and dst.stat().st_size > 0:
                return dst
            r = subprocess.run(["curl", "-sSL", "-f", "-m", "300", "-o", str(dst), url], capture_output=True)
            if r.returncode == 0 and dst.exists() and zipfile.is_zipfile(dst):
                return dst
            dst.unlink(missing_ok=True)
    sys.exit(f"no grading vector package found for {ems} AOI{aoi:02d} (check the activation page for the file name; "
             "pass it with --ems-zip)")


def read_layer(z: zipfile.ZipFile, key: str) -> list[dict]:
    names = [n for n in z.namelist() if key in n and n.endswith(".json")]
    feats = []
    for n in names:
        feats += json.loads(z.read(n))["features"]
    return feats


def centroid(geom) -> tuple[float, float]:
    t, c = geom["type"], geom["coordinates"]
    if t == "Point":
        return c[1], c[0]
    ring = c[0] if t == "Polygon" else c[0][0]
    a = np.array(ring)
    return float(a[:, 1].mean()), float(a[:, 0].mean())


def lines_of(geom) -> list[list]:
    return [geom["coordinates"]] if geom["type"] == "LineString" else list(geom["coordinates"])


# ---------------------------------------------------------------- network

class Net:
    def __init__(self, lat0: float):
        self.kx = 111320 * math.cos(math.radians(lat0))
        self.ky = 110574
        self.nodes: dict[tuple, int] = {}
        self.xy: list[tuple[float, float]] = []
        self.edges: list[tuple[int, int, float, str, str, int]] = []   # a, b, length m, class, grade, road id

    def node(self, lon: float, lat: float) -> int:
        k = (round(lon, 6), round(lat, 6))
        i = self.nodes.get(k)
        if i is None:
            i = self.nodes[k] = len(self.xy)
            self.xy.append((lon * self.kx, lat * self.ky))
        return i

    def add_road(self, coords, cls: str, grade: str, rid: int) -> None:
        prev = None
        for lon, lat, *_ in coords:
            i = self.node(lon, lat)
            if prev is not None and prev != i:
                (x0, y0), (x1, y1) = self.xy[prev], self.xy[i]
                self.edges.append((prev, i, math.hypot(x1 - x0, y1 - y0), cls, grade, rid))
            prev = i


def road_class(info: str | None) -> str:
    info = info or ""
    for key, cls in (("2111", "highway"), ("21120", "primary"), ("21121", "secondary"), ("21122", "local"),
                     ("21124", "track")):
        if info.startswith(key):
            return cls
    return "local"


def edge_states(net: Net, bxy: np.ndarray, bgrade: np.ndarray, cfg: dict) -> tuple[np.ndarray, np.ndarray, list[str]]:
    """Per edge: speed factor after the event (0 = closed) and the reason."""
    from scipy.spatial import cKDTree
    E = len(net.edges)
    fac = np.ones(E)
    why = [""] * E
    for i, (_, _, _, cls, grade, _) in enumerate(net.edges):
        if grade in ("Damaged", "Destroyed"):
            fac[i], why[i] = 0.0, f"road graded {grade.lower()}"
        elif grade == "Possibly damaged":
            fac[i], why[i] = cfg["possibly_damaged_road_factor"], "road possibly damaged"
    destroyed = bxy[bgrade == "Destroyed"]
    damaged = bxy[bgrade == "Damaged"]
    trees = {g: cKDTree(p) for g, p in (("Destroyed", destroyed), ("Damaged", damaged)) if len(p)}
    xy = np.array(net.xy)
    for i, (a, b, L, cls, grade, _) in enumerate(net.edges):
        if fac[i] == 0:
            continue
        n = max(2, int(L / 5) + 1)
        pts = xy[a] + (xy[b] - xy[a]) * np.linspace(0, 1, n)[:, None]
        for g, tree in trees.items():
            r = cfg["debris_m"][g][cls]
            if r <= 0:
                continue
            if any(tree.query_ball_point(pts, r, return_length=True) > 0):
                if g == "Destroyed":
                    fac[i], why[i] = 0.0, "debris from a destroyed building"
                else:
                    fac[i] = min(fac[i], cfg["damaged_building_road_factor"])
                    why[i] = why[i] or "next to a damaged building"
                break
    return fac, why


def times(net: Net, fac: np.ndarray, speeds: dict, sources: list[int]) -> np.ndarray:
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import dijkstra
    N = len(net.xy)
    rows, cols, w = [], [], []
    for i, (a, b, L, cls, _, _) in enumerate(net.edges):
        if fac[i] <= 0:
            continue
        h = L / 1000 / (speeds[cls] * fac[i])
        rows += [a, b]
        cols += [b, a]
        w += [h, h]
    G = coo_matrix((w + [1e-9] * len(sources), (rows + [N] * len(sources), cols + sources)), shape=(N + 1, N + 1)).tocsr()
    return dijkstra(G, directed=True, indices=N)[:N] * 60          # minutes


# ---------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ems", help="Copernicus EMS activation code, e.g. EMSR648")
    ap.add_argument("--aoi", type=int, action="append", default=[], help="area of interest number(s) in the activation")
    ap.add_argument("--ems-zip", action="append", default=[], help="downloaded grading vector package(s)")
    ap.add_argument("--from", dest="sources", action="append", default=[],
                    help="extra entry point lat,lon (depot, hospital, staging area); repeatable")
    ap.add_argument("--no-edge-entries", action="store_true", help="do not add entry points where main roads leave the area")
    ap.add_argument("--out", required=True)
    ap.add_argument("--cache", default=".sar-cache")
    a = ap.parse_args()
    cfg = P.load_model()["access"]
    zips = [Path(x) for x in a.ems_zip] + [fetch_ems(a.ems, n, Path(a.cache) / "ems") for n in a.aoi]
    if not zips:
        sys.exit("give --ems CODE --aoi N, or --ems-zip FILE")
    builds, roads, camps, aoi_polys = [], [], [], []
    for zp in zips:
        z = zipfile.ZipFile(zp)
        for f in read_layer(z, "builtUpP") + read_layer(z, "builtUpA"):
            la, lo = centroid(f["geometry"])
            builds.append((la, lo, f["properties"].get("damage_gra") or "No visible damage", f["properties"].get("info") or ""))
        roads += [f for f in read_layer(z, "transportationL") if (f["properties"].get("obj_type") or "").startswith("211")]
        camps += read_layer(z, "ancillaryCrisisInfo")
        aoi_polys += read_layer(z, "areaOfInterestA")
    if not roads:
        sys.exit("the package has no road layer")
    lat0 = float(np.mean([b[0] for b in builds])) if builds else 37.0
    net = Net(lat0)
    for rid, f in enumerate(roads):
        cls = road_class(f["properties"].get("info"))
        grade = f["properties"].get("damage_gra") or "No visible damage"
        for line in lines_of(f["geometry"]):
            net.add_road(line, cls, grade, rid)
    xy = np.array(net.xy)
    bxy = np.array([(lo * net.kx, la * net.ky) for la, lo, _, _ in builds]) if builds else np.zeros((0, 2))
    bgrade = np.array([g for _, _, g, _ in builds])
    log(f"{len(builds)} buildings ({sum(bgrade == 'Destroyed')} destroyed, {sum(bgrade == 'Damaged')} damaged), "
        f"{len(roads)} roads -> {len(net.xy)} nodes, {len(net.edges)} edges")
    # entry points
    src = []
    if not a.no_edge_entries:
        deg = np.zeros(len(xy), int)
        main = np.zeros(len(xy), bool)
        for a_, b_, _, cls, _, _ in net.edges:
            deg[a_] += 1
            deg[b_] += 1
            if cls in ("highway", "primary", "secondary"):
                main[a_] = main[b_] = True
        x0, y0 = xy.min(0)
        x1, y1 = xy.max(0)
        edge_d = np.minimum.reduce([xy[:, 0] - x0, x1 - xy[:, 0], xy[:, 1] - y0, y1 - xy[:, 1]])
        src = list(np.nonzero(main & (deg == 1) & (edge_d < cfg["entry_edge_m"]))[0])
    for s in a.sources:
        la, lo = map(float, s.split(","))
        d = np.hypot(xy[:, 0] - lo * net.kx, xy[:, 1] - la * net.ky)
        src.append(int(np.argmin(d)))
    if not src:
        sys.exit("no entry points: give --from lat,lon")
    fac_after, why = edge_states(net, bxy, bgrade, cfg)
    t_before = times(net, np.ones(len(net.edges)), cfg["speed_kmh"], src)
    t_after = times(net, fac_after, cfg["speed_kmh"], src)
    # building -> nearest node on the network, plus a walk from the road
    from scipy.spatial import cKDTree
    tree = cKDTree(xy)
    # a building is reached from any road point within carrying distance: vehicle time there + walking the rest
    if len(bxy):
        dist, ni = tree.query(bxy, k=min(24, len(xy)), distance_upper_bound=cfg["carry_m"])
        dist = np.atleast_2d(dist)
        ni = np.atleast_2d(ni)
        ok = np.isfinite(dist)
        ni = np.where(ok, ni, 0)
        walk_min = np.where(ok, dist / 1000 / cfg["walk_kmh"] * 60, np.inf)
        tb = np.min(np.where(ok, t_before[ni] + walk_min, np.inf), axis=1)
        ta = np.min(np.where(ok, t_after[ni] + walk_min, np.inf), axis=1)
    else:
        tb = ta = np.zeros(0)
    cut = ~np.isfinite(ta) & np.isfinite(tb)
    delay = np.where(np.isfinite(ta), ta - tb, np.nan)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    # cut-off clusters: buildings that lost vehicle access, grouped by 150 m
    clusters = []
    if cut.any():
        from scipy.sparse.csgraph import connected_components
        from scipy.sparse import coo_matrix
        idx = np.nonzero(cut)[0]
        pairs = cKDTree(bxy[idx]).query_pairs(150, output_type="ndarray")
        n = len(idx)
        A = coo_matrix((np.ones(len(pairs)), (pairs[:, 0], pairs[:, 1])), shape=(n, n)) if len(pairs) else coo_matrix((n, n))
        k, lab = connected_components(A, directed=False)
        for c in range(k):
            m = idx[lab == c]
            la = float(np.mean([builds[i][0] for i in m]))
            lo = float(np.mean([builds[i][1] for i in m]))
            g = bgrade[m]
            # walking distance to the nearest point still reachable by vehicle
            reach_nodes = np.nonzero(np.isfinite(t_after))[0]
            dwalk = float(cKDTree(xy[reach_nodes]).query([lo * net.kx, la * net.ky])[0]) if len(reach_nodes) else math.nan
            clusters.append({"lat": round(la, 6), "lon": round(lo, 6), "buildings": int(len(m)),
                             "destroyed": int((g == "Destroyed").sum()), "damaged": int((g == "Damaged").sum()),
                             "nearest_vehicle_access_m": round(dwalk)})
        clusters.sort(key=lambda c: (-(c["destroyed"] * 3 + c["damaged"]), -c["buildings"]))
        for i, c in enumerate(clusters):
            c["id"] = f"C{i + 1}"
    closed = int((fac_after == 0).sum())
    slowed = int(((fac_after > 0) & (fac_after < 1)).sum())
    closed_km = float(sum(e[2] for e, f in zip(net.edges, fac_after) if f == 0) / 1000)
    reasons = {}
    for f, w in zip(fac_after, why):
        if w:
            reasons[w] = reasons.get(w, 0) + 1
    hit = np.isin(bgrade, ["Destroyed", "Damaged"])
    summary = {
        "notice": "Estimate for response planning (decision support): debris distances and speeds are assumptions; "
                  "check road status on the ground.",
        "source": [str(z.name) for z in zips] + ["Copernicus Emergency Management Service (© European Union), rapid mapping"],
        "buildings": {g: int((bgrade == g).sum()) for g in GRADES},
        "entry_points": [{"lat": round(float(xy[i, 1] / net.ky), 6), "lon": round(float(xy[i, 0] / net.kx), 6)} for i in src],
        "roads": {"segments": len(net.edges), "closed": closed, "closed_km": round(closed_km, 2), "slowed": slowed,
                  "reasons": reasons},
        "buildings_cut_off": int(cut.sum()),
        "damaged_or_destroyed_cut_off": int((cut & hit).sum()),
        "median_minutes_before": round(float(np.nanmedian(tb[np.isfinite(tb)])), 1) if np.isfinite(tb).any() else None,
        "median_extra_minutes_damaged_buildings": round(float(np.nanmedian(delay[hit])), 1) if (hit & np.isfinite(delay)).any() else None,
        "cut_off_clusters": clusters[:20],
    }
    (out / "access.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False))
    with open(out / "buildings.csv", "w") as fh:
        fh.write("lat,lon,damage,minutes_before,minutes_after,cut_off\n")
        for (la, lo, g, _), b_, a_, c_ in zip(builds, tb, ta, cut):
            fh.write(f"{la:.6f},{lo:.6f},{g},{b_:.1f},{'' if not np.isfinite(a_) else f'{a_:.1f}'},{int(c_)}\n")
    feats = []
    for (a_, b_, L, cls, grade, rid), f, w in zip(net.edges, fac_after, why):
        if f < 1:
            (xa, ya), (xb, yb) = xy[a_], xy[b_]
            feats.append({"type": "Feature", "properties": {"state": "closed" if f == 0 else "slowed", "reason": w, "class": cls},
                          "geometry": {"type": "LineString", "coordinates": [[xa / net.kx, ya / net.ky], [xb / net.kx, yb / net.ky]]}})
    for c in clusters:
        feats.append({"type": "Feature", "properties": {"name": f"cut-off cluster {c['id']}", **c},
                      "geometry": {"type": "Point", "coordinates": [c["lon"], c["lat"]]}})
    (out / "access.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": feats}))
    render(out / "access.png", net, xy, fac_after, bxy, bgrade, cut, delay, src, clusters, summary)
    print(json.dumps({k: summary[k] for k in ("buildings", "roads", "buildings_cut_off", "damaged_or_destroyed_cut_off",
                                              "median_minutes_before", "median_extra_minutes_damaged_buildings")},
                     indent=1, ensure_ascii=False))
    for c in clusters[:8]:
        print(f"  {c['id']}: {c['buildings']} buildings ({c['destroyed']} destroyed, {c['damaged']} damaged) cut off, "
              f"nearest vehicle access {c['nearest_vehicle_access_m']} m @ {c['lat']},{c['lon']}")
    print(f"-> {out}/access.png, access.json, access.geojson, buildings.csv")


def render(path: Path, net: Net, xy, fac, bxy, bgrade, cut, delay, src, clusters, summary) -> None:
    from PIL import Image, ImageDraw, ImageFont
    W = 1000
    x0, y0 = xy.min(0)
    x1, y1 = xy.max(0)
    span = max(x1 - x0, y1 - y0) * 1.04
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    f = W / span

    def px(x, y):
        return ((x - cx) * f + W / 2, W / 2 - (y - cy) * f)
    im = Image.new("RGB", (W, W), (245, 244, 240))
    d = ImageDraw.Draw(im)
    for (a, b, L, cls, _, _), fc in zip(net.edges, fac):
        col = (170, 170, 170) if fc == 1 else ((235, 150, 30) if fc > 0 else (220, 20, 20))
        wd = {"highway": 4, "primary": 3, "secondary": 3}.get(cls, 1) + (1 if fc < 1 else 0)
        d.line([px(*xy[a]), px(*xy[b])], fill=col, width=wd)
    gcol = {"No visible damage": (120, 160, 120), "Possibly damaged": (230, 200, 60), "Damaged": (240, 120, 30), "Destroyed": (160, 0, 0)}
    for (x, y), g, c in zip(bxy, bgrade, cut):
        u, v = px(x, y)
        r = 3 if g in ("Damaged", "Destroyed") else 1.5
        d.ellipse([u - r, v - r, u + r, v + r], fill=gcol.get(g, (120, 120, 120)), outline=(90, 0, 160) if c else None)
    for i in src:
        u, v = px(*xy[i])
        d.polygon([(u, v - 9), (u - 8, v + 6), (u + 8, v + 6)], fill=(0, 110, 220), outline=(0, 0, 0))
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 14)
        big = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 15)
    except Exception:  # noqa: BLE001
        font = big = ImageFont.load_default()
    for c in clusters[:10]:
        u, v = px(c["lon"] * net.kx, c["lat"] * net.ky)
        d.text((u + 6, v - 8), c["id"], fill=(255, 255, 255), font=big, stroke_width=3, stroke_fill=(90, 0, 160))
    km = max(0.5, round(span / 1000 / 5 * 2) / 2)
    L = km * 1000 * f
    d.rectangle([20, W - 34, 40 + L + 60, W - 10], fill=(255, 255, 255))
    d.line([(25, W - 22), (25 + L, W - 22)], fill=(0, 0, 0), width=4)
    d.text((32 + L, W - 30), f"{km:g} km", fill=(0, 0, 0), font=font)
    s = summary
    lines = [f"access after the earthquake: {s['roads']['closed']} road segments closed ({s['roads']['closed_km']} km), "
             f"{s['roads']['slowed']} slowed",
             f"{s['buildings_cut_off']} buildings cut off from vehicles ({s['damaged_or_destroyed_cut_off']} of them damaged/destroyed)",
             "roads: grey open, orange slowed, red closed; buildings: dark red destroyed, orange damaged, yellow possibly",
             "purple ring: building no longer reachable by vehicle; C1, C2 ...: cut-off clusters; blue triangles: entry points"]
    d.rectangle([10, 10, 760, 18 + 19 * len(lines)], fill=(255, 255, 255))
    for i, t in enumerate(lines):
        d.text((16, 14 + 19 * i), t, fill=(0, 0, 0), font=font)
    d.text((W - 400, W - 22), "estimate for response planning - decision support only", fill=(60, 60, 60), font=font)
    im.save(path, quality=90)


if __name__ == "__main__":
    main()
