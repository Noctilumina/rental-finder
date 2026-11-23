"""Local web server: serves the UI and computes qualifying areas, listings and scores on the fly.

Run:  python server.py        then open http://127.0.0.1:8765
"""
import json
import math
import os
import statistics
import subprocess
import sys
import threading
import time
from functools import lru_cache

import numpy as np
import requests
import shapely
import uvicorn
from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pyproj import Transformer
from scipy.spatial import cKDTree

import home
import scoring
from pipeline.common import (BIKE_PARK_MIN, BIKE_SPEED_M_PER_MIN, CACHE, GRID_NPZ, LISTINGS_JSON, MAX_WALK_PARK_MIN,
                             META_JSON, NOISE_NPZ, PARKS_JSON, ROOT, STATIONS_JSON, SUPERMARKETS_JSON, USER_AGENT,
                             USER_JSON, WALK_SPEED_M_PER_MIN, load_grid, read_json, write_json)

PORT = int(os.environ.get("RF_PORT", 8765))
MIN_RENT = 300
PARK_ANY = MAX_WALK_PARK_MIN  # park slider at its maximum means "no park requirement"
WEB = ROOT / "web"
app = FastAPI(title="Rental finder")
to_rd = Transformer.from_crs(4326, 28992, always_xy=True)
to_wgs = Transformer.from_crs(28992, 4326, always_xy=True)
http = requests.Session()
http.headers["User-Agent"] = USER_AGENT


# ---------- data loading (reloaded when files change) ----------
_cache = {}


def load(path, default=None, loader=None):
    mtime = path.stat().st_mtime if path.exists() else None
    hit = _cache.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    val = (loader() if loader else read_json(path, default)) if mtime else default
    _cache[path] = (mtime, val)
    areas.cache_clear()
    located.cache_clear()
    scored.cache_clear()
    return val


def version():
    return tuple(p.stat().st_mtime if p.exists() else 0
                 for p in (STATIONS_JSON, GRID_NPZ, LISTINGS_JSON, META_JSON, scoring.SETTINGS_JSON))


def stations():
    return load(STATIONS_JSON, {"stations": []})


def bycode():
    return {s["code"]: s for s in stations()["stations"]}


def grid():
    return load(GRID_NPZ, None, load_grid)


def meta():
    """meta.json with your own last-stretch minutes and speeds applied (computed values kept as defaults)."""
    m = load(META_JSON, {})
    dest = m.get("destination")
    if not dest:
        return m
    t = scoring.travel_settings()
    arr = {}
    for code, leg in (dest.get("arrivals") or {}).items():
        if not leg:
            arr[code] = leg
            continue
        sp = t["speeds"]
        computed = {"walk": round(leg["m"] / (sp["walk"] * 1000 / 60)), "bike": round(leg["m"] / (sp["bike"] * 1000 / 60) + sp["bikePark"])}
        own = t["arrivals"].get(code, {})
        arr[code] = {**leg, "default": computed, **computed, **own, "custom": sorted(own)}
    return {**m, "destination": {**dest, "arrivals": arr, "speeds": t["speeds"], "defaultSpeeds": scoring.DEFAULT_SPEEDS}}


def supermarkets():
    return list(load(SUPERMARKETS_JSON, {"items": {}})["items"].values())


def parks():
    return list(load(PARKS_JSON, {"items": {}})["items"].values())


def noise_trees():
    def mk():
        z = np.load(NOISE_NPZ)
        return {c: cKDTree(np.column_stack([z["x"][z["cls"] == c], z["y"][z["cls"] == c]]))
                for c in range(5) if (z["cls"] == c).any()}
    return load(NOISE_NPZ, {}, mk)


def listings_data():
    return load(LISTINGS_JSON, {"items": [], "updated": None, "sources": {}})


def qualifying(train: int, direct: bool):
    out = {}
    for s in stations()["stations"]:
        m = s["direct"] if direct else s["any"]
        if s.get("always") or (m and m["q"] <= train):
            out[s["code"]] = s
    return out


# ---------- door to door ----------
def arrivals():
    return (meta().get("destination") or {}).get("arrivals") or {}


def typical_door(st, dest, last, mode):
    """Typical off-peak minutes from the station platform to the destination (wait + ride + last stretch)."""
    hubs = st.get("hubs") or {}
    if dest == "cs":
        e = hubs.get("IFF:ht")
        return e["typ"][mode] if e and e["typ"][mode] is not None else None
    best = None
    for code, leg in arrivals().items():
        e = hubs.get(code)
        if not e or e["typ"][mode] is None or not leg:
            continue
        t = e["typ"][mode] + leg[last]
        best = t if best is None or t < best else best
    return best


def direct_minutes(dd_m, last):
    """Straight from home to the destination by bike or on foot (route metres), or inf."""
    dd_m = np.asarray(dd_m, dtype=float)
    sp = scoring.travel_settings()["speeds"]
    if last == "walk":
        v = dd_m / (sp["walk"] * 1000 / 60)
    else:
        v = dd_m / (sp["bike"] * 1000 / 60) + sp["bikePark"]
    return np.where(dd_m >= 0, v, np.inf)


# ---------- areas ----------
BANDS = [20, 30, 40, 50, 60]


def to_geojson(geom, cell):
    geom = shapely.simplify(geom, cell * 0.3)
    geom = shapely.transform(geom, lambda c: np.column_stack(to_wgs.transform(c[:, 0], c[:, 1])))
    return shapely.geometry.mapping(shapely.set_precision(geom, 1e-5))


def union_cells(keys, cell):
    ix, iy = keys // 1_000_000, keys % 1_000_000
    return shapely.coverage_union_all(shapely.box(ix * cell, iy * cell, (ix + 1) * cell, (iy + 1) * cell))


def dest_walk_cells(g, destwalk):
    """Cells within `destwalk` minutes' walk of Lucas Gasselstraat 2 (at your walking speed)."""
    if not destwalk:
        return np.zeros(len(g["ckey"]), bool)
    return direct_minutes(g["dd"], "walk") <= destwalk


@lru_cache(maxsize=128)
def areas(walk, shop, park, train, direct, dest, last, heat, destwalk=0, _v=None):
    g = grid()
    if g is None:
        return {"type": "FeatureCollection", "features": []}, 0, {}
    cell, ckey = g["cell"], g["ckey"]
    mode = "direct" if direct else "any"
    sel = np.zeros(len(ckey), bool)
    best = np.full(len(ckey), np.inf, np.float32) if heat else None
    for code, s in qualifying(train, direct).items():
        if code not in g["stations"]:
            continue
        idx, t = g["stations"][code]
        m = t <= walk * 10
        sel[idx[m]] = True
        if heat:
            td = typical_door(s, dest, last, mode)
            if td is not None:
                np.minimum.at(best, idx[m], t[m] / 10 + td)
    near_dest = dest_walk_cells(g, destwalk)
    if heat and near_dest.any():
        np.minimum.at(best, np.nonzero(near_dest)[0], direct_minutes(g["dd"][near_dest], "walk").astype(np.float32))
    ok = g["sm"] <= shop * 10
    if park < PARK_ANY:
        ok &= g["pk"] <= park * 10
    sel = (sel | near_dest) & ok
    near_dest &= ok
    keys = ckey[sel]
    n = int(len(keys))
    feats = []
    if n:
        # very large areas are drawn on a coarser grid (100 m) to stay quick; listings still use the 50 m grid
        f = 2 if n > 150_000 else 1
        b = None
        if heat:
            b = best[sel]
            if dest == "lg":
                b = np.minimum(b, direct_minutes(g["dd"][sel], last))
        ck = (keys // 1_000_000 // f) * 1_000_000 + (keys % 1_000_000) // f
        uk, inv = np.unique(ck, return_inverse=True)
        if heat:
            ub = np.full(len(uk), np.inf, np.float32)
            np.minimum.at(ub, inv, b)
            edges = [-1] + BANDS + [1e9]
            parts = []
            for i in range(len(edges) - 1):
                m = (ub > edges[i]) & (ub <= edges[i + 1])
                if m.any():
                    geom = union_cells(uk[m], cell * f)
                    parts.append(geom)
                    feats.append({"type": "Feature", "properties": {"kind": "band", "band": i, "min": edges[i] if i else None,
                                                                     "max": edges[i + 1] if i < len(BANDS) else None},
                                  "geometry": to_geojson(geom, cell * f)})
            outline = shapely.coverage_union_all(parts) if parts else None
        else:
            outline = union_cells(uk, cell * f)
        if outline is not None:
            feats.insert(0, {"type": "Feature", "properties": {"kind": "outline", "cells": n, "km2": round(n * cell * cell / 1e6, 1)},
                             "geometry": to_geojson(outline, cell * f)})
        if near_dest.any():
            dk = ckey[near_dest]
            dk = np.unique((dk // 1_000_000 // f) * 1_000_000 + (dk % 1_000_000) // f)
            feats.append({"type": "Feature", "properties": {"kind": "destwalk", "minutes": destwalk},
                          "geometry": to_geojson(union_cells(dk, cell * f), cell * f)})
    brands = {}
    shops = supermarkets()
    if shops and n:
        sx, sy = to_rd.transform([s["lon"] for s in shops], [s["lat"] for s in shops])
        sk = np.floor(np.asarray(sx) / cell).astype(np.int64) * 1_000_000 + np.floor(np.asarray(sy) / cell).astype(np.int64)
        for s, inside in zip(shops, np.isin(sk, keys)):
            if inside:
                b_ = brand_of(s)
                brands[b_] = brands.get(b_, 0) + 1
    return {"type": "FeatureCollection", "features": feats}, n, brands


BRANDS = ["Albert Heijn", "Jumbo", "Lidl", "Aldi", "Plus", "Dirk", "Coop", "Spar", "Vomar", "DekaMarkt", "Hoogvliet", "Poiesz", "Boni", "Picnic"]


def brand_of(s):
    text = f"{s.get('brand') or ''} {s.get('name') or ''}".lower()
    for b in BRANDS:
        if b.lower() in text:
            return b
    if text.strip().startswith("ah ") or text.strip() == "ah":
        return "Albert Heijn"
    return "Other"


# ---------- locating points in the grid ----------
NEAR = 2  # homes sit back from the street and listing coordinates are approximate: look this many cells around
_off = np.array([(dx, dy) for dx in range(-NEAR, NEAR + 1) for dy in range(-NEAR, NEAR + 1)])
_extra = np.hypot(_off[:, 0], _off[:, 1]) * 50 / WALK_SPEED_M_PER_MIN  # straight-line walk to that cell (min)


def lookup(lats, lons, train, direct):
    """Per point: walk (min) to each qualifying station (matrix), and supermarket/park walk and destination metres."""
    g = grid()
    cell, ckey = g["cell"], g["ckey"]
    x, y = to_rd.transform(np.asarray(lons, float), np.asarray(lats, float))
    cx, cy = np.floor(np.asarray(x) / cell).astype(np.int64), np.floor(np.asarray(y) / cell).astype(np.int64)
    K = (cx[:, None] + _off[None, :, 0]) * 1_000_000 + (cy[:, None] + _off[None, :, 1])
    pos = np.clip(np.searchsorted(ckey, K), 0, len(ckey) - 1)
    valid = ckey[pos] == K
    E = np.broadcast_to(_extra, K.shape)
    big = np.float32(np.inf)
    sm = np.where(valid, g["sm"][pos] / 10 + E, big)
    pk = np.where(valid, g["pk"][pos] / 10 + E, big)
    sm[sm > 999] = big
    pk[pk > 999] = big
    ddv = g["dd"][pos]
    dd = np.where(valid & (ddv >= 0), ddv + E * WALK_SPEED_M_PER_MIN, big)
    codes = [c for c in qualifying(train, direct) if c in g["stations"]]
    W = np.full((len(K), len(codes)), big, np.float32)
    for j, code in enumerate(codes):
        idx, t = g["stations"][code]
        if not len(idx):
            continue
        p = np.clip(np.searchsorted(idx, pos), 0, len(idx) - 1)
        hit = valid & (idx[p] == pos)
        w = np.where(hit, t[p] / 10 + E, big)
        W[:, j] = w.min(axis=1)
    return codes, W, sm.min(axis=1), pk.min(axis=1), dd.min(axis=1)


@lru_cache(maxsize=64)
def located(walk, shop, park, train, direct, dest, last, destwalk=0, _v=None):
    items = listings_data()["items"]
    pts = [i for i, it in enumerate(items) if it.get("lat") is not None]
    if not pts or grid() is None:
        return {}
    codes, W, sm, pk, dd = lookup([items[i]["lat"] for i in pts], [items[i]["lon"] for i in pts], train, direct)
    by = bycode()
    mode = "direct" if direct else "any"
    tds = np.array([typical_door(by[c], dest, last, mode) if typical_door(by[c], dest, last, mode) is not None else 999 for c in codes])
    out = {}
    ok = (sm <= shop) & ((pk <= park) | (park >= PARK_ANY))
    walk_dest = direct_minutes(dd, "walk")
    for r, i in enumerate(pts):
        if not ok[r]:
            continue
        w = W[r]
        cand = np.nonzero(w <= walk)[0]
        wd = round(float(walk_dest[r])) if np.isfinite(walk_dest[r]) else None
        if not len(cand):
            if destwalk and wd is not None and walk_dest[r] <= destwalk:  # walking distance from the destination: no train needed
                out[items[i]["id"]] = {"station": None, "walk": None, "options": [], "walkDest": wd, "nearDest": True,
                                       "shop": round(float(sm[r])), "park": None if not np.isfinite(pk[r]) else round(float(pk[r])),
                                       "destM": round(float(dd[r]))}
            continue
        order = cand[np.argsort(w[cand] + tds[cand])]
        best = order[0]
        opts = sorted(cand, key=lambda j: w[j])[:4]
        if best not in opts:
            opts = [best] + opts[:3]
        out[items[i]["id"]] = {"station": codes[best], "walk": round(float(w[best])),
                               "options": [[codes[j], round(float(w[j]), 1)] for j in opts],
                               "shop": round(float(sm[r])), "park": None if not np.isfinite(pk[r]) else round(float(pk[r])),
                               "destM": None if not np.isfinite(dd[r]) else round(float(dd[r])),
                               "walkDest": wd, "nearDest": bool(destwalk and wd is not None and walk_dest[r] <= destwalk)}
    return out


# ---------- scores (independent of the sliders: widest areas, to Lucas Gasselstraat by bike) ----------
NOISE_L10 = {0: 80, 1: 75, 2: 71, 3: 67, 4: 70}   # rough level at 10 m, dB Lden (railway incl. its lower annoyance)


def noise_db(lats, lons):
    trees = noise_trees()
    x, y = to_rd.transform(np.asarray(lons, float), np.asarray(lats, float))
    pts = np.column_stack([x, y])
    total = np.full(len(pts), 10 ** 4.5)  # 45 dB background
    nearest = {}
    for c, tree in trees.items():
        d, _ = tree.query(pts, distance_upper_bound=3000)
        d = np.maximum(d, 10)
        L = np.where(np.isfinite(d), NOISE_L10[c] - 15 * np.log10(d / 10), -99)
        total += 10 ** (L / 10)
        nearest[c] = d
    return 10 * np.log10(total), nearest


@lru_cache(maxsize=4)
def scored(_v=None):
    items = listings_data()["items"]
    mine = home.get()
    if mine:  # your current home is scored the same way, for comparison (kept out of medians and percentiles)
        items = items + [home.as_item(mine)]
    pts = [i for i, it in enumerate(items) if it.get("lat") is not None]
    if not pts or grid() is None:
        return {}
    lats = [items[i]["lat"] for i in pts]
    lons = [items[i]["lon"] for i in pts]
    codes, W, sm, pk, dd = lookup(lats, lons, 60, False)
    by = bycode()
    tds = np.array([typical_door(by[c], "lg", "bike", "any") or 999 for c in codes], float)
    door = np.minimum((W + tds[None, :]).min(axis=1) if len(codes) else np.inf, direct_minutes(dd, "bike"))
    db, nearest = noise_db(lats, lons)
    ctx = [{"door": float(door[r]), "shop": float(sm[r]), "park": float(pk[r]), "noiseDb": float(db[r]),
            "motorwayM": float(nearest.get(0, [np.inf] * len(pts))[r]), "railM": float(nearest.get(4, [np.inf] * len(pts))[r])}
           for r in range(len(pts))]
    return scoring.score_all([items[i] for i in pts], ctx)


# ---------- API ----------
@app.get("/api/stations")
def api_stations():
    return {**stations(), "meta": meta()}


@app.get("/api/areas")
def api_areas(walk: int = 15, shop: int = 15, park: int = PARK_ANY, train: int = 30, direct: bool = True,
              dest: str = "lg", last: str = "bike", heat: bool = False, destwalk: int = 0):
    fc, n, brands = areas(walk, shop, park, train, direct, dest, last, heat, destwalk, version())
    return JSONResponse({"areas": fc, "stations": list(qualifying(train, direct)), "cells": n, "shopBrands": brands,
                         "bands": BANDS})


@app.get("/api/supermarkets")
def api_supermarkets():
    return supermarkets()


@app.get("/api/parks")
def api_parks():
    return parks()


def user_state():
    return load(USER_JSON, {})


def public_item(it, loc, sc, user):
    out = {k: v for k, v in it.items() if k not in ("ads",)}
    out.update(loc)
    out["score"] = sc
    out["user"] = user.get(it["id"])
    out["nAds"] = len(it.get("ads") or it.get("sources") or [])
    return out


@app.get("/api/listings")
def api_listings(walk: int = 15, shop: int = 15, park: int = PARK_ANY, train: int = 30, direct: bool = True,
                 dest: str = "lg", last: str = "bike", dups: bool = False, destwalk: int = 0):
    data = listings_data()
    loc = located(walk, shop, park, train, direct, dest, last, destwalk, version())
    sc = scored(version())
    user = user_state()
    out = []
    for item in data["items"]:
        l = loc.get(item["id"])
        if not l:
            continue
        if item.get("rent") is not None and item["rent"] < MIN_RENT:  # parking spaces, storage, placeholders
            continue
        base = public_item(item, l, sc.get(item["id"]), user)
        ads = item.get("ads") or []
        if dups and len(ads) > 1:
            for n, ad in enumerate(ads):
                out.append({**base, **{k: v for k, v in ad.items() if v is not None},
                            "id": f"{item['id']}#{n + 1}", "homeId": item["id"], "dupOf": item["id"] if n else None,
                            "sources": [s for s in item.get("sources", []) if s.get("url") == ad.get("url")] or item.get("sources")})
        else:
            out.append(base)
    return {"items": out, "updated": data.get("updated"), "sources": data.get("sources", {}),
            "lastRefresh": data.get("lastRefresh"), "total": len(data["items"]), "scoreInfo": scoring.info()}


@app.put("/api/travel")
def api_put_travel(body: dict = Body(...)):
    try:
        return scoring.save_travel(body or {})
    except (TypeError, ValueError, AttributeError):
        raise HTTPException(400, "minutes and speeds must be numbers")


def home_view():
    """Your current home with the same travel facts and score a listing gets (any station within an hour's walk)."""
    h = home.get()
    if not h:
        return {"home": None}
    out = {"home": h, "score": scored(version()).get(home.HOME_ID)}
    if grid() is not None:
        codes, W, sm, pk, dd = lookup([h["lat"]], [h["lon"]], 60, False)
        w = W[0]
        near = [j for j in np.argsort(w)[:4] if np.isfinite(w[j])]
        wd = direct_minutes(dd, "walk")[0]
        out["travel"] = {"options": [[codes[j], round(float(w[j]), 1)] for j in near],
                         "station": codes[near[0]] if near else None, "walk": round(float(w[near[0]])) if near else None,
                         "shop": None if not np.isfinite(sm[0]) else round(float(sm[0])),
                         "park": None if not np.isfinite(pk[0]) else round(float(pk[0])),
                         "destM": None if not np.isfinite(dd[0]) else round(float(dd[0])),
                         "walkDest": None if not np.isfinite(wd) else round(float(wd))}
    return out


@app.get("/api/home")
def api_home():
    return home_view()


@app.put("/api/home")
def api_put_home(body: dict = Body(...)):
    try:
        home.save(body or {})
    except home.HomeError as e:
        raise HTTPException(400, str(e))
    return home_view()


@app.delete("/api/home")
def api_delete_home():
    home.clear()
    return {"home": None}


@app.get("/api/weights")
def api_weights():
    return {"info": scoring.info(), "defaults": scoring.DEFAULT_WEIGHTS}


@app.put("/api/weights")
def api_put_weights(body: dict = Body(...)):
    w = body.get("weights") or {}
    if not isinstance(w, dict):
        raise HTTPException(400, "weights must be an object")
    try:
        saved = scoring.save_weights(w)
    except (TypeError, ValueError):
        raise HTTPException(400, "weights must be numbers")
    return {"weights": saved}


@app.put("/api/user/{hid}")
def api_user(hid: str, patch: dict = Body(...)):
    state = dict(user_state())
    cur = {**state.get(hid, {}), **{k: v for k, v in patch.items() if k in ("star", "status", "note", "hidden")}}
    cur["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    if not any(cur.get(k) for k in ("star", "status", "note", "hidden")):
        state.pop(hid, None)
    else:
        state[hid] = cur
    write_json(USER_JSON, state)
    return cur


# ---------- what's here? (map hover) ----------
PLACE_CACHE = CACHE / "places.json"
_places = read_json(PLACE_CACHE, {})
_place_lock = threading.Lock()


def place_name(lat, lon):
    key = f"{lat:.3f},{lon:.3f}"
    if key in _places:
        return _places[key]
    try:
        r = http.get("https://api.pdok.nl/bzk/locatieserver/search/v3_1/reverse",
                     params={"lat": lat, "lon": lon, "rows": 1, "type": "adres", "distance": 400,
                             "fl": "buurtnaam,wijknaam,woonplaatsnaam,gemeentenaam,straatnaam"}, timeout=10)
        docs = r.json()["response"]["docs"]
        if not docs:
            r = http.get("https://api.pdok.nl/bzk/locatieserver/search/v3_1/reverse",
                         params={"lat": lat, "lon": lon, "rows": 1, "type": "woonplaats", "fl": "woonplaatsnaam,gemeentenaam"}, timeout=10)
            docs = r.json()["response"]["docs"]
        d = docs[0] if docs else {}
    except Exception:
        return None
    val = {k: d.get(k) for k in ("straatnaam", "buurtnaam", "wijknaam", "woonplaatsnaam", "gemeentenaam")}
    with _place_lock:
        _places[key] = val
        if len(_places) % 20 == 0:
            write_json(PLACE_CACHE, _places)
    return val


@app.get("/api/point")
def api_point(lat: float, lon: float, walk: int = 15, shop: int = 15, park: int = PARK_ANY, train: int = 30,
              direct: bool = True, dest: str = "lg", last: str = "bike", destwalk: int = 0):
    out = {"place": place_name(lat, lon)}
    if grid() is not None:
        codes, W, sm, pk, dd = lookup([lat], [lon], train, direct)
        by = bycode()
        mode = "direct" if direct else "any"
        opts = []
        for j, c in enumerate(codes):
            if np.isfinite(W[0, j]):
                td = typical_door(by[c], dest, last, mode)
                opts.append({"code": c, "name": by[c]["name"], "walk": round(float(W[0, j])),
                             "door": None if td is None else round(float(W[0, j]) + td)})
        opts.sort(key=lambda o: o["walk"])
        direct_door = float(direct_minutes(dd, last)[0]) if dest == "lg" else math.inf
        walk_dest = float(direct_minutes(dd, "walk")[0])
        near_dest = bool(destwalk and walk_dest <= destwalk)
        if dest == "lg":
            direct_door = min(direct_door, walk_dest)
        doors = [o["door"] for o in opts if o["door"] is not None and o["walk"] <= walk] + [direct_door]
        out.update({"stations": opts[:3], "shop": None if not np.isfinite(sm[0]) else round(float(sm[0])),
                    "park": None if not np.isfinite(pk[0]) else round(float(pk[0])),
                    "direct": None if not np.isfinite(direct_door) else round(direct_door),
                    "door": None if not np.isfinite(min(doors)) else round(min(doors)),
                    "walkDest": None if not np.isfinite(walk_dest) else round(walk_dest), "nearDest": near_dest,
                    "inside": bool((near_dest or any(o["walk"] <= walk for o in opts)) and sm[0] <= shop
                                   and (park >= PARK_ANY or pk[0] <= park))})
    return out


# ---------- municipalities ----------
@app.get("/api/municipalities")
def api_municipalities():
    items = [it for it in listings_data()["items"] if it.get("status") != "removed" and not it.get("missing")
             and it.get("gemeente") and (it.get("rent") or 0) >= MIN_RENT]
    sc = scored(version())
    groups = {}
    for it in items:
        groups.setdefault(it["gemeente"], []).append(it)
    med = lambda v: round(statistics.median(v)) if v else None
    nb = scoring.neighbourhoods()
    out = []
    for g, its in groups.items():
        def summary(lst):
            rents = [i["rent"] for i in lst if i.get("rent")]
            ppm = [i["rent"] / i["m2"] for i in lst if i.get("rent") and i.get("m2")]
            m2 = [i["m2"] for i in lst if i.get("m2")]
            scores = [sc[i["id"]]["total"] for i in lst if sc.get(i["id"]) and sc[i["id"]].get("total") is not None]
            return {"n": len(lst), "rent": med(rents), "ppm2": round(statistics.median(ppm), 1) if ppm else None,
                    "m2": med(m2), "score": round(statistics.median(scores), 1) if scores else None,
                    "labelled": sum(1 for i in lst if i.get("energyLabel"))}
        buurten = {}
        for it in its:
            buurten.setdefault(it.get("buurtCode") or "?", []).append(it)
        code = its[0].get("gemeenteCode")
        out.append({"name": g, "code": code, **summary(its), "stats": nb.get(code),
                    "buurten": sorted([{"code": c, "name": l[0].get("buurt") or "Unknown", "wijk": l[0].get("wijk"),
                                        **summary(l), "stats": nb.get(c)} for c, l in buurten.items()],
                                      key=lambda b: -b["n"])})
    out.sort(key=lambda m: -m["n"])
    return {"items": out, "sources": scoring.NB_SOURCES}


# ---------- background jobs (refresh buttons) ----------
# A job runs as its own process (jobs.py) writing to data/job.log and data/job.json, so it keeps going when the
# server restarts; any server reads its state back from those files.
import jobs  # noqa: E402

job_lock = threading.Lock()


def pid_alive(pid):
    try:
        import psutil
        p = psutil.Process(int(pid))
        return p.status() != psutil.STATUS_ZOMBIE
    except ImportError:
        if os.name == "nt":
            return True  # can't tell without psutil; assume it still runs
        try:
            os.kill(int(pid), 0)
            return True
        except OSError:
            return False
    except Exception:
        return False


def current_job():
    j = read_json(jobs.JOB_JSON, {}) or {}
    if not j.get("name"):
        return {"name": None, "started": None, "finished": None, "code": None, "log": []}
    if j.get("finished") is None and not pid_alive(j.get("pid", 0)):
        j.update(finished=time.strftime("%Y-%m-%d %H:%M:%S"), code=None, stopped=True)  # killed before it could report
        write_json(jobs.JOB_JSON, j)
    try:
        lines = jobs.JOB_LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-300:]
    except OSError:
        lines = []
    return {**j, "log": lines}


def run_job(name):
    log = open(jobs.JOB_LOG, "w", encoding="utf-8")
    if os.name == "nt":  # own process group, not tied to the server's console window
        kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS}
    else:
        kw = {"start_new_session": True}
    proc = subprocess.Popen([sys.executable, "jobs.py", name], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, env={**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"},
                            **kw)
    log.close()
    write_json(jobs.JOB_JSON, {"name": name, "pid": proc.pid, "started": time.strftime("%Y-%m-%d %H:%M:%S"),
                               "finished": None, "code": None})
    threading.Thread(target=proc.wait, daemon=True).start()  # reap it (no zombie) while this server runs


@app.post("/api/energy-key")
def api_energy_key(body: dict = Body(...)):
    """Saves the EP-Online API key typed on the Data screen to ep-online-key.txt (local file only)."""
    from pipeline.common import EP_KEY_FILE
    k = str(body.get("key") or "").strip()
    if not k or len(k) > 200 or any(c.isspace() for c in k):
        raise HTTPException(400, "That doesn't look like an API key")
    # on Linux/macOS only you can read the file (created that way, so it is never readable by others)
    fd = os.open(EP_KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(k)
    if os.name != "nt":
        os.chmod(EP_KEY_FILE, 0o600)  # also for a key file that already existed
    return {"saved": True}


REFRESH_LOCK = ROOT / "data" / "refresh.lock"
REFRESH_LOG = ROOT / "data" / "refresh.log"


def external_refresh():
    """A listing refresh started outside the app (command line): (started, log lines) or None."""
    cur = read_json(jobs.JOB_JSON, {}) or {}
    if not REFRESH_LOCK.exists() or (cur.get("name") == "listings" and cur.get("finished") is None):
        return None
    try:
        pid, started = REFRESH_LOCK.read_text(encoding="utf-8").split()
        import psutil  # optional
        alive = psutil.pid_exists(int(pid))
    except ImportError:
        alive = True
    except Exception:
        return None
    if not alive:
        return None
    lines = REFRESH_LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-300:] if REFRESH_LOG.exists() else []
    return started, lines


@app.post("/api/refresh/{name}")
def api_refresh(name: str):
    if name not in jobs.JOBS:
        raise HTTPException(404)
    with job_lock:
        cur = current_job()
        if cur["name"] and cur["finished"] is None:
            raise HTTPException(409, f"{cur['name']} refresh is already running")
        if external_refresh():
            raise HTTPException(409, "A listing refresh is already running (started from the command line)")
        run_job(name)
    return {"started": name}


@app.get("/api/status")
def api_status():
    lst = listings_data()
    st = stations()
    from listings import energy
    ext = external_refresh()
    j = current_job()
    if ext:
        j = {"name": "listings", "started": ext[0].replace("T", " "), "finished": None, "code": None, "log": ext[1], "external": True}
    return {"job": j,
            "meta": meta(),
            "timetable": {"generated": st.get("generated"), "dates": st.get("dates"),
                          "stations": len(st.get("stations", []))},
            "listings": {"updated": lst.get("updated"), "sources": lst.get("sources", {}),
                         "total": len(lst.get("items", [])), "energy": lst.get("energy")},
            "energyKey": energy.has_key()}


@app.middleware("http")
async def no_cache(request, call_next):
    resp = await call_next(request)
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


app.mount("/", StaticFiles(directory=WEB), name="web")

if __name__ == "__main__":
    print(f"Rental finder running at http://127.0.0.1:{PORT}")
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
