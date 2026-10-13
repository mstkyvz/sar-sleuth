---
name: sar-sleuth
description: Where could a missing person have walked to? From the last seen point, the time since, and who the person is (age, sex, fitness, limp/cane, injury, dementia, can they run), compute a walking-time map over real terrain (slope, land cover, paths, water, cliffs, day/night) and give the likely and maximum reach as map, GeoJSON and numbers. Use for lost hikers, children, elderly people who wandered off, in forests, mountains or countryside.
---

# sar-sleuth (v0.1)

> `${CLAUDE_SKILL_DIR}` below is the directory containing this SKILL.md.

## What it computes

One command:

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/reach.py --at <lat,lon> --since <ISO time with offset> --hours <since last seen> \
  --age <years> [--sex m|f] [--fitness low|normal|high] [--mobility normal|limp|cane|walker] \
  [--injured] [--dementia] [--can-run yes|no] --out <dir>
```

1. **Person → speeds** (`person.py`): flat walking speed from age and sex (Bohannon & Andrews 2011; child norms under 20), scaled for fitness, limp/cane/walker, injury. Each person also gets a slope sensitivity, the steepest slope they can walk up or down, the steepest ground they can cross off paths, and whether / how long they can run.
2. **Time → budget** (hours of daytime walking at the person's speed):
   - **max**: the physical limit. Hours since last seen split into light and dark (sun below −6°); keeps going as long as the body allows (moving hours per 24 h by age, fatigue over days), runs at the start if able. Contained the found point in 70 of 71 solved cases.
   - **likely**: `min(calibrated cap, max)`. Solved cases show the walking time to the found point does **not** grow with time missing: people walk for a while, then stop. The cap per group (child 0–6, 7–15, adult, 65+, dementia) is the 75th percentile of that walking time over 70 cases (roads on); leave-one-out coverage 71 %. Geographic hold-out: international → Türkiye only 51 %, so in Türkiye mention that the orange band may matter.
2b. **Found-distance rings**: straight-line 25/50/75/95 % distances for the person's category and terrain (dementia, child 1–3 …, hiker; mountain/flat/urban/dry) from published lost-person statistics (`data/distance_stats.json`), clipped to the reachable area.
3. **Terrain → time map** (`reach.py`): elevation (AWS Terrarium), land cover (ESA WorldCover 10 m), OSM roads/tracks/paths (paved vs unpaved) and cliffs. Speed per step from Wood et al. 2023 (walking slope in the direction of travel, hill slope, path type, obstruction), so uphill and downhill differ. Water and cliffs block; roads over water (bridges) pass. Shortest walking time from the point to every cell on a 16-neighbour grid.
4. **Output** in `--out`: `reach.png` (red = likely limit, orange = max limit, blue = statistics rings, white = 1/2/4/8 h lines), `reach.geojson` (open in any map app), `reach.json` (every parameter, areas, farthest distance per direction), `time.npy` + `grid.json`.

5. **Priority** (`priority.py`, on by default): probability per cell = found-distance statistics × walking-time weight × attractors by category (paths, drainages, structures, downhill for dementia), `priority.png`, ranked `sectors.csv/geojson`, `gpx/sector_NN.gpx` for teams, and the weather over the missing hours (Open-Meteo) with cold/rain/heat flags in `reach.json` → `weather`. When answering, give the top sectors and the weather flags.

6. **Transit** (`transit.py`, `--transit auto|yes|no`): in built-up areas for ages 7+, GTFS timetables of the missing period (feeds found automatically in the Mobility Database catalog; `--gtfs <zip>` adds one) → Connection Scan from the stops the person can walk to → walking on from where they get off. `reach.json` → `transit`: stops reachable by vehicle with arrival times, extra reach. Report the farthest stops and line ends as places to check (drivers, CCTV); state it is conditional on boarding.

`person.py --at … --since … --age …` prints the profile and budget alone, without terrain.

## Question → command

| Question | Run |
|---|---|
| "A 3-year-old went missing at <place>; how far in N days?" | Geocode the place (village / yayla / trailhead; `nominatim.openstreetmap.org/search`, or an OSM `place=locality` near the village if the named spot is missing). Run `reach.py --age 3 --hours 24` and again with `--hours 48`, `72`. Answer with `likely` / `max` → `max_km` and `median_km` per run |
| "From <lat,lon>, a 60-year-old with a cane walks north/south: how far, to where?" | `reach.py --at <lat,lon> --age 60 --mobility cane --toward N --toward S` (Turkish `K G D B KD KB GD GB` also work, or a bearing in degrees). Answer with `toward.<dir>.likely/max`: farthest point (lat,lon), straight-line km, walking-route km; the routes are in `reach.geojson` |
| "Last seen at 18:00, found at 06:00: was X km plausible?" | `--since <18:00 with offset> --hours 12`; compare the found point's walking time `time.npy` (via `grid.json`) with the `likely` and `max` budgets |
| Unknown age / fitness / time | Run both ends (`--fitness low` and `high`, earliest and latest time) and report the range |

When the exact last seen spot is uncertain (a named yayla without a mapped point), say which point you used and how far it may be off; the reach limits shift with it.

## How to use the result

- Search **inside the red line first**, then the orange band. Inside the red area, start near the point and work outward along the blue rings: in the statistics half of the people of each category are found within the 50 % ring.
- Solved news cases were found farther out than the statistics rings suggest (only 10 % within the 50 % ring, n=71). News over-reports long, hard searches and rounds distances, so the rings were not changed; treat them as a lower bound on how far to search, and the red line as the area that holds about 3 in 4 cases.
- Read `reach.json` → `likely.farthest_km_by_direction`: a direction that goes much farther than the others is an easy corridor (road, valley floor, ridge path) and deserves early checking.
- Paths, tracks and roads are where walking is fast, so the contours stretch along them; drainages and valley floors pull downhill walkers.
- State the inputs you assumed. If the age, the mobility or the time last seen is uncertain, run both ends (e.g. `--fitness low` and `--fitness high`) and show both maps.

## Limits (say them when reporting)

- Every number is in `data/speed_model.json` with its source. `likely_cap` is calibrated (70 cases in `data/cases/`, scores in `data/cases/calibration_*.json`); blocks marked **ASSUMPTION** (dense vegetation factors, running, moving hours per day, night behaviour) only shape the **max** limit and are not calibrated yet.
- The adult (16–64) group has only 7 solved cases: its likely cap is the least reliable.
- Grid cells are 25–250 m, chosen from the area: small streams, fences, single steep steps and narrow paths missing from OSM are not seen. A 24 h max scenario for a fit adult covers ~100 km and runs at coarse resolution; for planning a ground search, also run with a smaller `--hours` window.
- Elevation is ~30 m resolution: short cliffs not tagged in OSM can be missed.
- It models walking only. A ride in a vehicle, a boat or public transport is outside the map.
- Overpass (OSM) can be slow; finished chunks are cached in `.sar-cache/`, so re-running continues where it stopped.

## Calibration

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/calibrate.py run data/cases/tr_cases.json data/cases/intl_cases.json --out calib/ --roads none --max-cells 300 --jobs 6
uv run ${CLAUDE_SKILL_DIR}/scripts/calibrate.py score calib/
```

Add solved cases to `data/cases/` (schema: see any entry), re-run, then update `likely_cap` in `data/speed_model.json`. Cases found long after death (> 10 days) are excluded from timing.

## Tests

```bash
cd ${CLAUDE_SKILL_DIR} && uv run -q --with numpy --with scipy --with pillow --with scikit-image --with rasterio python -m unittest discover -s tests
```
