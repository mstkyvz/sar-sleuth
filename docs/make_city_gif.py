# /// script
# requires-python = ">=3.10"
# dependencies = ["numpy", "scipy", "pillow", "rasterio", "scikit-image", "duckdb", "pandas"]
# ///
"""Animate an urban reach.py result by clock time: the walking zone grows and stops reached by bus/tram/metro light up
at their timetable arrival time.

  uv run docs/make_city_gif.py <reach.py --out dir> docs/img/city.gif [--minutes 10] [--size 560]
"""
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "skills" / "sar-sleuth" / "scripts"))
import reach as R  # noqa: E402

src, dst = Path(sys.argv[1]), Path(sys.argv[2])
step_min = int(sys.argv[sys.argv.index("--minutes") + 1]) if "--minutes" in sys.argv else 10
size = int(sys.argv[sys.argv.index("--size") + 1]) if "--size" in sys.argv else 560
rep = json.loads((src / "reach.json").read_text())
gm = rep["grid"]
g = R.Grid(gm["lat0"], gm["lon0"], gm["n"] * gm["res_m"], gm["res_m"])
t = np.load(src / "time.npy")
cache = Path(".sar-cache")
z = R.elevation(g, cache / "terrain")
lc = R.landcover(g, cache / "worldcover")
hs = R.hillshade(z, g.res)
base = np.zeros(z.shape + (3,), np.float32)
for k, rgb in R.LC_RGB.items():
    base[lc == k] = rgb
base = base * (0.45 + 0.55 * hs[..., None])
like = rep["budget"]["likely"]["walk_equiv_hours"]
since = datetime.fromisoformat(rep["last_seen"]["time"])
hours = rep["last_seen"]["hours_since"]
stops = rep.get("transit", {}).get("stops_by_vehicle_likely", [])
for s in stops:
    s["t"] = datetime.strptime(s["arrival"], "%Y-%m-%d %H:%M").replace(tzinfo=since.tzinfo)
try:
    font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 17)
except Exception:  # noqa: BLE001
    font = ImageFont.load_default()
k = size / g.size
frames = []
n = int(hours * 60 // step_min)
for i in list(range(0, n + 1)) + [n] * 10:
    clock = since + timedelta(minutes=i * step_min)
    walked = min(i * step_min / 60, like)                       # walking zone: grows until the likely cap
    img = base.copy()
    m = np.isfinite(t) & (t <= walked)
    img[m] = img[m] * 0.5 + np.array([230, 30, 30]) * 0.5
    im = Image.fromarray(img.clip(0, 255).astype(np.uint8)).resize((size, size), Image.LANCZOS)
    d = ImageDraw.Draw(im)
    shown = [s for s in stops if s["t"] <= clock]
    for s in shown:
        r, c = g.to_rc(s["lat"], s["lon"])
        x, y = float(c) * k, float(r) * k
        d.ellipse([x - 2.5, y - 2.5, x + 2.5, y + 2.5], fill=(160, 0, 210))
    cxy = g.n * k
    d.ellipse([cxy - 6, cxy - 6, cxy + 6, cxy + 6], fill=(255, 255, 0), outline=(0, 0, 0), width=2)
    far = max((s["km"] for s in shown), default=0)
    d.rectangle([0, 0, size, 52], fill=(255, 255, 255))
    d.text((10, 5), f"{clock:%H:%M}  ({i * step_min / 60:.1f} h after last seen)", fill=(0, 0, 0), font=font)
    d.text((10, 27), f"red: on foot   purple: {len(shown)} stops by bus/tram (farthest {far:.0f} km)", fill=(90, 0, 140), font=font)
    frames.append(im.convert("P", palette=Image.ADAPTIVE, colors=96))
dst.parent.mkdir(parents=True, exist_ok=True)
frames[0].save(dst, save_all=True, append_images=frames[1:], duration=160, loop=0, optimize=True)
print(f"{len(frames)} frames -> {dst} ({dst.stat().st_size // 1024} KB)")
