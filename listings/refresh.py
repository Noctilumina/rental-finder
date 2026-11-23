"""Refresh rental listings.

Default sources: Rentola (aggregator of Pararius, Funda, Kamernet and more) and Vesteda, read with plain requests.
Funda, Huurwoningen and Pararius adapters (browser-based) remain available with --sites, but those sites
answer automated visits with frequent "verify you are human" checks, so they are off by default.

1. Works out which towns lie around the stations (PDOK reverse geocoding, cached).
2. Searches each site in those towns (one page every few seconds) and keeps ads inside the widest possible
   areas (slider maxima). Reads each ad's own page once (cached) for description, photos and rooms.
3. Merges ads of the same home: same address (postcode + house number) with a similar floor area, or within
   15 m of each other with a similar floor area and rent.
4. Keeps history from earlier refreshes: first/last seen, price changes. Homes no longer found are re-checked:
   "removed" only when the site says the ad is gone or let; otherwise "not found in the last scan".
5. Adds municipality/neighbourhood, description facts and energy labels (listings/enrich.py).

Run:  python -m listings.refresh [--towns Vught,Boxtel] [--sites Funda,Pararius]
"""
import contextlib
import datetime as dt
import math
import re
import sys
import time

import numpy as np
import requests
from pyproj import Transformer
from scipy.spatial import cKDTree

from pipeline.common import progress
from pipeline.common import (CACHE, LISTINGS_JSON, MAX_WALK_SHOP_MIN, MAX_WALK_STATION_MIN, STATIONS_JSON,
                             USER_AGENT, load_grid, read_json, write_json)

from . import enrich, funda, rentola, vesteda
from .browser import Blocked, Browser
from .pararius_group import huurwoningen, pararius

ADAPTERS = [rentola, vesteda]
OPTIONAL = [funda, huurwoningen, pararius]  # browser-based; use --sites Funda,Huurwoningen,Pararius
TOWNS_JSON = CACHE / "towns2.json"
GEO_JSON = CACHE / "geocode.json"
DETAILS = CACHE / "details"
DETAILS.mkdir(parents=True, exist_ok=True)
DETAIL_MAX_AGE_H = 24
PDOK = "https://api.pdok.nl/bzk/locatieserver/search/v3_1"
SAME_PLACE_M = 15
KEEP_REMOVED_DAYS = 60

to_rd = Transformer.from_crs(4326, 28992, always_xy=True)
http = requests.Session()
http.headers["User-Agent"] = USER_AGENT


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def towns_for(stations):
    """Towns within walking reach of each station: a ring of points out to MAX_WALK_STATION_MIN."""
    cache = read_json(TOWNS_JSON, {})
    reach_km = MAX_WALK_STATION_MIN * 5 / 60 * 0.85
    out = set()
    for i, st in enumerate(stations, 1):
        progress("Finding towns near the stations", i, len(stations), span=(0.0, 0.03))
        if st["code"] not in cache:
            names = set()
            pts = [(0, 0)] + [(r * math.cos(a), r * math.sin(a)) for r in (reach_km / 3, 2 * reach_km / 3, reach_km)
                              for a in np.linspace(0, 2 * math.pi, 8, endpoint=False)]
            for dx, dy in pts:
                lat = st["lat"] + dy / 111.0
                lon = st["lon"] + dx / (111.0 * math.cos(math.radians(st["lat"])))
                try:
                    r = http.get(f"{PDOK}/reverse", params={"lat": lat, "lon": lon, "type": "woonplaats", "rows": 1,
                                                             "fl": "woonplaatsnaam,weergavenaam"}, timeout=30)
                    docs = r.json()["response"]["docs"]
                except Exception:
                    docs = []
                if docs:
                    names.add(docs[0].get("woonplaatsnaam") or docs[0]["weergavenaam"].split(",")[0].strip())
                time.sleep(0.1)
            cache[st["code"]] = sorted(names)
            write_json(TOWNS_JSON, cache)
        out.update(cache[st["code"]])
    return sorted(out)


def geocode(query, kind, cache):
    key = f"{kind}:{query}"
    if key in cache:
        return cache[key]
    try:
        r = http.get(f"{PDOK}/free", params={"q": query, "fq": f"type:{kind}", "rows": 1}, timeout=30)
        docs = r.json()["response"]["docs"]
    except Exception:
        return None
    pt = None
    if docs:
        lon, lat = map(float, docs[0]["centroide_ll"].removeprefix("POINT(").removesuffix(")").split())
        pt = [lat, lon]
    cache[key] = pt
    return pt


def locate_item(it, cache):
    """(lat, lon, exact) from the address, else the postcode."""
    if it.get("houseNumber") and it.get("postcode"):
        pt = geocode(f"{it['postcode']} {it['houseNumber']}", "adres", cache)
        if pt:
            return pt[0], pt[1], True
    if it.get("postcode"):
        pt = geocode(it["postcode"], "postcode", cache)
        if pt:
            return pt[0], pt[1], False
    return None


class Envelope:
    """Cells inside the widest possible areas (all stations, longest walk, supermarket within the slider max)."""

    def __init__(self):
        g = load_grid()
        if g is None:
            raise SystemExit("No walking areas yet: build the map data first.")
        self.cell = g["cell"]
        self.keys = g["ckey"][g["sm"] <= MAX_WALK_SHOP_MIN * 10]

    def inside(self, lat, lon, slack=1):
        x, y = to_rd.transform(lon, lat)
        cx, cy = int(x // self.cell), int(y // self.cell)
        k = np.array([(cx + dx) * 1_000_000 + cy + dy for dx in range(-slack, slack + 1) for dy in range(-slack, slack + 1)])
        p = np.clip(np.searchsorted(self.keys, k), 0, len(self.keys) - 1)
        return bool((self.keys[p] == k).any())


def detail_cached(ad, br, item, refresh=False):
    path = DETAILS / f"{ad.SITE}_{re.sub(r'[^A-Za-z0-9]+', '_', item['srcId'])[:80]}.json"
    d = read_json(path)
    max_age = getattr(ad, "DETAIL_MAX_AGE_H", DETAIL_MAX_AGE_H)
    if d and not refresh and time.time() - d.get("_fetched", 0) < max_age * 3600:
        return d
    d = ad.details(br, item["url"])
    d["_fetched"] = time.time()
    write_json(path, d)
    return d


# ---------- merging ads into homes ----------
def norm_num(hn):
    return re.sub(r"[^0-9A-Z]", "", str(hn or "").upper())


def addr_key(ad):
    if ad.get("postcode") and ad.get("houseNumber"):
        return f"{ad['postcode'].replace(' ', '').upper()}-{norm_num(ad['houseNumber'])}"
    return None


def street_key(ad):
    if not ad.get("street"):
        return None
    return f"{(ad.get('city') or '').lower()}|{re.sub(r'[^a-z]', '', ad['street'].lower())}"


def close(a, b, tol):
    return a is None or b is None or abs(a - b) <= tol


def same_home(a, b, dist_m):
    """Address match with a similar floor area, or same spot (15 m) with similar floor area and rent."""
    m2_ok = close(a.get("m2"), b.get("m2"), 3)
    ka, kb = addr_key(a), addr_key(b)
    if ka and kb:
        return ka == kb and m2_ok
    ra, rb = a.get("rent"), b.get("rent")
    rent_ok = bool(ra and rb and abs(ra - rb) <= 0.05 * max(ra, rb))
    if (ka or kb) and street_key(a) and street_key(a) == street_key(b):
        # one ad without a house number on the same street, close by: same size and rent
        return bool(dist_m is not None and dist_m <= 300 and a.get("m2") and b.get("m2") and abs(a["m2"] - b["m2"]) <= 1
                    and ra and rb and abs(ra - rb) <= 0.02 * max(ra, rb))
    if dist_m is not None and dist_m <= SAME_PLACE_M and a.get("m2") and b.get("m2") and m2_ok:
        ra, rb = a.get("rent"), b.get("rent")
        return bool(ra and rb and abs(ra - rb) <= 0.05 * max(ra, rb))
    return False


def cluster(ads):
    parent = list(range(len(ads)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    xy = np.array([to_rd.transform(a["lon"], a["lat"]) for a in ads]) if ads else np.zeros((0, 2))
    tree = cKDTree(xy) if len(ads) else None
    by_addr = {}
    for i, a in enumerate(ads):
        if addr_key(a):
            by_addr.setdefault(addr_key(a), []).append(i)
    pairs = set()
    by_street = {}
    for i, a in enumerate(ads):
        if street_key(a):
            by_street.setdefault(street_key(a), []).append(i)
    for idx in list(by_addr.values()) + list(by_street.values()):
        pairs.update((i, j) for i in idx for j in idx if i < j)
    if tree is not None:
        pairs.update(tree.query_pairs(SAME_PLACE_M))
    for i, j in sorted(pairs):
        d = float(np.hypot(*(xy[i] - xy[j])))
        if same_home(ads[i], ads[j], d):
            # don't chain two different addresses together through a third ad without one
            gi, gj = find(i), find(j)
            if gi != gj:
                ki = {addr_key(ads[k]) for k in range(len(ads)) if find(k) == gi} - {None}
                kj = {addr_key(ads[k]) for k in range(len(ads)) if find(k) == gj} - {None}
                if ki and kj and not ki & kj:
                    continue
                parent[gi] = gj
    groups = {}
    for i in range(len(ads)):
        groups.setdefault(find(i), []).append(ads[i])
    return list(groups.values())


FIELDS = ["street", "houseNumber", "postcode", "city", "district", "type", "rentIncl", "serviceCosts", "m2", "rooms",
          "bedrooms", "energyLabel", "interior", "availableFrom", "deposit", "pets", "photo", "photos", "description",
          "middleRent"]


def merge(group, prev_by_ad, prev, now):
    group.sort(key=lambda a: (a.get("exact") is not True, a.get("listedAt") or "9999"))
    live = [a for a in group if a.get("status") != "removed"]
    rep = live[0] if live else group[0]
    home = {k: None for k in FIELDS}
    for a in [rep] + group:
        for k in FIELDS:
            if home.get(k) is None and a.get(k) is not None:
                home[k] = a[k]
    rents = [a["rent"] for a in live if a.get("rent")] or [a["rent"] for a in group if a.get("rent")]
    hid = next((prev_by_ad[(a["site"], a["srcId"])] for a in group if (a["site"], a["srcId"]) in prev_by_ad), None)
    if not hid:
        k = addr_key(rep)
        hid = k or f"{rep['site'].lower()}-{rep['srcId']}"
    old = prev.get(hid, {})
    statuses = {a.get("status") or "available" for a in group}
    status = "available" if "available" in statuses else "under_offer" if "under_offer" in statuses else "removed"
    home.update({
        "id": hid, "address": f"{rep.get('street') or ''} {rep.get('houseNumber') or ''}".strip(),
        "lat": rep["lat"], "lon": rep["lon"], "exact": rep.get("exact", False),
        "rent": min(rents) if rents else None, "rentRange": [min(rents), max(rents)] if rents and min(rents) != max(rents) else None,
        "listedAt": min((a["listedAt"] for a in group if a.get("listedAt")), default=None),
        "firstSeen": old.get("firstSeen", now), "lastSeen": now, "missing": False,
        "status": status, "statusSince": old.get("statusSince", now) if old.get("status") == status else now,
        "removedAt": now if status == "removed" and old.get("status") != "removed" else old.get("removedAt") if status == "removed" else None,
        "sources": [{"site": a["site"], "url": a["url"], "srcId": a["srcId"], "rent": a.get("rent"), "listedAt": a.get("listedAt"),
                     "status": a.get("status")} for a in group],
        "ads": [{k: a.get(k) for k in ("site", "url", "srcId", "street", "houseNumber", "postcode", "rent", "m2", "rooms",
                                       "bedrooms", "listedAt", "photo", "title", "status")} for a in group],
    })
    history = list(old.get("priceHistory") or ([[old["firstSeen"][:10], old["rent"]]] if old.get("rent") else []))
    if home["rent"] and (not history or history[-1][1] != home["rent"]):
        history.append([now[:10], home["rent"]])
    home["priceHistory"] = history
    if len(history) > 1:
        home["priceChange"] = history[-1][1] - history[-2][1]
        home["priceChangeAt"] = history[-1][0]
    return home


def recheck(old, adapters_by_site, br, now):
    """A home from an earlier refresh that wasn't found this time: ask each of its ads' sites whether it's gone."""
    home = dict(old)
    gone, still = 0, 0
    for s in old.get("sources", []):
        ad = adapters_by_site.get(s["site"])
        if not ad or not getattr(ad, "READ_DETAILS", False):
            continue
        try:
            d = detail_cached(ad, br, {"srcId": s["srcId"], "url": s["url"]}, refresh=True)
        except Exception:
            continue
        if d.get("gone"):
            gone += 1
        else:
            still += 1
            if d.get("status"):
                home["status"] = d["status"]
    if gone and not still:
        if home.get("status") != "removed":
            home["status"], home["removedAt"], home["statusSince"] = "removed", now, now
        home["missing"] = False
    else:
        home["missing"] = True  # not in the search results, but not confirmed removed either
    return home


def main(only_towns=None, only_sites=None):
    from pipeline import rebuild
    rebuild.ensure(log=log)  # new install: build the map data first instead of stopping
    stations = read_json(STATIONS_JSON, {"stations": []})["stations"]
    if not stations:
        raise SystemExit("No station data yet: build the map data first.")
    env = Envelope()
    towns = only_towns or towns_for(stations)
    town_cache = read_json(TOWNS_JSON, {})
    near_stations = [st for st in stations if not only_towns or set(town_cache.get(st["code"], [])) & set(only_towns)]
    adapters = [a for a in ADAPTERS + OPTIONAL if (a.SITE in only_sites if only_sites else a in ADAPTERS)]
    needs_browser = any(getattr(a, "NEEDS_BROWSER", True) for a in adapters)
    log(f"Searching {len(towns)} towns on {', '.join(a.SITE for a in adapters)}: {', '.join(towns)}")
    if needs_browser:
        log("A browser window opens while the sites are read; you can minimise it.")
    prev_data = read_json(LISTINGS_JSON, {"items": []})
    prev = {it["id"]: it for it in prev_data["items"]}
    legacy_first = {}
    if prev and not any("ads" in it for it in prev.values()):  # file from before history was kept
        legacy_first = {addr_key(it): it.get("firstSeen") for it in prev.values() if addr_key(it)}
        prev = {}
    prev_by_ad = {(s["site"], s["srcId"]): it["id"] for it in prev.values() for s in it.get("sources", []) if s.get("srcId")}
    geo = read_json(GEO_JSON, {})
    sources = {}
    now = dt.datetime.now().isoformat(timespec="seconds")
    ads, seen_ads = [], set()

    # share of the whole refresh per site (the town-by-town search is most of the work)
    weights = [3 if getattr(a, "SEARCH_BY", "town") == "town" else 1 for a in adapters]
    edges = [0.03 + 0.80 * sum(weights[:i]) / sum(weights) for i in range(len(adapters) + 1)]
    with (Browser(log=log) if needs_browser else contextlib.nullcontext()) as br:
        for a_i, ad in enumerate(adapters):
            found, kept, errors = 0, 0, []
            read_details = getattr(ad, "READ_DETAILS", True)
            by_station = getattr(ad, "SEARCH_BY", "town") == "station"
            targets = near_stations if by_station else towns
            for n, target in enumerate(targets, 1):
                town = target["name"] if by_station else target
                progress(f"Searching {ad.SITE}: {town}", n - 1, len(targets), span=(edges[a_i], edges[a_i + 1]))
                try:
                    res = ad.search_station(br, target) if by_station else ad.search_town(br, town)
                except Blocked as e:
                    errors.append(str(e))
                    log(f"{ad.SITE}: {e}; skipping the rest of this site")
                    break
                except Exception as e:
                    errors.append(f"{town}: {type(e).__name__}: {e}")
                    log(f"{ad.SITE} {town}: failed ({type(e).__name__}: {e})")
                    continue
                if res is None:
                    log(f"{ad.SITE} {town}: town not found on this site")
                    continue
                found += len(res)
                new_here = 0
                for it in res:
                    if (ad.SITE, it.get("srcId")) in seen_ads:  # same ad found from two stations or towns
                        continue
                    loc = (it["lat"], it["lon"], True) if it.get("lat") and it.get("lon") else locate_item(it, geo)
                    if not loc or not env.inside(loc[0], loc[1], slack=2 if loc[2] else 6):
                        continue
                    if it.get("status") == "removed" and (ad.SITE, it.get("srcId")) not in prev_by_ad:
                        continue  # let before we ever saw it
                    seen_ads.add((ad.SITE, it.get("srcId")))
                    d = {}
                    try:
                        if read_details and it.get("status") != "removed":
                            d = detail_cached(ad, br, it)
                    except Blocked as e:
                        errors.append(str(e))
                        log(f"{ad.SITE}: listing pages keep showing a check; using search results only for this site")
                        read_details = False
                    except Exception as e:
                        log(f"  details failed for {it['url']}: {type(e).__name__}: {e}")
                    if d.get("gone"):
                        it["status"] = "removed"
                    lat, lon, exact = loc
                    if d.get("lat") and d.get("lon"):
                        lat, lon, exact = d["lat"], d["lon"], True
                    if not env.inside(lat, lon, slack=2 if exact else 4):
                        continue
                    kept += 1
                    new_here += 1
                    full = {**it, **{k: v for k, v in d.items() if v is not None and not k.startswith("_") and k != "gone"},
                            "lat": lat, "lon": lon, "exact": exact, "site": ad.SITE}
                    ads.append(full)
                log(f"{ad.SITE} {town} ({n}/{len(targets)}): {len(res)} listings, {new_here} new inside the areas")
                write_json(GEO_JSON, geo)
            sources[ad.SITE] = {"ok": not errors or found > 0, "found": found, "kept": kept,
                                "error": "; ".join(errors)[:300] or None}
            log(f"{ad.SITE}: {found} listings found, {kept} inside the widest areas")

        progress("Merging duplicate ads", 0, 1, span=(0.83, 0.84))
        homes = [merge(g, prev_by_ad, prev, now) for g in cluster(ads)]
        for h in homes:
            if h["id"] not in prev and legacy_first.get(addr_key(h)):
                h["firstSeen"] = legacy_first[addr_key(h)]
        log(f"{len(ads)} ads merged into {len(homes)} homes")

        # earlier homes that weren't found this time
        found_ids = {h["id"] for h in homes}
        searched_sites = {a.SITE for a in adapters}
        by_site = {a.SITE: a for a in adapters}
        cutoff = (dt.datetime.now() - dt.timedelta(days=KEEP_REMOVED_DAYS)).isoformat()
        rechecked = 0
        for k, (hid, old) in enumerate(prev.items(), 1):
            progress("Re-checking homes not found this time", k, len(prev), span=(0.84, 0.88))
            if hid in found_ids:
                continue
            if old.get("status") == "removed" and (old.get("removedAt") or now) < cutoff:
                continue  # removed long ago: drop
            if only_towns or not {s["site"] for s in old.get("sources", [])} & searched_sites:
                homes.append(old)  # not searched this time: keep as it was
                continue
            if old.get("status") == "removed":
                homes.append(old)
                continue
            homes.append(recheck(old, by_site, br, now))
            rechecked += 1
        log(f"Re-checked {rechecked} earlier homes not in today's results: "
            f"{sum(1 for h in homes if h.get('removedAt') == now)} removed, {sum(1 for h in homes if h.get('missing'))} not found")

    energy = enrich.run([h for h in homes if h.get("status") != "removed"], log=log, span=(0.88, 0.99))
    progress("Saving", 1, 1, span=(0.99, 1.0))
    new = sum(1 for h in homes if h.get("firstSeen") == now)
    write_json(LISTINGS_JSON, {"updated": now, "sources": sources, "items": homes, "energy": energy,
                               "lastRefresh": {"at": now, "new": new, "ads": len(ads)}})
    log(f"Saved {len(homes)} homes ({len(ads)} ads this time, {new} new)")


LOCK = CACHE.parent / "refresh.lock"


if __name__ == "__main__":
    import os
    arg = lambda name: sys.argv[sys.argv.index(name) + 1].split(",") if name in sys.argv else None
    LOCK.write_text(f"{os.getpid()} {dt.datetime.now().isoformat(timespec='seconds')}", encoding="utf-8")
    try:
        main(arg("--towns"), arg("--sites"))
    finally:
        LOCK.unlink(missing_ok=True)
