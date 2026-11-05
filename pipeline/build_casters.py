"""
Shadow casters and receivers.

  casters    every LoD2 building (part) with a measured height of at least 30 m (≈ 10+ storeys). Heights are the
             measured LoD2 heights, not storeys × 3 m. OSM attributes (name, storeys, use, architect …) are joined
             from the user's OSM extract of buildings with building:levels ≥ 10.
  receivers  every LoD2 building (part) of Frankfurt — the roofs the tower shadows fall on, at their own height.
  ground     ground elevation model interpolated from the LoD2 ground points (z0) — 25 m grid, m a.s.l.

  python pipeline/build_casters.py
Output  build/casters.parquet · build/receivers.parquet · build/ground.npz · docs/data/towers.geojson ·
        docs/data/stats.json (casters section)
"""
import argparse, json
from pathlib import Path

import numpy as np
import pandas as pd
import geopandas as gpd
from scipy.interpolate import griddata
from scipy.ndimage import gaussian_filter

MIN_H = 30.0
OSM_KEEP = ["@id", "name", "building", "building:levels", "height", "roof:shape", "architect", "start_date",
            "addr:street", "addr:housenumber", "wikipedia"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lod2", default="input/frankfurt_lod2.parquet")
    ap.add_argument("--osm", default="data/frankfurt_osm_10plus_levels_20261105.gpkg")
    ap.add_argument("--out", default="build")
    a = ap.parse_args()
    out = Path(a.out); out.mkdir(exist_ok=True)
    web = Path("docs/data"); web.mkdir(parents=True, exist_ok=True)

    b = gpd.read_parquet(a.lod2).to_crs(25832)
    b = b[b.h.notna() & (b.h > 0) & b.geometry.notna()].reset_index(drop=True)
    # Frankfurt lies between ~88 m (Main) and ~215 m a.s.l. (Berger Rücken); ground points outside that range are
    # parts below ground or data errors → taken from their neighbours via the ground model below
    b.loc[(b.z0 < 85) | (b.z0 > 220), "z0"] = np.nan
    # ---- ground model (m a.s.l.) from LoD2 ground points, 25 m grid
    ok = b.z0.notna().values
    pts = np.c_[b.geometry.centroid.x, b.geometry.centroid.y][ok]
    x0, y0, x1, y1 = b.total_bounds
    gx = np.arange(x0 - 2000, x1 + 2000, 25.0); gy = np.arange(y0 - 2000, y1 + 2000, 25.0)
    GX, GY = np.meshgrid(gx, gy)
    g = griddata(pts, b.z0.values[ok], (GX, GY), method="linear")
    gn = griddata(pts, b.z0.values[ok], (GX, GY), method="nearest")
    g = np.where(np.isnan(g), gn, g)
    g = gaussian_filter(g, 2)                                                # buildings sit on small pads; smooth
    cx_, cy_ = b.geometry.centroid.x.values, b.geometry.centroid.y.values
    fill = g[np.clip(((cy_ - gy[0]) / 25).astype(int), 0, len(gy) - 1), np.clip(((cx_ - gx[0]) / 25).astype(int), 0, len(gx) - 1)]
    b["z0"] = np.where(b.z0.isna(), fill, b.z0)
    np.savez_compressed(out / "ground.npz", z=g.astype("float32"), x0=gx[0], y0=gy[0], res=25.0)
    zt = b.ztop.astype(float) if "ztop" in b else pd.Series(np.nan, index=b.index)
    ok_top = zt.notna() & (zt > b.z0) & ((zt - b.z0 - b.h).abs() < 15)
    b["top"] = np.where(ok_top, zt, b.z0 + b.h)                            # roof top (m a.s.l.) used everywhere
    b["rid"] = np.arange(len(b))
    print(f"LoD2 parts: {len(b):,} · ground z0 {b.z0.min():.1f}–{b.z0.max():.1f} m a.s.l.")

    # ---- casters
    c = b[(b.h >= MIN_H) & b.fn.fillna("").str.startswith("31001")].copy()     # buildings only, no chimneys/masts
    osm = gpd.read_file(a.osm).to_crs(25832)
    osm = osm[[k for k in OSM_KEEP if k in osm] + ["geometry"]].copy()
    osm["osm_area"] = osm.area
    j = gpd.overlay(c[["rid", "geometry"]], osm, how="intersection", keep_geom_type=True)
    j["ia"] = j.area
    j = j.sort_values("ia", ascending=False).drop_duplicates("rid")
    c = c.merge(j.drop(columns="geometry"), on="rid", how="left")
    c["in_osm"] = c["@id"].notna()                                        # in the 10+ storey extract
    # names for towers outside the 10+ storey extract (storeys not tagged in OSM, e.g. Messeturm): every named OSM
    # building / building:part in Frankfurt, largest overlap wins
    nf = Path("input/osm_named_buildings.geojson")
    if nf.exists():
        nb = gpd.read_file(nf).to_crs(25832)
        nb = nb[nb.name.notna() & nb.geometry.is_valid]
        jn = gpd.overlay(c[["rid", "geometry"]], nb[["name", "osm", "architect", "start_date", "geometry"]],
                         how="intersection", keep_geom_type=True)
        jn["ia"] = jn.area
        jn = jn[jn.ia > 20].sort_values("ia", ascending=False).drop_duplicates("rid").set_index("rid")
        miss_n = c["name"].isna()
        c.loc[miss_n, "name"] = c.loc[miss_n, "rid"].map(jn["name"])
        for col, src in [("architect", "architect"), ("start_date", "start_date")]:
            if col in c:
                c[col] = c[col].fillna(c.rid.map(jn[src]))
            else:
                c[col] = c.rid.map(jn[src])
        c["@id"] = c["@id"].fillna(c.rid.map(jn["osm"]))
        print(f"names from all named OSM buildings: {miss_n.sum() - c.name.isna().sum():,} added")
    # same building (ALKIS oid): a part without its own OSM match inherits the building's OSM name
    if "oid" in c:
        nm = c.dropna(subset=["name"]).groupby("oid")["name"].first()
        c["name"] = c["name"].fillna(c.oid.map(nm))
    print(f"Casters (LoD2 parts ≥ {MIN_H:.0f} m): {len(c):,} parts of {c.oid.nunique():,} buildings · "
          f"matched to the OSM 10+ storey list: {c.in_osm.sum():,}")
    osm_hit = osm["@id"].isin(c["@id"].dropna())
    print(f"OSM 10+ storey buildings with a LoD2 part ≥ 30 m: {osm_hit.sum()} of {len(osm)}")

    # OSM buildings with 10+ storeys whose LoD2 parts are all < 30 m (the storey count says high-rise, the
    # measurement does not) — reported, not used as casters
    miss = osm[~osm_hit].copy()
    jm = gpd.sjoin(miss[["@id", "geometry"]], b[["h", "geometry"]], how="left", predicate="intersects")
    miss["lod2_hmax"] = miss["@id"].map(jm.groupby("@id").h.max())

    c["src_h"] = "LoD2 measured"
    c.to_parquet(out / "casters.parquet")
    b.to_parquet(out / "receivers.parquet")

    # ---- roof planes (LoD2 RoofSurfaces): the true roof shape of towers and receivers
    rp = Path(str(a.lod2).replace(".parquet", "_roofs.parquet"))
    nroof = 0
    if rp.exists() and "uid" in b:
        rf = gpd.read_parquet(rp)
        rf["rid"] = rf.uid.map(pd.Series(b.rid.values, index=b.uid.values))
        rf = rf[rf.rid.notna()].copy()
        rf["rid"] = rf.rid.astype(int)
        reach = c.geometry.buffer(2500).union_all()
        rf = rf[rf.intersects(reach)]
        rf.drop(columns="uid").to_parquet(out / "roofs.parquet")
        nroof = len(rf)
        print(f"Roof planes in reach: {nroof:,} · on towers: {rf.rid.isin(c.rid).sum():,}")

    print(f"Ground model {g.shape[1]}×{g.shape[0]} cells, {np.nanmin(g):.1f}–{np.nanmax(g):.1f} m a.s.l.")

    # ---- web: towers with attributes
    def num(v):
        try:
            x = float(str(v).replace("m", "").replace(",", ".").strip())
            return x if np.isfinite(x) else None
        except (ValueError, TypeError):
            return None
    t = c.to_crs(4326)
    feats = []
    for r in t.itertuples():
        p = {"i": int(r.rid), "h": round(float(r.h), 1), "z0": round(float(r.z0), 1)}
        for k, kk in [("name", "n"), ("building", "use"), ("architect", "arch"), ("start_date", "year"), ("roof", "roof")]:
            v = getattr(r, k, None)
            if isinstance(v, str) and v: p[kk] = v
        feats.append({"type": "Feature", "properties": p, "geometry": r.geometry.__geo_interface__})
    # storeys / OSM heights via a plain DataFrame (column names with ':' are not attributes)
    for f, (_, row) in zip(feats, t.iterrows()):
        lv, oh = num(row.get("building:levels")), num(row.get("height"))
        if lv: f["properties"]["lv"] = int(lv)
        if oh: f["properties"]["h_osm"] = oh
        if isinstance(row.get("@id"), str): f["properties"]["osm"] = row["@id"]
        if isinstance(row.get("addr:street"), str):
            f["properties"]["a"] = f"{row['addr:street']} {row.get('addr:housenumber') or ''}".strip()
    (web / "towers.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": feats},
                                                    ensure_ascii=False, separators=(",", ":")))

    stats = {"casters": {
        "min_h": MIN_H, "parts": int(len(c)), "buildings": int(c.oid.nunique()),
        "matched_osm_parts": int(c.in_osm.sum()), "osm_total": int(len(osm)), "osm_with_caster": int(osm_hit.sum()),
        "osm_without_caster": int((~osm_hit).sum()),
        "osm_without_caster_lod2_hmax_median": round(float(miss.lod2_hmax.median()), 1) if miss.lod2_hmax.notna().any() else None,
        "h_median": round(float(c.h.median()), 1), "h_max": round(float(c.h.max()), 1),
        "tallest": [{"name": r.name if isinstance(r.name, str) else None, "h": round(float(r.h), 1)}
                    for r in c.sort_values("h", ascending=False).drop_duplicates("oid").head(10).itertuples()],
        "roof_planes": nroof,
        "receivers": int(len(b)), "ground_range": [round(float(np.nanmin(g)), 1), round(float(np.nanmax(g)), 1)],
    }}
    p = web / "stats.json"
    s = json.loads(p.read_text()) if p.exists() else {}
    s.update(stats)
    p.write_text(json.dumps(s, ensure_ascii=False, indent=1))
    print(json.dumps(stats, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
