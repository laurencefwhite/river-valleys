# River Valleys

An interactive globe of the world's river valleys. Every outflow to the sea is a unit with its own
hue, and every tributary and sub-valley within it is a unit again, in a shade of that hue.

**Live:** https://laurencefwhite.github.io/river-valleys/

It is built on the globe engine of [World Time Zones](https://github.com/laurencefwhite/world-time-zones):
the same canvas globe, drag, zoom, fly-to, folding panels and touch behaviour.

## What it does

- **One hue per mouth.** Everything that reaches the sea by the same mouth shares a hue: about 18,400 outflows
  to the sea and 5,600 inland sinks. Neighbouring basins are given hues well apart. Basins with no way
  to the sea are drawn muted.
- **Shades within a basin.** The main stem runs dark and its tributaries light, and each tributary
  valley is split the same way again. The shade is read from the valley's Pfafstetter code: odd digits
  are reaches of the main stem numbered from the mouth, even digits the tributaries that join between.
- **Finer valleys as you zoom.** Five cuts of the same hierarchy - HydroBASINS levels 4 to 8, from 1,300
  valleys to 190,000 - swap in as the zoom rises, and the zoom runs to 64x. A valley's shade at one level
  is its parent's shade, nudged, so the picture holds still as it divides. Levels 7 and 8 come as tiles
  (`data/l7/`, `data/l8/`), fetched for whatever is on screen.
- **Hover a valley** for its river and the river path to it, read from the mouth up: `Thames / Cherwell /
  Ray`. A slash is a confluence, so what follows is a tributary of what came before. An angle bracket is
  the same water under another name: `Hawkesbury > Nepean`. A valley whose stream is not named in the
  sources ends the path as `[unnamed]`. The card also gives the sea reached, the valley's area and what
  it gathers, and the whole basin is outlined. Hover a city for the valley it stands in.
- **Search** for a basin, a river or a city. **Largest basins** in the key fly to each.
- **Layers:** valleys, tributary shading, divides, rivers, lakes, basin and tributary names, muted
  inland basins, country borders, cities, city names, graticule, slow spin.

## Running it

Open `index.html` in a browser. No build step and no server. `data/l5.js` and `data/l6.js` are loaded
only when the zoom calls for them.

## Rebuilding the data

```
cd build
npm install                     (mapshaper)
pip install shapely pyshp duckdb
sh fetch_raw.sh                 (HydroBASINS level 8 and Natural Earth, about 250 MB into build/raw)
python fetch_overture.py au gb  (OpenStreetMap waterway names by region; "world" is a 28 GB scan, leave it overnight)
python build_data.py            (--remap redoes the mapshaper steps, --rename redoes the river-name join)
```

How a valley gets its name: of the named waterways that pass within a kilometre of the boundary the valley
drains across, the one with the most length inside the valley. The path is then read down the flow network
of level 8 valleys. Going up from a river A into a differently named river B, B is a rename (`>`) if B is
the main stem (the larger branch) and no other branch above that point carries the name A; otherwise B is a
tributary (`/`). Where OpenStreetMap names have not been fetched, names fall back to Natural Earth's 10m
rivers, which name only the larger rivers.

`build/name_overrides.json` corrects basin and sea names by the Pfafstetter code of the mouth.
`build/work/mains_report.tsv` and `paths_sample.tsv` show what the naming came up with.

## Data and credits

| | |
|---|---|
| Valleys | [HydroBASINS](https://www.hydrosheds.org/products/hydrobasins) v1c, level 8 of the standard format, dissolved for levels 4-7. Lehner, B., Grill G. (2013). Global river hydrography and network routing: baseline data and new approaches to study the world's large river systems. *Hydrological Processes*, 27(15): 2171-2186. |
| Rivers, lakes, seas, countries, cities | [Natural Earth](https://www.naturalearthdata.com), public domain |
| River and creek names | [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors, via [Overture Maps](https://overturemaps.org) base/water, ODbL |

HydroBASINS is free for scientific, educational and commercial use with attribution, under the licence
in the HydroSHEDS technical documentation. River names come from Natural Earth's 10m rivers, so a valley
whose river is not in that set is shown as unnamed. Small stretches of coast hold many short rivers, each
with its own mouth; at these levels HydroBASINS lumps them, and the map calls them coastal valleys.
