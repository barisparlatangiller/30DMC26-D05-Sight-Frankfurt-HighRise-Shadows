"""
Web layers for the map.

  sunloss/MM.png + sunloss/index.json   hours of direct sun lost to the towers on the 21st of each month
                                        (5 m grid, warped to Web Mercator, classed and coloured, transparent below 0.5 h)
  buildings.pmtiles  layer "b"          every LoD2 building part within reach of a tower shadow (h = height above
                                        ground, t = 1 for towers ≥ 30 m), Z12–16
  towers.geojson                        + per month: shaded ground / roofs (ha·h) and number of buildings shaded
  stats.json["sunloss"]                 areas per month and loss class

  python pipeline/build_web.py
"""
import json, subprocess, tempfile, time
from pathlib import Path

import numpy as np
import geopandas as gpd
import shapely
from PIL import Image
from rasterio.transform import Affine
from rasterio.warp import reproject, Resampling, calculate_default_transform, transform_bounds

B, W = Path("build"), Path("docs/data")
BINS = [0.5, 1, 2, 3, 5]
COLS = [(208, 226, 242, 150), (150, 192, 228, 175), (84, 146, 205, 195), (38, 94, 166, 210), (16, 44, 104, 225)]   # light → deep blue: shade = cooler


def sunloss():
    x0, y1, res, w, h = np.load(B / "sunloss_meta.npy")
    w, h = int(w), int(h)
    src_tr = Affine(res, 0, x0, 0, -res, y1)
    bounds = (x0, y1 - h * res, x0 + w * res, y1)
    dst_tr, dw, dh = calculate_default_transform(25832, 3857, w, h, *bounds)
    out, stats = {}, {}
    for f in sorted(B.glob("sunloss_[0-9][0-9].npy")):
        mo = f.stem[-2:]
        a = np.load(f)
        dst = np.zeros((dh, dw), dtype="float32")
        reproject(a, dst, src_transform=src_tr, src_crs=25832, dst_transform=dst_tr, dst_crs=3857,
                  resampling=Resampling.nearest)
        cls = np.digitize(dst, BINS)                                       # 0 = < 0.5 h
        rgba = np.zeros((dh, dw, 4), dtype="uint8")
        for i, c in enumerate(COLS, start=1):
            rgba[cls == i] = c
        Image.fromarray(rgba, "RGBA").save(W / "sunloss" / f"{mo}.png", optimize=True)
        stats[mo] = {"km2": [round(float((a >= b).sum() * res * res / 1e6), 3) for b in BINS],
                     "max_h": round(float(a.max()), 1)}
    l, b, r, t = transform_bounds(3857, 4326, dst_tr.c, dst_tr.f + dh * dst_tr.e, dst_tr.c + dw * dst_tr.a, dst_tr.f)
    (W / "sunloss" / "index.json").write_text(json.dumps({"corners": [[l, t], [r, t], [r, b], [l, b]], "bins": BINS}))
    return stats


def buildings():
    r = gpd.read_parquet(B / "receivers.parquet")
    c = gpd.read_parquet(B / "casters.parquet")
    reach = shapely.union_all(c.geometry.buffer(2500).values)
    r = r[r.intersects(reach)].copy()
    r["t"] = (r.h >= 30).astype(int)
    r4 = r.to_crs(4326)
    with tempfile.TemporaryDirectory() as td:
        seq = Path(td) / "b.geojsonl"
        with open(seq, "w") as f:
            for row in r4.itertuples():
                p = {"h": round(float(row.h), 1), "t": int(row.t), "i": int(row.rid)}
                f.write(json.dumps({"type": "Feature", "properties": p, "geometry": row.geometry.__geo_interface__},
                                   separators=(",", ":")) + "\n")
        cmd = ["tippecanoe", "-o", str(W / "buildings.pmtiles"), "--force", "-l", "b", "-Z", "12", "-z", "16",
               "--drop-smallest-as-needed", "--coalesce-densest-as-needed", "--extend-zooms-if-still-dropping",
               "--simplification=2", "-P", str(seq)]
        subprocess.run(cmd, check=True)
    return int(len(r))


def towers():
    ts = json.loads((B / "tower_stats.json").read_text())
    p = W / "towers.geojson"
    fc = json.loads(p.read_text())
    for f in fc["features"]:
        i = str(f["properties"]["i"])
        f["properties"]["m"] = {mo: v[i] for mo, v in ts.items() if i in v}
    p.write_text(json.dumps(fc, ensure_ascii=False, separators=(",", ":")))
    # ranking for June and December by ground + roof area-time
    rank = {}
    for mo in ("06", "12", "03"):
        rows = [(f["properties"].get("n"), f["properties"]["h"], sum(f["properties"]["m"].get(mo, [0, 0, 0])[:2]),
                 f["properties"]["m"].get(mo, [0, 0, 0])[2]) for f in fc["features"]]
        rows.sort(key=lambda x: -x[2])
        rank[mo] = [{"name": n, "h": h, "ha_h": round(v, 1), "buildings": k} for n, h, v, k in rows[:10]]
    return rank


def shadow_tiles():
    """hourly GeoJSON per month → one vector-tile file per month (layer "s", hr = hour), loaded per viewport"""
    sizes = {}
    for md in sorted((W / "shadows").glob("[0-9][0-9]")):
        if not list(md.glob("[0-9][0-9].json")):
            continue                                                      # already tiled
        with tempfile.TemporaryDirectory() as td:
            seq = Path(td) / "s.geojsonl"
            with open(seq, "w") as f:
                for hf in sorted(md.glob("[0-9][0-9].json")):
                    hr = int(hf.stem)
                    for ft in json.loads(hf.read_text())["features"]:
                        ft["properties"]["hr"] = hr
                        f.write(json.dumps(ft, separators=(",", ":")) + "\n")
            out = W / "shadows" / f"{md.name}.pmtiles"
            subprocess.run(["tippecanoe", "-o", str(out), "--force", "-l", "s", "-Z", "13", "-z", "16",
                            "--no-feature-limit", "--no-tile-size-limit", "--no-tiny-polygon-reduction",
                            "--simplification=2", "-q", str(seq)], check=True)
        for hf in md.glob("[0-9][0-9].json"):
            hf.unlink()                                                   # keep index.json; tiles replace the hours
        sizes[md.name] = round(out.stat().st_size / 1e6, 1)
    print("shadow tiles (MB):", sizes)
    return sizes


def main():
    (W / "sunloss").mkdir(parents=True, exist_ok=True)
    s = json.loads((W / "stats.json").read_text())
    s["sunloss"] = sunloss()
    s["buildings_in_reach"] = buildings()
    s["tower_rank"] = towers()
    s["shadow_tiles_mb"] = shadow_tiles()
    s["build"] = int(time.time())                                        # cache key for the tile files
    (W / "stats.json").write_text(json.dumps(s, ensure_ascii=False, indent=1))
    print(json.dumps({k: s[k] for k in ("sunloss", "buildings_in_reach", "tower_rank")}, ensure_ascii=False, indent=1)[:3000])


if __name__ == "__main__":
    main()
