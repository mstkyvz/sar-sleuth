# /// script
# requires-python = ">=3.10"
# dependencies = ["numpy", "scipy", "pillow", "rasterio", "scikit-image", "duckdb", "pandas"]
# ///
"""Search management: update the probability map as teams search and find nothing (Bayesian search theory).

  search.py init  <reach.py --out dir> [--outside 0.05]        start a search session from the initial map
  search.py log   <dir> --sector 3 --pod 0.6 [--team A]          a team searched sector 3, estimated POD 60 %
  search.py log   <dir> --gpx track.gpx [--spacing 20]           POD per cell from a team's GPS track
  search.py status <dir> [--top 10]                               re-ranked sectors, map, overall probability of success

Bayes: after a search with detection probability POD in a cell that held probability p (and the person was not
found), p' = p (1 - POD) / (1 - sum p POD). Everything else, including "outside the search area", goes up.

POD from a track (Koopman's random-search law): coverage C = W x L / A, POD = 1 - exp(-C), with W the effective
sweep width for the ground type (data/speed_model.json -> "sweep_width_m"), L the track length in the cell, A the
cell area. Repeated searches multiply: (1 - POD_total) = (1 - POD_1)(1 - POD_2) ...
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import person as P  # noqa: E402
import reach as R  # noqa: E402


def load(d: Path):
    s = json.loads((d / "search.json").read_text())
    rep = json.loads((d / "reach.json").read_text())
    gm = rep["grid"]
    g = R.Grid(gm["lat0"], gm["lon0"], gm["n"] * gm["res_m"], gm["res_m"])
    p = np.load(d / "search_prob.npy")
    return s, rep, g, p


def save(d: Path, s: dict, p: np.ndarray) -> None:
    np.save(d / "search_prob.npy", p.astype(np.float64))
    (d / "search.json").write_text(json.dumps(s, indent=1, ensure_ascii=False))


def sectors_of(d: Path) -> list[dict]:
    return [f["properties"] | {"box": [f["geometry"]["coordinates"][0][0][1], f["geometry"]["coordinates"][0][0][0],
                                       f["geometry"]["coordinates"][0][2][1], f["geometry"]["coordinates"][0][2][0]]}
            for f in json.loads((d / "sectors.geojson").read_text())["features"]]


def box_mask(g, box) -> np.ndarray:
    s_, w, n, e = box
    return (g.lat >= s_) & (g.lat <= n) & (g.lon >= w) & (g.lon <= e)


def bayes(p: np.ndarray, outside: float, pod: np.ndarray) -> tuple[np.ndarray, float, float]:
    """Update after an unsuccessful search. Returns new cell probabilities, new outside, and the POS of that search."""
    pos = float((p * pod).sum())
    denom = 1 - pos
    return p * (1 - pod) / denom, outside / denom, pos


def cmd_init(a) -> None:
    d = Path(a.dir)
    prob = np.load(d / "prob.npy").astype(np.float64)
    prob = prob / prob.sum() * (1 - a.outside)
    s = {"created": datetime.now(timezone.utc).isoformat(timespec="seconds"), "outside": a.outside,
         "outside_initial": a.outside,
         "outside_note": "probability that the person is not in the mapped area at all (rest of world)",
         "log": [], "initial_sectors": [x["rank"] for x in sectors_of(d)][:10]}
    save(d, s, prob)
    print(f"search session started in {d}: {len(sectors_of(d))} sectors, outside the area {a.outside:.0%}")


def track_pod(g, lc: np.ndarray, gpx: Path, spacing: float | None, ws: dict, searchers: int = 1) -> np.ndarray:
    """POD per cell from a GPS track: coverage = sweep width x length in cell / cell area."""
    txt = gpx.read_text(encoding="utf-8", errors="ignore")
    pts = [(float(a_), float(b_)) for a_, b_ in re.findall(r'<trkpt[^>]*lat="([-\d.]+)"[^>]*lon="([-\d.]+)"', txt)]
    pts += [(float(a_), float(b_)) for b_, a_ in re.findall(r'<trkpt[^>]*lon="([-\d.]+)"[^>]*lat="([-\d.]+)"', txt)]
    if len(pts) < 2:
        sys.exit(f"{gpx}: no track points")
    length = np.zeros(g.lat.shape)
    for (la0, lo0), (la1, lo1) in zip(pts, pts[1:]):
        seg = math.hypot((la1 - la0) * R.M_LAT, (lo1 - lo0) * g.m_lon)
        n = max(1, int(seg / (g.res / 3)))
        for i in range(n):
            f = (i + 0.5) / n
            r, c = g.to_rc(la0 + (la1 - la0) * f, lo0 + (lo1 - lo0) * f)
            r, c = int(round(float(r))), int(round(float(c)))
            if 0 <= r < g.size and 0 <= c < g.size:
                length[r, c] += seg / n
    W = np.full(g.lat.shape, float(ws["0"]))
    for code, w in ws.items():
        if not code.startswith("_"):
            W[lc == int(code)] = w
    if searchers > 1:                                # a line abreast: each covers min(W, spacing) of fresh ground
        W = np.minimum(W, spacing or W) * searchers
    eff = W * length                                  # swept area (m²) put on the track cells ...
    on = length > 0
    swath = float(np.median(W[on])) if on.any() else g.res
    k = int(round(swath / g.res))
    if k > 1:                                         # ... then spread over the swath width (area is conserved)
        from scipy.ndimage import uniform_filter
        eff = uniform_filter(eff, size=k, mode="constant")
    cov = eff / g.res ** 2
    return 1 - np.exp(-cov)



def cmd_log(a) -> None:
    d = Path(a.dir)
    s, rep, g, p = load(d)
    if a.sector is not None:
        sec = next((x for x in sectors_of(d) if x["rank"] == a.sector), None)
        if not sec:
            sys.exit(f"no sector {a.sector}")
        pod = np.where(box_mask(g, sec["box"]), a.pod, 0.0)
        what = f"sector {a.sector}, POD {a.pod:.0%}"
    elif a.gpx:
        lc = R.landcover(g, Path(a.cache) / "worldcover")
        ws = P.load_model()["sweep_width_m"]
        pod = track_pod(g, lc, Path(a.gpx), a.spacing, ws, a.searchers)
        what = f"track {Path(a.gpx).name} ({a.searchers} searcher(s)), mean POD on covered cells {pod[pod > 0.01].mean():.0%}"
    else:
        sys.exit("give --sector N --pod 0.x, or --gpx track.gpx")
    p2, out2, pos = bayes(p, s["outside"], pod)
    s["outside"] = out2
    s["log"].append({"time": a.time or datetime.now(timezone.utc).isoformat(timespec="minutes"), "team": a.team,
                     "what": what, "pos_of_this_search": round(pos, 4), "note": a.note})
    np.save(d / f"pod_{len(s['log']):03d}.npy", pod.astype(np.float32))
    save(d, s, p2)
    print(f"logged: {what}; this search had a {pos:.1%} chance of finding them; outside the area now {out2:.1%}")


def cmd_status(a) -> None:
    d = Path(a.dir)
    s, rep, g, p = load(d)
    secs = sectors_of(d)
    for x in secs:
        m = box_mask(g, x["box"])
        x["now"] = float(p[m].sum())
        x["now_den"] = x["now"] / max(x["area_km2"], 1e-6)
    secs.sort(key=lambda x: -x["now_den"])
    pods = sorted(d.glob("pod_*.npy"))
    miss = np.ones(g.lat.shape)
    for f in pods:
        miss *= 1 - np.load(f)
    cum_pod = 1 - miss
    p0 = np.load(d / "prob.npy").astype(np.float64)
    p0 = p0 / p0.sum() * (1 - s.get("outside_initial", 0.05))
    pos_total = float((p0 * cum_pod).sum())          # chance the searches so far would have found them (initial map)
    print(f"searches logged: {len(s['log'])}; cumulative probability of success so far {pos_total:.1%}; "
          f"outside the area now {s['outside']:.1%}")
    print(f"\nnext sectors (highest probability per km² now):\n{'rank':>4} {'was':>4} {'now %':>7} {'per km²':>8} {'area km²':>9}")
    for i, x in enumerate(secs[:a.top]):
        print(f"{i + 1:>4} {x['rank']:>4} {x['now']:>7.1%} {x['now_den']:>8.3f} {x['area_km2']:>9.2f}")
    s["status"] = {"time": datetime.now(timezone.utc).isoformat(timespec="minutes"), "pos_total": round(pos_total, 4),
                   "next": [{"sector": x["rank"], "prob_now": round(x["now"], 4)} for x in secs[:a.top]]}
    (d / "search.json").write_text(json.dumps(s, indent=1, ensure_ascii=False))
    render(d, g, p, cum_pod, secs[:a.top], s, pos_total)


def render(d: Path, g, p, cum_pod, nxt, s, pos_total) -> None:
    from PIL import Image, ImageDraw, ImageFont
    cache = Path(".sar-cache")
    z = R.elevation(g, cache / "terrain")
    hs = R.hillshade(z, g.res)
    gray = (60 + 150 * hs)[..., None].repeat(3, -1)
    pos_ = p[p > 0]
    p99 = np.percentile(pos_, 99) if pos_.size else 1
    v = np.clip(p / p99, 0, 1) ** 0.6
    heat = np.stack([255 * np.clip(v * 2, 0, 1), 255 * np.clip(v * 2 - 0.6, 0, 1), 40 * (1 - v)], -1)
    a_ = np.clip(v * 1.6, 0, 0.85)[..., None]
    img = gray * (1 - a_) + heat * a_
    searched = cum_pod > 0.05
    img[searched] = img[searched] * (1 - 0.5 * cum_pod[searched, None]) + np.array([60, 140, 255]) * 0.5 * cum_pod[searched, None]
    im = Image.fromarray(img.clip(0, 255).astype(np.uint8))
    sc = max(1, int(round(1000 / g.size)))
    im = im.resize((g.size * sc, g.size * sc), Image.NEAREST) if sc > 1 else im.resize((1000, 1000), Image.LANCZOS)
    k = im.size[0] / g.size
    dr = ImageDraw.Draw(im)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 14)
        big = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 16)
    except Exception:  # noqa: BLE001
        font = big = ImageFont.load_default()
    for i, x in enumerate(nxt):
        la0, lo0, la1, lo1 = x["box"]
        r0, c0 = g.to_rc(la1, lo0)
        r1, c1 = g.to_rc(la0, lo1)
        b = [float(c0) * k, float(r0) * k, float(c1) * k, float(r1) * k]
        dr.rectangle(b, outline=(0, 200, 90), width=3)
        dr.text((b[0] + 3, b[1] + 2), f"{i + 1}", fill=(255, 255, 255), font=big, stroke_width=2, stroke_fill=(0, 90, 40))
    c = g.n * k + k / 2
    dr.ellipse([c - 7, c - 7, c + 7, c + 7], fill=(255, 255, 0), outline=(0, 0, 0), width=2)
    lines = [f"search status after {len(s['log'])} search(es): probability of success so far {pos_total:.0%}",
             "colour: probability now (yellow = highest); blue tint: already searched (stronger = higher POD)",
             f"green boxes: next {len(nxt)} sectors in order; outside the area now {s['outside']:.0%}"]
    dr.rectangle([10, 10, 700, 18 + 19 * len(lines)], fill=(255, 255, 255))
    for i, t in enumerate(lines):
        dr.text((16, 14 + 19 * i), t, fill=(0, 0, 0), font=font)
    im.save(d / "search_status.png", quality=90)
    print(f"-> {d}/search_status.png")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("init")
    i.add_argument("dir")
    i.add_argument("--outside", type=float, default=0.05, help="initial chance the person is outside the mapped area")
    lg = sub.add_parser("log")
    lg.add_argument("dir")
    lg.add_argument("--sector", type=int, help="sector rank as in sectors.csv")
    lg.add_argument("--pod", type=float, help="estimated probability of detection for that sector, 0-1")
    lg.add_argument("--gpx", help="GPS track of the team (one searcher, or the line's centre with --searchers)")
    lg.add_argument("--searchers", type=int, default=1, help="searchers walking abreast along the track")
    lg.add_argument("--spacing", type=float, help="spacing between searchers in m (caps the sweep width)")
    lg.add_argument("--team")
    lg.add_argument("--time")
    lg.add_argument("--note")
    lg.add_argument("--cache", default=".sar-cache")
    st = sub.add_parser("status")
    st.add_argument("dir")
    st.add_argument("--top", type=int, default=10)
    a = ap.parse_args()
    if a.cmd == "log" and a.sector is not None and a.pod is None:
        sys.exit("--sector needs --pod")
    {"init": cmd_init, "log": cmd_log, "status": cmd_status}[a.cmd](a)


if __name__ == "__main__":
    main()
