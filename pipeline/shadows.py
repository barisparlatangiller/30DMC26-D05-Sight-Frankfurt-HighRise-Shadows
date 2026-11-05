"""
Shadows of Frankfurt's buildings of 30 m and more, cast onto the real surfaces around them.

For one sun position, a point P at height z (a roof or the ground, m a.s.l.) lies in a tower's shadow when the ray
from P towards the sun passes below the tower's roof while it crosses the tower:

    shaded(P)  ⇔  z_P < max over tower cells Q upstream of P of ( roof_Q − |PQ| · tan(α) )

Roofs (LoD2 roof planes), roof tops and the ground are burnt once into a 2 m surface raster. Towers are
processed in 1.5 km tiles on a 2 m grid rotated to the sun direction, where "upstream" is one axis and the
max is a cumulative maximum that also remembers which tower set it (attribution). Tower roofs follow their LoD2 roof
planes (setbacks, stepped and sloped crowns), so towers also shade each other and their own lower setbacks. The
surface z_P is every LoD2 roof in reach — again from its roof planes — or, between buildings, the ground model.
Only buildings ≥ 30 m cast shadows; lower buildings receive but neither cast nor block.

Days     the 21st of every month 2026, local time (CET/CEST), every 30 minutes while the sun is ≥ 5° high
Output   docs/data/shadows/MM/HH.json   full hours only (k = g ground | r roof, h = roof height above ground)
         docs/data/shadows/MM/index.json
         build/sunloss_MM.npy (hours of sun lost, from all 30-minute steps) · build/tower_stats.json
         stats.json["shadows"]

  python pipeline/shadows.py [--months 6] [--step 30] [--res 2] [--workers 4]
"""
import argparse, json, math, os, time
from collections import defaultdict
from multiprocessing import Pool
from pathlib import Path

import numpy as np
import pandas as pd
import geopandas as gpd
import shapely
from shapely import affinity
from shapely.strtree import STRtree
from rasterio import features
from rasterio.transform import Affine
from scipy.ndimage import map_coordinates
import pvlib

LAT, LON = 50.1109, 8.6821
MIN_ALT = 5.0
MAX_LEN = 2500.0                               # cap on shadow length (m); at 5° a 200 m tower reaches ~2.3 km
TILE = 1500.0
G = {}                                         # data shared with worker processes (fork)


def sun_times(month, step):
    day = pd.Timestamp(f"2026-{month:02d}-21", tz="Europe/Berlin")
    t = pd.date_range(day + pd.Timedelta(hours=4), day + pd.Timedelta(hours=22), freq=f"{step}min")
    sp = pvlib.solarposition.get_solarposition(t, LAT, LON, altitude=110)
    sp = sp[sp.apparent_elevation >= MIN_ALT]
    return [(ts, float(r.azimuth), float(r.apparent_elevation)) for ts, r in sp.iterrows()]


def ground_at(x, y):
    g = G["ground"]
    return map_coordinates(g["z"], [(y - g["y0"]) / g["res"], (x - g["x0"]) / g["res"]], order=1, mode="nearest")


def sample(arr, X, Y, fill):
    """nearest-cell lookup in a global 2 m raster (row 0 = north)"""
    g = G["grid"]
    ix = ((X - g["x0"]) / g["res"]).astype(np.int64); iy = ((g["y1"] - Y) / g["res"]).astype(np.int64)
    ok = (ix >= 0) & (iy >= 0) & (ix < arr.shape[1]) & (iy < arr.shape[0])
    out = np.full(X.shape, fill, dtype=arr.dtype)
    out[ok] = arr[iy[ok], ix[ok]]
    return out


def tile_shadow(tile, az, alt, res):
    """shadows cast by the towers of one tile → (polygons [(geom, receiver index or -1)], per-tower stats)"""
    C = G["casters"]
    ids = G["tiles"][tile]
    a = math.radians(az)
    d = np.array([-math.sin(a), -math.cos(a)])               # shadow direction (away from the sun)
    p = np.array([d[1], -d[0]])
    tan = math.tan(math.radians(alt))
    ox, oy = G["tile_xy"][tile]
    # window in the rotated frame (w across, u along the shadow): the tile's towers plus the shadow length
    pts = np.concatenate([np.asarray(C["geom"][i].exterior.coords) for i in ids])
    w = (pts[:, 0] - ox) * p[0] + (pts[:, 1] - oy) * p[1]
    u = (pts[:, 0] - ox) * d[0] + (pts[:, 1] - oy) * d[1]
    L = min(MAX_LEN, max(0.0, (C["top"][ids].max() - (C["z0"][ids].min() - 15)) / tan))
    w0, w1, u0, u1 = w.min() - 2 * res, w.max() + 2 * res, u.min() - 2 * res, u.max() + L + 2 * res
    ncol, nrow = int(math.ceil((w1 - w0) / res)), int(math.ceil((u1 - u0) / res))
    uc = u0 + (np.arange(nrow) + 0.5) * res
    wc = w0 + (np.arange(ncol) + 0.5) * res
    WW, UU = np.meshgrid(wc, uc)
    X = ox + WW * p[0] + UU * d[0]; Y = oy + WW * p[1] + UU * d[1]
    lab = sample(G["CAST"], X, Y, -1)
    lab = np.where((lab >= 0) & (G["tile_of"][np.maximum(lab, 0)] == tile), lab, -1)
    me = lab >= 0
    if not me.any():
        return [], {}
    Z = sample(G["ZS"], X, Y, np.float32(np.inf)).astype("float64")    # roof planes / roofs / ground, m a.s.l.
    rid = sample(G["RID"], X, Y, -1)
    T = np.where(me, Z + uc[:, None] * tan, -np.inf)
    # cumulative max along the shadow direction, remembering which tower set it
    S = np.empty_like(T); who = np.full(T.shape, -1, dtype=np.int32)
    cur = np.full(ncol, -np.inf); cid = np.full(ncol, -1, dtype=np.int32)
    for row in range(nrow):
        up = T[row] > cur
        cur = np.where(up, T[row], cur); cid = np.where(up, lab[row], cid)
        S[row] = cur; who[row] = cid
    S -= uc[:, None] * tan
    shade = (Z < S - 0.25) & np.isfinite(S) & (who >= 0)
    if not shade.any():
        return [], {}
    per, cell = {}, res * res
    w_s, r_s = who[shade], rid[shade]
    for t in np.unique(w_s):
        m = w_s == t
        rr = r_s[m]
        own = C["rloc"][t]
        per[int(t)] = (float((rr < 0).sum() * cell), float((rr >= 0).sum() * cell),
                       set(int(x) for x in np.unique(rr[(rr >= 0) & (rr != own)])))
    labv = np.where(shade, rid + 1, -1).astype("int32")                  # 0 ground, k+1 roof of receiver k
    tr = Affine(res, 0, w0, 0, res, u0)
    out = []
    for geom, v in features.shapes(labv, mask=shade, transform=tr, connectivity=4):
        rings = [np.asarray(rg) for rg in geom["coordinates"]]
        tw = [np.c_[ox + rg[:, 0] * p[0] + rg[:, 1] * d[0], oy + rg[:, 0] * p[1] + rg[:, 1] * d[1]] for rg in rings]
        out.append((shapely.Polygon(tw[0], tw[1:]), int(v) - 1))
    return out, per


def build_grid(r, faces, c, bounds, res):
    """global surface rasters (2 m): ZS = roof planes > roof tops > ground (m a.s.l.), RID = receiver, CAST = tower"""
    x0, y0, x1, y1 = bounds
    x0, y0 = math.floor(x0 / res) * res, math.floor(y0 / res) * res
    w, h = int(math.ceil((x1 - x0) / res)), int(math.ceil((y1 - y0) / res))
    y1 = y0 + h * res
    tr = Affine(res, 0, x0, 0, -res, y1)
    G["grid"] = {"x0": x0, "y1": y1, "res": res}
    t0 = time.time()
    ZS = np.empty((h, w), dtype="float32")
    g = G["ground"]
    xs = x0 + (np.arange(w) + 0.5) * res
    for r0 in range(0, h, 1000):                                      # ground model, in row blocks
        ys = y1 - (np.arange(r0, min(h, r0 + 1000)) + 0.5) * res
        XX, YY = np.meshgrid(xs, ys)
        ZS[r0:r0 + len(ys)] = map_coordinates(g["z"], [(YY - g["y0"]) / g["res"], (XX - g["x0"]) / g["res"]], order=1, mode="nearest")
    order = np.argsort(r.top.values)
    top = features.rasterize(zip(r.geometry.values[order], r.top.values[order].astype("float32")), out_shape=(h, w),
                             transform=tr, fill=np.nan, dtype="float32")
    RID = features.rasterize(zip(r.geometry.values[order], order.astype("int32")), out_shape=(h, w), transform=tr,
                             fill=-1, dtype="int32")
    m = ~np.isnan(top)
    ZS[m] = top[m]
    del top
    if faces is not None and len(faces):
        FI = features.rasterize(zip(faces.geometry.values, np.arange(len(faces), dtype="int32")), out_shape=(h, w),
                                transform=tr, fill=-1, dtype="int32")
        iy, ix = np.nonzero(FI >= 0)
        k = FI[iy, ix]
        X = x0 + (ix + 0.5) * res; Y = y1 - (iy + 0.5) * res
        z = faces.pz.values[k] + faces.a.values[k] * (X - faces.px.values[k]) + faces.b.values[k] * (Y - faces.py.values[k])
        ZS[iy, ix] = z.astype("float32")
        del FI, iy, ix, k, X, Y, z
    CAST = features.rasterize(zip(c.geometry.values, np.arange(len(c), dtype="int32")), out_shape=(h, w),
                              transform=tr, fill=-1, dtype="int32")
    G["ZS"], G["RID"], G["CAST"] = ZS, RID, CAST
    print(f"surface grid {w}×{h} at {res} m built in {time.time() - t0:.0f} s", flush=True)


def run_step(args):
    """one time step: all tiles → (index, union WKB, web GeoJSON string or None, ground m², roof m², per-tower)"""
    ti, az, alt, res, web = args
    parts, per = [], {}
    for tile in range(len(G["tiles"])):
        sh, pt = tile_shadow(tile, az, alt, res)
        parts += sh
        for k, v in pt.items():
            e = per.setdefault(k, [0.0, 0.0, set()])
            e[0] += v[0]; e[1] += v[1]; e[2] |= v[2]
    if not parts:
        return ti, None, ('{"type":"FeatureCollection","features":[]}' if web else None), 0.0, 0.0, per
    df = gpd.GeoDataFrame({"rid": [r for _, r in parts]}, geometry=[g for g, _ in parts], crs=25832)
    df = df.dissolve("rid").reset_index()
    df["geometry"] = shapely.make_valid(df.geometry.simplify(0.8).values)
    df = df[df.area >= 2.0]
    ag, ar = float(df[df.rid < 0].area.sum()), float(df[df.rid >= 0].area.sum())
    u = shapely.to_wkb(shapely.union_all(df.geometry.values))
    js = None
    if web:
        df["k"] = np.where(df.rid < 0, "g", "r")                         # roofs: one feature per 1 m roof height
        df["h"] = np.where(df.rid < 0, 0, np.round(G["rh"][np.maximum(df.rid.values, 0)]))
        dd = df.dissolve(["k", "h"]).reset_index()
        d4 = dd.to_crs(4326)
        g4 = shapely.transform(d4.geometry.values, lambda xy: np.round(xy, 5))   # ~1 m, keeps files small
        g4 = shapely.make_valid(g4)
        feats = []
        for row, geom in zip(dd.itertuples(), g4):
            if geom is None or geom.is_empty:
                continue
            pr = {"k": row.k}
            if row.k == "r":
                pr["h"] = float(row.h)
            feats.append({"type": "Feature", "properties": pr,
                          "geometry": json.loads(shapely.to_geojson(geom))})
        js = json.dumps({"type": "FeatureCollection", "features": feats}, separators=(",", ":"))
    return ti, u, js, ag, ar, per


def prepare(B, res, limit=0, area=None):
    """load towers, receivers, ground and roof planes into G and build the surface grid.
    area: optional polygon — only towers whose shadow can reach it (video window)"""
    c = gpd.read_parquet(B / "casters.parquet").sort_values("h", ascending=False).reset_index(drop=True)
    if limit:
        c = c.head(limit).reset_index(drop=True)
    if area is not None:
        c = c[c.geometry.distance(area) < MAX_LEN].reset_index(drop=True)
    r = gpd.read_parquet(B / "receivers.parquet")
    g = np.load(B / "ground.npz")
    G["ground"] = {"z": g["z"], "x0": float(g["x0"]), "y0": float(g["y0"]), "res": float(g["res"])}
    reach = shapely.union_all(c.geometry.buffer(MAX_LEN).values)
    r = r[r.intersects(reach)].reset_index(drop=True)
    loc = {int(v): i for i, v in enumerate(r.rid.values)}
    G["rh"] = r.h.values
    G["casters"] = {"geom": c.geometry.values, "top": c.top.values, "z0": c.z0.values, "rid": c.rid.values,
                    "rloc": np.array([loc.get(int(v), -2) for v in c.rid.values])}
    faces = None
    if (B / "roofs.parquet").exists():
        faces = gpd.read_parquet(B / "roofs.parquet")
        faces = faces[faces.intersects(reach)].reset_index(drop=True)
        print(f"roof planes in reach {len(faces):,}", flush=True)
    build_grid(r, faces, c, shapely.bounds(reach), res)
    cc = c.geometry.centroid
    groups = defaultdict(list)
    for i, k in enumerate(zip((cc.x // TILE).astype(int), (cc.y // TILE).astype(int))):
        groups[k].append(i)
    G["tiles"] = [np.array(v) for v in groups.values()]
    G["tile_xy"] = [(float(cc.x.values[v].mean()), float(cc.y.values[v].mean())) for v in G["tiles"]]
    G["tile_of"] = np.zeros(len(c), dtype=np.int32)
    for t, v in enumerate(G["tiles"]):
        G["tile_of"][v] = t
    return c, r, reach


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", default="1,2,3,4,5,6,7,8,9,10,11,12")
    ap.add_argument("--step", type=int, default=30)
    ap.add_argument("--res", type=float, default=2.0)
    ap.add_argument("--loss-res", type=float, default=5.0)
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 2)
    ap.add_argument("--build", default="build")
    ap.add_argument("--limit", type=int, default=0, help="test: only the N tallest casters")
    a = ap.parse_args()
    B = Path(a.build); web = Path("docs/data"); (web / "shadows").mkdir(parents=True, exist_ok=True)

    c, r, reach = prepare(B, a.res, limit=a.limit)
    print(f"towers {len(c):,} in {len(G['tiles'])} tiles · receivers in reach {len(r):,} · workers {a.workers}", flush=True)

    bx0, by0, bx1, by1 = shapely.bounds(reach)
    lres = a.loss_res
    lw, lh = int((bx1 - bx0) / lres) + 1, int((by1 - by0) / lres) + 1
    ltr = Affine(lres, 0, bx0, 0, -lres, by1)
    stats = {"days": {}, "step_min": a.step, "res_m": a.res, "min_alt": MIN_ALT, "towers": int(len(c)),
             "tower_buildings": int(c.oid.nunique()) if "oid" in c else None}
    tower_stats = {}
    for mo in [int(m) for m in a.months.split(",")]:
        tsteps = sun_times(mo, a.step)
        t0 = time.time()
        jobs = [(i, az, alt, a.res, ts.minute == 0) for i, (ts, az, alt) in enumerate(tsteps)]
        md = web / "shadows" / f"{mo:02d}"; md.mkdir(parents=True, exist_ok=True)
        for f in md.glob("*.json"):
            f.unlink()
        loss = np.zeros((lh, lw), dtype="float32")
        area_g, area_r = [0.0] * len(tsteps), [0.0] * len(tsteps)
        tw, hours, size, nfeat = {}, {}, 0, 0
        with Pool(a.workers) as pool:
            for ti, u, js, ag, ar, per in pool.imap_unordered(run_step, jobs, chunksize=1):
                ts, az, alt = tsteps[ti]
                area_g[ti], area_r[ti] = ag, ar
                if u is not None:
                    loss += features.rasterize([(shapely.from_wkb(u), 1)], out_shape=(lh, lw), transform=ltr,
                                               dtype="uint8").astype("float32") * (a.step / 60)
                for ci, (g_, r_, ks) in (per or {}).items():
                    e = tw.setdefault(ci, [0.0, 0.0, set()])
                    e[0] += g_ * a.step / 60 / 1e4; e[1] += r_ * a.step / 60 / 1e4; e[2] |= ks
                if js is not None:
                    f = md / f"{ts.hour:02d}.json"
                    f.write_text(js)
                    size += f.stat().st_size; nfeat += js.count('"Feature"')
                    hours[ti] = {"t": ts.strftime("%H:%M"), "f": f"{ts.hour:02d}", "az": round(az, 1), "alt": round(alt, 1),
                                 "g": round(ag / 1e4, 1), "r": round(ar / 1e4, 1)}
        tower_stats[f"{mo:02d}"] = {int(c.rid.values[ci]): [round(v[0], 2), round(v[1], 2), len(v[2])] for ci, v in tw.items()}
        hours = [hours[k] for k in sorted(hours)]
        (md / "index.json").write_text(json.dumps({"hours": hours}))
        np.save(B / f"sunloss_{mo:02d}.npy", loss)
        stats["days"][f"{mo:02d}"] = {
            "steps": len(tsteps), "first": tsteps[0][0].strftime("%H:%M") if tsteps else None,
            "last": tsteps[-1][0].strftime("%H:%M") if tsteps else None,
            "max_alt": round(max([x for _, _, x in tsteps], default=0), 1),
            "times": [ts.strftime("%H:%M") for ts, _, _ in tsteps],
            "ground_ha": [round(v / 1e4, 1) for v in area_g], "roof_ha": [round(v / 1e4, 1) for v in area_r],
            "loss_km2_1h": round(float((loss >= 1).sum() * lres * lres / 1e6), 2),
            "loss_km2_3h": round(float((loss >= 3).sum() * lres * lres / 1e6), 2),
            "loss_max_h": round(float(loss.max()), 1), "mb": round(size / 1e6, 2), "features": nfeat,
            "sec": round(time.time() - t0, 1)}
        print(f"month {mo:02d}: {len(tsteps)} steps, {len(hours)} hourly files, {size / 1e6:.1f} MB, {time.time() - t0:.0f} s", flush=True)
    np.save(B / "sunloss_meta.npy", np.array([bx0, by1, lres, lw, lh]))
    (B / "tower_stats.json").write_text(json.dumps(tower_stats))
    p = web / "stats.json"
    s = json.loads(p.read_text()) if p.exists() else {}
    s["shadows"] = stats
    p.write_text(json.dumps(s, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
