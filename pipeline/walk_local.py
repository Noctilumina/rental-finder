"""Walking areas from local OpenStreetMap extracts.

Reads Geofabrik province extracts (data/raw/osm/*.osm.pbf; downloaded when missing), builds one walking network
around all candidate stations and computes, per 50 m cell of the Dutch RD grid:
  - walk time to every station within MAX_WALK_STATION_MIN,
  - walk time to the nearest supermarket and the nearest park,
  - route distance to the destination (for going there directly by bike or on foot).
Also collects busy roads and railways (for the noise estimate) and park outlines (map layer).

Outputs: data/grid.npz, data/supermarkets.json, data/parks.json, data/noise.npz, destination in data/meta.json.

Run:  python -m pipeline.walk_local [--download]
"""
import math
import re
import sys
import time

import numpy as np
import osmium
import requests
import shapely
from pyproj import Transformer
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import dijkstra
from scipy.spatial import cKDTree

from .common import (BIKE_PARK_MIN, BIKE_SPEED_M_PER_MIN, CELL_M, DATA, DESTINATION_QUERY, MAX_WALK_PARK_MIN,
                     MAX_WALK_SHOP_MIN, MAX_WALK_STATION_MIN, META_JSON, NOISE_NPZ, PARKS_JSON, RAW, STATIONS_JSON,
                     SUPERMARKETS_JSON, USER_AGENT, WALK_SPEED_M_PER_MIN, read_json, save_grid, write_json)

OSM_DIR = RAW / "osm"
PROVINCES = ["noord-brabant", "gelderland", "utrecht", "limburg", "zuid-holland", "noord-holland", "flevoland"]
GEOFABRIK = "https://download.geofabrik.de/europe/netherlands/{p}/{p}-latest.osm.pbf"

ST_M = MAX_WALK_STATION_MIN * WALK_SPEED_M_PER_MIN
SM_M = MAX_WALK_SHOP_MIN * WALK_SPEED_M_PER_MIN
PK_M = MAX_WALK_PARK_MIN * WALK_SPEED_M_PER_MIN
DEST_M = 16000          # direct trips to the destination are computed up to this route distance
GRAPH_DIST = int(ST_M + SM_M) + 200
SAMPLE_M = 20           # sample spacing along streets
SNAP_M = 75             # a cell counts when a street sample lies within this distance of its centre
STATION_SNAP_M = 80     # network nodes this close to the station point count as "at the station"
PARK_MIN_HA = 0.5
PARK_EDGE_M = 25        # network nodes this close to a park's edge count as "at the park"

to_rd = Transformer.from_crs(4326, 28992, always_xy=True)
to_wgs = Transformer.from_crs(28992, 4326, always_xy=True)

# Roughly OSMnx's "walk" network: anything walkable, excluding motorways and private/forbidden ways.
WALK_HIGHWAYS = {"footway", "path", "pedestrian", "steps", "living_street", "residential", "service",
                 "unclassified", "tertiary", "tertiary_link", "secondary", "secondary_link", "primary",
                 "primary_link", "track", "cycleway", "road", "corridor", "bridleway", "elevator", "crossing"}
# Noise sources: class per OSM tag (0 motorway, 1 trunk, 2 primary, 3 secondary, 4 railway)
NOISE_ROADS = {"motorway": 0, "motorway_link": 0, "trunk": 1, "trunk_link": 1, "primary": 2, "secondary": 3}
NOISE_STEP_M = 25


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def download(missing_only=False):
    OSM_DIR.mkdir(parents=True, exist_ok=True)
    for p in PROVINCES:
        dest = OSM_DIR / f"{p}.osm.pbf"
        if missing_only and dest.exists():
            continue
        log(f"Downloading {p} extract from Geofabrik")
        for url in extract_urls(p):
            with requests.get(url, stream=True, timeout=120, headers={"User-Agent": USER_AGENT}) as r:
                if r.status_code == 404:
                    # "latest" can point to today's file before Geofabrik has published it: try the day before
                    log(f"  not available yet: {r.url.rsplit('/', 1)[-1]}")
                    continue
                r.raise_for_status()
                tmp = dest.with_suffix(".part")
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        f.write(chunk)
                tmp.replace(dest)
                log(f"  got {r.url.rsplit('/', 1)[-1]}")
                break
        else:
            if dest.exists():
                log(f"  no {p} extract available right now; keeping the one downloaded earlier")
            else:
                raise RuntimeError(f"Geofabrik has no downloadable {p} extract right now; try again in an hour")


def extract_urls(p):
    """The 'latest' extract, then the dated ones listed on Geofabrik's page for the province, newest first."""
    yield GEOFABRIK.format(p=p)
    base = GEOFABRIK.format(p=p).rsplit("/", 1)[0]
    try:
        page = requests.get(base + "/", timeout=60, headers={"User-Agent": USER_AGENT}).text
    except requests.RequestException:
        return
    for d in sorted(set(re.findall(rf"{re.escape(p)}-(\d{{6}})\.osm\.pbf", page)), reverse=True)[:5]:
        yield f"{base}/{p}-{d}.osm.pbf"


def geocode(query):
    r = requests.get("https://api.pdok.nl/bzk/locatieserver/search/v3_1/free",
                     params={"q": query, "fq": "type:adres", "rows": 1}, timeout=30, headers={"User-Agent": USER_AGENT})
    doc = r.json()["response"]["docs"][0]
    lon, lat = map(float, doc["centroide_ll"].removeprefix("POINT(").removesuffix(")").split())
    return {"label": doc["weergavenaam"], "lat": lat, "lon": lon}


def tiles_near(stations, radius_m):
    """0.01-degree tiles that lie within radius of any station."""
    out = set()
    for s in stations:
        dlat = radius_m / 111_000 + 0.01
        dlon = radius_m / (111_000 * math.cos(math.radians(s["lat"]))) + 0.01
        for i in range(int((s["lat"] - dlat) * 100), int((s["lat"] + dlat) * 100) + 1):
            for j in range(int((s["lon"] - dlon) * 100), int((s["lon"] + dlon) * 100) + 1):
                out.add((i, j))
    return out


def read_osm(points):
    tiles = tiles_near(points, GRAPH_DIST + 1500)
    in_area = lambda lat, lon: (int(lat * 100), int(lon * 100)) in tiles
    ways_ids, ways_lon, ways_lat = [], [], []
    shops, parks = {}, {}
    noise = []  # (lon array, lat array, class)
    for pbf in sorted(OSM_DIR.glob("*.osm.pbf")):
        log(f"Reading {pbf.name}")
        n_ways = 0
        fp = (osmium.FileProcessor(str(pbf)).with_locations()
              .with_areas(osmium.filter.TagFilter(("leisure", "park")))
              .with_filter(osmium.filter.KeyFilter("highway", "shop", "leisure", "railway")))
        for obj in fp:
            tags = obj.tags
            if obj.is_area():
                if tags.get("leisure") != "park" or tags.get("access") in ("private", "no"):
                    continue
                try:
                    polys = []
                    for outer in obj.outer_rings():
                        ring = [(n.lon, n.lat) for n in outer]
                        holes = [[(n.lon, n.lat) for n in inner] for inner in obj.inner_rings(outer)]
                        polys.append((ring, holes))
                except osmium.InvalidLocationError:
                    continue
                if not polys or not in_area(polys[0][0][0][1], polys[0][0][0][0]):
                    continue
                key = f"{'way' if obj.from_way() else 'relation'}/{obj.orig_id()}"
                parks[key] = (tags.get("name"), polys)
                continue
            if obj.is_node():
                if tags.get("shop") == "supermarket" and in_area(obj.location.lat, obj.location.lon):
                    shops[f"node/{obj.id}"] = (obj.location.lat, obj.location.lon, tags.get("name"), tags.get("brand"))
                continue
            if not obj.is_way():
                continue
            try:
                locs = [(n.ref, n.location.lon, n.location.lat) for n in obj.nodes]
            except osmium.InvalidLocationError:
                continue
            if not locs:
                continue
            mid = locs[len(locs) // 2]
            near = in_area(locs[0][2], locs[0][1]) or in_area(mid[2], mid[1]) or in_area(locs[-1][2], locs[-1][1])
            if not near:
                continue
            if tags.get("shop") == "supermarket":
                shops[f"way/{obj.id}"] = (sum(l[2] for l in locs) / len(locs), sum(l[1] for l in locs) / len(locs),
                                          tags.get("name"), tags.get("brand"))
            hw = tags.get("highway")
            cls = NOISE_ROADS.get(hw)
            if cls is None and tags.get("railway") == "rail" and tags.get("service") not in ("yard", "siding", "spur") \
                    and tags.get("usage") not in ("industrial", "military", "tourism"):
                cls = 4
            if cls is not None and len(locs) >= 2:
                noise.append((np.array([l[1] for l in locs]), np.array([l[2] for l in locs]), cls))
            if hw not in WALK_HIGHWAYS or len(locs) < 2:
                continue
            foot, access = tags.get("foot"), tags.get("access")
            if foot in ("no", "private") or (access in ("no", "private") and foot not in ("yes", "designated", "permissive")):
                continue
            ways_ids.append(np.fromiter((l[0] for l in locs), dtype=np.int64, count=len(locs)))
            ways_lon.append(np.fromiter((l[1] for l in locs), dtype=np.float64, count=len(locs)))
            ways_lat.append(np.fromiter((l[2] for l in locs), dtype=np.float64, count=len(locs)))
            n_ways += 1
        log(f"  {n_ways} walkable ways; {len(shops)} supermarkets, {len(parks)} parks so far")
    return ways_ids, ways_lon, ways_lat, shops, parks, noise


def build_graph(ways_ids, ways_lon, ways_lat):
    ids = np.concatenate(ways_ids)
    lon = np.concatenate(ways_lon)
    lat = np.concatenate(ways_lat)
    lens = np.array([len(w) for w in ways_ids])
    starts = np.cumsum(lens) - lens
    pos = np.arange(len(ids))
    is_last = np.zeros(len(ids), bool)
    is_last[starts + lens - 1] = True
    a = pos[~is_last]
    b = a + 1
    uniq, inv = np.unique(ids, return_inverse=True)
    x_all, y_all = to_rd.transform(lon, lat)
    x = np.zeros(len(uniq)); y = np.zeros(len(uniq))
    x[inv] = x_all; y[inv] = y_all
    u, v = inv[a], inv[b]
    keep = u != v
    u, v = u[keep], v[keep]
    L = np.hypot(x[u] - x[v], y[u] - y[v])
    n = len(uniq)
    G = coo_matrix((np.concatenate([L, L]), (np.concatenate([u, v]), np.concatenate([v, u]))), shape=(n, n)).tocsr()
    log(f"Walking network: {n} nodes, {len(u)} segments")
    return G, x, y, u, v, L


def samples(dist, x, y, u, v, L):
    du, dv = dist[u], dist[v]
    m = np.isfinite(du) | np.isfinite(dv)
    du, dv, uu, vv, LL = du[m], dv[m], u[m], v[m], L[m]
    k = np.maximum(1, (LL // SAMPLE_M).astype(int))
    rep = np.repeat(np.arange(len(LL)), k + 1)
    offsets = np.cumsum(k + 1) - (k + 1)
    t = (np.arange(len(rep)) - offsets[rep]) / k[rep]
    sx = x[uu][rep] + (x[vv][rep] - x[uu][rep]) * t
    sy = y[uu][rep] + (y[vv][rep] - y[uu][rep]) * t
    sd = np.minimum(du[rep] + LL[rep] * t, dv[rep] + LL[rep] * (1 - t))
    return sx, sy, sd


class SampleTree:
    def __init__(self, sx, sy, sd):
        self.sd = np.append(sd, np.inf)
        self.tree = cKDTree(np.column_stack([sx, sy])) if len(sx) else None

    def values(self, centres):
        if self.tree is None:
            return np.full(len(centres), np.inf)
        out = np.empty(len(centres))
        for i in range(0, len(centres), 500_000):  # chunks keep memory in check
            c = centres[i:i + 500_000]
            dd, ii = self.tree.query(c, k=6, distance_upper_bound=SNAP_M)
            vals = self.sd[np.minimum(ii, len(self.sd) - 1)] + np.where(np.isinf(dd), 0, dd)
            out[i:i + 500_000] = vals.min(axis=1)
        return out


def park_items(parks, x, y, ntree):
    """Park list for the map, and the network nodes at each park (inside it or near its edge)."""
    items, nodes = {}, set()
    for key, (name, polys) in parks.items():
        rd_polys = []
        for ring, holes in polys:
            if len(ring) < 4:
                continue
            rx, ry = to_rd.transform([p[0] for p in ring], [p[1] for p in ring])
            hs = []
            for h in holes:
                if len(h) >= 4:
                    hx, hy = to_rd.transform([p[0] for p in h], [p[1] for p in h])
                    hs.append(list(zip(hx, hy)))
            rd_polys.append(shapely.Polygon(list(zip(rx, ry)), hs))
        if not rd_polys:
            continue
        geom = shapely.make_valid(shapely.MultiPolygon(rd_polys) if len(rd_polys) > 1 else rd_polys[0])
        ha = geom.area / 10_000
        if ha < PARK_MIN_HA:
            continue
        minx, miny, maxx, maxy = geom.bounds
        cand = ntree.query_ball_point([(minx + maxx) / 2, (miny + maxy) / 2],
                                      math.hypot(maxx - minx, maxy - miny) / 2 + PARK_EDGE_M)
        if cand:
            cand = np.asarray(cand)
            grown = shapely.buffer(geom, PARK_EDGE_M)
            shapely.prepare(grown)
            hit = cand[shapely.contains_xy(grown, x[cand], y[cand])]
            nodes.update(hit.tolist())
        c = geom.representative_point()
        clon, clat = to_wgs.transform(c.x, c.y)
        simple = shapely.simplify(geom, 8)
        simple = shapely.transform(simple, lambda a: np.column_stack(to_wgs.transform(a[:, 0], a[:, 1])))
        simple = shapely.set_precision(simple, 1e-5)
        items[key] = {"id": key, "name": name, "lat": round(clat, 6), "lon": round(clon, 6), "ha": round(ha, 1),
                      "geometry": shapely.geometry.mapping(simple)}
    return items, nodes


def noise_points(noise):
    xs, ys, cs = [], [], []
    for lon, lat, cls in noise:
        x, y = to_rd.transform(lon, lat)
        x, y = np.asarray(x), np.asarray(y)
        seg = np.hypot(np.diff(x), np.diff(y))
        for i, L in enumerate(seg):
            n = max(1, int(L // NOISE_STEP_M))
            t = np.arange(n) / n
            xs.append(x[i] + (x[i + 1] - x[i]) * t); ys.append(y[i] + (y[i + 1] - y[i]) * t)
            cs.append(np.full(n, cls, np.int8))
    if not xs:
        return np.zeros(0, np.float32), np.zeros(0, np.float32), np.zeros(0, np.int8)
    return np.concatenate(xs).astype(np.float32), np.concatenate(ys).astype(np.float32), np.concatenate(cs)


def build():
    data = read_json(STATIONS_JSON)
    if not data:
        raise SystemExit("Run python -m pipeline.stations first")
    stations = data["stations"]
    download(missing_only=True)
    dest = geocode(DESTINATION_QUERY)
    ways_ids, ways_lon, ways_lat, shops, parks, noise = read_osm(stations + [dest])
    G, x, y, u, v, L = build_graph(ways_ids, ways_lon, ways_lat)
    del ways_ids, ways_lon, ways_lat
    ntree = cKDTree(np.column_stack([x, y]))

    # noise sources
    nx_, ny_, nc_ = noise_points(noise)
    np.savez_compressed(NOISE_NPZ, x=nx_, y=ny_, cls=nc_)
    log(f"{len(nx_)} noise sample points (busy roads and railways)")

    # supermarkets
    shop_items, shop_nodes = {}, set()
    for oid, (lat, lon, name, brand) in shops.items():
        sx, sy = to_rd.transform(lon, lat)
        d, i = ntree.query([sx, sy])
        if d <= 150:
            shop_nodes.add(int(i))
        shop_items[oid] = {"id": oid, "name": name, "brand": brand, "lat": round(lat, 6), "lon": round(lon, 6)}
    log(f"{len(shop_items)} supermarkets; computing walking distances to them")
    sm_tree = SampleTree(*samples(dijkstra(G, directed=False, indices=sorted(shop_nodes), limit=SM_M, min_only=True),
                                  x, y, u, v, L))

    # parks
    park_list, park_nodes = park_items(parks, x, y, ntree)
    log(f"{len(park_list)} parks of {PARK_MIN_HA} ha or more; computing walking distances to them")
    pk_tree = SampleTree(*samples(dijkstra(G, directed=False, indices=sorted(park_nodes), limit=PK_M, min_only=True),
                                  x, y, u, v, L)) if park_nodes else SampleTree([], [], [])

    # destination
    dx, dy = to_rd.transform(dest["lon"], dest["lat"])
    dnear = ntree.query_ball_point([dx, dy], 60) or [int(ntree.query([dx, dy])[1])]
    dist_dest = dijkstra(G, directed=False, indices=dnear, limit=DEST_M, min_only=True)
    dd_tree = SampleTree(*samples(dist_dest, x, y, u, v, L))

    # walk to each station
    per_station = {}
    for n, st in enumerate(stations, 1):
        sx, sy = to_rd.transform(st["lon"], st["lat"])
        near = ntree.query_ball_point([sx, sy], STATION_SNAP_M) or [int(ntree.query([sx, sy])[1])]
        if st.get("arrival"):
            dm = float(np.min(dist_dest[near]))
            st["_destM"] = dm if np.isfinite(dm) else None
        dist_st = dijkstra(G, directed=False, indices=near, limit=ST_M, min_only=True)
        st_tree = SampleTree(*samples(dist_st, x, y, u, v, L))
        x0, x1 = int((sx - ST_M) // CELL_M), int((sx + ST_M) // CELL_M) + 1
        y0, y1 = int((sy - ST_M) // CELL_M), int((sy + ST_M) // CELL_M) + 1
        gx, gy = np.meshgrid(np.arange(x0, x1), np.arange(y0, y1))
        gx, gy = gx.ravel(), gy.ravel()
        centres = np.column_stack([(gx + 0.5) * CELL_M, (gy + 0.5) * CELL_M])
        inside = np.hypot(centres[:, 0] - sx, centres[:, 1] - sy) <= ST_M
        gx, gy, centres = gx[inside], gy[inside], centres[inside]
        t = st_tree.values(centres) / WALK_SPEED_M_PER_MIN
        keep = t <= MAX_WALK_STATION_MIN
        per_station[st["code"]] = (gx[keep].astype(np.int64) * 1_000_000 + gy[keep], np.round(t[keep] * 10).astype(np.int16))
        log(f"[{n}/{len(stations)}] {st['name']}: {int(keep.sum())} cells")

    # cells within walking reach of the destination itself (homes there need no train)
    gx, gy = np.meshgrid(np.arange(int((dx - ST_M) // CELL_M), int((dx + ST_M) // CELL_M) + 1),
                         np.arange(int((dy - ST_M) // CELL_M), int((dy + ST_M) // CELL_M) + 1))
    gx, gy = gx.ravel(), gy.ravel()
    c = np.column_stack([(gx + 0.5) * CELL_M, (gy + 0.5) * CELL_M])
    near_dest = dd_tree.values(c) <= ST_M
    dest_keys = gx[near_dest].astype(np.int64) * 1_000_000 + gy[near_dest]
    log(f"{int(near_dest.sum())} cells within {MAX_WALK_STATION_MIN} min walk of the destination")

    ckey = np.unique(np.concatenate([k for k, _ in per_station.values()] + [dest_keys]))
    centres = np.column_stack([(ckey // 1_000_000 + 0.5) * CELL_M, (ckey % 1_000_000 + 0.5) * CELL_M])
    log(f"{len(ckey)} cells in all; supermarket, park and destination distances per cell")
    to10 = lambda m: np.where(np.isfinite(m), np.round(m / WALK_SPEED_M_PER_MIN * 10), 9999).astype(np.int16)
    sm = to10(sm_tree.values(centres))
    pk = to10(pk_tree.values(centres))
    ddm = dd_tree.values(centres)
    dd = np.where(np.isfinite(ddm), np.round(ddm), -1).astype(np.int32)
    stations_arr = {code: (np.searchsorted(ckey, k).astype(np.int32), t) for code, (k, t) in per_station.items()}
    save_grid(ckey, sm, pk, dd, stations_arr)

    def leg(m):
        return None if m is None else {"m": round(m), "walk": round(m / WALK_SPEED_M_PER_MIN),
                                       "bike": round(m / BIKE_SPEED_M_PER_MIN) + BIKE_PARK_MIN}
    dest["arrivals"] = {st["code"]: leg(st.get("_destM")) for st in stations if st.get("arrival")}
    hub = dest["arrivals"].get("IFF:ht") or {}
    dest.update(distFromHubM=hub.get("m"), walkFromHubMin=hub.get("walk"), bikeFromHubMin=hub.get("bike"))
    log(f"Destination {dest['label']}: " + ", ".join(f"{c} {v['m'] if v else '?'} m" for c, v in dest["arrivals"].items()))

    write_json(SUPERMARKETS_JSON, {"items": shop_items})
    write_json(PARKS_JSON, {"items": park_list})
    meta = read_json(META_JSON, {})
    meta.update({"mapBuilt": time.strftime("%Y-%m-%d %H:%M"), "stationsWithGrid": len(per_station),
                 "supermarkets": len(shop_items), "parks": len(park_list), "cells": int(len(ckey)),
                 "mapSource": "Geofabrik OpenStreetMap extracts", "destination": dest})
    write_json(META_JSON, meta)
    log(f"Grid for {len(per_station)} stations, {len(shop_items)} supermarkets, {len(park_list)} parks")


if __name__ == "__main__":
    if "--download" in sys.argv:
        download()
    build()
