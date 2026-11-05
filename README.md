# Frankfurt's Giants · Who gets the sun?

**#30DayMapChallenge 2026 · Day 5 · Senses: Sight · Frankfurt**

🌍 **Live map:** https://barisparlatangiller.github.io/30DMC26-D05-Sight-Frankfurt-HighRise-Shadows/ · all 30 maps: https://barisparlatangiller.github.io/

How do Frankfurt's buildings of 30 m and more (about 10+ storeys) shade the streets and rooftops around them?
The map draws their shadows on the 21st of every month, every 30 minutes, onto the **real surfaces** around them:
every roof at its own height and the ground between them. It also shows how many hours of direct sun each spot
loses to the towers over a whole day.

*Findings are filled in after the first full build.*

## Method

1. **Towers.** Every building part in Hessen's LoD2 3D building model with a measured height ≥ 30 m. Names, storeys,
   use and architect are joined from an OpenStreetMap extract of buildings with `building:levels ≥ 10`
   (`data/`, 5 Nov 2026).
2. **Receivers.** Every LoD2 building part around the towers, each at its own roof height above sea level, plus a
   ground model interpolated from the LoD2 ground points. Towers and receivers follow their **LoD2 roof planes**
   (each RoofSurface fitted as a plane), so setbacks, stepped crowns and pitched roofs keep their shape. Low buildings receive shadow but **cast none**: the map
   isolates the towers' effect.
3. **Shadow test.** A point at height z is in a tower's shadow when the line from the point towards the sun passes
   the tower footprint below the tower top: `z < max(top − distance · tan(sun altitude))`. Per tower and time
   step this is solved on a 2 m grid rotated to the sun direction, using a cumulative
   maximum (`pipeline/shadows.py`).
4. **Sun.** pvlib (NREL SPA) for Frankfurt (50.11° N, 8.68° E), the 21st of each month 2026, local time, every
   30 minutes while the sun is ≥ 5° high. Shadows are capped at 2.5 km.
5. **Lost sun hours.** The 30-minute steps are added up on a 5 m grid per day.

## Limits

Spires and antennas thinner than the LoD2 model, shadows on façades, trees and terrain shading are left out, and clear sky is assumed. The results describe geometry. They are not measured
sunlight, temperature or property values. Buildings outside the city of Frankfurt receive no shadow in this model.

## Build

```bash
pip install -r requirements.txt   # + tippecanoe
python pipeline/fetch_lod2.py          # HVBG Geodaten online, ~0.5 GB
python pipeline/lod2_to_buildings.py   # footprints, heights, ground and roof elevations
python pipeline/build_casters.py       # towers ≥ 30 m, receivers, ground model, OSM join
python pipeline/shadows.py             # 12 days × 30-minute steps
python pipeline/build_web.py           # lost sun hours, buildings tiles, tower stats
```

The GitHub Action runs this on every push, publishes `docs/` to Pages and pushes the result files to the
`data` branch.

## Data & licences

* 3D-Gebäudemodelle LoD2, Kreisfreie Stadt Frankfurt am Main © HVBG Hessen, dl-de/zero-2-0
* OpenStreetMap © OpenStreetMap contributors, ODbL 1.0
* pvlib python (BSD-3), solar position after Reda & Andreas (2004)
* Basemap © CARTO, © OpenStreetMap contributors

Code MIT · Barış Parlatangiller · built with AI assistance (Claude, Anthropic)
