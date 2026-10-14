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
    ap.add_argument("--route-to", action="append", default=[], help="also route to this lat,lon (a building, a camp); repeatable")
    ap.add_argument("--routes", type=int, default=5, help="routes to the N worst cut-off clusters (default 5)")
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
    with np.errstate(invalid="ignore"):
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
    # ---- routes with directions, on a real map
    import directions as DR
    _, pred = DR.route_tree(net, fac_after, cfg["speed_kmh"], src)
    names = DR.name_edges(net, roads, Path(a.cache) / "names")
    reach_nodes = np.nonzero(np.isfinite(t_after))[0]
    rtree = cKDTree(xy[reach_nodes])
    targets = [(c["id"], f"Cluster {c['id']}: {c['buildings']} buildings ({c['destroyed']} destroyed, {c['damaged']} damaged)",
                c["lat"], c["lon"]) for c in clusters[:a.routes]]
    for i, s_ in enumerate(a.route_to):
        la, lo = map(float, s_.split(","))
        targets.append((f"T{i + 1}", f"Target T{i + 1} ({la:.5f}, {lo:.5f})", la, lo))
    routes = []
    gdir = out / "gpx"
    gdir.mkdir(exist_ok=True)
    for tid, label, la, lo in targets:
        txy = np.array([lo * net.kx, la * net.ky])
        cand = rtree.query_ball_point(txy, 600) or [int(rtree.query(txy)[1])]
        nodes_c = reach_nodes[cand]
        walk_m = np.hypot(*(xy[nodes_c] - txy).T)
        score = t_after[nodes_c] + walk_m / 1000 / cfg["walk_kmh"] * 60
        stop = int(nodes_c[int(np.argmin(score))])
        wm = float(np.hypot(*(xy[stop] - txy)))
        path = DR.path_to(pred, stop, len(xy))
        steps = DR.instructions(net, path, names)
        if wm > 15:
            steps.append({"text": f"Vehicle stops here (road ahead closed or none); continue on foot about {DR.fmt_m(wm)} "
                                  f"{DR.COMPASS[int((DR.bearing(xy[stop], txy) + 22.5) // 45) % 8]} to the target",
                          "distance_m": round(wm), "lat": round(float(xy[stop][1] / net.ky), 6), "lon": round(float(xy[stop][0] / net.kx), 6)})
        drive_km = sum(st["distance_m"] for st in steps[:-1 if wm > 15 else None]) / 1000
        mins = float(t_after[stop])
        r = {"id": tid, "label": label, "drive_km": round(drive_km, 2), "drive_min": round(mins, 1), "walk_m": round(wm),
             "summary": f"{drive_km:.1f} km by vehicle (~{mins:.0f} min) + {DR.fmt_m(wm)} on foot",
             "steps": [st["text"] for st in steps],
             "drive": [[round(float(xy[k][1] / net.ky), 6), round(float(xy[k][0] / net.kx), 6)] for k in path],
             "walk": [[round(float(xy[stop][1] / net.ky), 6), round(float(xy[stop][0] / net.kx), 6)], [la, lo]] if wm > 15 else [],
             "target": [la, lo]}
        routes.append(r)
        feats.append({"type": "Feature", "properties": {"name": f"route to {tid}", "summary": r["summary"]},
                      "geometry": {"type": "LineString", "coordinates": [[q[1], q[0]] for q in r["drive"]]}})
        trk = "".join(f'<trkpt lat="{q[0]}" lon="{q[1]}"/>' for q in r["drive"] + r["walk"])
        (gdir / f"route_{tid}.gpx").write_text('<?xml version="1.0" encoding="UTF-8"?>\n<gpx version="1.1" creator="sar-sleuth" '
                                               f'xmlns="http://www.topografix.com/GPX/1/1"><trk><name>{label}</name><trkseg>{trk}</trkseg></trk></gpx>\n')
    summary["routes"] = [{k: v for k, v in r.items() if k not in ("drive", "walk")} for r in routes]
    (out / "access.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False))
    (out / "access.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": feats}))
    lat_all, lon_all = xy[:, 1] / net.ky, xy[:, 0] / net.kx
    bbox = (float(lat_all.min()), float(lon_all.min()), float(lat_all.max()), float(lon_all.max()))
    road_feats = [f_ for f_ in feats if f_["properties"].get("state")]
    data = {"roads": {"type": "FeatureCollection", "features": road_feats},
            "buildings": [[round(b[0], 6), round(b[1], 6), b[2], int(c_)] for b, c_ in zip(builds, cut) if b[2] != "No visible damage"],
            "entries": summary["entry_points"], "routes": routes, "bounds": [[bbox[0], bbox[1]], [bbox[2], bbox[3]]]}
    sm = (f"<b>{summary['roads']['closed']}</b> road segments closed ({summary['roads']['closed_km']} km), "
          f"<b>{summary['buildings_cut_off']}</b> buildings cut off from vehicles. Pick a route:")
    DR.write_html(out / "access.html", "Access after the earthquake", sm, data)
    render_map(out / "access.png", net, xy, fac_after, builds, bgrade, cut, src, clusters, routes, summary, bbox,
               Path(a.cache) / "tiles")
    print(json.dumps({k: summary[k] for k in ("buildings", "roads", "buildings_cut_off", "damaged_or_destroyed_cut_off",
                                              "median_minutes_before", "median_extra_minutes_damaged_buildings")},
                     indent=1, ensure_ascii=False))
    for r in routes[:3]:
        print(f"\nroute to {r['label']}: {r['summary']}")
        for i, t_ in enumerate(r["steps"], 1):
            print(f"   {i}. {t_}")
    for c in clusters[:8]:
        print(f"  {c['id']}: {c['buildings']} buildings ({c['destroyed']} destroyed, {c['damaged']} damaged) cut off, "
              f"nearest vehicle access {c['nearest_vehicle_access_m']} m @ {c['lat']},{c['lon']}")
    print(f"-> {out}/access.png, access.html (interactive), access.json, access.geojson, buildings.csv, gpx/")


def render_map(path: Path, net: Net, xy, fac, builds, bgrade, cut, src, clusters, routes, summary, bbox, cache: Path) -> None:
    """Closed roads, damage and the first route with directions, on an OpenStreetMap-based basemap."""
    import directions as DR
    from PIL import ImageDraw, ImageFont
    pad = 0.08
    s_, w_, n_, e_ = bbox
    dy, dx = (n_ - s_) * pad, (e_ - w_) * pad
    im, proj, _ = DR.basemap((s_ - dy, w_ - dx, n_ + dy, e_ + dx), cache, max_px=1400)
    im = im.convert("RGB")
    d = ImageDraw.Draw(im, "RGBA")
    ll = lambda i: proj(xy[i][1] / net.ky, xy[i][0] / net.kx)  # noqa: E731
    for (a, b, L, cls, _, _), fc in zip(net.edges, fac):
        if fc < 1:
            d.line([ll(a), ll(b)], fill=(214, 31, 31, 255) if fc == 0 else (235, 150, 30, 230), width=4 if fc == 0 else 3)
    gcol = {"Possibly damaged": (230, 200, 60, 220), "Damaged": (240, 122, 30, 235), "Destroyed": (139, 0, 0, 255)}
    for (la, lo, g, _), c in zip(builds, cut):
        if g not in gcol:
            continue
        u, v = proj(la, lo)
        r = 3.5 if g != "Possibly damaged" else 2.5
        d.ellipse([u - r, v - r, u + r, v + r], fill=gcol[g], outline=(90, 0, 160, 255) if c else None, width=2 if c else 1)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 15)
        small = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 13)
        big = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 16)
    except Exception:  # noqa: BLE001
        font = small = big = ImageFont.load_default()
    if routes:
        r = routes[0]
        pts = [proj(q[0], q[1]) for q in r["drive"]]
        d.line(pts, fill=(31, 79, 224, 235), width=7, joint="curve")
        for i in range(1, 8):                                    # arrowheads
            j = int(len(pts) * i / 8)
            if 2 < j < len(pts):
                (x0, y0), (x1, y1) = pts[j - 3], pts[j]
                ang = math.atan2(y1 - y0, x1 - x0)
                a1 = (x1 - 15 * math.cos(ang) + 8 * math.sin(ang), y1 - 15 * math.sin(ang) - 8 * math.cos(ang))
                a2 = (x1 - 15 * math.cos(ang) - 8 * math.sin(ang), y1 - 15 * math.sin(ang) + 8 * math.cos(ang))
                d.polygon([(x1, y1), a1, a2], fill=(255, 255, 255, 255), outline=(31, 79, 224, 255))
        if r["walk"]:
            (u0, v0), (u1, v1) = proj(*r["walk"][0]), proj(*r["walk"][1])
            n = max(2, int(math.hypot(u1 - u0, v1 - v0) / 10))
            for k in range(0, n, 2):
                d.line([(u0 + (u1 - u0) * k / n, v0 + (v1 - v0) * k / n), (u0 + (u1 - u0) * (k + 1) / n, v0 + (v1 - v0) * (k + 1) / n)],
                       fill=(31, 79, 224, 255), width=4)
        u, v = pts[0]
        d.ellipse([u - 9, v - 9, u + 9, v + 9], fill=(45, 108, 255, 255), outline=(0, 0, 0, 255), width=2)
        d.text((u + 11, v - 9), "START", fill=(0, 0, 0), font=big, stroke_width=3, stroke_fill=(255, 255, 255))
    for c in clusters[:10]:
        u, v = proj(c["lat"], c["lon"])
        d.text((u + 7, v - 9), c["id"], fill=(255, 255, 255), font=big, stroke_width=3, stroke_fill=(90, 0, 160))
    for i in src:
        u, v = ll(i)
        d.polygon([(u, v - 10), (u - 9, v + 7), (u + 9, v + 7)], fill=(0, 110, 220, 255), outline=(0, 0, 0, 255))
    s = summary
    head = [f"Access after the earthquake: {s['roads']['closed']} road segments closed ({s['roads']['closed_km']} km), "
            f"{s['buildings_cut_off']} buildings cut off from vehicles",
            "red: closed road  orange: slowed  dark red / orange / yellow dots: destroyed / damaged / possibly damaged",
            "purple ring: cut off;  C1, C2 ...: cut-off clusters;  blue triangles: entry points"]
    d.rectangle([10, 10, 790, 16 + 20 * len(head)], fill=(255, 255, 255, 235))
    for i, t in enumerate(head):
        d.text((16, 13 + 20 * i), t, fill=(0, 0, 0), font=small if i else font)
    # directions and credits in a panel under the map, so nothing on the map is covered
    from PIL import Image
    lines = []
    if routes:
        r = routes[0]
        lines = [f"Route to {r['label']}", r["summary"]] + [f"{i}. {t}" for i, t in enumerate(r["steps"], 1)]
    lines += ["", "Estimate for response planning - decision support only. Damage: Copernicus EMS (© European Union). "
                  "Map: © OpenStreetMap contributors."]
    panel_h = 16 + 20 * len(lines)
    full = Image.new("RGB", (im.size[0], im.size[1] + panel_h), (255, 255, 255))
    full.paste(im, (0, 0))
    dp = ImageDraw.Draw(full)
    for i, t in enumerate(lines):
        col = (31, 79, 224) if (routes and i < 2) else ((90, 90, 90) if i == len(lines) - 1 else (0, 0, 0))
        dp.text((16, im.size[1] + 10 + 20 * i), t, fill=col, font=big if (routes and i == 0) else font)
    im = full
    im.save(path, quality=92)


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
