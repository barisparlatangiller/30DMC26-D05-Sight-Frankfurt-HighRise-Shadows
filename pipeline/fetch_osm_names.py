"""
Names of Frankfurt's buildings from OpenStreetMap (Overpass API): every building or building:part with a name.
Used only to label the towers (the user's 10+ storey extract misses towers whose storeys are not tagged in OSM,
e.g. Messeturm or Main Tower).

  python pipeline/fetch_osm_names.py   → input/osm_named_buildings.geojson (EPSG:4326)
"""
import json, sys, time
from pathlib import Path

import requests

BBOX = (50.015, 8.47, 50.23, 8.80)                 # Frankfurt am Main
Q = f"""[out:json][timeout:240];
(way["building"]["name"]({BBOX[0]},{BBOX[1]},{BBOX[2]},{BBOX[3]});
 way["building:part"]["name"]({BBOX[0]},{BBOX[1]},{BBOX[2]},{BBOX[3]});
 relation["building"]["name"]({BBOX[0]},{BBOX[1]},{BBOX[2]},{BBOX[3]}););
out geom;"""
URLS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter"]
OUT = Path("input/osm_named_buildings.geojson")


def main():
    data = None
    for k in range(6):
        url = URLS[k % len(URLS)]
        try:
            r = requests.post(url, data={"data": Q}, timeout=300,
                              headers={"User-Agent": "30DayMapChallenge-research (github.com/barisparlatangiller)"})
            r.raise_for_status()
            data = r.json(); break
        except Exception as e:                                          # noqa: BLE001
            print("retry", k + 1, url, e, flush=True); time.sleep(20)
    if data is None:
        sys.exit("Overpass not reachable")
    feats = []
    for el in data["elements"]:
        t = el.get("tags", {})
        rings = []
        if el["type"] == "way" and el.get("geometry"):
            rings = [[[p["lon"], p["lat"]] for p in el["geometry"]]]
        elif el["type"] == "relation":
            rings = [[[p["lon"], p["lat"]] for p in m["geometry"]] for m in el.get("members", [])
                     if m.get("role") == "outer" and m.get("geometry")]
        for ring in rings:
            if len(ring) >= 4 and ring[0] == ring[-1]:
                feats.append({"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [ring]},
                              "properties": {"osm": f"{el['type']}/{el['id']}", "name": t.get("name"),
                                             "levels": t.get("building:levels"), "height": t.get("height"),
                                             "building": t.get("building") or t.get("building:part"),
                                             "architect": t.get("architect"), "start_date": t.get("start_date")}})
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps({"type": "FeatureCollection", "features": feats}))
    print(f"named OSM buildings: {len(feats):,} (timestamp {data.get('osm3s', {}).get('timestamp_osm_base')})")


if __name__ == "__main__":
    main()
