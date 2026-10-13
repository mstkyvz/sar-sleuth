"""Public transport for urban searches: could they have taken a bus, tram or metro, and where could they be now?

Used by reach.py (--transit). No keys:
  - feed discovery: Mobility Database catalog (files.mobilitydatabase.org/feeds_v2.csv), GTFS feeds whose bounding box
    overlaps the search area
  - schedule: the real timetable (GTFS stop_times) for the dates of the missing period, from the last seen time
  - routing: Connection Scan (earliest arrival at every stop), boarding only where the person can walk to the stop
    within their walking budget, short walking transfers between nearby stops

The result is a set of alighting stops with (arrival time, walking hours already used); reach.py then walks on from
there with the remaining walking budget.
"""
from __future__ import annotations

import csv
import io
import math
import subprocess
import sys
import time
import zipfile
from datetime import timedelta
from pathlib import Path

import numpy as np

CATALOG = "https://files.mobilitydatabase.org/feeds_v2.csv"
TRANSFER_M = 200          # walking transfer between different stops within this distance
BOARD_SLACK_S = 60        # a person can board a vehicle that departs at least this long after they reach the stop


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def _get(url: str, dst: Path, max_age_days: float = 7, timeout: int = 300) -> Path:
    if dst.exists() and time.time() - dst.stat().st_mtime < max_age_days * 86400 and dst.stat().st_size > 0:
        return dst
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".part")
    subprocess.run(["curl", "-sSL", "-f", "-m", str(timeout), "-A", "sar-sleuth/0.1", "-o", str(tmp), url], check=True)
    tmp.replace(dst)
    return dst


def find_feeds(bbox: tuple[float, float, float, float], cache: Path) -> list[dict]:
    """Active, key-free GTFS feeds whose bounding box overlaps (s, w, n, e)."""
    s, w, n, e = bbox
    cat = _get(CATALOG, cache / "feeds_v2.csv", max_age_days=7, timeout=120)
    out = []
    for r in csv.DictReader(open(cat, encoding="utf-8")):
        if r.get("data_type") != "gtfs" or r.get("status") in ("deprecated", "inactive"):
            continue
        if (r.get("urls.authentication_type") or "0") not in ("0", ""):
            continue
        url = r.get("urls.latest") or r.get("urls.direct_download") or ""
        if not url.endswith(".zip"):
            continue
        try:
            bs, bn = float(r["location.bounding_box.minimum_latitude"]), float(r["location.bounding_box.maximum_latitude"])
            bw, be = float(r["location.bounding_box.minimum_longitude"]), float(r["location.bounding_box.maximum_longitude"])
        except (TypeError, ValueError):
            continue
        if bs > n or bn < s or bw > e or be < w:
            continue
        out.append({"id": r["id"], "provider": r.get("provider"), "url": url, "license": r.get("urls.license"),
                    "place": r.get("location.municipality") or r.get("location.subdivision_name")})
    return out


def _secs(x: str) -> int:
    h, m, s_ = x.strip().split(":")
    return int(h) * 3600 + int(m) * 60 + int(s_)


def _active_services(z: zipfile.ZipFile, day) -> set[str]:
    names = set(z.namelist())
    act = set()
    ymd = day.strftime("%Y%m%d")
    wd = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"][day.weekday()]
    if "calendar.txt" in names:
        for r in csv.DictReader(io.TextIOWrapper(z.open("calendar.txt"), "utf-8-sig")):
            if r["start_date"] <= ymd <= r["end_date"] and r.get(wd) == "1":
                act.add(r["service_id"])
    if "calendar_dates.txt" in names:
        for r in csv.DictReader(io.TextIOWrapper(z.open("calendar_dates.txt"), "utf-8-sig")):
            if r["date"] == ymd:
                (act.add if r["exception_type"] == "1" else act.discard)(r["service_id"])
    return act


def load_connections(zpath: Path, since, hours: float, bbox) -> dict:
    """Elementary connections (stop A dep t -> stop B arr t) of the missing period, times in seconds since `since`."""
    import pandas as pd
    s, w, n, e = bbox
    z = zipfile.ZipFile(zpath)
    stops = pd.read_csv(z.open("stops.txt"), dtype=str, encoding="utf-8-sig", usecols=lambda c: c in ("stop_id", "stop_name", "stop_lat", "stop_lon"))
    stops["stop_lat"] = stops["stop_lat"].astype(float)
    stops["stop_lon"] = stops["stop_lon"].astype(float)
    stops = stops[(stops.stop_lat >= s) & (stops.stop_lat <= n) & (stops.stop_lon >= w) & (stops.stop_lon <= e)].reset_index(drop=True)
    if stops.empty:
        return {"stops": stops, "conn": np.zeros((0, 5), np.int64)}
    sidx = {sid: i for i, sid in enumerate(stops.stop_id)}
    trips = pd.read_csv(z.open("trips.txt"), dtype=str, encoding="utf-8-sig", usecols=["trip_id", "service_id"])
    st = pd.read_csv(z.open("stop_times.txt"), dtype=str, encoding="utf-8-sig",
                     usecols=["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"])
    st = st[st.stop_id.isin(sidx)]
    st = st.dropna(subset=["arrival_time", "departure_time"])
    st["seq"] = st.stop_sequence.astype(int)
    st["arr"] = st.arrival_time.map(_secs)
    st["dep"] = st.departure_time.map(_secs)
    st["si"] = st.stop_id.map(sidx)
    st = st.sort_values(["trip_id", "seq"])
    nxt = st.shift(-1)
    same = (nxt.trip_id == st.trip_id).to_numpy()
    base = pd.DataFrame({"trip_id": st.trip_id.to_numpy()[same], "a": st.si.to_numpy()[same], "dep": st.dep.to_numpy()[same],
                         "b": nxt.si.to_numpy()[same].astype(int), "arr": nxt.arr.to_numpy()[same].astype(int)})
    base = base.merge(trips, on="trip_id")
    tcode = {t: i for i, t in enumerate(base.trip_id.unique())}
    base["ti"] = base.trip_id.map(tcode)
    start_local = since.replace(hour=0, minute=0, second=0, microsecond=0)
    conns = []
    for dd in range(-1, int(math.ceil(hours / 24)) + 1):      # previous day too: its after-midnight trips
        day = start_local + timedelta(days=dd)
        act = _active_services(z, day)
        if not act:
            continue
        b = base[base.service_id.isin(act)]
        off = (day - since).total_seconds()
        dep = b.dep.to_numpy() + off
        arr = b.arr.to_numpy() + off
        keep = (dep >= 0) & (dep <= hours * 3600)
        conns.append(np.stack([b.a.to_numpy()[keep], b.b.to_numpy()[keep], dep[keep].astype(np.int64),
                               arr[keep].astype(np.int64), b.ti.to_numpy()[keep] + dd * 10_000_000], 1))
    conn = np.concatenate(conns) if conns else np.zeros((0, 5), np.int64)
    conn = conn[np.argsort(conn[:, 2], kind="stable")]
    return {"stops": stops, "conn": conn.astype(np.int64)}


def scan(stops, conn: np.ndarray, walk_h_to_stop: np.ndarray, walk_kmh: float, walk_budget_h: float) -> tuple[np.ndarray, np.ndarray]:
    """Connection Scan. walk_h_to_stop[i] = walking hours from the last seen point to stop i (inf if unreachable).

    Returns per stop: earliest arrival (s since last seen, inf if never) and walking hours used on the way.
    Walking to the first stop happens right at the start (people tend to head off at once); a stop is boardable only
    if that walk fits the walking budget.
    """
    from scipy.spatial import cKDTree
    N = len(stops)
    INF = np.inf
    arr_t = np.full(N, INF)
    walk_used = np.full(N, INF)
    ok = np.isfinite(walk_h_to_stop) & (walk_h_to_stop <= walk_budget_h)
    arr_t[ok] = walk_h_to_stop[ok] * 3600
    walk_used[ok] = walk_h_to_stop[ok]
    # walking transfers
    lat, lon = stops.stop_lat.to_numpy(), stops.stop_lon.to_numpy()
    xy = np.stack([lon * 111320 * math.cos(math.radians(float(lat.mean()))), lat * 110574], 1)
    tree = cKDTree(xy)
    pairs = tree.query_pairs(TRANSFER_M, output_type="ndarray")
    nb = [[] for _ in range(N)]
    for i, j in pairs:
        d = float(np.hypot(*(xy[i] - xy[j]))) * 1.3 / 1000 / walk_kmh      # hours, 1.3 = street detour
        nb[i].append((j, d))
        nb[j].append((i, d))
    trip_on: dict[int, float] = {}             # trip -> walking used when boarded
    for a, b, dep, arr, ti in conn:
        w = trip_on.get(ti)
        if w is None and arr_t[a] + BOARD_SLACK_S <= dep:
            w = walk_used[a]
            trip_on[ti] = w
        if w is None:
            continue
        if arr < arr_t[b] or (arr == arr_t[b] and w < walk_used[b]):
            arr_t[b], walk_used[b] = arr, w
            for j, dh in nb[b]:
                ta, wj = arr + dh * 3600, w + dh
                if wj <= walk_budget_h and ta < arr_t[j]:
                    arr_t[j], walk_used[j] = ta, wj
    return arr_t, walk_used
