"""
Download the Frankfurt LoD2 package from HVBG "Geodaten online" (Downloadcenter › 3D-Daten › 3D-Gebäudemodelle LoD2 ›
Kreisfreie Stadt Frankfurt). The download link contains the current date, so it is read from the page first.

  python pipeline/fetch_lod2.py            → input/lod2/PKT_Kreisfreie Stadt Frankfurt.zip (~0.5 GB)
"""
import re, sys, time
from pathlib import Path
from urllib.parse import urljoin, unquote, quote

import requests

API = ("https://gds.hessen.de/INTERSHOP/rest/WFS/HLBG-Geodaten-Site/-/downloadcenter"
       "?path=3D-Daten/3D-Geb%C3%A4udemodelle/3D-Geb%C3%A4udemodelle%20LoD2/Kreisfreie%20Stadt%20Frankfurt&navigation=all")
OUT = Path("input/lod2")
S = requests.Session()
S.headers["User-Agent"] = "30DayMapChallenge-research (github.com/barisparlatangiller)"


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    have = list(OUT.glob("*.zip"))
    if have and sum(f.stat().st_size for f in have) > 100e6:
        print("LoD2 package already cached:", [f.name for f in have]); return
    txt = S.get(API, timeout=120).text                 # JSON behind the download page
    m = re.search(r'"uri"\s*:\s*"(/downloadcenter/[^"]*PKT_[^"]*\.zip)"', txt)
    if not m:
        sys.exit("package link not found in the download-center API")
    url = urljoin("https://gds.hessen.de", quote(m.group(1)))
    target = OUT / unquote(url.rsplit("/", 1)[-1])
    print("Downloading", url, flush=True)
    for k in range(4):
        try:
            with S.get(url, stream=True, timeout=900) as r:
                r.raise_for_status()
                with open(target, "wb") as f:
                    for chunk in r.iter_content(1 << 22):
                        f.write(chunk)
            break
        except Exception as e:
            print("  retry", k + 1, e, flush=True); time.sleep(20)
    print(f"{target.name}: {target.stat().st_size / 1e6:.0f} MB")


if __name__ == "__main__":
    main()
