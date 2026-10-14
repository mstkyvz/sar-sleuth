"""Heading somewhere: the person set off from the last seen point towards a known destination (a village, a hut,
the car park). Instead of a circle, a corridor:

  - the best walking route X -> Y (an arrow), from the same terrain model
  - corridor bands: places on routes at most 15 / 35 / 75 % longer than the best one. A person who took a wrong
    turn, followed a stream down or contoured around a steep slope is most likely still inside them.
  - branches: parts of the 35 % band that leave the best route by more than a few hundred metres. These are the
    alternative lines worth checking (where the terrain lets people drift off)
  - where along the route they could be by now (likely / max walking budget)

Used by reach.py (--to lat,lon).
"""
from __future__ import annotations

import math

import numpy as np

BANDS = (0.15, 0.35, 0.75)


def corridor(g, G, t_from: np.ndarray, pred: np.ndarray, dest: tuple[float, float], T_like: float, T_max: float,
             branch_m: float = 300, z_arr: np.ndarray | None = None) -> tuple[dict, dict]:
    from scipy.ndimage import distance_transform_edt, label
    from scipy.sparse.csgraph import dijkstra
    S = g.size
    r, c = g.to_rc(dest[0], dest[1])
    r, c = int(round(float(r))), int(round(float(c)))
    if not (0 <= r < S and 0 <= c < S):
        return {"error": "destination outside the grid"}, {}
    yi = r * S + c
    t_xy = float(t_from[r, c])
    if not math.isfinite(t_xy):
        return {"error": "destination not reachable on foot from the last seen point"}, {}
    t_to = dijkstra(G.T.tocsr(), directed=True, indices=yi, limit=t_xy * (1 + BANDS[-1]) * 1.05).reshape(S, S)
    excess = (t_from + t_to - t_xy) / t_xy            # 0 on the best route, 0.35 = a route 35 % longer
    # best route by predecessors
    cells, k = [], yi
    while k >= 0 and len(cells) < S * S:
        cells.append(k)
        k = pred[k]
    cells = np.array(cells[::-1])
    rr, cc = cells // S, cells % S
    la, lo = g.to_ll(rr, cc)
    route = [[round(float(x), 6), round(float(y), 6)] for x, y in zip(lo, la)]
    seg_km = np.hypot(np.diff(rr), np.diff(cc)) * g.res / 1000
    route_km = float(seg_km.sum())
    # where along the route by now
    t_on = t_from[rr, cc]

    def at(budget):
        i = int(np.searchsorted(t_on, budget, side="right") - 1)
        i = max(0, min(i, len(cells) - 1))
        return {"lat": round(float(la[i]), 6), "lon": round(float(lo[i]), 6), "walking_h": round(float(t_on[i]), 2),
                "km_along_route": round(float(seg_km[:i].sum()), 2), "share_of_route": round(float(seg_km[:i].sum() / max(route_km, 1e-9)), 2),
                "arrived": bool(budget >= t_xy)}
    # branches of the 35 % band away from the best route
    on_route = np.zeros((S, S), bool)
    on_route[rr, cc] = True
    dist_px, (ir, ic) = distance_transform_edt(~on_route, return_indices=True)
    dist_route_m = dist_px * g.res
    # index along the route of the nearest route cell, and which side of the route a cell is on
    pos = np.full((S, S), -1, int)
    pos[rr, cc] = np.arange(len(rr))
    near_i = pos[ir, ic]
    nxt = np.clip(near_i + 3, 0, len(rr) - 1)
    prv = np.clip(near_i - 3, 0, len(rr) - 1)
    dr_, dc_ = rr[nxt] - rr[prv], cc[nxt] - cc[prv]
    vr, vc = np.indices((S, S))[0] - ir, np.indices((S, S))[1] - ic
    side = np.sign(dc_ * vr - dr_ * vc)                     # +1 / -1: right / left of the direction of travel
    band = np.isfinite(excess) & (excess <= BANDS[1])
    branches = []
    for sd, side_name in ((-1, "left"), (1, "right")):
        off = band & (dist_route_m > branch_m) & (side == sd)
        lab, n = label(off)
        for i in range(1, n + 1):
            m = lab == i
            area = float(m.sum()) * g.res ** 2 / 1e6
            if area < 0.05:
                continue
            mr, mc = np.nonzero(m)
            j = np.argmax(dist_route_m[m])
            bl, bo = g.to_ll(mr[j], mc[j])
            ki = int(np.median(near_i[m]))                  # where along the route this side-branch hangs
            el, eo = g.to_ll(rr[ki], cc[ki])
            dz = float(np.median(z_arr[m]) - z_arr[rr[ki], cc[ki]]) if z_arr is not None else None
            branches.append({"side": side_name, "area_km2": round(area, 3), "max_off_route_m": int(dist_route_m[m].max()),
                             "far_point": {"lat": round(float(bl), 6), "lon": round(float(bo), 6)},
                             "leaves_route_at": {"lat": round(float(el), 6), "lon": round(float(eo), 6),
                                                 "km_along_route": round(float(seg_km[:ki].sum()), 2)},
                             "elevation_vs_route_m": None if dz is None else int(round(dz)),
                             "min_excess": round(float(excess[m].min()), 2)})
    branches.sort(key=lambda b: -b["area_km2"])
    for i, b in enumerate(branches):
        b["id"] = f"B{i + 1}"
    info = {"destination": {"lat": dest[0], "lon": dest[1]}, "best_route_walking_h": round(t_xy, 2),
            "best_route_km": round(route_km, 2),
            "straight_km": round(float(math.hypot(g.x[r, c], g.y[r, c])) / 1000, 2),
            "position_likely": at(T_like), "position_max": at(T_max),
            "bands": {f"{int(b * 100)}%": round(float((np.isfinite(excess) & (excess <= b)).sum()) * g.res ** 2 / 1e6, 2)
                      for b in BANDS},
            "branches": branches[:8], "route": route}
    return info, {"excess": excess, "route_rc": (rr, cc)}
