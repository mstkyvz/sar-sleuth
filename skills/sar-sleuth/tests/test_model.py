"""Model checks on synthetic terrain (no network).

  cd skills/sar-sleuth && uv run -q --with numpy --with scipy --with pillow --with scikit-image --with rasterio --with pandas python -m unittest discover -s tests
"""
import math
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import person as P  # noqa: E402
import reach as R  # noqa: E402

MODEL = P.load_model()


def flat_grid(n=40, res=25.0):
    g = R.Grid(41.0, 29.0, n * res, res)
    z = np.zeros(g.lat.shape, np.float32)
    return g, z


def run(g, z, lc, road=None, cliff=None, person=None, limit=10.0):
    road = np.zeros(z.shape, np.uint8) if road is None else road
    cliff = np.zeros(z.shape, bool) if cliff is None else cliff
    pr = P.build_profile(person or P.Person(age=35, sex="m"), MODEL)
    cls, extra, passable = R.cell_classes(lc, road, cliff, MODEL)
    return R.travel_hours(g, z, cls, extra, passable, pr, MODEL, limit)[0], pr


class PersonTest(unittest.TestCase):
    def test_adult_flat_speed_matches_bohannon(self):
        pr = P.build_profile(P.Person(age=45, sex="m"), MODEL)
        self.assertAlmostEqual(pr.walk_kmh, 1.43 * 3.6, places=2)

    def test_slower_and_more_sensitive_with_age_and_cane(self):
        young = P.build_profile(P.Person(age=30), MODEL)
        old = P.build_profile(P.Person(age=82), MODEL)
        cane = P.build_profile(P.Person(age=82, mobility="cane"), MODEL)
        self.assertGreater(young.walk_kmh, old.walk_kmh)
        self.assertGreater(old.walk_kmh, cane.walk_kmh)
        self.assertLess(young.sens, old.sens)
        self.assertLess(cane.max_climb, young.max_climb)
        self.assertIsNone(old.run_kmh)
        self.assertIsNotNone(young.run_kmh)

    def test_can_run_override(self):
        self.assertIsNotNone(P.build_profile(P.Person(age=75, can_run=True), MODEL).run_kmh)
        self.assertIsNone(P.build_profile(P.Person(age=30, can_run=False), MODEL).run_kmh)

    def test_budget_night_and_cap(self):
        pr = P.build_profile(P.Person(age=35), MODEL)
        t0 = datetime(2026, 6, 21, 12, 0, tzinfo=timezone(timedelta(hours=3)))
        day = P.budget(pr, t0, 4, 41.0, 29.0)                   # midday, all light
        night = P.budget(pr, t0 + timedelta(hours=12), 4, 41.0, 29.0)
        self.assertAlmostEqual(day["max"]["moving_hours"], 4.0, places=1)
        self.assertLess(night["likely"]["walk_equiv_hours"], day["likely"]["walk_equiv_hours"])
        long = P.budget(pr, t0, 24, 41.0, 29.0)
        self.assertLessEqual(long["likely"]["moving_hours"], pr.cap_likely + 1e-6)
        three = P.budget(pr, t0, 72, 41.0, 29.0)
        self.assertLess(three["max"]["moving_hours"], 3 * long["max"]["moving_hours"])   # fatigue over days
        kid = P.build_profile(P.Person(age=3), MODEL)
        self.assertEqual(P.budget(kid, t0 + timedelta(hours=12), 4, 41.0, 29.0)["likely"]["moving_hours"], 0.0)  # toddler: no moving at night
        self.assertGreater(long["max"]["run_bonus_hours"], 0)

    def test_sun_altitude(self):
        noon = datetime(2026, 6, 21, 9, 50, tzinfo=timezone.utc)          # local solar noon at 29 E
        self.assertAlmostEqual(P.sun_altitude(noon, 41.0, 29.0), 90 - 41 + 23.44, delta=1.0)
        midnight = datetime(2026, 6, 21, 21, 50, tzinfo=timezone.utc)
        self.assertLess(P.sun_altitude(midnight, 41.0, 29.0), -6)


class TerrainTest(unittest.TestCase):
    def test_flat_paved_speed(self):
        g, z = flat_grid()
        lc = np.full(z.shape, 50, np.uint8)
        road = np.zeros(z.shape, np.uint8)
        road[g.n, :] = 1                                             # east-west road through the point
        t, pr = run(g, z, lc, road)
        d_km = (g.n - 2) * g.res / 1000
        self.assertAlmostEqual(t[g.n, 2], d_km / pr.walk_kmh, delta=0.01 * d_km / pr.walk_kmh)

    def test_offroad_slower_than_road_and_forest_slower_than_grass(self):
        g, z = flat_grid()
        grass = run(g, z, np.full(z.shape, 30, np.uint8))[0]
        forest = run(g, z, np.full(z.shape, 10, np.uint8))[0]
        scrub = run(g, z, np.full(z.shape, 20, np.uint8))[0]
        self.assertLess(grass[0, g.n], forest[0, g.n])
        self.assertLess(forest[0, g.n], scrub[0, g.n])

    def test_uphill_slower_than_downhill(self):
        g, z = flat_grid()
        z = (g.y * 0.15).astype(np.float32)                         # 15 % slope rising to the north
        lc = np.full(z.shape, 30, np.uint8)
        t, _ = run(g, z, lc)
        self.assertGreater(t[1, g.n], t[-2, g.n] * 1.1)              # north (uphill) takes longer than south (Wood: ~18 % at 8.5 deg)

    def test_isotropy_on_flat_ground(self):
        g, z = flat_grid(60)
        t, _ = run(g, z, np.full(z.shape, 30, np.uint8))
        rr, cc = np.nonzero(np.isfinite(t))
        r = 50
        ang = np.radians(np.arange(0, 360, 5))
        vals = [t[int(round(g.n - r * math.cos(a))), int(round(g.n + r * math.sin(a)))] for a in ang]
        self.assertLess((max(vals) - min(vals)) / np.mean(vals), 0.06)  # 16-neighbour grid: < 6 % spread

    def test_water_and_cliff_block_bridge_passes(self):
        g, z = flat_grid()
        lc = np.full(z.shape, 30, np.uint8)
        lc[g.n - 5, :] = 80                                          # river north of the point, one cell wide
        t, _ = run(g, z, lc)
        self.assertTrue(np.isinf(t[0, g.n]))                          # knight moves can't jump it
        road = np.zeros(z.shape, np.uint8)
        road[:, g.n] = 2                                             # a path with a bridge
        t2, _ = run(g, z, lc, road)
        self.assertTrue(np.isfinite(t2[0, g.n]))
        cliff = np.zeros(z.shape, bool)
        cliff[g.n + 5, :] = True
        t3, _ = run(g, z, np.full(z.shape, 30, np.uint8), cliff=cliff)
        self.assertTrue(np.isinf(t3[-1, g.n]))

    def test_cane_blocked_by_steep_slope(self):
        g, z = flat_grid()
        z = (np.abs(g.y) * 0.7).astype(np.float32)                  # 35 deg hillsides both ways
        lc = np.full(z.shape, 30, np.uint8)
        fit, _ = run(g, z, lc)
        cane, _ = run(g, z, lc, person=P.Person(age=75, mobility="cane"))
        self.assertTrue(np.isfinite(fit[0, g.n]))
        self.assertTrue(np.isinf(cane[0, g.n]))
        road = np.zeros(z.shape, np.uint8)
        road[:, g.n] = 2                                             # a path up the slope: still too steep to climb
        self.assertTrue(np.isinf(run(g, z, lc, road, person=P.Person(age=75, mobility="cane"))[0][0, g.n]))


if __name__ == "__main__":
    unittest.main()


class DirectionTest(unittest.TestCase):
    def test_farthest_follows_road_and_route_connects(self):
        g, z = flat_grid()
        lc = np.full(z.shape, 20, np.uint8)                          # scrub everywhere
        road = np.zeros(z.shape, np.uint8)
        road[: g.n + 1, g.n] = 1                                     # paved road north only
        pr = P.build_profile(P.Person(age=35, sex="m"), MODEL)
        cls, extra, passable = R.cell_classes(lc, road, np.zeros(z.shape, bool), MODEL)
        t, pred = R.travel_hours(g, z, cls, extra, passable, pr, MODEL, 0.15)
        n = R.farthest(g, t, pred, 0.15, R.parse_bearing("K"))       # Turkish K = north
        s = R.farthest(g, t, pred, 0.15, R.parse_bearing("S"))
        self.assertGreater(n["straight_km"], 1.5 * s["straight_km"])        # road ~1.75x scrub speed
        self.assertEqual(n["route"][0], [round(g.lon0, 6), round(g.lat0, 6)])
        self.assertAlmostEqual(n["route"][-1][1], n["lat"], places=5)
        self.assertGreaterEqual(n["route_km"], n["straight_km"] - 1e-6)


class TransitTest(unittest.TestCase):
    def test_connection_scan(self):
        import pandas as pd
        import transit as T
        stops = pd.DataFrame({"stop_id": ["A", "B", "C", "D"], "stop_lat": [41.0, 41.01, 41.02, 41.0201],
                              "stop_lon": [29.0, 29.0, 29.0, 29.0005]})
        # trip 1: A 600 s -> B 900 -> C 1200; trip 2 leaves A at 100 s (before they can reach the stop)
        conn = np.array([[0, 1, 600, 900, 1], [1, 2, 900, 1200, 1], [0, 1, 100, 400, 2]], np.int64)
        conn = conn[np.argsort(conn[:, 2])]
        walk = np.array([0.1, np.inf, np.inf, np.inf])          # 6 min walk to A only
        arr, used = T.scan(stops, conn, walk, 4.0, 1.0)
        self.assertEqual(arr[1], 900)
        self.assertEqual(arr[2], 1200)
        self.assertAlmostEqual(used[2], 0.1)
        self.assertTrue(np.isfinite(arr[3]))                    # D is a ~40 m walk from C
        arr2, _ = T.scan(stops, conn, np.array([0.2, np.inf, np.inf, np.inf]), 4.0, 0.15)
        self.assertTrue(np.isinf(arr2[2]))                      # stop A beyond the walking budget


class HeadingTest(unittest.TestCase):
    def test_corridor_follows_road_and_finds_detour(self):
        import route as RT
        g, z = flat_grid(50)
        lc = np.full(z.shape, 20, np.uint8)                          # scrub
        road = np.zeros(z.shape, np.uint8)
        road[: g.n + 1, g.n] = 1                                     # straight road north to the destination
        pr = P.build_profile(P.Person(age=35, sex="m"), MODEL)
        cls, extra, passable = R.cell_classes(lc, road, np.zeros(z.shape, bool), MODEL)
        t, pred = R.travel_hours(g, z, cls, extra, passable, pr, MODEL, 5.0)
        dest = g.to_ll(2, g.n)
        info, arr = RT.corridor(g, R.travel_hours.graph, t, pred, (float(dest[0]), float(dest[1])), 0.2, 2.0, z_arr=z)
        self.assertAlmostEqual(info["best_route_km"], (g.n - 2) * g.res / 1000, delta=0.05)
        ex = arr["excess"]
        self.assertAlmostEqual(float(ex[g.n // 2, g.n]), 0.0, places=3)          # on the road: best route
        self.assertGreater(float(ex[g.n // 2, g.n + 15]), 0.15)                  # off in the scrub: a longer route
        self.assertFalse(info["position_likely"]["arrived"])
        self.assertTrue(info["position_max"]["arrived"])
