"""Walking times from any place you click, over the same street network and at the same walking speed as the
rest of the app, plus the walking routes to stations, supermarkets, parks and Lucas Gasselstraat 2 nearby.

Kept light:
- the network is loaded only when you first click a place, in one compact copy (about 250 MB for the whole area),
  and dropped again after 10 minutes without a click;
- each calculation stops at the furthest walk it needs (30 minutes for the colours);
- answers for the same spot are remembered.
"""
import threading
import time
from collections import OrderedDict

import numpy as np
import shapely
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra
from scipy.spatial import cKDTree

from pipeline.common import WALKGRAPH_NPZ

SNAP_M = 75            # a map cell takes the time of a street within this distance (same as the grid)
SAMPLE_M = 20
START_SNAP_M = 60      # streets this close to the clicked place count as its doorstep
STATION_SNAP_M = 80
SHOP_SNAP_M = 75
PARK_EDGE_M = 25
ISO_MIN = 30           # colours go out to this many minutes
ISO_STEP = 5
IDLE_UNLOAD_S = 600
BUCKET_M = 100         # spatial lookup: nodes sorted by 100 m square

_lock = threading.Lock()
_g = {"mtime": None}
_cache = OrderedDict()
_last_use = [0.0]


def available():
    return WALKGRAPH_NPZ.exists()


def _bucket(x, y):
    return (np.floor(np.asarray(x) / BUCKET_M).astype(np.int64) * 100_000) + np.floor(np.asarray(y) / BUCKET_M).astype(np.int64)


def _unload_when_idle():
    while True:
        time.sleep(60)
        with _lock:
            if _g.get("G") is not None and time.time() - _last_use[0] > IDLE_UNLOAD_S:
                _g.clear()
                _g["mtime"] = None
                _cache.clear()


threading.Thread(target=_unload_when_idle, daemon=True).start()


def graph():
    """The street network (loaded on first use, reloaded after a map rebuild), or None if not built yet."""
    if not WALKGRAPH_NPZ.exists():
        return None
    mtime = WALKGRAPH_NPZ.stat().st_mtime
    _last_use[0] = time.time()
    with _lock:
        if _g.get("mtime") != mtime:
            _g.clear()
            t0 = time.time()
            z = np.load(WALKGRAPH_NPZ)
            x, y = z["x"].astype(np.float32), z["y"].astype(np.float32)
            u, v, L = z["u"], z["v"], z["L"].astype(np.float64)
            n = len(x)
            G = csr_matrix((np.concatenate([L, L]), (np.concatenate([u, v]), np.concatenate([v, u]))), shape=(n, n))
            del u, v, L, z
            key = _bucket(x, y)
            order = np.argsort(key, kind="stable").astype(np.int32)
            _g.update(mtime=mtime, x=x, y=y, G=G, skey=key[order], order=order, loadS=round(time.time() - t0, 1))
            del key
            _cache.clear()
    return _g


def _near(g, px, py, r):
    """Network nodes within r metres of (px, py), using the 100 m squares; widens the search if there are none."""
    for rr in (r, 300, 1000):
        k = int(np.ceil(rr / BUCKET_M))
        bx, by = int(np.floor(px / BUCKET_M)), int(np.floor(py / BUCKET_M))
        idx = []
        for dx in range(-k, k + 1):
            lo_key = (bx + dx) * 100_000 + by - k
            hi_key = (bx + dx) * 100_000 + by + k
            a, b = np.searchsorted(g["skey"], [lo_key, hi_key + 1])
            if b > a:
                idx.append(g["order"][a:b])
        if idx:
            idx = np.concatenate(idx)
            d = np.hypot(g["x"][idx] - px, g["y"][idx] - py)
            hit = idx[d <= rr]
            if len(hit):
                return hit.tolist()
    return []


def _path(pred, node, g, to_wgs, start_xy):
    """Route from the start to `node` as [lat, lon] points (simplified to a few metres)."""
    seq = []
    while node >= 0 and len(seq) < 20000:
        seq.append(node)
        node = pred[node]
    seq.reverse()
    xs, ys = g["x"][seq].astype(float), g["y"][seq].astype(float)
    line = shapely.simplify(shapely.linestrings(np.column_stack([np.r_[start_xy[0], xs], np.r_[start_xy[1], ys]])), 4)
    c = shapely.get_coordinates(line)
    lon, lat = to_wgs.transform(c[:, 0], c[:, 1])
    return [[round(a, 6), round(b, 6)] for a, b in zip(lat, lon)]


def walk_from(px, py, speed_m_min, cell, places, to_wgs, union_cells, to_geojson, limits, iso=True):
    """Bands of walking time around (px, py) in RD metres, and routes to the `places` within their limits.

    places: list of {kind, name, x, y, ...} candidates; limits: {kind: minutes}.
    """
    key = (round(px / 10), round(py / 10), round(speed_m_min, 2), iso, tuple(sorted(limits.items())), len(places))
    if key in _cache:
        _last_use[0] = time.time()
        _cache.move_to_end(key)
        return _cache[key]
    g = graph()
    if g is None:
        return None
    t0 = time.time()
    max_min = max([ISO_MIN if iso else 0] + list(limits.values()))
    max_m = max_min * speed_m_min
    src = _near(g, px, py, START_SNAP_M)
    if not src:
        return {"bands": None, "places": [], "minutes": ISO_MIN, "step": ISO_STEP, "ms": 0}
    s0 = float(np.min(np.hypot(g["x"][src] - px, g["y"][src] - py)))  # from the door to the street
    dist, pred, _ = dijkstra(g["G"], directed=False, indices=src, limit=max_m, min_only=True, return_predecessors=True)
    dist += s0
    reached = np.nonzero(np.isfinite(dist))[0]
    out = {"bands": None, "places": [], "minutes": ISO_MIN, "step": ISO_STEP}

    if iso and len(reached):
        # street segments touching a reached point, sampled every ~20 m
        G = g["G"]
        cnt = np.diff(G.indptr)[reached]
        uu = np.repeat(reached, cnt)
        vv = np.concatenate([G.indices[G.indptr[r]:G.indptr[r + 1]] for r in reached]) if len(reached) else np.array([], int)
        LL = np.concatenate([G.data[G.indptr[r]:G.indptr[r + 1]] for r in reached]) if len(reached) else np.array([])
        du, dv = dist[uu], dist[vv]
        k = np.maximum(1, (LL // SAMPLE_M).astype(int))
        rep = np.repeat(np.arange(len(LL)), k + 1)
        t = (np.arange(len(rep)) - (np.cumsum(k + 1) - (k + 1))[rep]) / k[rep]
        x, y = g["x"], g["y"]
        sx = x[uu][rep] + (x[vv][rep] - x[uu][rep]) * t
        sy = y[uu][rep] + (y[vv][rep] - y[uu][rep]) * t
        sd = np.minimum(du[rep] + LL[rep] * t, dv[rep] + LL[rep] * (1 - t))
        ok = np.isfinite(sd)
        tree = cKDTree(np.column_stack([sx[ok], sy[ok]]))
        sd = np.append(sd[ok], np.inf)
        r = ISO_MIN * speed_m_min + SNAP_M
        ix = np.arange(int((px - r) // cell), int((px + r) // cell) + 1)
        iy = np.arange(int((py - r) // cell), int((py + r) // cell) + 1)
        gx, gy = np.meshgrid(ix, iy)
        gx, gy = gx.ravel(), gy.ravel()
        cen = np.column_stack([(gx + 0.5) * cell, (gy + 0.5) * cell])
        dd, ii = tree.query(cen, k=4, distance_upper_bound=SNAP_M)
        val = (sd[np.minimum(ii, len(sd) - 1)] + np.where(np.isinf(dd), 0, dd)).min(axis=1) / speed_m_min
        keys = gx.astype(np.int64) * 1_000_000 + gy
        feats = []
        for b in range(ISO_MIN // ISO_STEP):
            lo, hi = b * ISO_STEP, (b + 1) * ISO_STEP
            sel = (val > lo) & (val <= hi) if b else (val <= hi)
            if sel.any():
                feats.append({"type": "Feature", "properties": {"kind": "walkband", "band": b, "max": hi},
                              "geometry": to_geojson(union_cells(keys[sel], cell), cell)})
        out["bands"] = {"type": "FeatureCollection", "features": feats}

    # nearby places within their limits, with the walking route
    best = {}
    for p in places:
        lim = limits.get(p["kind"])
        if not lim:
            continue
        if p["kind"] == "park":
            cand = set()
            for q in p["edge"]:
                cand.update(_near(g, q[0], q[1], PARK_EDGE_M))
        else:
            r = STATION_SNAP_M if p["kind"] == "station" else SHOP_SNAP_M if p["kind"] == "shop" else START_SNAP_M
            cand = _near(g, p["x"], p["y"], r)
        cand = [c for c in cand if np.isfinite(dist[c])]
        if not cand:
            continue
        node = min(cand, key=lambda c: dist[c])
        mins = dist[node] / speed_m_min
        if mins <= lim:
            best.setdefault(p["kind"], []).append((mins, node, p))
    caps = {"station": 4, "shop": 4, "park": 3, "dest": 1}
    for kind, lst in best.items():
        for mins, node, p in sorted(lst, key=lambda t: t[0])[:caps.get(kind, 3)]:
            lon, lat = (p["lon"], p["lat"]) if kind == "park" else to_wgs.transform(p["x"], p["y"])
            out["places"].append({"kind": kind, "name": p["name"], "min": round(float(mins), 1), "lat": lat, "lon": lon,
                                  "id": p.get("id"), "route": _path(pred, node, g, to_wgs, (px, py))})
    out["places"].sort(key=lambda q: q["min"])
    out["ms"] = round((time.time() - t0) * 1000)
    _cache[key] = out
    while len(_cache) > 64:
        _cache.popitem(last=False)
    return out
