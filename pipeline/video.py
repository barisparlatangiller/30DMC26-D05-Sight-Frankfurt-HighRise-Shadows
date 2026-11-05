"""
A ~12 s time-lapse of 21 June for LinkedIn: the towers' shadows sweep over Frankfurt every 5 minutes while the hours of
direct sun they take build up in blue. Rendered straight from the shadow engine (no screen recording).

  python pipeline/video.py [--workers 4]
Output  build/video/day5_frankfurt_shadows_21jun.mp4 (1080×1350, H.264) · build/video/cover.png
"""
import argparse, json, math, os, subprocess, time
from multiprocessing import Pool
from pathlib import Path

import numpy as np
import geopandas as gpd
import shapely
import requests
from PIL import Image, ImageDraw, ImageFont
from pyproj import Transformer
from rasterio import features
from rasterio.transform import Affine

import shadows as S

W, H = 1080, 1350
MAP_Y, MAP_H = 190, 1060                     # map panel inside the frame
CENTER = (8.6705, 50.1112)                   # Bankenviertel / Westend / Messe
SPAN = 3600.0                                # metres across the map panel
FPS = 24
BG, BLD, TOWER, WATER, INK, INK2 = (246, 243, 238), (214, 209, 200), (232, 89, 12), (222, 229, 233), (43, 41, 37), (111, 106, 97)
SHADE = np.array([30, 38, 92])
BLUES = np.array([[208, 226, 242], [150, 192, 228], [84, 146, 205], [38, 94, 166], [16, 44, 104]], dtype=float)
B = Path("build"); OUT = B / "video"


def font(size, bold=False):
    for name in (["Inter:bold", "DejaVu Sans:bold"] if bold else ["Inter", "DejaVu Sans"]):
        try:
            f = subprocess.run(["fc-match", "-f", "%{file}", name], capture_output=True, text=True).stdout.strip()
            if f:
                return ImageFont.truetype(f, size)
        except Exception:                                                # noqa: BLE001
            pass
    return ImageFont.load_default()


def water(bounds4326):
    w, s_, e, n = bounds4326
    q = f'[out:json][timeout:120];(way["natural"="water"]({s_},{w},{n},{e});relation["natural"="water"]({s_},{w},{n},{e});way["waterway"="riverbank"]({s_},{w},{n},{e}););out geom;'
    try:
        d = requests.post("https://overpass-api.de/api/interpreter", data={"data": q}, timeout=180).json()
    except Exception as e:                                               # noqa: BLE001
        print("water not available:", e); return []
    polys = []
    for el in d["elements"]:
        rings = [el["geometry"]] if el["type"] == "way" and el.get("geometry") else \
                [m["geometry"] for m in el.get("members", []) if m.get("role") == "outer" and m.get("geometry")]
        for rg in rings:
            if len(rg) >= 4:
                polys.append(shapely.Polygon([(p["lon"], p["lat"]) for p in rg]))
    return polys


def blues(hours):
    """hours of sun lost → RGB + alpha (0.5 h faint … 5 h deep)"""
    t = np.clip(hours / 5.0, 0, 1) * (len(BLUES) - 1)
    i = np.clip(np.floor(t).astype(int), 0, len(BLUES) - 2)
    f = (t - i)[..., None]
    rgb = BLUES[i] * (1 - f) + BLUES[i + 1] * f
    a = np.clip(hours / 1.0, 0, 1) * 0.85
    return rgb, a


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 2)
    ap.add_argument("--step", type=int, default=5)
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    fr = OUT / "frames"; fr.mkdir(exist_ok=True)
    for f in fr.glob("*.png"):
        f.unlink()

    to25 = Transformer.from_crs(4326, 25832, always_xy=True)
    cx, cy = to25.transform(*CENTER)
    px = SPAN / W
    x0, y1 = cx - SPAN / 2, cy + MAP_H * px / 2
    tr = Affine(px, 0, x0, 0, -px, y1)
    area = shapely.box(x0, y1 - MAP_H * px, x0 + SPAN, y1)
    c, r, reach = S.prepare(B, 2.0, area=area)
    print(f"video: {len(c)} towers can reach the frame", flush=True)

    # static base: water, every building, towers
    base = np.zeros((MAP_H, W, 3), dtype=float); base[:] = BG
    to4 = Transformer.from_crs(25832, 4326, always_xy=True)
    bw = to4.transform(area.bounds[0], area.bounds[1]) + to4.transform(area.bounds[2], area.bounds[3])
    wp = [shapely.transform(p, lambda xy: np.c_[to25.transform(xy[:, 0], xy[:, 1])]) for p in water((bw[0], bw[1], bw[2], bw[3]))]
    if wp:
        m = features.rasterize(wp, out_shape=(MAP_H, W), transform=tr, dtype="uint8").astype(bool)
        base[m] = WATER
    rr = r[r.intersects(area)]
    m = features.rasterize(rr.geometry.values, out_shape=(MAP_H, W), transform=tr, dtype="uint8").astype(bool)
    base[m] = BLD
    tow = features.rasterize(c[c.intersects(area)].geometry.values, out_shape=(MAP_H, W), transform=tr, dtype="uint8").astype(bool)

    # shadows every 5 minutes on 21 June
    steps = S.sun_times(6, a.step)
    jobs = [(i, az, alt, 2.0, False) for i, (_, az, alt) in enumerate(steps)]
    masks = [None] * len(steps)
    t0 = time.time()
    with Pool(a.workers) as pool:
        for ti, u, _, _, _, _ in pool.imap_unordered(S.run_step, jobs, chunksize=1):
            if u is not None:
                g = shapely.intersection(shapely.from_wkb(u), area.buffer(50))
                masks[ti] = features.rasterize([(g, 1)], out_shape=(MAP_H, W), transform=tr, dtype="uint8").astype(bool) if not g.is_empty else None
    print(f"{len(steps)} steps in {time.time() - t0:.0f} s", flush=True)

    st = json.loads(Path("docs/data/stats.json").read_text())
    city3h = st.get("shadows", {}).get("days", {}).get("06", {}).get("loss_km2_3h")
    ntow = st.get("casters", {}).get("buildings")
    F = {k: font(*v) for k, v in {"title": (42, True), "eyebrow": (24, True), "clock": (64, True), "small": (26, False),
                                  "label": (22, True), "big": (52, True), "foot": (22, False)}.items()}

    def frame(loss, shade, clock, alt, title_alpha=0.0, outro=0.0):
        img = base.copy()
        rgb, al = blues(loss)
        img = img * (1 - al[..., None]) + rgb * al[..., None]
        if shade is not None:
            img[shade] = img[shade] * 0.62 + SHADE * 0.38
        img[tow] = TOWER
        im = Image.new("RGB", (W, H), BG)
        im.paste(Image.fromarray(img.astype("uint8")), (0, MAP_Y))
        d = ImageDraw.Draw(im)
        d.text((48, 36), "#30DAYMAPCHALLENGE · DAY 5 · SENSES: SIGHT · FRANKFURT", font=F["eyebrow"], fill=(194, 87, 12))
        d.text((48, 74), "Who gets the sun next to a tower?", font=F["title"], fill=INK)
        d.text((48, 134), "21 June · shadows of every building of 30 m and more", font=F["small"], fill=INK2)
        if clock and outro == 0:
            d.text((W - 48, 36), clock, font=F["clock"], fill=INK, anchor="ra")
            d.text((W - 48, 112), f"sun {alt:.0f}° high", font=F["small"], fill=INK2, anchor="ra")
        elif outro > 0:
            d.text((W - 48, 44), "whole day", font=F["label"], fill=INK2, anchor="ra")
        # legend
        ly = MAP_Y + MAP_H + 22
        for k in range(5):
            d.rectangle([48 + k * 70, ly, 48 + (k + 1) * 70 - 2, ly + 16], fill=tuple(int(v) for v in BLUES[k]))
        for k, t in enumerate(["0.5", "1", "2", "3", "5+ h"]):
            d.text((48 + k * 70, ly + 22), t, font=F["foot"], fill=INK2)
        d.text((420, ly - 2), "direct sun lost to the towers, accumulating", font=F["foot"], fill=INK2)
        d.rectangle([420, ly + 30, 438, ly + 46], fill=TOWER); d.text((446, ly + 26), "building ≥ 30 m (LoD2)", font=F["foot"], fill=INK2)
        if outro > 0:
            ov = Image.new("RGBA", (W, H), (0, 0, 0, 0)); o = ImageDraw.Draw(ov)
            a_ = int(235 * min(1, outro))
            o.rounded_rectangle([60, MAP_Y + 300, W - 60, MAP_Y + 760], 28, fill=(253, 251, 247, a_))
            col = (16, 44, 104, int(255 * min(1, outro))); c2 = (43, 41, 37, int(255 * min(1, outro)))
            o.text((W // 2, MAP_Y + 350), "One summer day", font=F["small"], fill=c2, anchor="ma")
            o.text((W // 2, MAP_Y + 400), f"{city3h:.1f} km²" if city3h else "", font=F["big"], fill=col, anchor="ma")
            o.text((W // 2, MAP_Y + 470), "of Frankfurt lose 3+ hours of direct sun", font=F["small"], fill=c2, anchor="ma")
            o.text((W // 2, MAP_Y + 506), f"to its {ntow:,} buildings of 30 m and more" if ntow else "", font=F["small"], fill=c2, anchor="ma")
            o.text((W // 2, MAP_Y + 600), "Interactive map · every month, every hour", font=F["label"], fill=c2, anchor="ma")
            o.text((W // 2, MAP_Y + 640), "barisparlatangiller.github.io", font=F["label"], fill=(194, 87, 12, int(255 * min(1, outro))), anchor="ma")
            im = Image.alpha_composite(im.convert("RGBA"), ov).convert("RGB")
        return im

    n = 0
    loss = np.zeros((MAP_H, W), dtype="float32")
    zero = np.zeros_like(loss)
    for k in range(int(FPS * 0.6)):                                          # intro: empty city at dawn
        frame(zero, None, steps[0][0].strftime("%H:%M"), steps[0][2]).save(fr / f"f{n:04d}.png"); n += 1
    for i, (ts, az, alt) in enumerate(steps):
        if masks[i] is not None:
            loss += masks[i] * (a.step / 60)
        frame(loss, masks[i], ts.strftime("%H:%M"), alt).save(fr / f"f{n:04d}.png"); n += 1
    for k in range(int(FPS * 3.2)):                                          # outro: the day's total + numbers
        frame(loss, None, "whole day", steps[-1][2], outro=min(1, k / (FPS * 0.6))).save(fr / f"f{n:04d}.png"); n += 1
    frame(loss, None, "whole day", steps[-1][2], outro=1).save(OUT / "cover.png")
    out = OUT / "day5_frankfurt_shadows_21jun.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(FPS), "-i", str(fr / "f%04d.png"),
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "24", "-pix_fmt", "yuv420p",
                    "-movflags", "+faststart", str(out)], check=True)
    print(f"video: {n} frames, {n / FPS:.1f} s, {out.stat().st_size / 1e6:.1f} MB", flush=True)


if __name__ == "__main__":
    main()
