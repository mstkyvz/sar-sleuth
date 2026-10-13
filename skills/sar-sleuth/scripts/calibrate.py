# /// script
# requires-python = ">=3.10"
# dependencies = ["numpy", "pillow"]
# ///
"""Run reach.py on solved cases and measure how well the limits contain the found points.

  calibrate.py run cases.json [more.json ...] --out calib/       # runs every usable case (cached per case)
  calibrate.py score calib/                                       # coverage table

For each case: walking hours from the last seen point to the found point (t_found). With only a reported
distance, t_found is the smallest walking time on the ring at that straight-line distance (the easiest
direction: an optimistic lower bound). Coverage = share of cases with t_found <= the likely / max budget.
"""
from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
MAX_ELAPSED_H = 240
CATS = {"child": "child", "autism": "autism", "dementia": "dementia"}


def usable(c: dict) -> str | None:
    ls, f = c.get("last_seen", {}), c.get("found", {})
    if ls.get("lat") is None or ls.get("lon") is None:
        return "no last-seen coordinates"
    if not c.get("elapsed_hours"):
        return "no elapsed time"
    if f.get("lat") is None and f.get("distance_km_reported") is None:
        return "no found point or distance"
    if (c.get("person") or {}).get("age") is None:
        return "no age"
    if c["elapsed_hours"] > MAX_ELAPSED_H:
        return f"found after more than {MAX_ELAPSED_H} h: walking time unknown (often found long after death)"
    return None


def since_of(c: dict) -> str:
    t = c["last_seen"].get("time")
    if t:
        return t
    ft = c["found"].get("time")
    if ft:
        from datetime import datetime, timedelta
        return (datetime.fromisoformat(ft) - timedelta(hours=c["elapsed_hours"])).isoformat()
    return "2025-06-15T12:00:00+00:00"            # unknown date: midsummer noon (noted in the score output)


def run(a) -> None:
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    cases = [c for p in a.cases for c in json.loads(Path(p).read_text())]
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(a.jobs) as ex:
        list(ex.map(lambda c: one(a, out, c), cases))


def one(a, out: Path, c: dict) -> None:
    if True:
        why = usable(c)
        d = out / c["id"]
        if why:
            print(f"skip {c['id']}: {why}")
            return
        if (d / "reach.json").exists() and not a.force:
            return
        p = c["person"]
        cmd = ["uv", "run", "-q", str(HERE / "reach.py"), f"--at={c['last_seen']['lat']},{c['last_seen']['lon']}",
               "--since", since_of(c), "--hours", str(c["elapsed_hours"]), "--age", str(p["age"]), "--out", str(d),
               "--toward", "N", "--max-cells", str(a.max_cells), "--no-render", "--no-priority", "--transit", "no"]
        if p.get("sex") in ("m", "f"):
            cmd += ["--sex", p["sex"]]
        if p.get("mobility") in ("limp", "cane", "walker"):
            cmd += ["--mobility", p["mobility"]]
        if p.get("category") == "dementia":
            cmd += ["--dementia"]
        if a.roads != "auto":
            cmd += ["--roads", a.roads]
        if a.no_conditions:
            cmd += ["--conditions", "0"]
        print(f"run {c['id']} ({p['age']} y, {c['elapsed_hours']} h)", flush=True)
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode:
            print(f"  FAILED: {r.stderr.strip().splitlines()[-1][:200] if r.stderr.strip() else r.returncode}")
        (out / c["id"]).mkdir(exist_ok=True)
        (out / c["id"] / "case.json").write_text(json.dumps(c, ensure_ascii=False, indent=1))


def t_found(d: Path, c: dict) -> tuple[float, str]:
    g = json.loads((d / "grid.json").read_text())
    t = np.load(d / "time.npy")
    n, res, lat0, lon0 = g["n"], g["res_m"], g["lat0"], g["lon0"]
    m_lon = 111320.0 * math.cos(math.radians(lat0))
    f = c["found"]
    if f.get("lat") is not None and (f.get("precision_m") or 0) <= 1500:
        r = int(round(-(f["lat"] - lat0) * 110574.0 / res + n))
        cc = int(round((f["lon"] - lon0) * m_lon / res + n))
        if 0 <= r < t.shape[0] and 0 <= cc < t.shape[1]:
            win = t[max(0, r - 1):r + 2, max(0, cc - 1):cc + 2]      # ±1 cell for point precision
            return float(np.min(win)), "point"
        return math.inf, "point outside grid"
    km = f.get("distance_km_reported")
    if km is None:
        la, lo = f["lat"], f["lon"]
        km = math.hypot((la - lat0) * 110.574, (lo - lon0) * m_lon / 1000)
    ax = (np.arange(t.shape[0]) - n) * res / 1000
    dist = np.hypot(ax[None, :], ax[:, None])
    ring = np.abs(dist - km) <= max(res / 1000, 0.1 * km)
    if not ring.any():
        return math.inf, "ring outside grid"
    vals = t[ring]
    vals = vals[np.isfinite(vals)]
    if not vals.size:
        return math.inf, "ring unreachable"
    # direction unknown: the median over all directions (the easiest direction would bias walking time low)
    return float(np.median(vals)), "ring (median direction)"


def found_km(c: dict, rep: dict) -> float | None:
    f = c["found"]
    if f.get("lat") is not None and (f.get("precision_m") or 0) <= 1500:
        lat0, lon0 = c["last_seen"]["lat"], c["last_seen"]["lon"]
        return math.hypot((f["lat"] - lat0) * 110.574, (f["lon"] - lon0) * 111.320 * math.cos(math.radians(lat0)))
    return f.get("distance_km_reported")


def budget_now(c: dict) -> dict:
    sys.path.insert(0, str(HERE))
    import person as P
    from datetime import datetime
    p = c["person"]
    per = P.Person(age=p["age"], sex=p.get("sex") or "any", mobility=p.get("mobility") or "normal",
                   dementia=p.get("category") == "dementia")
    pr = P.build_profile(per)
    return P.budget(pr, datetime.fromisoformat(since_of(c)), c["elapsed_hours"], c["last_seen"]["lat"], c["last_seen"]["lon"])


def score(a) -> None:
    rows = []
    for d in sorted(Path(a.out).iterdir()):
        if not (d / "reach.json").exists():
            continue
        c = json.loads((d / "case.json").read_text())
        rep = json.loads((d / "reach.json").read_text())
        tf, how = t_found(d, c)
        b = budget_now(c)                       # budgets from the current speed_model.json: no terrain rerun needed
        km_found = found_km(c, rep)
        rg = rep.get("rings", {})
        ring_q = None
        if km_found is not None and rg.get("km"):
            sys.path.insert(0, str(HERE))
            import reach as R
            tab = R.pick_table(rg["category"], rg["terrain"])
            qs = [q / 2 for q in range(0, 200)]
            vals = [R.percentile_km(tab["p"], q) for q in qs]
            ring_q = next((q for q, v in zip(qs, vals) if v is not None and v >= km_found), ">max")
        rows.append({"ring_q": ring_q, "km_found": km_found, "id": c["id"], "age": c["person"]["age"], "cat": c["person"].get("category"), "h": c["elapsed_hours"],
                     "km": c["found"].get("distance_km_reported"), "t_found": tf, "how": how,
                     "likely": b["likely"]["walk_equiv_hours"], "max": b["max"]["walk_equiv_hours"]})
    if not rows:
        sys.exit("no scored cases")
    print(f"{'id':32} {'age':>4} {'cat':10} {'h':>6} {'km':>6} {'t_found':>8} {'likely':>7} {'max':>6}  in  ring%")
    for r in rows:
        flag = "L" if r["t_found"] <= r["likely"] else ("M" if r["t_found"] <= r["max"] else "-")
        print(f"{r['id'][:32]:32} {r['age']:>4} {str(r['cat'])[:10]:10} {r['h']:>6.1f} {str(r['km']):>6} "
              f"{r['t_found']:>8.2f} {r['likely']:>7.2f} {r['max']:>6.2f}  {flag}  {str(r['ring_q']):>5}  "
              f"{r['how'] if r['how'] != 'point' else ''}")
    n = len(rows)
    inL = sum(r["t_found"] <= r["likely"] for r in rows)
    inM = sum(r["t_found"] <= r["max"] for r in rows)
    ratio = sorted(r["t_found"] / r["likely"] for r in rows if r["likely"] > 0)
    print(f"\n{n} cases: inside likely {inL} ({inL / n:.0%}), inside max {inM} ({inM / n:.0%})")
    print("t_found / likely budget, quartiles: " + ", ".join(f"{np.percentile(ratio, q):.2f}" for q in (25, 50, 75, 90)))
    rq = [r["ring_q"] for r in rows if r["ring_q"] is not None]
    if rq:
        num = [100 if q == ">max" else q for q in rq]
        print("found distance inside the statistics rings: " + ", ".join(
            f"{q}% ring {sum(x <= q for x in num) / len(num):.0%}" for q in (25, 50, 75, 95)) + f"  (n={len(num)}; ideal 25/50/75/95 %)")
    json.dump(rows, open(Path(a.out) / "score.json", "w"), indent=1, default=str)


GROUPS = (("child_0_6", 0, 6), ("child_7_15", 7, 15), ("adult_16_64", 16, 64), ("elderly_65", 65, 130))


def group_of(age: float, cat: str | None) -> str:
    if cat == "dementia":
        return "dementia"
    return next(g for g, lo, hi in GROUPS if lo <= age <= hi + 0.999)


def fit(a) -> None:
    """Per-group likely cap = 75th percentile of walking hours to the found point; with honest checks."""
    import datetime as _dt
    rows = []
    for d in sorted(Path(a.out).iterdir()):
        if not (d / "reach.json").exists():
            continue
        c = json.loads((d / "case.json").read_text())
        rep = json.loads((d / "reach.json").read_text())
        tf, how = t_found(d, c)
        if not math.isfinite(tf):
            continue
        rows.append({"id": c["id"], "g": group_of(c["person"]["age"], c["person"].get("category")),
                     "tr": c["country"].upper() == "TR", "prec": c["last_seen"].get("precision_m") or 0,
                     "tf": tf, "max": budget_now(c)["max"]["walk_equiv_hours"]})
    q = a.quantile

    def caps(rs):
        out = {}
        for g in {r["g"] for r in rs}:
            v = [r["tf"] for r in rs if r["g"] == g]
            out[g] = float(np.percentile(v, q)) if v else None
        return out

    def cover(rs, cp):
        hit = [r["tf"] <= min(r["max"], cp.get(r["g"]) or 0) for r in rs if cp.get(r["g"])]
        return (sum(hit) / len(hit), len(hit)) if hit else (float("nan"), 0)

    allc = caps(rows)
    print(f"{len(rows)} cases with a finite walking time to the found point; cap = {q:.0f}th percentile per group\n")
    print(f"{'group':12} {'n':>3} {'cap h':>6} {'in-sample':>9} {'LOO':>6}")
    loo_all = []
    for g in sorted(allc):
        rs = [r for r in rows if r["g"] == g]
        ins = cover(rs, allc)[0]
        loo = []
        for i, r in enumerate(rs):
            rest = [x["tf"] for j, x in enumerate(rs) if j != i]
            loo.append(r["tf"] <= min(r["max"], float(np.percentile(rest, q))) if rest else False)
        loo_all += loo
        print(f"{g:12} {len(rs):>3} {allc[g]:>6.2f} {ins:>9.0%} {np.mean(loo):>6.0%}")
    print(f"{'all':12} {len(rows):>3} {'':>6} {cover(rows, allc)[0]:>9.0%} {np.mean(loo_all):>6.0%}")
    tr = [r for r in rows if r["tr"]]
    intl = [r for r in rows if not r["tr"]]
    c1, n1 = cover(tr, caps(intl))
    c2, n2 = cover(intl, caps(tr))
    print(f"\ngeographic hold-out: fit on international -> Türkiye {c1:.0%} (n={n1}); fit on Türkiye -> international {c2:.0%} (n={n2})")
    good = [r for r in rows if r["prec"] <= 2000]
    cg = caps(good)
    print(f"precise start points only (<= 2 km, n={len(good)}): caps " +
          ", ".join(f"{g} {cg[g]:.2f}" for g in sorted(cg)) + f"; coverage of all cases with these caps {cover(rows, cg)[0]:.0%}")
    if a.write:
        mp = HERE.parent / "data" / "speed_model.json"
        m = json.loads(mp.read_text())
        lc = m["likely_cap"]
        for g, lo, hi in GROUPS:
            if g in allc:
                lc["groups"][g].update({"hours": round(allc[g], 2), "n": sum(r["g"] == g for r in rows)})
        lc["dementia"].update({"hours": round(allc["dementia"], 2), "n": sum(r["g"] == "dementia" for r in rows)})
        lc["_source"] = (f"CALIBRATED {_dt.date.today()} on {len(rows)} solved cases (data/cases/; roads on; direction-unknown cases "
                         f"use the median walking time over all directions at the reported distance). The likely budget is "
                         f"min(cap, physical max); cap = {q:.0f}th percentile of walking hours to the found point per group, because "
                         f"walking time does not grow with time missing. Leave-one-out coverage {np.mean(loo_all):.0%}; geographic "
                         f"hold-out {c1:.0%} (international -> Türkiye) and {c2:.0%} (Türkiye -> international). "
                         f"Re-fit with scripts/calibrate.py fit <dir> --write.")
        mp.write_text(json.dumps(m, indent=1, ensure_ascii=False))
        print(f"\nwrote caps to {mp}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("cases", nargs="+")
    r.add_argument("--out", required=True)
    r.add_argument("--force", action="store_true")
    r.add_argument("--max-cells", type=int, default=450)
    r.add_argument("--jobs", type=int, default=4, help="cases run in parallel (default 4)")
    r.add_argument("--no-conditions", action="store_true")
    r.add_argument("--roads", default="auto", choices=["auto", "overture", "overpass", "none"])
    s = sub.add_parser("score")
    s.add_argument("out")
    f = sub.add_parser("fit", help="fit the per-group likely caps and report hold-out coverage")
    f.add_argument("out")
    f.add_argument("--quantile", type=float, default=75)
    f.add_argument("--write", action="store_true", help="write the caps into data/speed_model.json")
    a = ap.parse_args()
    {"run": run, "score": score, "fit": fit}[a.cmd](a)


if __name__ == "__main__":
    main()
