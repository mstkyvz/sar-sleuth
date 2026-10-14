"""Routes on a real map: best open route, turn-by-turn directions with street names, a basemap PNG and an
interactive HTML map (Leaflet + OpenStreetMap tiles).

Used by access.py. Street names: the damage product's own road names where given, otherwise the nearest named
segment in Overture Maps (OpenStreetMap-derived). Basemap: OpenStreetMap standard tiles (© OpenStreetMap contributors), fetched politely and cached.
"""
from __future__ import annotations

import json
import math
import re
import subprocess
import sys
from pathlib import Path

import numpy as np

TILES = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
ATTRIB = "© OpenStreetMap contributors"
UA = "sar-sleuth/0.1 (+https://github.com/mstkyvz/sar-sleuth)"


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- routing

def route_tree(net, fac: np.ndarray, speeds: dict, sources: list[int]):
    """Minutes and predecessors from the nearest source, on open roads."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import dijkstra
    N = len(net.xy)
    rows, cols, w = [], [], []
    for (a, b, L, cls, _, _), f in zip(net.edges, fac):
        if f <= 0:
            continue
        h = L / 1000 / (speeds[cls] * f)
        rows += [a, b]
        cols += [b, a]
        w += [h, h]
    G = coo_matrix((w + [1e-9] * len(sources), (rows + [N] * len(sources), cols + sources)), shape=(N + 1, N + 1)).tocsr()
    d, pred = dijkstra(G, directed=True, indices=N, return_predecessors=True)
    return d[:N] * 60, pred


def path_to(pred: np.ndarray, node: int, N: int) -> list[int]:
    out, k = [], node
    while k >= 0 and k != N and len(out) < N + 1:
        out.append(k)
        k = pred[k]
    return out[::-1]


def bearing(p, q) -> float:
    return math.degrees(math.atan2(q[0] - p[0], q[1] - p[1])) % 360


COMPASS = ["north", "north-east", "east", "south-east", "south", "south-west", "west", "north-west"]


def instructions(net, nodes: list[int], names: dict[int, str | None]) -> list[dict]:
    """Group the path into steps by street; turn direction from the change of heading between steps."""
    if len(nodes) < 2:
        return []
    eidx = {}
    for i, (a, b, *_rest) in enumerate(net.edges):
        eidx[(a, b)] = eidx[(b, a)] = i
    xy = net.xy
    segs = []
    for a, b in zip(nodes, nodes[1:]):
        i = eidx.get((a, b))
        if i is None:
            continue
        e = net.edges[i]
        nm = names.get(e[5]) or None
        segs.append({"a": a, "b": b, "len": e[2], "name": nm, "cls": e[3], "rid": e[5], "brg": bearing(xy[a], xy[b])})
    steps = []
    for s_ in segs:
        key = s_["name"] or f"~{s_['cls']}"
        if steps:
            cur = steps[-1]
            turn = (s_["brg"] - cur["last_brg"] + 540) % 360 - 180
            same = key == cur["key"] and abs(turn) < 50
            unnamed_straight = s_["name"] is None and cur["name"] is None and abs(turn) < 30
            if same or unnamed_straight:
                cur["len"] += s_["len"]
                cur["last_brg"] = s_["brg"]
                cur["end"] = s_["b"]
                continue
        else:
            turn = None
        steps.append({"key": key, "name": s_["name"], "cls": s_["cls"], "len": s_["len"], "turn": turn,
                      "first_brg": s_["brg"], "last_brg": s_["brg"], "start": s_["a"], "end": s_["b"]})
    out = []
    for i, st in enumerate(steps):
        where = st["name"] or {"highway": "the highway", "primary": "the main road", "secondary": "the road",
                               "local": "the street", "track": "the track"}[st["cls"]] + " (no name)"
        if i == 0:
            verb = f"Head {COMPASS[int((st['first_brg'] + 22.5) // 45) % 8]} on {where}"
        else:
            t = st["turn"]
            if abs(t) < 30:
                verb = f"Continue onto {where}"
            elif abs(t) < 60:
                verb = f"Bear {'right' if t > 0 else 'left'} onto {where}"
            elif abs(t) < 150:
                verb = f"Turn {'right' if t > 0 else 'left'} onto {where}"
            else:
                verb = f"Make a U-turn onto {where}"
        lon, lat = xy[st["start"]][0] / net.kx, xy[st["start"]][1] / net.ky
        out.append({"text": f"{verb}, {fmt_m(st['len'])}", "street": st["name"], "distance_m": round(st["len"]),
                    "lat": round(lat, 6), "lon": round(lon, 6)})
    return out


def fmt_m(m: float) -> str:
    return f"{m / 1000:.1f} km" if m >= 1000 else f"{int(round(m, -1))} m"


# ---------------------------------------------------------------- street names

def overture_names(bbox, cache: Path) -> list[tuple[str, list[tuple[float, float]]]]:
    s, w, n, e = bbox
    key = cache / f"names_{s:.4f}_{w:.4f}_{n:.4f}_{e:.4f}.json"
    if key.exists():
        return json.loads(key.read_text())
    import duckdb
    rel = sorted(re.findall(r"<Prefix>release/([^<]+)/</Prefix>", subprocess.run(
        ["curl", "-sS", "-m", "30", "https://overturemaps-us-west-2.s3.us-west-2.amazonaws.com/?list-type=2&prefix=release/&delimiter=/"],
        capture_output=True, text=True).stdout))[-1]
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs; INSTALL spatial; LOAD spatial; SET s3_region='us-west-2';")
    rows = con.execute(f"""SELECT names.primary, ST_AsText(geometry) FROM read_parquet(
        's3://overturemaps-us-west-2/release/{rel}/theme=transportation/type=segment/*', hive_partitioning=1)
        WHERE subtype='road' AND names.primary IS NOT NULL AND bbox.xmax > {w} AND bbox.xmin < {e}
        AND bbox.ymax > {s} AND bbox.ymin < {n}""").fetchall()
    out = []
    for nm, wkt in rows:
        pts = [(float(a), float(b)) for a, b in re.findall(r"(-?[\d.]+) (-?[\d.]+)", wkt or "")]
        if len(pts) >= 2:
            out.append((nm, pts))
    cache.mkdir(parents=True, exist_ok=True)
    key.write_text(json.dumps(out, ensure_ascii=False))
    return out


def name_edges(net, roads: list[dict], cache: Path, max_m: float = 20) -> dict[int, str | None]:
    """Road id -> street name: the product's name if known, else the nearest named Overture segment."""
    from scipy.spatial import cKDTree
    names: dict[int, str | None] = {}
    for rid, f in enumerate(roads):
        nm = (f["properties"].get("name") or "").strip()
        names[rid] = None if nm.lower() in ("", "unknown", "none") else nm
    xy = np.array(net.xy)
    lon = xy[:, 0] / net.kx
    lat = xy[:, 1] / net.ky
    try:
        ov = overture_names((lat.min(), lon.min(), lat.max(), lon.max()), cache)
    except Exception as ex:  # noqa: BLE001
        log(f"street names: Overture unavailable ({str(ex)[:80]}); using the product's names only")
        return names
    pts, lab = [], []
    for nm, line in ov:
        for (x0, y0), (x1, y1) in zip(line, line[1:]):
            L = math.hypot((x1 - x0) * net.kx, (y1 - y0) * net.ky)
            for f_ in np.linspace(0, 1, max(2, int(L / 8) + 1)):
                pts.append(((x0 + (x1 - x0) * f_) * net.kx, (y0 + (y1 - y0) * f_) * net.ky))
                lab.append(nm)
    if not pts:
        return names
    tree = cKDTree(np.array(pts))
    votes: dict[int, dict[str, float]] = {}
    for a, b, L, cls, grade, rid in net.edges:
        if names.get(rid):
            continue
        mid = (xy[a] + xy[b]) / 2
        d, i = tree.query(mid, distance_upper_bound=max_m)
        if np.isfinite(d):
            v = votes.setdefault(rid, {})
            v[lab[i]] = v.get(lab[i], 0) + L
    for rid, v in votes.items():
        names[rid] = max(v.items(), key=lambda kv: kv[1])[0]
    return names


# ---------------------------------------------------------------- basemap

def _tile_xy(lat, lon, z):
    s = 2 ** z * 256
    x = (np.asarray(lon) + 180) / 360 * s
    la = np.radians(np.asarray(lat))
    y = (1 - np.log(np.tan(la) + 1 / np.cos(la)) / math.pi) / 2 * s
    return x, y


def basemap(bbox, cache: Path, max_px: int = 1300):
    """Stitched tiles covering bbox (s, w, n, e). Returns (image, projector lat,lon -> px)."""
    from PIL import Image
    from concurrent.futures import ThreadPoolExecutor
    s, w, n, e = bbox
    z = 18
    while z > 3:
        x0, y1 = _tile_xy(s, w, z)
        x1, y0 = _tile_xy(n, e, z)
        if max(x1 - x0, y1 - y0) <= max_px:
            break
        z -= 1
    x0, y1 = _tile_xy(s, w, z)
    x1, y0 = _tile_xy(n, e, z)
    tx0, tx1, ty0, ty1 = int(x0 // 256), int(x1 // 256), int(y0 // 256), int(y1 // 256)
    cache.mkdir(parents=True, exist_ok=True)
    jobs = [(x, y) for y in range(ty0, ty1 + 1) for x in range(tx0, tx1 + 1)]

    def one(t):
        p = cache / f"osm_{z}_{t[0]}_{t[1]}.png"
        if not p.exists():                          # OSM tile policy: identify the app, cache, keep the volume low
            url = TILES.format(z=z, x=t[0], y=t[1])
            subprocess.run(["curl", "-sSL", "-f", "-m", "30", "-A", UA, "-o", str(p), url], capture_output=True)
        try:
            return Image.open(p).convert("RGB")
        except Exception:  # noqa: BLE001
            return Image.new("RGB", (256, 256), (240, 240, 240))
    mos = Image.new("RGB", ((tx1 - tx0 + 1) * 256, (ty1 - ty0 + 1) * 256))
    with ThreadPoolExecutor(2) as ex:
        for (x, y), im in zip(jobs, ex.map(one, jobs)):
            mos.paste(im, ((x - tx0) * 256, (y - ty0) * 256))
    ox, oy = x0 - tx0 * 256, y0 - ty0 * 256
    crop = mos.crop((int(ox), int(oy), int(ox + (x1 - x0)), int(oy + (y1 - y0))))
    crop = Image.blend(crop, Image.new("RGB", crop.size, (255, 255, 255)), 0.35)   # lighter, so overlays stand out

    def proj(lat, lon):
        px_, py_ = _tile_xy(lat, lon, z)
        return float(px_ - x0), float(py_ - y0)
    return crop, proj, z


# ---------------------------------------------------------------- interactive map

HTML = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>html,body,#m{height:100%;margin:0;font:14px system-ui,sans-serif}
#p{position:absolute;z-index:1000;top:10px;right:10px;width:330px;max-height:85%;overflow:auto;background:#fff;
padding:10px 12px;border-radius:8px;box-shadow:0 2px 8px rgba(0,0,0,.25)}#p h3{margin:4px 0 6px}#p ol{padding-left:20px;margin:4px 0}
.k{display:inline-block;width:18px;height:4px;margin-right:6px;vertical-align:middle}.note{color:#666;font-size:12px}
button{margin:2px 4px 2px 0;padding:3px 8px;border:1px solid #bbb;border-radius:5px;background:#f6f6f6;cursor:pointer}</style></head>
<body><div id="m"></div><div id="p"><h3>__TITLE__</h3><div id="sum">__SUMMARY__</div>
<div><span class="k" style="background:#d61f1f"></span>closed road &nbsp;<span class="k" style="background:#eb961e"></span>slowed
&nbsp;<span class="k" style="background:#1f4fe0"></span>route</div><div id="btns"></div><div id="dir"></div>
<p class="note">Estimate for response planning (decision support). Check road status on the ground.<br>Damage: Copernicus EMS (© European Union). Map: © OpenStreetMap contributors.</p></div>
<script>
const D=__DATA__;
const m=L.map('m');L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19,
attribution:'© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'}).addTo(m);
L.geoJSON(D.roads,{style:f=>({color:f.properties.state=='closed'?'#d61f1f':'#eb961e',weight:f.properties.state=='closed'?4:3})}).addTo(m);
const gc={'Destroyed':'#8b0000','Damaged':'#f07a1e','Possibly damaged':'#e6c83c'};
D.buildings.forEach(b=>L.circleMarker([b[0],b[1]],{radius:b[2]=='Destroyed'?4:3,color:b[3]?'#5a00a0':gc[b[2]],weight:b[3]?2:1,fillColor:gc[b[2]],fillOpacity:.9}).addTo(m));
D.entries.forEach(e=>L.marker([e.lat,e.lon],{title:'entry point'}).addTo(m).bindPopup('Entry point'));
let cur=null;
function show(r){ if(cur) m.removeLayer(cur); cur=L.layerGroup().addTo(m);
 L.polyline(r.drive,{color:'#1f4fe0',weight:6,opacity:.85}).addTo(cur);
 if(r.walk.length) L.polyline(r.walk,{color:'#1f4fe0',weight:4,dashArray:'6 8'}).addTo(cur);
 L.circleMarker(r.drive[0],{radius:7,color:'#000',fillColor:'#2d6cff',fillOpacity:1}).addTo(cur);
 L.circleMarker(r.target,{radius:9,color:'#5a00a0',fillColor:'#b07cff',fillOpacity:.9}).addTo(cur).bindPopup(r.label);
 m.fitBounds(L.latLngBounds(r.drive.concat(r.walk,[r.target])).pad(.25));
 document.getElementById('dir').innerHTML='<h3>'+r.label+'</h3><b>'+r.summary+'</b><ol>'+r.steps.map(s=>'<li>'+s+'</li>').join('')+'</ol>';}
D.routes.forEach((r,i)=>{const b=document.createElement('button');b.textContent=r.id;b.onclick=()=>show(r);document.getElementById('btns').appendChild(b);});
m.fitBounds(D.bounds); if(D.routes.length) show(D.routes[0]);
</script></body></html>"""


def write_html(path: Path, title: str, summary_html: str, data: dict) -> None:
    html = HTML.replace("__TITLE__", title).replace("__SUMMARY__", summary_html).replace(
        "__DATA__", json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    path.write_text(html, encoding="utf-8")
