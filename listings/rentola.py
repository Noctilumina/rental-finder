"""Rentola (rentola.nl): a rental aggregator that re-lists homes from Pararius, Funda, Kamernet and others.

Its city pages (/huren?location=<Town>&page=N) answer plain requests and carry every result as JSON-LD
(schema.org RealEstateListing) with street, postcode, exact coordinates, rent, floor area, bedrooms and the date
it was listed. The listing name adds furnishing and availability ("... Unfurnished Available From 2027-01-01").
Approach follows danieltyukov/nl-property-finder (adapters/rentola.ts). Plain, slow requests, no browser.
"""
import json
import re
import time

import requests

from pipeline.common import USER_AGENT

SITE = "Rentola"
BASE = "https://rentola.nl"
NEEDS_BROWSER = False
READ_DETAILS = True    # listing pages add the description (energy label, deposit, demands), photos, rooms
DETAIL_MAX_AGE_H = 24 * 30
DELAY_S = 1.2
MAX_PAGES = 30

_http = requests.Session()
_http.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.5"})
_last = [0.0]


def _get(url, params):
    wait = DELAY_S - (time.time() - _last[0])
    if wait > 0:
        time.sleep(wait)
    _last[0] = time.time()
    return _http.get(url, params=params, timeout=30)


def slug(town):
    return town.replace("'", "").replace("’", "").strip()


def _items(html):
    for blk in re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S):
        try:
            d = json.loads(blk)
        except ValueError:
            continue
        for x in d if isinstance(d, list) else [d]:
            main = x.get("mainEntity") if isinstance(x, dict) else None
            lst = (main or x).get("itemListElement") if isinstance(main or x, dict) else None
            if isinstance(lst, list):
                return lst
    return []


def _parse(el):
    it = el.get("item") or {}
    offer = it.get("offers") or {}
    home = offer.get("itemOffered") or {}
    addr = (home.get("address") or {}).get("streetAddress") or ""
    m = re.match(r"\s*(.*?),\s*(\d{4}\s?[A-Z]{2})\s+(.*?)(?:,\s*Netherlands)?\s*$", addr)
    street, postcode, city = (m.group(1), m.group(2).replace(" ", ""), m.group(3)) if m else (addr.split(",")[0], None, (home.get("address") or {}).get("addressLocality"))
    num = re.match(r"(.*?)\s+(\d+\s?[A-Za-z]?(?:[-\s]\w+)?)$", street or "")
    name = it.get("name") or ""
    low = name.lower()
    interior = ("furnished" if re.search(r"\bfurnished\b", low) and "unfurnished" not in low
                else "upholstered" if "upholstered" in low or "semi-furnished" in low
                else "unfurnished" if "unfurnished" in low else None)
    avail = re.search(r"available from (\d{4}-\d{2}-\d{2})", low)
    geo = home.get("geo") or {}
    kind = {"Apartment": "Appartement", "House": "Huis", "SingleFamilyResidence": "Huis", "Room": "Kamer"}.get(home.get("@type"), home.get("@type"))
    if re.match(r"room\b|shared", low):
        kind = "Kamer"
    elif low.startswith("studio"):
        kind = "Studio"
    url = it.get("url") or el.get("url")
    return {
        "site": SITE, "url": url, "srcId": (url or "").rstrip("/").split("-")[-1],
        "type": kind, "street": num.group(1) if num else street, "houseNumber": num.group(2) if num else None,
        "postcode": postcode, "city": city,
        "lat": geo.get("latitude"), "lon": geo.get("longitude"),
        "rent": int(offer["price"]) if isinstance(offer.get("price"), (int, float)) else None,
        "m2": int((home.get("floorSize") or {}).get("value")) if (home.get("floorSize") or {}).get("value") else None,
        "rooms": (home.get("numberOfBedrooms") or {}).get("value"),  # Rentola puts the room count here (Pararius "kamers")
        "interior": interior,
        "availableFrom": avail.group(1) if avail else None,
        "listedAt": (offer.get("validFrom") or "")[:10] or None,
        "status": "available" if "InStock" in (offer.get("availability") or "InStock") else "under_offer",
        "photo": it.get("image") if isinstance(it.get("image"), str) else None,
        "title": name,
    }


def search_town(br, town, log=print):
    loc = slug(town)
    out, seen = [], set()
    for page in range(1, MAX_PAGES + 1):
        r = _get(f"{BASE}/huren", {"location": loc, **({"page": page} if page > 1 else {})})
        if r.status_code == 404:
            return None if page == 1 else out
        r.raise_for_status()
        items = _items(r.text)
        new = 0
        for el in items:
            rec = _parse(el)
            if rec["url"] and rec["srcId"] not in seen and rec["type"] != "Kamer" and (rec["rent"] or 0) >= 300:
                seen.add(rec["srcId"])
                out.append(rec)
                new += 1
        if not items or new == 0 or f"page={page + 1}" not in r.text:
            break
    return out


def details(br, url):
    """Description, photos, rooms and pets from the listing page; {"gone": True} when the ad was removed."""
    r = _get(url, None)
    if r.status_code in (404, 410):
        return {"gone": True}
    r.raise_for_status()
    for blk in re.findall(r'<script type="application/ld\+json">(.*?)</script>', r.text, re.S):
        try:
            d = json.loads(blk)
        except ValueError:
            continue
        if not (isinstance(d, dict) and d.get("@type") == "RealEstateListing"):
            continue
        offer = d.get("offers") or {}
        home = offer.get("itemOffered") or {}
        imgs = d.get("image") or []
        imgs = [imgs] if isinstance(imgs, str) else imgs
        val = lambda k: (home.get(k) or {}).get("value") if isinstance(home.get(k), dict) else home.get(k)
        desc = (d.get("description") or "").strip()
        desc = re.sub(r"[ \t]*\n[ \t]*", "\n", desc)
        desc = re.sub(r"\n{3,}", "\n\n", desc).removeprefix("Beschrijving").strip()
        return {
            "description": desc or None, "photos": imgs[:12], "photo": imgs[0] if imgs else None,
            "rooms": val("numberOfRooms"), "bedrooms": val("numberOfBedrooms"),
            "pets": home.get("petsAllowed") if isinstance(home.get("petsAllowed"), bool) else None,
            "status": "available" if "InStock" in (offer.get("availability") or "InStock") else "under_offer",
            "rent": int(offer["price"]) if isinstance(offer.get("price"), (int, float)) else None,
        }
    return {}
