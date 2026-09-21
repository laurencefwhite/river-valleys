"""Pulls the named rivers, streams and canals out of Overture Maps' water layer (OpenStreetMap names, ODbL)
into build/raw/overture/part-<region>.parquet, skipping any region already there.

  python fetch_overture.py au gb        named regions (see REGIONS)
  python fetch_overture.py world        the globe in eighteen 60 x 50 degree cells, after the named regions

A feature belongs to the cell that holds the south-west corner of its bounding box, so cells never repeat
one another; the named regions do overlap the cells, and build_data.py drops the repeats by id.
The whole world is a scan of about 28 GB: leave it running overnight on a slow link.
"""
import duckdb, os, sys, time

REL = '2026-08-19.0'
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, 'raw', 'overture')
os.makedirs(OUT, exist_ok=True)

REGIONS = {
    'au': (112, -44.5, 179, -9),          # Australia and New Zealand
    'gb': (-11, 49.5, 2.2, 61),           # the British Isles
}

args = sys.argv[1:] or ['au', 'gb']
jobs = []
for a in args:
    if a == 'world':
        for x in range(-180, 180, 60):
            for y in range(-60, 90, 50):
                jobs.append(('cell_%+04d_%+03d' % (x, y), (x, y, x + 60, y + 50)))
    else:
        jobs.append((a, REGIONS[a]))

con = duckdb.connect()
con.execute("INSTALL httpfs; LOAD httpfs; INSTALL spatial; LOAD spatial; SET threads=4; SET memory_limit='3GB'; SET preserve_insertion_order=false; SET s3_region='us-west-2'; SET http_retries=10;")
src = "s3://overturemaps-us-west-2/release/%s/theme=base/type=water/*.parquet" % REL
for name, (x0, y0, x1, y1) in jobs:
    dst = os.path.join(OUT, 'part-%s.parquet' % name).replace(os.sep, '/')
    if os.path.exists(dst):
        continue
    t = time.time()
    con.execute("""COPY (SELECT id, names.primary AS name, names.common['en'] AS name_en, subtype,
                   ST_AsWKB(ST_SimplifyPreserveTopology(geometry, 0.0003)) AS wkb,
                   bbox.xmin AS x0, bbox.ymin AS y0, bbox.xmax AS x1, bbox.ymax AS y1
                   FROM read_parquet('%s')
                   WHERE bbox.xmin >= %f AND bbox.xmin < %f AND bbox.ymin >= %f AND bbox.ymin < %f
                     AND names.primary IS NOT NULL AND subtype IN ('river','stream','canal')
                     AND ST_GeometryType(geometry) IN ('LINESTRING','MULTILINESTRING'))
                   TO '%s.tmp' (FORMAT parquet)""" % (src, x0, x1, y0, y1, dst))
    os.replace(dst + '.tmp', dst)
    n = con.execute("SELECT count(*) FROM read_parquet('%s')" % dst).fetchone()[0]
    print(name, n, 'rows', round(os.path.getsize(dst) / 1e6, 1), 'MB', round(time.time() - t), 's', flush=True)
