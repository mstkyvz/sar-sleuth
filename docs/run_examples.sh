#!/bin/sh
# Regenerate examples/ and the README images. Run from the repository root.
set -e
R="uv run -q skills/sar-sleuth/scripts/reach.py"
TMP="${TMPDIR:-/tmp}/sar-examples"
mkdir -p "$TMP"

$R --at 41.4690,34.7672 --since 2026-10-10T08:00+03:00 --hours 48 --age 69 --sex f --category forager --grid-km 15 --out examples/boyabat_woman_69_likely
for h in 48 72 120; do
  res=250; [ "$h" = 120 ] && res=400
  case $h in 48) d=2days;; 72) d=3days;; 120) d=5days;; esac
  $R --at 41.4690,34.7672 --since 2026-10-10T08:00+03:00 --hours $h --age 69 --sex f --category forager \
     --roads none --res $res --no-priority --out examples/boyabat_woman_69_$d
done
$R --at 39.3341,35.7679 --since 2026-05-28T20:15+03:00 --hours 10 --age 3 --sex f --out examples/cayiralan_child_3
$R --at 40.0985,29.1300 --to 40.1370,29.1062 --since 2026-10-13T11:00+03:00 --hours 3 --age 35 --sex m --view-km 6 --out examples/uludag_hiker_heading
uv run -q docs/make_route_gif.py examples/uludag_hiker_heading docs/img/uludag_heading.gif --view-km 6 --size 520
cp examples/uludag_hiker_heading/route.png docs/img/uludag_heading_route.png
$R --at 40.0985,29.1300 --since 2026-10-13T11:00+03:00 --hours 24 --age 60 --sex m --mobility cane --toward N --toward S --out examples/uludag_man_60_cane
$R --at 36.8875,30.7056 --since 2026-10-13T10:00+03:00 --hours 6 --age 78 --sex m --dementia --out examples/antalya_dementia_78

uv run -q docs/make_gif.py examples/boyabat_woman_69_likely docs/img/boyabat_reach.gif --step 0.15 --size 420
$R --at 36.8875,30.7056 --since 2026-10-13T10:00+03:00 --hours 4 --age 78 --sex m --dementia --max-cells 700 --no-priority --out "$TMP/antalya_gif"
uv run -q docs/make_city_gif.py "$TMP/antalya_gif" docs/img/antalya_city.gif --minutes 10 --size 520

cp examples/boyabat_woman_69_likely/reach.png docs/img/boyabat_map.png
cp examples/boyabat_woman_69_likely/priority.png docs/img/boyabat_priority.png
cp examples/cayiralan_child_3/reach.png docs/img/cayiralan_child_map.png
cp examples/uludag_man_60_cane/reach.png docs/img/uludag_cane_map.png
cp examples/antalya_dementia_78/reach.png docs/img/antalya_transit_map.png
cp docs/img/boyabat_reach.gif examples/boyabat_woman_69_likely/
# keep the repository light: per-cell arrays are regenerated on demand
find examples -name '*.npy' -delete
find examples -name grid.json -delete
