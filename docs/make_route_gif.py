# /// script
# requires-python = ">=3.10"
# dependencies = ["numpy", "scipy", "pillow", "rasterio", "scikit-image", "duckdb", "pandas"]
# ///
"""Animate a heading (--to) result: the front of possible positions moves along the corridor, spreading where the
terrain lets people drift off the best route.

  uv run docs/make_route_gif.py <reach.py --out dir with --to> docs/img/route.gif [--view-km 4.5] [--size 520]
"""
import json
import math
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "skills" / "sar-sleuth" / "scripts"))
import reach as R  # noqa: E402

src, dst = Path(sys.argv[1]), Path(sys.argv[2])
arg = lambda k, d: type(d)(sys.argv[sys.argv.index(k) + 1]) if k in sys.argv else d  # noqa: E731
view_km, size = arg("--view-km", 4.5), arg("--size", 520)
rep = json.loads((src / "reach.json").read_text())
gm, hd = rep["grid"], rep["heading"]
g = R.Grid(gm["lat0"], gm["lon0"], gm["n"] * gm["res_m"], gm["res_m"])
t, ex = np.load(src / "time.npy"), np.load(src / "excess.npy")
cache = Path(".sar-cache")
z = R.elevation(g, cache / "terrain")
lc = R.landcover(g, cache / "worldcover")
vg, cut = R.view_crop(g, view_km)
z, lc, t, ex = cut(z), cut(lc), cut(t), cut(ex)
hs = R.hillshade(z, g.res)
base = np.zeros(z.shape + (3,), np.float32)
for k_, rgb in R.LC_RGB.items():
    base[lc == k_] = rgb
base = base * (0.45 + 0.55 * hs[..., None])
base = base * 0.55 + 255 * 0.45 * hs[..., None]
k = size / vg.size
pts = []
for lo, la in hd["route"]:
    r, c = vg.to_rc(la, lo)
    pts.append((float(c) * k + k / 2, float(r) * k + k / 2))
txy = hd["best_route_walking_h"]
like = rep["budget"]["likely"]["walk_equiv_hours"]
try:
    font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 16)
except Exception:  # noqa: BLE001
    font = ImageFont.load_default()
corr = np.isfinite(ex) & (ex <= 0.5) & np.isfinite(t)
dtau = txy / 14
frames = []
taus = list(np.linspace(0, txy * 1.3, 40)) + [txy * 1.3] * 10
for tau in taus:
    img = base.copy()
    trail = corr & (t <= tau)
    front = corr & (t <= tau) & (t >= tau - dtau)
    img[trail] = img[trail] * 0.65 + np.array([255, 170, 40]) * 0.35
    img[front] = img[front] * 0.35 + np.array([220, 20, 20]) * 0.65
    im = Image.fromarray(img.clip(0, 255).astype(np.uint8)).resize((size, size), Image.LANCZOS)
    d = ImageDraw.Draw(im)
    d.line(pts, fill=(20, 60, 230), width=3)
    x0, y0 = pts[0]
    d.ellipse([x0 - 6, y0 - 6, x0 + 6, y0 + 6], fill=(255, 255, 0), outline=(0, 0, 0), width=2)
    x1, y1 = pts[-1]
    d.polygon([(x1, y1), (x1, y1 - 22), (x1 + 15, y1 - 17), (x1, y1 - 12)], fill=(20, 160, 60), outline=(0, 0, 0))
    d.rectangle([0, 0, size, 48], fill=(255, 255, 255))
    d.text((8, 4), f"{tau:4.2f} h walking   best route {hd['best_route_km']} km / {txy:.2f} h", fill=(0, 0, 0), font=font)
    d.text((8, 25), "red: where they could be now (on near-best routes)   orange: passed", fill=(150, 20, 20), font=font)
    frames.append(im.convert("P", palette=Image.ADAPTIVE, colors=96))
dst.parent.mkdir(parents=True, exist_ok=True)
frames[0].save(dst, save_all=True, append_images=frames[1:], duration=150, loop=0, optimize=True)
print(f"{len(frames)} frames -> {dst} ({dst.stat().st_size // 1024} KB)")
