"""Walking times around every candidate station, on a 50 m grid.

Stations close to each other are grouped, and one OpenStreetMap walking network (via OSMnx / Overpass) is
downloaded per group. Along that network this computes:
  - walk time to each station (from any point within MAX_WALK_STATION_MIN),
  - walk time to the nearest supermarket (shop=supermarket),
and stores both per 50 m cell of the Dutch RD grid (EPSG:28992), so the server can draw exact areas for any
slider setting without recomputing routes.

Outputs: data/grid.json, data/supermarkets.json, data/meta.json (destination and its walk from Den Bosch CS).

Run:  python -m pipeline.walk [--only CODE,CODE] [--force]
"""
import math
import os
import sys
import time
from pathlib import Path

import networkx as nx
import numpy as np
import osmnx as ox
import requests
from pyproj import Transformer
from scipy.spatial import cKDTree
from shapely.geometry import LineString, Point
from shapely.ops import unary_union

from .common import (CACHE, CELL_M, DATA, DESTINATION_QUERY, GRID_JSON, MAX_WALK_SHOP_MIN,
                     MAX_WALK_STATION_MIN, META_JSON, STATIONS_JSON, USER_AGENT, WALK_SPEED_M_PER_MIN,
                     read_json, write_json)

SUPERMARKETS_JSON = DATA / "supermarkets.json"
PER_STATION = CACHE / "walk"
PER_STATION.mkdir(parents=True, exist_ok=True)

ST_M = MAX_WALK_STATION_MIN * WALK_SPEED_M_PER_MIN        # reach around a station
SM_M = MAX_WALK_SHOP_MIN * WALK_SPEED_M_PER_MIN           # reach around a supermarket
GRAPH_DIST = int(ST_M + 15 * WALK_SPEED_M_PER_MIN) + 100  # exact supermarket times up to 15 min everywhere
CLUSTER_M = 4000    # stations closer than this share one download
SAMPLE_M = 20       # sample spacing along streets
SNAP_M = 75         # a cell counts when a street sample lies within this distance of its centre (homes sit back from the street)
STATION_SNAP_M = 80 # network nodes this close to the station point count as "at the station"

# OSMnx cache file names are long hashes; keep them on a short path (Windows path limit).
OSM_CACHE = Path(os.environ.get("RF_OSM_CACHE", Path.home() / ".cache" / "rental-finder" / "osmnx"))
OSM_CACHE.mkdir(parents=True, exist_ok=True)
ox.settings.use_cache = True
ox.settings.cache_folder = str(OSM_CACHE)
ox.settings.log_console = False
ox.settings.requests_timeout = 300
ox.settings.http_user_agent = USER_AGENT
if os.environ.get("RF_OVERPASS"):  # e.g. https://overpass.private.coffee/api/interpreter when the main server is busy
    ox.settings.overpass_url = os.environ["RF_OVERPASS"].removesuffix("/interpreter")

to_rd = Transformer.from_crs(4326, 28992, always_xy=True)
to_wgs = Transformer.from_crs(28992, 4326, always_xy=True)


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def cache_file(code):
    return PER_STATION / f"{code.replace(':', '_')}.json"


def clusters(stations):
    pts = [to_rd.transform(s["lon"], s["lat"]) for s in stations]
    parent = list(range(len(stations)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(stations)):
        for j in range(i + 1, len(stations)):
            if math.dist(pts[i], pts[j]) < CLUSTER_M:
                parent[find(i)] = find(j)
    groups = {}
    for i, s in enumerate(stations):
        groups.setdefault(find(i), []).append((s, pts[i]))
    return list(groups.values())


def edge_samples(G, dist: dict):
    """Points every SAMPLE_M along every edge with interpolated network distance to the sources."""
    xs, ys, ds = [], [], []
    for u, v, data in G.edges(data=True):
        du, dv = dist.get(u, math.inf), dist.get(v, math.inf)
        if du == math.inf and dv == math.inf:
            continue
        geom = data.get("geometry")
        if geom is None:
            geom = LineString([(G.nodes[u]["x"], G.nodes[u]["y"]), (G.nodes[v]["x"], G.nodes[v]["y"])])
        L = geom.length
        n = max(1, int(L // SAMPLE_M))
        for i in range(n + 1):
            s = L * i / n
            p = geom.interpolate(s)
            xs.append(p.x); ys.append(p.y)
            ds.append(min(du + s, dv + (L - s)))
    return np.array(xs), np.array(ys), np.array(ds)


def cell_values(samples, centres):
    xs, ys, ds = samples
    if len(xs) == 0:
        return np.full(len(centres), np.inf)
    tree = cKDTree(np.column_stack([xs, ys]))
    dd, ii = tree.query(centres, k=6, distance_upper_bound=SNAP_M)
    ds_ext = np.append(ds, np.inf)
    vals = ds_ext[np.minimum(ii, len(ds))] + np.where(np.isinf(dd), 0, dd)
    return vals.min(axis=1)


def process_cluster(group, supermarkets: dict):
    names = ", ".join(s["name"] for s, _ in group)
    area_rd = unary_union([Point(p).buffer(GRAPH_DIST) for _, p in group])
    area_wgs = ox.projection.project_geometry(area_rd, crs="EPSG:28992", to_latlong=True)[0]
    log(f"Downloading walking network for {names}")
    G = ox.graph_from_polygon(area_wgs, network_type="walk", retain_all=True)
    G = ox.project_graph(G, to_crs="EPSG:28992")
    Gu = ox.convert.to_undirected(G)
    node_ids = np.array(list(Gu.nodes))
    node_xy = np.array([(Gu.nodes[n]["x"], Gu.nodes[n]["y"]) for n in node_ids])
    ntree = cKDTree(node_xy)

    log(f"Downloading supermarkets for {names}")
    try:
        feats = ox.features_from_polygon(area_wgs, tags={"shop": "supermarket"})
    except Exception:  # osmnx raises when there are none
        feats = None
    shop_nodes = set()
    if feats is not None and len(feats):
        feats = feats.to_crs(28992)
        for idx, row in feats.iterrows():
            c = row.geometry if row.geometry.geom_type == "Point" else row.geometry.centroid
            d, i = ntree.query([c.x, c.y])
            if d <= 120:
                shop_nodes.add(node_ids[i])
            oid = f"{idx[0]}/{idx[1]}" if isinstance(idx, tuple) else str(idx)
            if oid not in supermarkets:
                wl, wa = to_wgs.transform(c.x, c.y)
                name, brand = row.get("name"), row.get("brand")
                supermarkets[oid] = {"id": oid, "name": name if isinstance(name, str) else None,
                                     "brand": brand if isinstance(brand, str) else None,
                                     "lat": round(wa, 6), "lon": round(wl, 6)}
    dist_sm = (nx.multi_source_dijkstra_path_length(Gu, shop_nodes, cutoff=SM_M, weight="length")
               if shop_nodes else {})
    sm_samples = edge_samples(Gu, dist_sm)

    results = {}
    for st, (sx, sy) in group:
        near = ntree.query_ball_point([sx, sy], STATION_SNAP_M) or [int(ntree.query([sx, sy])[1])]
        dist_st = nx.multi_source_dijkstra_path_length(Gu, {node_ids[i] for i in near}, cutoff=ST_M,
                                                       weight="length")
        st_samples = edge_samples(Gu, dist_st)
        x0, x1 = int((sx - ST_M) // CELL_M), int((sx + ST_M) // CELL_M) + 1
        y0, y1 = int((sy - ST_M) // CELL_M), int((sy + ST_M) // CELL_M) + 1
        gx, gy = np.meshgrid(np.arange(x0, x1), np.arange(y0, y1))
        gx, gy = gx.ravel(), gy.ravel()
        centres = np.column_stack([(gx + 0.5) * CELL_M, (gy + 0.5) * CELL_M])
        inside = np.hypot(centres[:, 0] - sx, centres[:, 1] - sy) <= ST_M
        gx, gy, centres = gx[inside], gy[inside], centres[inside]
        t_st = cell_values(st_samples, centres) / WALK_SPEED_M_PER_MIN
        t_sm = cell_values(sm_samples, centres) / WALK_SPEED_M_PER_MIN
        keep = t_st <= MAX_WALK_STATION_MIN
        results[st["code"]] = {
            "ix": gx[keep].tolist(), "iy": gy[keep].tolist(),
            "st": np.round(t_st[keep] * 10).astype(int).tolist(),
            "sm": np.where(np.isinf(t_sm[keep]), 9999, np.round(t_sm[keep] * 10)).astype(int).tolist(),
        }
        log(f"  {st['name']}: {int(keep.sum())} cells")
        if st.get("hub"):
            results["_hub"] = st_samples
    log(f"  {len(shop_nodes)} supermarkets in this area")
    return results


def geocode(q: str):
    r = requests.get("https://api.pdok.nl/bzk/locatieserver/search/v3_1/free",
                     params={"q": q, "rows": 1, "fq": "type:adres"}, timeout=30,
                     headers={"User-Agent": USER_AGENT})
    r.raise_for_status()
    doc = r.json()["response"]["docs"][0]
    lon, lat = map(float, doc["centroide_ll"].removeprefix("POINT(").removesuffix(")").split())
    return {"label": doc["weergavenaam"], "lat": lat, "lon": lon}


def build(only=None, force=False, part=None):
    """part=(i, n): process only every n-th download area starting at i (for parallel workers)."""
    data = read_json(STATIONS_JSON)
    if not data:
        raise SystemExit("Run python -m pipeline.stations first")
    stations = data["stations"]
    if only:
        stations = [s for s in stations if s["code"] in only]
    supermarkets = read_json(SUPERMARKETS_JSON, {"items": {}})["items"]
    meta = read_json(META_JSON, {})
    groups = clusters(stations)
    log(f"{len(stations)} stations in {len(groups)} download areas")
    for n, group in enumerate(groups, 1):
        if part and (n - 1) % part[1] != part[0]:
            continue
        todo = [s for s, _ in group if force or not cache_file(s["code"]).exists()
                or (s.get("hub") and "destination" not in meta)]
        if not todo:
            log(f"[{n}/{len(groups)}] {', '.join(s['name'] for s, _ in group)}: cached")
            continue
        log(f"[{n}/{len(groups)}]")
        for attempt in range(3):
            try:
                res = process_cluster(group, supermarkets)
                break
            except Exception as e:  # Overpass hiccups
                log(f"  failed ({type(e).__name__}: {e}); retrying in 30 s")
                time.sleep(30)
        else:
            log("  skipped after 3 failures")
            continue
        hub_samples = res.pop("_hub", None)
        for code, cells in res.items():
            write_json(cache_file(code), cells)
        write_json(PER_STATION / f"shops_{n}.json", {"items": supermarkets})
        if hub_samples is not None:
            dest = geocode(DESTINATION_QUERY)
            dx, dy = to_rd.transform(dest["lon"], dest["lat"])
            d = cell_values(hub_samples, np.array([[dx, dy]]))[0]
            dest["walkFromHubMin"] = round(float(d) / WALK_SPEED_M_PER_MIN) if np.isfinite(d) else None
            meta["destination"] = dest
            write_json(META_JSON, meta)
            log(f"  Destination {dest['label']}: {dest['walkFromHubMin']} min walk from Den Bosch CS")

    if part:
        log("Worker done; run without --part to merge everything into grid.json")
        return
    for f in PER_STATION.glob("shops_*.json"):  # supermarkets found by (parallel) workers
        supermarkets.update(read_json(f, {"items": {}})["items"])
    write_json(SUPERMARKETS_JSON, {"items": supermarkets})
    meta = read_json(META_JSON, {}) | {k: v for k, v in meta.items() if k == "destination"}
    grid = {"cell": CELL_M, "crs": "EPSG:28992", "stations": {}}
    for st in data["stations"]:
        if cache_file(st["code"]).exists():
            grid["stations"][st["code"]] = read_json(cache_file(st["code"]))
    write_json(GRID_JSON, grid)
    meta["mapBuilt"] = time.strftime("%Y-%m-%d %H:%M")
    meta["stationsWithGrid"] = len(grid["stations"])
    meta["supermarkets"] = len(supermarkets)
    write_json(META_JSON, meta)
    log(f"Grid for {len(grid['stations'])} stations, {len(supermarkets)} supermarkets")


if __name__ == "__main__":
    only = None
    if "--only" in sys.argv:
        only = set(sys.argv[sys.argv.index("--only") + 1].split(","))
    part = None
    if "--part" in sys.argv:
        i, n = sys.argv[sys.argv.index("--part") + 1].split("/")
        part = (int(i), int(n))
    build(only=only, force="--force" in sys.argv, part=part)
