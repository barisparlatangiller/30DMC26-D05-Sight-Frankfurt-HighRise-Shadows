"""
Hessen 3D building models (LoD2, CityGML 1.0) → one footprint per building or building part with its height.

Source: HVBG Geodaten online, Downloadcenter › 3D-Daten › 3D-Gebäudemodelle LoD2 › Kreisfreie Stadt Frankfurt
        (one package ZIP with one ZIP per district / Gemarkung).
For every bldg:Building and bldg:BuildingPart the GroundSurface polygons are merged into a footprint and stored with
  h     bldg:measuredHeight (m above ground)
  fn    ALKIS building function code of the parent building (e.g. 31001_1000 = residential building)
  oid   ALKIS object id
  z0    lowest ground point of the GroundSurface (m a.s.l.)
  ztop  highest RoofSurface point (m a.s.l.)
Output GeoParquet (EPSG:25832).

  python pipeline/lod2_to_buildings.py --lod2 input/lod2 --out input/frankfurt_lod2.parquet
"""
import argparse, io, zipfile
from pathlib import Path

import numpy as np
import geopandas as gpd
from lxml import etree
from shapely.geometry import Polygon
from shapely.ops import unary_union

GML = "{http://www.opengis.net/gml}"


def local(tag):
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def own(el, name, stop=("Building", "BuildingPart")):
    """descendants called `name` that belong to `el` itself, not to a nested BuildingPart"""
    out = []
    for d in el.iter():
        if local(d.tag) != name:
            continue
        p = d.getparent()
        while p is not None and p is not el:
            if local(p.tag) in stop:
                break
            p = p.getparent()
        if p is el:
            out.append(d)
    return out


def footprint(el):
    """footprint polygon + lowest ground elevation (m above sea level, DHHN2016) of the GroundSurface"""
    polys, zs = [], []
    for gs in own(el, "GroundSurface"):
        for pl in gs.iter(f"{GML}posList"):
            v = [float(t) for t in pl.text.split()]
            dim = int(pl.get("srsDimension", 3))
            pts = [(v[i], v[i + 1]) for i in range(0, len(v), dim)]
            if dim == 3:
                zs += v[2::3]
            if len(pts) >= 4:
                p = Polygon(pts).buffer(0)
                if not p.is_empty:
                    polys.append(p)
    return (unary_union(polys) if polys else None), (min(zs) if zs else None)


def roof_top(el):
    """highest roof point (m a.s.l.) of the RoofSurfaces that belong to this building (part)"""
    z = None
    for rs in own(el, "RoofSurface"):
        for pl in rs.iter(f"{GML}posList"):
            v = pl.text.split()
            dim = int(pl.get("srsDimension", 3))
            if dim == 3 and v:
                m = max(float(t) for t in v[2::3])
                z = m if z is None else max(z, m)
    return z


def roof_faces(el):
    """roof planes of this building (part): 2D polygon + plane z = cz + a·(x−cx) + b·(y−cy), m a.s.l."""
    out = []
    for rs in own(el, "RoofSurface"):
        for pl in rs.iter(f"{GML}posList"):
            dim = int(pl.get("srsDimension", 3))
            if dim != 3:
                continue
            v = np.array(pl.text.split(), dtype=float).reshape(-1, 3)
            if len(v) < 4:
                continue
            p = Polygon(v[:, :2])
            if not p.is_valid:
                p = p.buffer(0)
            if p.is_empty or p.area < 0.5:
                continue                                       # vertical / degenerate (walls drawn as roof)
            c = v.mean(0)
            A = np.c_[v[:, 0] - c[0], v[:, 1] - c[1]]
            try:
                (a, b), *_ = np.linalg.lstsq(A, v[:, 2] - c[2], rcond=None)
            except np.linalg.LinAlgError:
                a = b = 0.0
            if not (np.isfinite(a) and np.isfinite(b)) or abs(a) > 5 or abs(b) > 5:
                a = b = 0.0; c[2] = v[:, 2].max()
            out.append((p, float(c[0]), float(c[1]), float(c[2]), float(a), float(b)))
    return out


def text(el, name):
    e = own(el, name)
    return e[0].text.strip() if e and e[0].text else None


def gen_attr(el, name):
    for a in el.iter():
        if local(a.tag) in ("stringAttribute", "intAttribute", "doubleAttribute") and a.get("name") == name:
            for v in a:
                if local(v.tag) == "value" and v.text:
                    return v.text.strip()
    return None


def parse(stream, src):
    rows, roofs = [], []
    for _, el in etree.iterparse(stream, events=("end",), huge_tree=True):
        if local(el.tag) != "Building":
            continue
        fn = text(el, "function")
        oid = gen_attr(el, "ALKISOID") or el.get(f"{GML}id")
        parts = [d for d in el.iter() if local(d.tag) == "BuildingPart"]
        for b in [el] + parts:
            fp, z0 = footprint(b)
            if fp is None:
                continue
            h = text(b, "measuredHeight")
            uid = f"{src}:{len(rows)}:{oid}"
            for p, cx, cy, cz, aa, bb in roof_faces(b):
                roofs.append({"uid": uid, "px": cx, "py": cy, "pz": cz, "a": aa, "b": bb, "geometry": p})
            rows.append({"uid": uid, "oid": oid, "part": b is not el, "fn": text(b, "function") or fn,
                         "h": float(h) if h else None, "roof": text(b, "roofType"), "z0": z0, "ztop": roof_top(b),
                         "src": src, "geometry": fp})
        el.clear()
        while el.getprevious() is not None:
            del el.getparent()[0]
    return rows, roofs


def read_zip(z, src):
    rows, roofs = [], []
    for m in z.namelist():
        n = m.lower()
        if n.endswith(".zip"):
            with z.open(m) as f:
                r, rf = read_zip(zipfile.ZipFile(io.BytesIO(f.read())), Path(m).stem)
        elif n.endswith((".gml", ".xml")):
            with z.open(m) as f:
                r, rf = parse(f, Path(m).stem)
            print(f"  {m}: {len(rows) + len(r):,} footprints so far", flush=True)
        else:
            continue
        rows += r; roofs += rf
    return rows, roofs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lod2", default="input/lod2")
    ap.add_argument("--out", default="input/frankfurt_lod2.parquet")
    a = ap.parse_args()
    rows, roofs = [], []
    for f in sorted(Path(a.lod2).rglob("*")):
        if f.suffix.lower() == ".zip":
            r, rf = read_zip(zipfile.ZipFile(f), f.stem)
        elif f.suffix.lower() in (".gml", ".xml"):
            with open(f, "rb") as s:
                r, rf = parse(s, f.stem)
        else:
            continue
        rows += r; roofs += rf
    if not rows:
        raise SystemExit("no LoD2 buildings found")
    g = gpd.GeoDataFrame(rows, geometry="geometry", crs=25832)
    g = g[g.geometry.notna() & ~g.geometry.is_empty & (g.area > 1)]
    g.to_parquet(a.out, compression="zstd")
    rf = gpd.GeoDataFrame(roofs, geometry="geometry", crs=25832)
    rf.to_parquet(str(a.out).replace(".parquet", "_roofs.parquet"), compression="zstd")
    print(f"Wrote {len(rf):,} roof planes")
    print(f"Wrote {len(g):,} footprints ({g.oid.nunique():,} buildings) → {a.out}")
    print(g.h.describe().round(1))
    print(g.fn.value_counts().head(12))


if __name__ == "__main__":
    main()
