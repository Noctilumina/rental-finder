"""Vesteda (vesteda.com): a large landlord of mid-rent apartments, searched around each station.

Uses the JSON search behind the site's own search page (POST /api/units/search/facet with coordinates and a
radius), as documented in danieltyukov/nl-property-finder (adapters/vesteda.ts). Plain, slow requests.
"""
import time

import requests

from pipeline.common import USER_AGENT

SITE = "Vesteda"
BASE = "https://www.vesteda.com"
NEEDS_BROWSER = False
READ_DETAILS = False
SEARCH_BY = "station"
RADIUS_KM = 5
DELAY_S = 2.5
STATUS = {1: "available", 5: "available", 3: "under_offer", 4: "under_offer", 2: "removed"}  # 2 = rented

_http = requests.Session()
_http.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
_last = [0.0]


def _units(o):
    if isinstance(o, dict):
        if "priceUnformatted" in o:
            yield o
            return
        for v in o.values():
            yield from _units(v)
    elif isinstance(o, list):
        for v in o:
            yield from _units(v)


def search_station(br, st, log=print):
    wait = DELAY_S - (time.time() - _last[0])
    if wait > 0:
        time.sleep(wait)
    _last[0] = time.time()
    name = st["name"]
    body = {"filters": [], "latitude": st["lat"], "longitude": st["lon"], "place": name,
            "placeObject": {"placeType": "1", "name": name, "latitude": str(st["lat"]), "longitude": str(st["lon"])},
            "placeType": 1, "radius": RADIUS_KM, "sorting": 1, "priceFrom": 500, "priceTo": 9999, "language": "nl"}
    r = _http.post(f"{BASE}/api/units/search/facet", json=body, timeout=30)
    r.raise_for_status()
    out, seen = [], set()
    for u in _units(r.json().get("results", {}).get("objects")):
        if not u.get("id") or not u.get("url") or u.get("entitytypeid") == 2 or u.get("status") not in STATUS:
            continue
        if u["id"] in seen:
            continue
        seen.add(u["id"])
        num = " ".join(x for x in [u.get("houseNumber"), u.get("houseNumberAddition")] if x)
        city = (u.get("city") or "").title().replace("'S-", "'s-")
        out.append({
            "site": SITE, "url": BASE + u["url"], "srcId": str(u["id"]),
            "type": u.get("entitysubtypelabel") or "Appartement",
            "street": u.get("street"), "houseNumber": num or None,
            "postcode": (u.get("postalCode") or "").replace(" ", "") or None, "city": city,
            "district": u.get("district"), "lat": u.get("latitude"), "lon": u.get("longitude"),
            "rent": int(u["priceUnformatted"]) if u.get("priceUnformatted") else None,
            "m2": u.get("size"), "bedrooms": u.get("numberOfBedRooms"),
            "status": STATUS[u["status"]],
            "photo": u.get("imageBig") or u.get("imageSmall"),
            "middleRent": bool(u.get("onlyMiddleRent")),
        })
    return out


def details(br, url):
    return {}
