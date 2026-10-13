"""Search priority: probability per cell, ranked search sectors, GPX for teams, weather.

Used by reach.py (not a CLI on its own). Every factor is in data/speed_model.json -> "priority" with its source.

P(cell) is proportional to
  ring density   radial found-distance distribution of the category (data/distance_stats.json), per km²
  x time weight  1 inside the likely limit, falling off to 0 at the physical limit
  x attractors   paths/roads, drainages, structures, downhill (dementia), by category
normalised to 1 over the reachable area.
"""
from __future__ import annotations

import json
import math
import subprocess
from datetime import timedelta, timezone
from pathlib import Path

import numpy as np


# ---------------------------------------------------------------- probability

def ring_density(dist_km: np.ndarray, pct: dict, percentile_km) -> np.ndarray:
    """Per-area density from the category's cumulative found-distance curve (straight-line)."""
    qs = np.arange(0, 100.5, 0.5)
    ds = np.array([percentile_km(pct, q) if percentile_km(pct, q) is not None else np.nan for q in qs])
    ok = np.isfinite(ds)
    qs, ds = qs[ok], ds[ok]
    # tail beyond the last given percentile: continue the last slope out to 3x the last distance
    if qs[-1] < 100:
        last_q, last_d = qs[-1], ds[-1]
        slope = (ds[-1] - ds[-5]) / (qs[-1] - qs[-5]) if len(ds) > 5 else last_d / last_q
        ext_q = np.linspace(last_q, 100, 20)[1:]
        ds = np.concatenate([ds, last_d + (ext_q - last_q) * max(slope, 1e-3) * 3])
        qs = np.concatenate([qs, ext_q])
    ds = np.maximum.accumulate(ds) + np.arange(len(ds)) * 1e-6        # strictly increasing for interpolation
    # density per km² = dF/dr / (2 pi r) on a fine radial axis. Below half the 25 % distance the density is held
    # flat (a piecewise-linear F would make it blow up as 1/r at the point itself).
    r = np.linspace(0, max(ds[-1], float(dist_km.max())) + 1e-3, 4000)
    F = np.interp(r, ds, qs / 100, left=0, right=1)
    from scipy.ndimage import gaussian_filter1d
    dF = gaussian_filter1d(np.gradient(F, r), sigma=8)
    r_flat = max(0.05, 0.5 * (percentile_km(pct, 25) or 0.1))
    f = dF / (2 * math.pi * np.maximum(r, r_flat))
    return np.interp(dist_km, r, f)


def flow_accumulation(z: np.ndarray) -> np.ndarray:
    """D8 upstream cell count (drainage lines show up as high values)."""
    S0, S1 = z.shape
    zz = z.astype(np.float64)
    order = np.argsort(-zz, axis=None)
    acc = np.ones(z.size, np.float64)
    pad = np.pad(zz, 1, constant_values=np.inf)
    nbr = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]
    drops = np.stack([(zz - pad[1 + dr:1 + dr + S0, 1 + dc:1 + dc + S1]) / math.hypot(dr, dc) for dr, dc in nbr])
    best = np.argmax(drops, axis=0)
    has = np.max(drops, axis=0) > 0
    dr = np.array([n[0] for n in nbr])[best]
    dc = np.array([n[1] for n in nbr])[best]
    rr, cc = np.indices(z.shape)
    recv = np.where(has, (rr + dr) * S1 + (cc + dc), -1).ravel()
    for i in order:                     # highest first: pass accumulated flow downstream
        j = recv[i]
        if j >= 0:
            acc[j] += acc[i]
    return acc.reshape(z.shape)


def probability(g, z, lc, road, t, T_like, T_max, table, category, cfg, percentile_km) -> tuple[np.ndarray, dict]:
    dist_km = np.hypot(g.x, g.y) / 1000
    dens = ring_density(dist_km, table["p"], percentile_km)
    reach = np.isfinite(t) & (t <= T_max)
    tw = np.where(t <= T_like, 1.0, np.clip((T_max - t) / max(T_max - T_like, 1e-6), 0, 1) * cfg["beyond_likely"])
    tw = np.where(reach, tw, 0.0)
    att = np.ones(z.shape, np.float64)
    a = cfg["attractors"].get(category, cfg["attractors"]["default"])
    used = {}
    if a.get("path"):
        from scipy.ndimage import binary_dilation
        near = binary_dilation(road > 0, iterations=max(1, int(round(50 / g.res))))
        att[near] *= a["path"]
        used["path"] = a["path"]
    if a.get("drainage"):
        acc = flow_accumulation(z)
        thr = max(20, int(cfg["drainage_min_km2"] * 1e6 / g.res ** 2))
        att[acc >= thr] *= a["drainage"]
        used["drainage"] = a["drainage"]
    if a.get("structure"):
        att[lc == 50] *= a["structure"]
        used["structure"] = a["structure"]
    if a.get("downhill"):
        att[z < z[g.n, g.n] - 10] *= a["downhill"]
        used["downhill"] = a["downhill"]
    P = dens * tw * att
    s = P.sum()
    P = P / s if s > 0 else P
    return P, {"factors": used, "beyond_likely_weight": cfg["beyond_likely"]}


# ---------------------------------------------------------------- sectors

def sectors(g, P: np.ndarray, t, T_max, size_km: float | None, n_target: int) -> list[dict]:
    reach = np.isfinite(t) & (t <= T_max)
    if size_km is None:                                       # sector size so the top cells form ~n_target sectors
        order = np.sort(P[reach])[::-1]
        k = np.searchsorted(np.cumsum(order), 0.8) + 1         # cells holding 80 % of the probability
        area80 = k * g.res ** 2 / 1e6
        size_km = float(np.clip(math.sqrt(area80 / n_target), 0.2, 5.0))
        size_km = round(size_km * 10) / 10
    cells = max(1, int(round(size_km * 1000 / g.res)))
    out = []
    S = g.size
    for r0 in range(0, S, cells):
        for c0 in range(0, S, cells):
            blk = (slice(r0, min(S, r0 + cells)), slice(c0, min(S, c0 + cells)))
            poa = float(P[blk].sum())
            if poa <= 0:
                continue
            r1, c1 = min(S, r0 + cells) - 0.5, min(S, c0 + cells) - 0.5
            la0, lo0 = g.to_ll(r0 - 0.5, c0 - 0.5)
            la1, lo1 = g.to_ll(r1, c1)
            area = float(reach[blk].sum()) * g.res ** 2 / 1e6
            out.append({"poa": poa, "area_km2": round(area, 3), "pden": poa / max(area, 1e-6),
                        "box": [float(min(la0, la1)), float(min(lo0, lo1)), float(max(la0, la1)), float(max(lo0, lo1))]})
    out.sort(key=lambda s: -s["pden"])                         # search the densest first (POA per km²)
    cum = 0.0
    for i, s in enumerate(out):
        cum += s["poa"]
        s["rank"] = i + 1
        s["cum_poa"] = round(cum, 4)
        s["poa"] = round(s["poa"], 4)
        s["pden"] = round(s["pden"], 4)
    return out, size_km


def write_sectors(out_dir: Path, secs: list[dict], top: int) -> None:
    feats = []
    for s in secs:
        la0, lo0, la1, lo1 = s["box"]
        feats.append({"type": "Feature", "properties": {k: v for k, v in s.items() if k != "box"},
                      "geometry": {"type": "Polygon", "coordinates": [[[lo0, la0], [lo1, la0], [lo1, la1], [lo0, la1], [lo0, la0]]]}})
    (out_dir / "sectors.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": feats}))
    gdir = out_dir / "gpx"
    gdir.mkdir(exist_ok=True)
    for s in secs[:top]:
        la0, lo0, la1, lo1 = s["box"]
        pts = [(la0, lo0), (la0, lo1), (la1, lo1), (la1, lo0), (la0, lo0)]
        trk = "".join(f'<trkpt lat="{a:.6f}" lon="{b:.6f}"/>' for a, b in pts)
        (gdir / f"sector_{s['rank']:02d}.gpx").write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n<gpx version="1.1" creator="sar-sleuth" xmlns="http://www.topografix.com/GPX/1/1">'
            f'<metadata><name>Sector {s["rank"]}</name><desc>POA {s["poa"]:.1%}, {s["area_km2"]} km2</desc></metadata>'
            f'<wpt lat="{(la0 + la1) / 2:.6f}" lon="{(lo0 + lo1) / 2:.6f}"><name>S{s["rank"]}</name></wpt>'
            f'<trk><name>Sector {s["rank"]}</name><trkseg>{trk}</trkseg></trk></gpx>\n')
    lines = ["rank,poa,cum_poa,area_km2,poa_per_km2,south,west,north,east"]
    lines += [f'{s["rank"]},{s["poa"]},{s["cum_poa"]},{s["area_km2"]},{s["pden"]},' + ",".join(f"{v:.6f}" for v in s["box"])
              for s in secs]
    (out_dir / "sectors.csv").write_text("\n".join(lines) + "\n")


# ---------------------------------------------------------------- weather

def weather(lat: float, lon: float, since, hours: float) -> dict:
    """Hourly weather over the missing period from Open-Meteo (no key). Archive for older dates, forecast API for recent."""
    t0 = since.astimezone(timezone.utc)
    t1 = t0 + timedelta(hours=hours)
    from datetime import datetime
    now = datetime.now(timezone.utc)
    var = "temperature_2m,precipitation,snowfall,wind_speed_10m,apparent_temperature"
    if (now - t1).days > 6:
        url = (f"https://archive-api.open-meteo.com/v1/archive?latitude={lat}&longitude={lon}&start_date={t0:%Y-%m-%d}"
               f"&end_date={t1:%Y-%m-%d}&hourly={var}&timezone=UTC")
    else:
        past = max(1, (now - t0).days + 2)
        url = (f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&hourly={var}"
               f"&past_days={min(past, 92)}&forecast_days=3&timezone=UTC")
    try:
        r = subprocess.run(["curl", "-sS", "-m", "30", url], capture_output=True, text=True, check=True)
        h = json.loads(r.stdout)["hourly"]
    except Exception as ex:  # noqa: BLE001
        return {"error": f"Open-Meteo: {str(ex)[:120]}"}
    times = [datetime.fromisoformat(x).replace(tzinfo=timezone.utc) for x in h["time"]]
    idx = [i for i, x in enumerate(times) if t0 <= x <= t1]
    if not idx:
        return {"error": "no weather hours in the period"}

    def col(k):
        return [h[k][i] for i in idx if h[k][i] is not None]

    tmp, app, pr, sn, wd = col("temperature_2m"), col("apparent_temperature"), col("precipitation"), col("snowfall"), col("wind_speed_10m")
    w = {"source": "Open-Meteo (open-meteo.com, CC BY 4.0)", "hours": len(idx),
         "temp_min_c": round(min(tmp), 1), "temp_max_c": round(max(tmp), 1),
         "feels_like_min_c": round(min(app), 1) if app else None,
         "rain_mm": round(sum(pr), 1), "snow_cm": round(sum(sn), 1), "wind_max_kmh": round(max(wd), 1) if wd else None}
    flags = []
    if (w["feels_like_min_c"] is not None and w["feels_like_min_c"] < 5) or w["temp_min_c"] < 5:
        flags.append("cold: hypothermia risk, worse if wet; people tend to seek shelter")
    if w["rain_mm"] > 10:
        flags.append("heavy rain: tracks washed out, streams up, slower movement, hypothermia risk")
    if w["snow_cm"] > 1:
        flags.append("snowfall: much slower movement, tracks may be visible")
    if w["temp_max_c"] > 32:
        flags.append("heat: dehydration risk; people head for shade and water")
    w["flags"] = flags
    return w
