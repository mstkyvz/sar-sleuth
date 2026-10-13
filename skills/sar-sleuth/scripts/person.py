# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Who is lost and how long they have been out -> speeds and a moving-time budget.

  person.py --age 72 --sex f --mobility cane --since 2026-10-13T14:00+03:00 --hours 24 --at 41.17,28.98

Prints the profile, every number it used and where that number comes from (data/speed_model.json).
The budget is in "daytime walking hours": reach.py turns it into an area with the terrain model.
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

MODEL_PATH = Path(__file__).resolve().parent.parent / "data" / "speed_model.json"


def load_model(path: Path = MODEL_PATH) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def interp(table: list[list[float]], x: float) -> float:
    """Piecewise-linear lookup in [[x, y], ...] (sorted by x), clamped at both ends."""
    if x <= table[0][0]:
        return table[0][1]
    for (x0, y0), (x1, y1) in zip(table, table[1:]):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return table[-1][1]


@dataclass
class Person:
    age: float
    sex: str = "any"                 # m / f / any
    fitness: str = "normal"          # low / normal / high
    mobility: str = "normal"         # normal / limp / cane / walker
    injured: bool = False
    dementia: bool = False
    can_run: bool | None = None      # None = decide from age, mobility, injury


@dataclass
class Profile:
    walk_kmh: float                  # flat paved walking speed
    speed_factor: float              # walk_kmh / Wood flat reference
    sens: float                      # multiplies slope terms of the terrain model
    max_climb: float                 # deg, steepest walking slope possible
    max_hill: float                  # deg, steepest ground that can be crossed off paths
    run_kmh: float | None
    run_minutes: float
    activity: dict
    cap_likely: float                # moving hours per 24 h
    cap_max: float
    likely_cap: float | None = None  # walking hours: the 'likely' budget never exceeds this (calibrated)
    notes: list[str] = field(default_factory=list)


def walk_speed(p: Person, m: dict) -> tuple[float, str]:
    """Flat walking speed in m/s and the table used."""
    if p.age < 20:
        return interp(m["walk_speed_child"]["any"], p.age), "walk_speed_child"
    sex = {"m": "male", "f": "female"}.get(p.sex)
    if sex:
        return interp(m["walk_speed_adult"][sex], p.age), f"walk_speed_adult.{sex}"
    v = (interp(m["walk_speed_adult"]["male"], p.age) + interp(m["walk_speed_adult"]["female"], p.age)) / 2
    return v, "walk_speed_adult (mean of male and female)"


def build_profile(p: Person, m: dict | None = None) -> Profile:
    m = m or load_model()
    mod = m["modifiers"]
    notes = []
    v, src = walk_speed(p, m)
    notes.append(f"flat walking speed {v:.2f} m/s from {src}")
    fit, mob = mod["fitness"][p.fitness], mod["mobility"][p.mobility]
    speed = v * 3.6 * fit["speed"] * mob["speed"]
    sens = interp(mod["age_sens"], p.age) * fit["sens"] * mob["sens"]
    climb = interp(mod["age_max_climb"], p.age)
    if mob["max_climb"] is not None:
        climb = min(climb, mob["max_climb"])
    if p.injured:
        inj = mod["injured"]
        speed *= inj["speed"]
        sens *= inj["sens"]
        climb = min(climb, inj["max_climb"])
        notes.append("injured: slower, slopes cost more, no running")
    if p.fitness == "high":
        climb += 5
    # running
    r = m["running"]
    lo, hi = r["default_can_run_ages"]
    can_run = p.can_run
    if can_run is None:
        can_run = lo <= p.age <= hi and p.mobility == "normal" and not p.injured
    run_kmh = run_min = None
    if can_run:
        rr = r["child_7_12"] if p.age < 13 else r[p.fitness]
        run_kmh, run_min = rr["kmh"] * mob["speed"], rr["minutes"]
        notes.append(f"can run: {run_kmh:.1f} km/h on the flat for {run_min} min in total (assumption)")
    else:
        notes.append("does not run (age, mobility or injury; override with --can-run yes)")
    # activity
    a = m["activity"]
    caps = next(c for c in a["cap_hours_per_24h"] if c["ages"][0] <= p.age <= c["ages"][1] + 0.999)
    k = a["fitness_cap"][p.fitness] * a["mobility_cap"][p.mobility] * (a["injured_cap"] if p.injured else 1.0)
    cap_l, cap_x = caps["likely"] * k, caps["max"] * k
    act = {"night_speed": a["night_speed"], "likely": dict(a["likely"]), "max": dict(a["max"])}
    if "night_by_age" in a:
        for sc in ("likely", "max"):
            act[sc]["night"] = interp(a["night_by_age"][sc], p.age)
    act["day_decay"] = a.get("day_decay", {"likely": [1.0], "max": [1.0]})
    lc = m.get("likely_cap")
    cap_walk = None
    if lc:
        cap_walk = lc["dementia"]["hours"] if p.dementia else next(
            g["hours"] for g in lc["groups"].values() if g["ages"][0] <= p.age <= g["ages"][1] + 0.999)
    if p.dementia:
        act["likely"] = dict(a["dementia"]["likely"])
        cap_l = min(cap_x, cap_l * a["dementia"]["cap_factor"])
        notes.append("dementia: keeps moving more, also at night (assumption)")
    return Profile(walk_kmh=speed, speed_factor=speed / m["terrain"]["flat_reference_kmh"], sens=sens,
                   max_climb=climb, max_hill=climb + mod["hill_over_climb"], run_kmh=run_kmh, run_minutes=run_min or 0, activity=act,
                   cap_likely=cap_l, cap_max=cap_x, likely_cap=cap_walk, notes=notes)


# ---------------------------------------------------------------- sun

def sun_altitude(t: datetime, lat: float, lon: float) -> float:
    """Solar altitude in degrees (NOAA approximation, good to ~0.5 deg)."""
    t = t.astimezone(timezone.utc)
    doy = t.timetuple().tm_yday
    hour = t.hour + t.minute / 60 + t.second / 3600
    g = 2 * math.pi / 365 * (doy - 1 + (hour - 12) / 24)
    eqt = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                    - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g) - 0.006758 * math.cos(2 * g)
            + 0.000907 * math.sin(2 * g) - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    tst = hour * 60 + eqt + 4 * lon
    ha = math.radians(tst / 4 - 180)
    la = math.radians(lat)
    cz = math.sin(la) * math.sin(decl) + math.cos(la) * math.cos(decl) * math.cos(ha)
    return math.degrees(math.asin(max(-1.0, min(1.0, cz))))


# ---------------------------------------------------------------- budget

def budget(pr: Profile, since: datetime, hours: float, lat: float, lon: float, step_min: int = 10) -> dict:
    """Moving-time budget for the 'likely' and 'max' scenarios, in daytime-walking hours.

    Walks the elapsed time in steps: each step adds (share of time moving) x (night speed if dark).
    Raw moving time is capped per 24 h; running (max scenario only) converts walking minutes into
    faster minutes at the start.
    """
    out = {}
    n = max(1, int(round(hours * 60 / step_min)))
    dt = hours / n
    dark = [sun_altitude(since + timedelta(hours=(i + 0.5) * dt), lat, lon) < -6 for i in range(n)]
    for sc, cap24 in (("likely", pr.cap_likely), ("max", pr.cap_max)):
        f = pr.activity[sc]
        decay = pr.activity.get("day_decay", {}).get(sc, [1.0])
        moving = eq = 0.0
        day_moving = [0.0] * (int(hours // 24) + 2)
        for i, d in enumerate(dark):
            day = int(i * dt // 24)
            cap_today = cap24 * decay[min(day, len(decay) - 1)]
            share = min((f["night"] if d else f["day"]) * dt, cap_today - day_moving[day])
            if share <= 0:
                continue
            day_moving[day] += share
            moving += share
            eq += share * (pr.activity["night_speed"] if d else 1.0)
        run_bonus = 0.0
        if sc == "max" and pr.run_kmh:
            run_h = min(pr.run_minutes / 60, moving)
            run_bonus = run_h * (pr.run_kmh / pr.walk_kmh - 1)
        walk = eq + run_bonus
        capped = sc == "likely" and pr.likely_cap is not None and walk > pr.likely_cap
        if capped:
            walk = pr.likely_cap                        # people walk for a while, then stop (calibrated per group)
        out[sc] = {"moving_hours": round(moving, 2), "walk_equiv_hours": round(walk, 2),
                   "run_bonus_hours": round(run_bonus, 2),
                   "cap_hours_by_day": [round(cap24 * decay[min(k, len(decay) - 1)], 2) for k in range(len(day_moving) - 1)]}
        if sc == "likely":
            out[sc]["calibrated_cap_hours"] = pr.likely_cap
            out[sc]["capped"] = capped
    if pr.likely_cap is not None:
        # calibrated: the likely budget is the group's cap, never more than the physical max (early hours)
        out["likely"]["walk_equiv_hours"] = round(min(pr.likely_cap, out["max"]["walk_equiv_hours"]), 2)
        out["likely"]["capped"] = pr.likely_cap <= out["max"]["walk_equiv_hours"]
        out["likely"]["rule"] = "min(calibrated cap, max budget)"
    out["dark_hours"] = round(sum(dark) * dt, 2)
    out["hours"] = hours
    return out


def parse_time(s: str, lon: float) -> tuple[datetime, str | None]:
    t = datetime.fromisoformat(s)
    if t.tzinfo is None:
        off = round(lon / 15)
        t = t.replace(tzinfo=timezone(timedelta(hours=off)))
        return t, f"no UTC offset given: assumed UTC{off:+d} from longitude; pass e.g. 2026-10-13T14:00+03:00"
    return t, None


def add_args(ap: argparse.ArgumentParser) -> None:
    g = ap.add_argument_group("person")
    g.add_argument("--age", type=float, required=True)
    g.add_argument("--sex", choices=["m", "f", "any"], default="any")
    g.add_argument("--fitness", choices=["low", "normal", "high"], default="normal")
    g.add_argument("--mobility", choices=["normal", "limp", "cane", "walker"], default="normal",
                   help="limp = limps or has a bad leg; cane / walker = uses one")
    g.add_argument("--injured", action="store_true", help="hurt after going missing (fall, sprain)")
    g.add_argument("--dementia", action="store_true")
    g.add_argument("--can-run", choices=["yes", "no"])
    t = ap.add_argument_group("time")
    t.add_argument("--since", required=True, help="last seen, ISO time with UTC offset, e.g. 2026-10-13T14:00+03:00")
    t.add_argument("--hours", type=float, default=24, help="hours since last seen (default 24)")


def person_from_args(a) -> Person:
    return Person(age=a.age, sex=a.sex, fitness=a.fitness, mobility=a.mobility, injured=a.injured,
                  dementia=a.dementia, can_run=None if a.can_run is None else a.can_run == "yes")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--at", required=True, help="last seen point lat,lon (for daylight)")
    add_args(ap)
    a = ap.parse_args()
    lat, lon = map(float, a.at.split(","))
    since, warn = parse_time(a.since, lon)
    pr = build_profile(person_from_args(a))
    b = budget(pr, since, a.hours, lat, lon)
    if warn:
        print("WARNING:", warn)
    print(json.dumps({"profile": asdict(pr), "budget": b}, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
