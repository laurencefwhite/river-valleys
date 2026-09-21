"""Builds data/ for the river valleys globe.

Inputs in build/raw: HydroBASINS v1c level 8 for the nine regions (fetch_raw.sh), Natural Earth 10m rivers,
lakes and marine polygons, and - if fetch_overture.py has been run - OpenStreetMap's named waterways from
Overture Maps. Every coarser level is level 8 dissolved on a shorter Pfafstetter code, so the five cuts nest.

  python build_data.py            names, paths, hues, data files (mapshaper steps run only if their output is missing)
  python build_data.py --remap    redo the mapshaper steps as well
  python build_data.py --rename   redo the river-name join (slow with the OSM names) rather than reuse work/names8.json
"""
import glob, json, math, os, pickle, subprocess, sys, collections, shutil
import shapefile
from shapely.geometry import shape
from shapely.strtree import STRtree
from shapely.validation import make_valid
from shapely import wkb as shp_wkb
import shapely

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, 'raw')
WORK = os.path.join(HERE, 'work')
OUT = os.path.join(HERE, '..', 'data')
OVT = os.path.join(RAW, 'overture')
CLOCK = os.path.join(HERE, '..', '..', 'world-clock-site', 'index.html')
os.makedirs(WORK, exist_ok=True)
os.makedirs(OUT, exist_ok=True)

FINEST = 8
# level: (simplify metres, smallest island kept, quantisation, tile prefix length or 0 for one global file)
LEVELS = collections.OrderedDict([
    (4, (8000, '400km2', '2e4', 0)),
    (5, (4500, '120km2', '5e4', 0)),
    (6, (3000, '40km2', '1e5', 0)),
    (7, (1200, '12km2', '4e5', 3)),
    (8, (500, '3km2', '1e6', 4)),
])
OVERRIDES_FILE = os.path.join(HERE, 'name_overrides.json')
SRC = 'raw/hybas_*_lev08_v1c/hybas_*_lev08_v1c.shp'


def sh(cmd):
    print('>', cmd[:170], flush=True)
    subprocess.run(cmd, shell=True, check=True, cwd=HERE)


def mapshaper():
    remap = '--remap' in sys.argv
    if remap or not os.path.exists(os.path.join(WORK, 'g8.geojson')):
        sh('npx mapshaper-xl 12gb -i %s combine-files snap -merge-layers -filter-fields PFAF_ID '
           '-rename-fields p=PFAF_ID -simplify interval=400 keep-shapes -clean '
           '-o format=geojson precision=0.0001 work/g8.geojson' % SRC)
    for lvl, (interval, isl, q, _) in LEVELS.items():
        dst = 'work/l%d.topo.json' % lvl
        if os.path.exists(os.path.join(HERE, dst)) and not remap:
            continue
        sh('npx mapshaper-xl 12gb -i %s combine-files snap -merge-layers -filter-fields PFAF_ID '
           '-each "p=Math.floor(PFAF_ID/%d)" -dissolve p -simplify interval=%d keep-shapes '
           '-filter-islands min-area=%s -clean -o format=topojson quantization=%s %s'
           % (SRC, 10 ** (FINEST - lvl), interval, isl, q, dst))
    if remap or not os.path.exists(os.path.join(WORK, 'rivers.topo.json')):
        sh('npx mapshaper -i raw/ne_10m_rivers_lake_centerlines/ne_10m_rivers_lake_centerlines.shp '
           '-filter-fields name,name_en,scalerank,featurecla -each "n=name_en||name, s=scalerank, '
           'k=(featurecla==\'Lake Centerline\'?1:0)" -filter-fields n,s,k -simplify interval=1500 '
           '-o format=topojson quantization=1e5 work/rivers.topo.json')
        sh('npx mapshaper -i raw/ne_10m_lakes/ne_10m_lakes.shp -filter "scalerank<=6" '
           '-filter-fields name -rename-fields n=name -simplify interval=2000 keep-shapes '
           '-o format=topojson quantization=1e5 work/lakes.topo.json')


def read_attrs():
    a = {}
    for f in glob.glob(os.path.join(RAW, 'hybas_*_lev08_v1c', '*.shp')):
        r = shapefile.Reader(f[:-4])
        for rec in r.iterRecords():
            d = rec.as_dict()
            a[d['PFAF_ID']] = d
    return a


def geom_of(g):
    s = shape(g)
    if not s.is_valid:
        s = make_valid(s)
    return s


def km_len(line):
    """Length in km of a lon/lat line, near enough for ranking."""
    if line.is_empty:
        return 0.0
    tot = 0.0
    for part in getattr(line, 'geoms', [line]):
        if part.geom_type not in ('LineString', 'LinearRing'):
            continue
        c = list(part.coords)
        for (x0, y0), (x1, y1) in zip(c, c[1:]):
            k = math.cos(math.radians((y0 + y1) / 2))
            tot += math.hypot((x1 - x0) * k, y1 - y0) * 111.2
    return tot


# ---------------------------------------------------------------- river names
CLASS_W = {'river': 1.0, 'stream': 0.8, 'canal': 0.25, 'ne': 1.0}


def load_lines_ne():
    out = []
    rv = shapefile.Reader(os.path.join(RAW, 'ne_10m_rivers_lake_centerlines', 'ne_10m_rivers_lake_centerlines'),
                          encoding='utf-8')
    for sr in rv.iterShapeRecords():
        d = sr.record.as_dict()
        nm = (d.get('name_en') or d.get('name') or '').strip()
        if nm:
            out.append((nm, 'ne', shape(sr.shape.__geo_interface__)))
    return out


def clean_name(n):
    """'River Thames' and 'Hawkesbury River' become Thames and Hawkesbury, as an atlas index has them; a creek,
    brook or burn keeps its word, since South Creek is not 'South'."""
    n = (n or '').strip()
    if n.lower().startswith('river ') and len(n) > 8:
        n = n[6:]
    elif n.lower().endswith(' river') and len(n) > 8:
        n = n[:-6]
    return n if 1 < len(n) < 60 else ''


G = {'within': {}, 'joined': {}}          # what the name join learnt besides the names, for the paths


def name_units(attrs, ids, geoms, gidx):
    """The river that leaves each level 8 valley: of the named waterways passing within a kilometre of the
    boundary the valley drains across, the one with the most length inside the valley. A valley that ends at
    the sea or in a sink has no such boundary, and takes the longest river in it."""
    cache = os.path.join(WORK, 'names8.json')
    if os.path.exists(cache) and '--rename' not in sys.argv:
        j = json.load(open(cache, encoding='utf-8'))
        water = pickle.load(open(os.path.join(WORK, 'water8.pkl'), 'rb')) if os.path.exists(os.path.join(WORK, 'water8.pkl')) else {}
        G.update(within={int(k): v for k, v in j.get('within', {}).items()}, joined={int(k): v for k, v in j.get('joined', {}).items()})
        return {int(k): v for k, v in j['names'].items()}, {int(k): v for k, v in j['inside'].items()}, water
    hy2p = {d['HYBAS_ID']: p for p, d in attrs.items()}
    parts = sorted(glob.glob(os.path.join(OVT, 'part-*.parquet')))
    use_osm = bool(parts)
    print('names from', 'OpenStreetMap (Overture) + Natural Earth' if use_osm else 'Natural Earth only', flush=True)
    ne = load_lines_ne()
    names, inside, present, water, weak = {}, {}, {}, {}, {}
    within, joined = G['within'], G['joined']
    # by region, so that only a region's waterways are in memory at once
    regions = collections.defaultdict(list)
    for p in ids:
        regions[str(p)[:2]].append(p)
    if use_osm:
        import duckdb
        con = duckdb.connect()
        con.execute("CREATE VIEW w AS SELECT * FROM read_parquet('%s', union_by_name=true)" % os.path.join(OVT, 'part-*.parquet').replace(os.sep, '/'))
    for rg, plist in sorted(regions.items()):
        bx = [180, 90, -180, -90]
        for p in plist:
            b = geoms[gidx[p]].bounds
            bx = [min(bx[0], b[0]), min(bx[1], b[1]), max(bx[2], b[2]), max(bx[3], b[3])]
        lines = [(n, c, g) for (n, c, g) in ne if g.bounds[2] >= bx[0] and g.bounds[0] <= bx[2] and g.bounds[3] >= bx[1] and g.bounds[1] <= bx[3]]
        ne_names = set(n for n, _, _ in lines)
        if use_osm:
            rows = con.execute("SELECT DISTINCT ON (id) name, name_en, subtype, wkb FROM w WHERE x1 >= ? AND x0 <= ? AND y1 >= ? AND y0 <= ?",
                               [bx[0] - 0.05, bx[2] + 0.05, bx[1] - 0.05, bx[3] + 0.05]).fetchall()
            for nm, en, st, w in rows:
                n = clean_name(en) or clean_name(nm)
                if not n or st == 'canal':               # a canal crosses divides, and is nobody's tributary
                    continue
                try:
                    lines.append((n, st, shp_wkb.loads(bytes(w))))
                except Exception:
                    pass
        print('region', rg, len(plist), 'valleys', len(lines), 'waterways', flush=True)
        if not lines:
            continue
        lg = [l[2] for l in lines]
        tree = STRtree(lg)
        for p in plist:
            g = geoms[gidx[p]]
            d = attrs[p]
            down = hy2p.get(d['NEXT_DOWN'])
            cand = tree.query(g, predicate='intersects')
            if not len(cand):
                continue
            foot = footline = None
            dgeom = geoms[gidx[down]] if down in gidx else None
            if dgeom is not None:
                try:
                    footline = g.boundary.intersection(dgeom.buffer(0.0008))
                    foot = None if footline.is_empty else footline.buffer(0.01)
                except Exception:
                    foot = None
            tot = collections.defaultdict(float)
            near = {}
            pieces = collections.defaultdict(list)
            isriver = set()
            # Natural Earth's rivers are drawn for a wall map and wander a kilometre or two off their beds, into
            # the wrong valley as often as not, so they name a valley only where nothing surveyed is to hand
            surveyed = any(lines[i][1] != 'ne' for i in cand)
            for i in cand:
                n, c, l = lines[i]
                if surveyed and c == 'ne':
                    continue
                if c != 'stream':
                    isriver.add(n)
                try:
                    clip = l.intersection(g)
                    L = km_len(clip) * CLASS_W.get(c, 0.5)
                except Exception:
                    continue
                if L <= 0:
                    continue
                tot[n] += L
                if c != 'ne':
                    pieces[n].append((c, clip))
            if not tot:
                continue
            # A river is mapped in many pieces, and the modelled foot can sit a kilometre from the real junction,
            # so what counts is the name: some piece of it by the foot, on this side or the other.
            beyond = set()
            across = collections.defaultdict(float)          # what runs by the foot on the far side, and how much
            if foot is not None:
                for i in tree.query(foot, predicate='intersects'):
                    n, c, l = lines[i]
                    if surveyed and c == 'ne':
                        continue
                    try:
                        far_km = km_len(l.intersection(dgeom).intersection(foot))
                    except Exception:
                        far_km = 0
                    if far_km > 0 and c != 'stream':
                        across[n] += far_km
                    if n not in tot:
                        continue
                    near[n] = min(near.get(n, 9), l.distance(footline))
                    if far_km > 0:
                        beyond.add(n)
            within[p] = {n: round(tot[n], 1) for n in tot if tot[n] >= 1.0}
            inside[p] = [n for n in sorted(tot, key=lambda n: -tot[n])[:3] if tot[n] >= 2]
            # the waterways worth drawing in this valley: its rivers, its longest creeks, and whatever is at its foot
            keep = set(sorted(tot, key=lambda n: -tot[n])[:4]) | set(near)
            w = []
            for n, pcs in pieces.items():
                for c, clip in pcs:
                    if n in keep or c == 'river':
                        for part in getattr(clip, 'geoms', [clip]):
                            if part.geom_type == 'LineString' and part.length > 0.003:
                                w.append((n, 1 if c == 'river' else 0, shapely.to_wkb(part.simplify(0.0006))))
            if w:
                water[p] = w
            if foot is None:
                present[p] = {n: round(tot[n], 1) for n in tot if tot[n] >= 1.0}     # a mouth: settled afterwards
                continue
            # The river that leaves a valley is on both sides of its foot. A side creek that joins just above
            # the foot is only on this side, and the river being joined is only on the far side.
            # Being on both sides counts fourfold, not absolutely: the Murrumbidgee runs a hundred kilometres to a
            # junction just inside its last valley, and must not lose that valley to the anabranch that does cross.
            pool = [n for n in near if tot[n] >= 1.0]
            if pool:
                names[p] = max(pool, key=lambda n: (n in isriver, tot[n] * (4 if n in beyond else 1)))   # a river before a creek
                if names[p] not in beyond:
                    weak[p] = within[p]
            else:
                weak[p] = within[p]
            if across:
                joined[p] = {n: round(k, 2) for n, k in across.items()}
    # a mouth takes the river its trunk brings down to it, if that river is in it; otherwise its longest
    ups = collections.defaultdict(list)
    for p, d in attrs.items():
        dn = hy2p.get(d['NEXT_DOWN'])
        if dn:
            ups[dn].append(p)
    # and where nothing was found on both sides of a foot, the river the trunk brings in goes on if it is there
    # at all: on flat country the mapped channel and the modelled one part company near the junctions
    for p in sorted(weak, key=lambda p: -attrs[p]['DIST_SINK']):
        up = max(ups[p], key=lambda u: attrs[u]['UP_AREA']) if ups.get(p) else None
        if up and names.get(up) in weak[p]:
            names[p] = names[up]
    for p, tot in present.items():
        if not tot:
            continue
        up = max(ups[p], key=lambda u: attrs[u]['UP_AREA']) if ups.get(p) else None
        if up and names.get(up) in tot:
            names[p] = names[up]
        else:
            names[p] = max(tot, key=lambda n: tot[n])
    json.dump({'names': names, 'inside': inside, 'within': within, 'joined': joined}, open(cache, 'w', encoding='utf-8'), ensure_ascii=False)
    pickle.dump(water, open(os.path.join(WORK, 'water8.pkl'), 'wb'))
    return names, inside, water


# ---------------------------------------------------------------- river paths
def build_paths(attrs, names, main_name):
    """For every level 8 valley, the river names from the mouth up to it, e.g. Thames/Cherwell/Ray.
    '/' is a confluence: what follows is a tributary of what came before. '>' is the same water under a new
    name: the downstream name is carried by no branch above that point, and what follows is the main stem."""
    hy2p = {d['HYBAS_ID']: p for p, d in attrs.items()}
    down = {p: hy2p.get(d['NEXT_DOWN']) for p, d in attrs.items()}
    ups = collections.defaultdict(list)
    for p, dn in down.items():
        if dn:
            ups[dn].append(p)
    stem_up = {dn: max(us, key=lambda u: attrs[u]['UP_AREA']) for dn, us in ups.items()}
    order = [p for p, dn in down.items() if not dn]                  # mouths first, then up the network,
    for p in order:                                                   # so a valley's downstream is always done
        order.extend(ups.get(p, ()))
    dist = {p: d['DIST_SINK'] for p, d in attrs.items()}
    far = {}
    for p, n in names.items():
        k = (attrs[p]['MAIN_BAS'], n)
        if k not in far or dist[p] > dist[far[k]]:
            far[k] = p
    path = {}                # p -> tuple of (sep, name); the first sep is ''
    on_stem = {}             # p -> still on the unbroken main stem of the last named river below
    for p in order:
        dn = down[p]
        n = names.get(p, '')
        if not dn:
            base = main_name.get(attrs[p]['MAIN_BAS']) or n
            if n and ('–' in base or base.endswith(' basin')):
                base = n                                 # Murray-Darling names a basin; the river at its mouth is the Murray
            pt = (('', base),) if base else ()
            if n and base and n != base:
                pt = pt + (('>', n),)
            path[p] = pt
            on_stem[p] = True
            continue
        pd = path[dn]
        stem = on_stem[dn] and stem_up[dn] == p
        last = pd[-1][1] if pd else ''
        if not n or n == last:
            path[p] = pd
            on_stem[p] = stem if not n else True
            if not n and not stem and pd and pd[-1][1] != '?':
                path[p] = pd + (('/', '?'),)             # a side valley whose stream the gazetteers do not name
                on_stem[p] = True
            continue
        if n in [x[1] for x in pd]:                      # the name went away and came back: an anabranch, or noise
            k = max(i for i, x in enumerate(pd) if x[1] == n)
            path[p] = pd[:k + 1]
            on_stem[p] = True
            continue
        within, joined = G['within'], G['joined']
        carried = any(names.get(u, '') == last for u in ups[dn] if u != p)
        if not carried and last and last != '?':
            # or the old name goes on further up by another branch: follow its farthest valley down, and see
            # whether it comes through here
            f = far.get((attrs[p]['MAIN_BAS'], last))
            if f is not None and dist[f] > dist[dn]:
                q = f
                while q and q != p and q != dn:
                    q = down[q]
                carried = q == dn
        # Where the old name stops, one of the waters above is the same river renamed: the one that is already
        # running under its new name inside the junction valley (the Nepean, below the Hawkesbury). Failing that
        # evidence, the larger branch. Everything else there is a tributary.
        # what this valley joins, when that is not the river below: only if the river below is nowhere by the foot
        ac = joined.get(p) or {}
        J = None
        if isinstance(ac, dict) and last not in ac:
            others = [x for x in ac if x != n]
            J = max(others, key=lambda x: ac[x]) if others else None
        w = within.get(dn, {})
        if not (J and J != last and J != n and w.get(J, 0) >= 5 and J not in [x[1] for x in pd]):
            J = None
        target = None
        if not carried and last and last != '?':
            pool = [x for x in set(names.get(u, '') for u in ups[dn]) | ({J} if J else set()) if x and x != last and x in w]
            if pool:
                target = max(pool, key=lambda x: w[x])
            elif stem:
                target = n
        if pd and pd[-1][1] == '?':
            pd = pd[:-1]
        if J:                                            # this valley joins J, and J is what comes down to here
            pd = pd + (('>' if target == J else '/', J),)
            path[p] = pd + (('/', n),)
        else:
            path[p] = pd + ((('>' if target == n else '/') if pd else '', n),)
        on_stem[p] = True
    return path


def path_str(pt):
    return ''.join(s + n for s, n in pt)


# ---------------------------------------------------------------- topology helpers
def flat(a):
    for b in a:
        if isinstance(b, list):
            yield from flat(b)
        else:
            yield b


def arc_users(obj):
    users = collections.defaultdict(list)
    for gi, g in enumerate(obj['geometries']):
        if 'arcs' in g:
            for a in flat(g['arcs']):
                users[a if a >= 0 else ~a].append(gi)
    return users


def arc_lengths(topo):
    sx, sy = topo['transform']['scale']
    ty = topo['transform']['translate'][1]
    out = []
    for arc in topo['arcs']:
        x = y = 0; L = 0.0; prev = None
        for dx, dy in arc:
            x += dx; y += dy
            pt = (x * sx, y * sy + ty)
            if prev:
                L += math.hypot((pt[0] - prev[0]) * math.cos(math.radians(pt[1])), pt[1] - prev[1])
            prev = pt
        out.append(L)
    return out


def remap_arcs(a, m):
    if isinstance(a, list):
        return [remap_arcs(b, m) for b in a]
    return m[a] if a >= 0 else ~m[~a]


def main():
    mapshaper()
    attrs = read_attrs()
    print('level 8 valleys', len(attrs), flush=True)

    gj = json.load(open(os.path.join(WORK, 'g8.geojson'), encoding='utf-8'))
    ids, geoms = [], []
    for f in gj['features']:
        if f['geometry'] and f['properties']['p'] in attrs:
            ids.append(f['properties']['p'])
            geoms.append(geom_of(f['geometry']))
    del gj
    gidx = {p: i for i, p in enumerate(ids)}
    names, inside, water = name_units(attrs, ids, geoms, gidx)
    kids = collections.defaultdict(list)
    for p in water:
        kids[str(p)[:3]].append(p); kids[str(p)[:4]].append(p)
    print('named', len(names), 'of', len(attrs), flush=True)

    # ---- the basins: everything that shares a mouth
    hy2p = {d['HYBAS_ID']: p for p, d in attrs.items()}
    mains = {}
    for p, d in attrs.items():
        m = mains.setdefault(d['MAIN_BAS'], {'units': [], 'area': 0.0})
        m['units'].append(p)
        m['area'] += d['SUB_AREA']
    order = sorted(mains, key=lambda k: -mains[k]['area'])

    lakes = shapefile.Reader(os.path.join(RAW, 'ne_10m_lakes', 'ne_10m_lakes'), encoding='utf-8')
    lake_g, lake_n = [], []
    for sr in lakes.iterShapeRecords():
        n = (sr.record.as_dict().get('name') or '').strip()
        if n:
            lake_g.append(geom_of(sr.shape.__geo_interface__)); lake_n.append(n)
    lake_tree = STRtree(lake_g)
    seas = shapefile.Reader(os.path.join(RAW, 'ne_10m_geography_marine_polys', 'ne_10m_geography_marine_polys'),
                            encoding='utf-8')
    sea_g, sea_n = [], []
    for sr in seas.iterShapeRecords():
        n = (sr.record.as_dict().get('name') or '').strip()
        if n.isupper():
            n = n.title().replace(' Of ', ' of ')
        if n and sr.record.as_dict().get('featurecla') != 'river':
            sea_g.append(geom_of(sr.shape.__geo_interface__)); sea_n.append(n)
    sea_tree = STRtree(sea_g)

    overrides = json.load(open(OVERRIDES_FILE, encoding='utf-8')) if os.path.exists(OVERRIDES_FILE) else {}
    # overrides are keyed by the mouth's code at level 6 or finer; the largest basin under that code takes it
    taken = set()

    def override(kind, outlet):
        s = str(outlet)
        for key, val in overrides.get(kind, {}).items():
            if s.startswith(key) and (kind, key) not in taken:
                taken.add((kind, key))
                return val
        return None

    main_name = {}
    for k in order:
        m = mains[k]
        outlet = hy2p.get(k)
        m['outlet'] = outlet
        od = attrs[outlet]
        m['id'] = outlet
        m['endo'] = 1 if od['ENDO'] == 2 else 0
        m['lump'] = [n for n in inside.get(outlet, [])] if (od['COAST'] and len(m['units']) == 1) else None
        name = '' if m['lump'] is not None else names.get(outlet, '')
        if m['lump'] is None and len(m['units']) > 1:
            # A delta gives the mouth a branch's name (Rosetta, Chilia), so the basin takes the name most of its
            # trunk carries: the valleys that more than half the basin drains through.
            trunk = collections.Counter(names[p] for p in m['units'] if names.get(p) and attrs[p]['UP_AREA'] > 0.5 * m['area'])
            if trunk:
                mode = trunk.most_common(1)[0][1]
                for p in sorted(m['units'], key=lambda p: attrs[p]['DIST_SINK']):
                    if attrs[p]['UP_AREA'] > 0.5 * m['area'] and trunk.get(names.get(p), 0) >= 0.5 * mode:
                        name = names[p]
                        break
        m['name'] = override('main', outlet) or name
        m['k'] = len(os.path.commonprefix([str(p) for p in m['units']]))

        sink = ''
        if m['area'] >= 300 or m['name']:                 # the sea is only ever shown for these
            g = geoms[gidx[outlet]] if outlet in gidx else None
            if g is not None and m['endo']:
                best = 0
                for i in lake_tree.query(g):
                    try:
                        a = lake_g[i].intersection(g).area
                    except Exception:
                        a = 0
                    if a > best:
                        best, sink = a, lake_n[i]
                if not sink:
                    for i in sea_tree.query(g):
                        if sea_g[i].intersects(g):
                            sink = sea_n[i]
            elif g is not None:
                b = g.buffer(0.3)
                best = 0
                for i in sea_tree.query(b):
                    try:
                        a = sea_g[i].intersection(b).area
                    except Exception:
                        a = 0
                    if a > best:
                        best, sink = a, sea_n[i]
                if not sink:
                    i = sea_tree.nearest(g)
                    if i is not None and sea_g[i].distance(g) < 4:
                        sink = sea_n[i]
        m['sink'] = override('sea', outlet) or sink
        if m['endo'] and not m['name'] and m['sink']:
            m['name'] = m['sink'] + ' basin'
        main_name[k] = m['name']
    print('basins', len(order), flush=True)

    paths = build_paths(attrs, names, main_name)

    # ---- hue: neighbours must differ, big basins choose first
    topo8 = json.load(open(os.path.join(WORK, 'l8.topo.json'), encoding='utf-8'))
    obj8 = list(topo8['objects'].values())[0]
    alen = arc_lengths(topo8)
    users = arc_users(obj8)
    gm = [attrs[g['properties']['p']]['MAIN_BAS'] if g['properties']['p'] in attrs else None for g in obj8['geometries']]
    nb = collections.defaultdict(lambda: collections.defaultdict(float))
    for a, us in users.items():
        ms = set(gm[i] for i in us if gm[i] is not None)
        if len(ms) == 2:
            x, y = list(ms)
            nb[x][y] += alen[a]; nb[y][x] += alen[a]
    del topo8, obj8, users, alen
    HUES = [h for h in [(i * 7 % 24) * 15 for i in range(24)] if not 185 <= h <= 265]      # blue is kept for water; ties fall to the first, so the order is scattered
    used = collections.Counter()
    hue = {}

    def cd(a, b):
        d = abs(a - b) % 360
        return min(d, 360 - d)
    for k in order:
        n1 = [hue[j] for j in nb[k] if j in hue]
        n2 = [hue[j2] for j in nb[k] for j2 in nb[j] if j2 in hue and j2 != k]
        best, bs = None, -1e9
        for h in HUES:
            s = min([cd(h, x) for x in n1] + [180]) + 0.2 * min([cd(h, x) for x in n2] + [180]) - 0.02 * used[h]
            if s > bs:
                bs, best = s, h
        hue[k] = best
        used[best] += 1

    # ---- anchors for labels and search
    for k in order:
        m = mains[k]
        m['hue'] = hue[k]
        m['anchor'] = None
        if not m['name']:
            continue
        if m['area'] < 12000:
            if m['outlet'] in gidx:
                pt = geoms[gidx[m['outlet']]].representative_point().coords[0]
                m['anchor'] = [round(pt[0], 2), round(pt[1], 2)]
            continue
        parts = [geoms[gidx[p]] for p in m['units'] if p in gidx]
        if not parts:
            continue
        u = shapely.union_all(parts).simplify(0.02)
        try:
            pt = shapely.maximum_inscribed_circle(u, tolerance=0.05).coords[0]
        except Exception:
            pt = u.representative_point().coords[0]
        m['anchor'] = [round(pt[0], 2), round(pt[1], 2)]

    def main_record(k):
        m = mains[k]
        return [m['name'], m['sink'], int(round(m['area'])), m['hue'], m['endo'], 1 if m['lump'] is not None else 0,
                m['k'], m['anchor'], len(m['units']), m['lump'] or []]

    # ---- the five cuts
    for d in ('l7', 'l8'):
        if os.path.isdir(os.path.join(OUT, d)):
            shutil.rmtree(os.path.join(OUT, d))
    base_mains = set(k for k in order if mains[k]['area'] >= 2500)
    tile_index = {}
    for lvl, (_, _, _, tile_len) in LEVELS.items():
        div = 10 ** (FINEST - lvl)
        area = collections.defaultdict(float)
        up = collections.defaultdict(float)
        foot = {}
        share = collections.defaultdict(lambda: collections.defaultdict(float))
        for p, d in attrs.items():
            q = p // div
            area[q] += d['SUB_AREA']
            share[q][d['MAIN_BAS']] += d['SUB_AREA']
            if d['UP_AREA'] >= up[q]:
                up[q] = d['UP_AREA']; foot[q] = p
        topo = json.load(open(os.path.join(WORK, 'l%d.topo.json' % lvl), encoding='utf-8'))
        obj = list(topo['objects'].values())[0]
        geos = [g for g in obj['geometries'] if g['properties']['p'] in area and 'arcs' in g]
        obj['geometries'] = geos
        for g in geos:
            q = g['properties']['p']
            mk = max(share[q], key=lambda j: share[q][j])
            g['_m'] = mk
            g['_path'] = path_str(paths.get(foot[q], ()))
            g['properties'] = {'p': q, 'm': mains[mk]['id'], 'a': int(round(area[q])), 'u': int(round(up[q]))}
            if lvl <= 6:
                base_mains.add(mk)
        users = arc_users(obj)

        def arc_class(a):
            us = set(users[a])
            if len(us) == 1:
                return 'c'
            return 'd' if len(set(geos[i]['_m'] for i in us)) > 1 else 's'

        def water_of(code, tol, q):
            wn, widx, out = [], {}, []
            for p8 in kids.get(code, ()):
                for n, st, wb in water[p8]:
                    g = shp_wkb.loads(wb).simplify(tol)
                    c = [(int(round(x * q)), int(round(y * q))) for x, y in g.coords]
                    flatc = [c[0][0], c[0][1]]
                    for (x0, y0), (x1, y1) in zip(c, c[1:]):
                        if x1 != x0 or y1 != y0:
                            flatc += [x1 - x0, y1 - y0]
                    if len(flatc) < 4:
                        continue
                    if n not in widx:
                        widx[n] = len(wn); wn.append(n)
                    out.append([widx[n], st, str(p8)[:4], flatc])
            return {'names': wn, 'q': q, 'lines': out} if out else None

        def write(dst, var, glist, with_mains, code=None):
            used_arcs = sorted(set(a if a >= 0 else ~a for g in glist for a in flat(g['arcs'])))
            amap = {a: i for i, a in enumerate(used_arcs)}
            plist, pidx = [], {}
            out_g = []
            for g in glist:
                s = g['_path']
                if s not in pidx:
                    pidx[s] = len(plist); plist.append(s)
                pr = dict(g['properties']); pr['h'] = pidx[s]
                out_g.append({'type': g['type'], 'arcs': remap_arcs(g['arcs'], amap), 'properties': pr})
            lines = {'c': [], 'd': [], 's': []}
            for a in used_arcs:
                lines[arc_class(a)].append(amap[a])
            t = {'type': 'Topology', 'transform': topo['transform'], 'arcs': [topo['arcs'][a] for a in used_arcs],
                 'objects': {'u': {'type': 'GeometryCollection', 'geometries': out_g}}, 'lines': lines, 'paths': plist}
            if code:
                wt = water_of(code, 0.004 if lvl == 7 else 0.0012, 1000 if lvl == 7 else 10000)
                if wt:
                    t['water'] = wt
            if with_mains:
                t['mains'] = {str(mains[k]['id']): main_record(k) for k in set(g['_m'] for g in glist) if k not in base_mains}
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, 'w', encoding='utf-8') as fh:
                fh.write(var[0])
                json.dump(t, fh, separators=(',', ':'), ensure_ascii=False)
                fh.write(var[1])
            return os.path.getsize(dst)

        if not tile_len:
            n = write(os.path.join(OUT, 'l%d.js' % lvl), ('window.RV_DATA=window.RV_DATA||{};window.RV_DATA.l%d=' % lvl, ';\n'), geos, False)
            print('level', lvl, len(geos), 'valleys', round(n / 1e6, 2), 'MB', flush=True)
        else:
            groups = collections.defaultdict(list)
            for g in geos:
                groups[str(g['properties']['p'])[:tile_len]].append(g)
            tot = 0; big = 0
            for code, glist in groups.items():
                n = write(os.path.join(OUT, 'l%d' % lvl, code + '.js'), ('RV_TILE(%d,"%s",' % (lvl, code), ');\n'), glist, True, code)
                tot += n; big = max(big, n)
            tile_index[lvl] = sorted(groups)
            print('level', lvl, len(geos), 'valleys in', len(groups), 'tiles', round(tot / 1e6, 1), 'MB, largest', round(big / 1e6, 2), flush=True)
        del topo, obj, geos, users

    # ---- base: basins, rivers, lakes, and the world clock's countries and cities
    html = open(CLOCK, encoding='utf-8').read()
    start = html.index('window.WTZ_DATA=') + len('window.WTZ_DATA=')
    wtz = json.loads(html[start:html.index('\n', start)].rstrip().rstrip(';'))
    base = {
        'mains': {str(mains[k]['id']): main_record(k) for k in order if k in base_mains},
        'tiles': {str(l): {'len': LEVELS[l][3], 'codes': c} for l, c in tile_index.items()},
        'counts': {'sea': sum(1 for k in order if not mains[k]['endo']), 'inland': sum(1 for k in order if mains[k]['endo']),
                   'levels': {str(l): len(set(p // 10 ** (FINEST - l) for p in attrs)) for l in LEVELS}},
        'rivers': json.load(open(os.path.join(WORK, 'rivers.topo.json'), encoding='utf-8')),
        'lakes': json.load(open(os.path.join(WORK, 'lakes.topo.json'), encoding='utf-8')),
        'countries': wtz['countries'],
        'cities': [[c[0], c[1], c[3], c[4], c[5], c[6], c[7]] for c in wtz['cities']],
    }
    with open(os.path.join(OUT, 'base.js'), 'w', encoding='utf-8') as fh:
        fh.write('window.RV_DATA=window.RV_DATA||{};Object.assign(window.RV_DATA,')
        json.dump(base, fh, separators=(',', ':'), ensure_ascii=False)
        fh.write(');\n')
    print('base.js', round(os.path.getsize(os.path.join(OUT, 'base.js')) / 1e6, 2), 'MB;', len(base['mains']), 'of', len(order), 'basins in it')

    with open(os.path.join(WORK, 'mains_report.tsv'), 'w', encoding='utf-8') as fh:
        for k in order[:600]:
            m = mains[k]
            fh.write('%s\t%s\t%s\t%d\t%d\t%s\t%s\n' % (m['outlet'], m['name'] or '-', m['sink'] or '-', m['area'],
                     len(m['units']), 'endo' if m['endo'] else '', m['hue']))
    with open(os.path.join(WORK, 'paths_sample.tsv'), 'w', encoding='utf-8') as fh:
        for p in sorted(paths)[::40]:
            fh.write('%s\t%s\n' % (p, path_str(paths[p])))


if __name__ == '__main__':
    main()
