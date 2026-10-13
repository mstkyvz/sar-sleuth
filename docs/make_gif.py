# /// script
# requires-python = ">=3.10"
# dependencies = ["numpy", "scipy", "pillow", "rasterio", "scikit-image", "duckdb"]
# ///
"""Animate a reach.py result: the reachable area growing hour by hour.

  uv run docs/make_gif.py <reach.py --out dir> docs/img/reach.gif [--step 0.1] [--size 640]
"""
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "skills" / "sar-sleuth" / "scripts"))
import reach as R  # noqa: E402

src, dst = Path(sys.argv[1]), Path(sys.argv[2])
step = float(sys.argv[sys.argv.index("--step") + 1]) if "--step" in sys.argv else 0.1
size = int(sys.argv[sys.argv.index("--size") + 1]) if "--size" in sys.argv else 640
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
mx = rep["budget"]["max"]["walk_equiv_hours"]
try:
    font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 18)
except Exception:  # noqa: BLE001
    font = ImageFont.load_default()
frames = []
end = min(mx, like * 2.2, float(np.nanmax(np.where(np.isfinite(t), t, np.nan))))   # stop past the likely zone
hours = list(np.arange(step, end + step / 2, step)) + [end] * 12
for h in hours:
    img = base.copy()
    m = np.isfinite(t) & (t <= h)
    col = np.array([230, 30, 30]) if h <= like else np.array([255, 150, 0])
    img[m] = img[m] * 0.5 + col * 0.5
    im = Image.fromarray(img.clip(0, 255).astype(np.uint8)).resize((size, size), Image.LANCZOS)
    d = ImageDraw.Draw(im)
    c = size / 2
    d.ellipse([c - 6, c - 6, c + 6, c + 6], fill=(255, 255, 0), outline=(0, 0, 0), width=2)
    km = float(np.hypot(g.x, g.y)[m].max() / 1000) if m.any() else 0
    label = f"{h:4.1f} h walking   farthest {km:4.1f} km   " + ("likely zone" if h <= like else "beyond likely: physical limit")
    d.rectangle([0, 0, size, 30], fill=(255, 255, 255))
    d.text((10, 6), label, fill=(0, 0, 0), font=font)
    frames.append(im.convert("P", palette=Image.ADAPTIVE, colors=128))
dst.parent.mkdir(parents=True, exist_ok=True)
frames[0].save(dst, save_all=True, append_images=frames[1:], duration=120, loop=0, optimize=True)
print(f"{len(frames)} frames -> {dst} ({dst.stat().st_size // 1024} KB)")
