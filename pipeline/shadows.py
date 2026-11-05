"""
Shadows of Frankfurt's buildings of 30 m and more, cast onto the real surfaces around them.

For one sun position, a point P at height z (a roof or the ground, m a.s.l.) lies in the shadow of a tower when the
ray from P towards the sun passes below the tower top while it crosses the tower footprint:

    shaded(P)  ⇔  z_P < max over tower cells Q upstream of P of ( top_Q − |PQ| · tan(α) )

Per tower and time step this is solved on a 2 m grid in a frame rotated to the sun direction, where "upstream"
is one axis and the max is a cumulative maximum (numpy, exact for prismatic towers). The surface z_P comes from
every LoD2 roof in reach (each at its own height) or, between buildings, from the ground model. Only the
towers cast shadows: low buildings neither shade nor block here, they only receive.

Days     the 21st of every month 2026, local time (CET/CEST), every 30 minutes while the sun is ≥ 5° high
Output   docs/data/shadows/MM/TT.json  (one file per month and time step: k = g ground | r roof, h = roof height)
         docs/data/shadows/MM/index.json (local times and sun positions)
         docs/data/sunloss/MM.png + .json   (hours of direct sun lost to towers that day, 5 m grid, Web Mercator)
         stats.json["shadows"]

  python pipeline/shadows.py [--months 6] [--step 30] [--res 2] [--workers 4]
"""
import argparse, json, math, os, time
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
G = {}                                         # data shared with worker processes (fork)


def sun_times(month, step):
    day = pd.Timestamp(f"2026-{month:02d}-21", tz="Europe/Berlin")
    t = pd.date_range(day + pd.Timedelta(hours=4), day + pd.Timedelta(hours=22), freq=f"{step}min")
    sp = pvlib.solarposition.get_solarposition(t, LAT, LON, altitude=110)
    sp = sp[sp.apparent_elevation >= MIN_ALT]
    return [(ts, float(r.azimuth), float(r.apparent_elevation)) for ts, r in sp.iterrows()]


def ground_at(x, y):
    g = G["ground"]
    col = (x - g["x0"]) / g["res"]; row = (y - g["y0"]) / g["res"]
    return map_coordinates(g["z"], [row, col], order=1, mode="nearest")


def plane_z(F, idx, rot, shape, tr, X, Y, base):
    """z of the LoD2 roof planes idx on the window grid; cells no plane covers keep base"""
    lab = features.rasterize([(rot(F["geom"][k]), int(n)) for n, k in enumerate(idx)], out_shape=shape,
                             transform=tr, fill=-1, dtype="int32")
    m = lab >= 0
    if not m.any():
        return base
    k = np.asarray(idx)[lab[m]]
    z = F["cz"][k] + F["a"][k] * (X[m] - F["cx"][k]) + F["b"][k] * (Y[m] - F["cy"][k])
    out = base.copy()
    out[m] = z
    return out


def tower_shadow(ci, az, alt, res):
    """shadow of caster ci for sun (azimuth, altitude) → list of (world polygon, kind, receiver id)"""
    c = G["casters"]
    poly, top = c["geom"][ci], c["top"][ci]
    a = math.radians(az)
    d = np.array([-math.sin(a), -math.cos(a)])               # shadow direction (away from the sun)
    p = np.array([d[1], -d[0]])                               # perpendicular
    tan = math.tan(math.radians(alt))
    ox, oy = poly.centroid.x, poly.centroid.y
    # rotated frame: w = along p, u = along d
    rot = lambda g: affinity.affine_transform(g, [p[0], p[1], d[0], d[1], -(p[0] * ox + p[1] * oy), -(d[0] * ox + d[1] * oy)])
    rp = rot(poly)
    w0, u0, w1, u1 = rp.bounds
    L = min(MAX_LEN, max(0.0, (top - (c["z0"][ci] - 15)) / tan))
    w0 -= 2 * res; w1 += 2 * res; u0 -= 2 * res; u1 = u1 + L + 2 * res
    ncol, nrow = int(math.ceil((w1 - w0) / res)), int(math.ceil((u1 - u0) / res))
    tr = Affine(res, 0, w0, 0, res, u0)
    me = features.rasterize([(rp, 1)], out_shape=(nrow, ncol), transform=tr, dtype="uint8", all_touched=False).astype(bool)
    if not me.any():
        return []
    uc = u0 + (np.arange(nrow) + 0.5) * res
    to_w = lambda wu: (ox + wu[..., 0] * p[0] + wu[..., 1] * d[0], oy + wu[..., 0] * p[1] + wu[..., 1] * d[1])
    wc = w0 + (np.arange(ncol) + 0.5) * res
    WW, UU = np.meshgrid(wc, uc)
    X, Y = to_w(np.stack([WW, UU], -1))
    # tower top per cell: its LoD2 roof planes (setbacks, sloped or stepped crowns), else the measured top
    Zt = np.full((nrow, ncol), top, dtype="float64")
    F = G.get("faces")
    if F is not None:
        fi = F["by_rid"].get(int(c["rid_orig"][ci]), [])
        if len(fi):
            Zt = plane_z(F, fi, rot, (nrow, ncol), tr, X, Y, Zt)
    T = np.where(me, Zt + uc[:, None] * tan, -np.inf)
    S = np.maximum.accumulate(T, axis=0) - uc[:, None] * tan          # shadow ceiling (m a.s.l.)
    # world → window polygon for receiver lookup
    corners = np.array([[w0, u0], [w1, u0], [w1, u1], [w0, u1]])
    cx, cy = to_w(corners)
    win = shapely.Polygon(np.c_[cx, cy])
    # surface height: ground model, then receiver roofs (low → high so the higher part wins)
    Z = ground_at(X.ravel(), Y.ravel()).reshape(X.shape)
    rid = np.full((nrow, ncol), -1, dtype=np.int32)
    R = G["recv"]
    hits = R["tree"].query(win, predicate="intersects")
    hits = hits[R["top"][hits] < top]                                   # higher roofs can never be shaded
    if len(hits):
        hits = hits[np.argsort(R["top"][hits])]
        shapes_z = [(rot(R["geom"][k]), float(R["top"][k])) for k in hits]
        Zr = features.rasterize(shapes_z, out_shape=(nrow, ncol), transform=tr, fill=np.nan, dtype="float32")
        shapes_i = [(rot(R["geom"][k]), int(k)) for k in hits]
        rid = features.rasterize(shapes_i, out_shape=(nrow, ncol), transform=tr, fill=-1, dtype="int32")
        Z = np.where(np.isnan(Zr), Z, Zr)
        if F is not None:                                               # sloped / stepped roofs of the receivers
            fh = F["tree"].query(win, predicate="intersects")
            fh = fh[F["rid"][fh] != int(c["rid_orig"][ci])]
            if len(fh):
                Z = plane_z(F, fh, rot, (nrow, ncol), tr, X, Y, Z)
    own = rid == c["rid"][ci]
    shade = (Z < S - 0.05) & ~me & ~own & np.isfinite(S)
    if not shade.any():
        return []
    lab = np.where(shade, rid + 1, -1).astype("int32")                  # 0 = ground, k+1 = roof of receiver k
    out = []
    for geom, v in features.shapes(lab, mask=shade, transform=tr, connectivity=4):
        ring = np.asarray(geom["coordinates"][0])
        holes = [np.asarray(h) for h in geom["coordinates"][1:]]
        ex, ey = to_w(ring)
        pg = shapely.Polygon(np.c_[ex, ey], [np.c_[hx, hy] for hx, hy in [to_w(h) for h in holes]])
        out.append((pg, int(v) - 1))
    return out


def run_step(args):
    ti, az, alt, res = args
    parts, per = [], {}
    for ci in G["active"](az, alt):
        sh = tower_shadow(ci, az, alt, res)
        if sh:
            per[ci] = (sum(g.area for g, k in sh if k < 0), sum(g.area for g, k in sh if k >= 0),
                       {k for _, k in sh if k >= 0})
        parts += sh
    if not parts:
        return ti, None, per
    df = gpd.GeoDataFrame({"rid": [r for _, r in parts]}, geometry=[g for g, _ in parts], crs=25832)
    df = df.dissolve("rid").reset_index()
    df["geometry"] = shapely.make_valid(df.geometry.simplify(0.8).values)
    df = df[df.area >= 2.0]
    return ti, df, per


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
    (web / "sunloss").mkdir(parents=True, exist_ok=True)

    c = gpd.read_parquet(B / "casters.parquet").sort_values("h", ascending=False).reset_index(drop=True)
    if a.limit:
        c = c.head(a.limit)
    r = gpd.read_parquet(B / "receivers.parquet")
    g = np.load(B / "ground.npz")
    G["ground"] = {"z": g["z"], "x0": float(g["x0"]), "y0": float(g["y0"]), "res": float(g["res"])}
    G["casters"] = {"geom": list(c.geometry.values), "top": c.top.values, "z0": c.z0.values, "rid": c.rid.values,
                    "rid_orig": c.rid.values.copy()}
    if (B / "roofs.parquet").exists():
        f = gpd.read_parquet(B / "roofs.parquet")
        by = f.groupby("rid").indices
        G["faces"] = {"geom": f.geometry.values, "cx": f.px.values, "cy": f.py.values, "cz": f.pz.values,
                      "a": f.a.values, "b": f.b.values, "rid": f.rid.values, "tree": STRtree(f.geometry.values),
                      "by_rid": {int(k): v for k, v in by.items()}}
        print(f"roof planes {len(f):,}", flush=True)
    # receivers near any caster only (keeps the tree small)
    reach = shapely.union_all(c.geometry.buffer(MAX_LEN).values)
    r = r[r.intersects(reach)].reset_index(drop=True)
    rgeom = r.geometry.values
    # map original rid → local index, so the caster's own part can be excluded
    G["recv"] = {"geom": rgeom, "top": r.top.values, "tree": STRtree(rgeom)}
    loc = {int(v): i for i, v in enumerate(r.rid.values)}
    c["rid_orig"] = c.rid.values
    G["casters"]["rid"] = np.array([loc.get(int(v), -2) for v in c.rid.values])
    G["active"] = lambda az, alt: range(len(c))
    print(f"casters {len(c)} · receivers in reach {len(r):,} · workers {a.workers}", flush=True)

    # sun-loss grid (EPSG:25832, then warped to 3857 for the web)
    bx0, by0, bx1, by1 = shapely.bounds(reach)
    lres = a.loss_res
    lw, lh = int((bx1 - bx0) / lres) + 1, int((by1 - by0) / lres) + 1
    ltr = Affine(lres, 0, bx0, 0, -lres, by1)
    stats = {"days": {}, "step_min": a.step, "res_m": a.res, "min_alt": MIN_ALT}
    tower_stats = {}
    months = [int(m) for m in a.months.split(",")]
    for mo in months:
        tsteps = sun_times(mo, a.step)
        t0 = time.time()
        jobs = [(i, az, alt, a.res) for i, (_, az, alt) in enumerate(tsteps)]
        with Pool(a.workers) as pool:
            res, pers = {}, {}
            for ti, df, per in pool.imap_unordered(run_step, jobs, chunksize=1):
                res[ti] = df; pers[ti] = per
        # per tower: shaded area × time (ha·h) on ground and roofs, and how many other buildings it shades that day
        tw = {}
        for ti, per in pers.items():
            for ci, (ag, ar, ks) in (per or {}).items():
                e = tw.setdefault(ci, [0.0, 0.0, set()])
                e[0] += ag * a.step / 60 / 1e4; e[1] += ar * a.step / 60 / 1e4; e[2] |= ks
        tower_stats[f"{mo:02d}"] = {int(c.rid_orig.values[ci]): [round(v[0], 2), round(v[1], 2), len(v[2])] for ci, v in tw.items()}
        loss = np.zeros((lh, lw), dtype="float32")
        area_g, area_r, nfeat, size = [], [], 0, 0
        md = web / "shadows" / f"{mo:02d}"; md.mkdir(parents=True, exist_ok=True)
        for f in md.glob("*.json"):
            f.unlink()
        for i, (ts, az, alt) in enumerate(tsteps):
            df = res.get(i)
            feats = []
            if df is not None and len(df):
                u = shapely.union_all(df.geometry.values)
                loss += features.rasterize([(u, 1)], out_shape=(lh, lw), transform=ltr, dtype="uint8").astype("float32") * (a.step / 60)
                area_g.append(float(df[df.rid < 0].area.sum())); area_r.append(float(df[df.rid >= 0].area.sum()))
                d4 = df.to_crs(4326)
                for row, geom in zip(df.itertuples(), d4.geometry.values):
                    p = {"k": "g" if row.rid < 0 else "r"}
                    if row.rid >= 0:
                        p["h"] = round(float(r.h.values[int(row.rid)]), 1)
                    feats.append({"type": "Feature", "properties": p,
                                  "geometry": json.loads(shapely.to_geojson(shapely.set_precision(geom, 1e-5)))})
            else:
                area_g.append(0.0); area_r.append(0.0)
            f = md / f"{i:02d}.json"
            f.write_text(json.dumps({"type": "FeatureCollection", "features": feats}, separators=(",", ":")))
            nfeat += len(feats); size += f.stat().st_size
        times = [ts.strftime("%H:%M") for ts, _, _ in tsteps]
        (md / "index.json").write_text(json.dumps({"times": times, "sun": [[round(az, 1), round(alt, 1)] for _, az, alt in tsteps]}))
        np.save(B / f"sunloss_{mo:02d}.npy", loss)
        stats["days"][f"{mo:02d}"] = {
            "steps": len(tsteps), "first": times[0] if times else None, "last": times[-1] if times else None,
            "max_alt": round(max([x for _, _, x in tsteps], default=0), 1),
            "ground_km2": [round(v / 1e6, 3) for v in area_g], "roof_km2": [round(v / 1e6, 3) for v in area_r],
            "loss_km2_1h": round(float((loss >= 1).sum() * lres * lres / 1e6), 2),
            "loss_km2_3h": round(float((loss >= 3).sum() * lres * lres / 1e6), 2),
            "loss_max_h": round(float(loss.max()), 1),
            "mb": round(size / 1e6, 2), "polygons": nfeat,
            "sec": round(time.time() - t0, 1)}
        print(f"month {mo:02d}: {len(tsteps)} steps, {nfeat:,} polygons, "
              f"{stats['days'][f'{mo:02d}']['mb']} MB, {time.time() - t0:.0f} s", flush=True)
    np.save(B / "sunloss_meta.npy", np.array([bx0, by1, lres, lw, lh]))
    (B / "tower_stats.json").write_text(json.dumps(tower_stats))
    p = web / "stats.json"
    s = json.loads(p.read_text()) if p.exists() else {}
    s["shadows"] = stats
    p.write_text(json.dumps(s, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
