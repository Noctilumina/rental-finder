"""Funda (rentals), read through the shared browser.

Funda's search and listing pages embed all listing data as a Nuxt 3 payload (<script id="__NUXT_DATA__">).
Approach and field names follow danieltyukov/nl-property-finder (packages/sources/src/adapters/funda.ts).
"""
import json
import re
import urllib.parse

SITE = "Funda"
BASE = "https://www.funda.nl"
MAX_PAGES = 30
WRAPPERS = {"Reactive", "ShallowReactive", "Ref", "ShallowRef", "EmptyRef", "EmptyShallowRef"}
NOT_A_HOME = {"parking", "storage", "storage_space", "land", "berth", "pitch", "substructure"}
OPEN = {"none", "available", "beschikbaar"}


def revive(payload):
    """Rebuild a Nuxt 3 devalue payload (flat array with index references)."""
    if not isinstance(payload, list):
        return None
    memo = {}

    def at(x):
        return hydrate(x) if isinstance(x, int) and not isinstance(x, bool) else None

    def hydrate(i):
        if i in memo:
            return memo[i]
        if not (0 <= i < len(payload)):
            return None
        v = payload[i]
        if isinstance(v, list):
            if v and isinstance(v[0], str):
                tag = v[0]
                if tag in WRAPPERS:
                    memo[i] = None
                    r = at(v[1]) if len(v) > 1 else None
                    memo[i] = r
                    return r
                if tag == "Date":
                    memo[i] = v[1]
                    return v[1]
                if tag == "Set":
                    out = []
                    memo[i] = out
                    out.extend(at(x) for x in v[1:])
                    return out
                if tag in ("Map", "null"):
                    out = {}
                    memo[i] = out
                    for k in range(1, len(v) - 1, 2):
                        key = at(v[k]) if tag == "Map" else v[k]
                        out[str(key)] = at(v[k + 1])
                    return out
                memo[i] = None
                return None
            out = []
            memo[i] = out
            out.extend(at(x) for x in v)
            return out
        if isinstance(v, dict):
            out = {}
            memo[i] = out
            for k, x in v.items():
                out[k] = at(x)
            return out
        memo[i] = v
        return v

    import sys
    old = sys.getrecursionlimit()
    sys.setrecursionlimit(max(old, 20000))
    try:
        return hydrate(0)
    finally:
        sys.setrecursionlimit(old)


def nuxt(html):
    m = re.search(r'<script[^>]*id="__NUXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return None
    try:
        return revive(json.loads(m.group(1)))
    except Exception:
        return None


def find_listings(root):
    try:
        direct = root["pinia"]["search"]["listings"]
        if isinstance(direct, list):
            return [x for x in direct if isinstance(x, dict)]
    except (KeyError, TypeError):
        pass
    seen = set()

    def walk(o, d):
        if d > 14 or id(o) in seen:
            return None
        seen.add(id(o))
        if isinstance(o, list):
            if o and all(isinstance(x, dict) and "object_detail_page_relative_url" in x for x in o):
                return o
            for x in o:
                r = walk(x, d + 1)
                if r:
                    return r
        elif isinstance(o, dict):
            for x in o.values():
                r = walk(x, d + 1)
                if r:
                    return r
        return None

    return walk(root, 0) or []


def _first(v):
    return v[0] if isinstance(v, list) and v else v


def _num(v):
    v = _first(v)
    try:
        return int(float(v)) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def slug(town):
    n = town.lower().strip().replace("'", "").replace("’", "")
    return re.sub(r"[^a-z0-9]+", "-", n).strip("-")


def search_town(br, town, log=print):
    area = slug(town)
    q = lambda v: urllib.parse.quote(json.dumps(v))
    out = []
    for page in range(1, MAX_PAGES + 1):
        url = (f"{BASE}/zoeken/huur?selected_area={q([area])}&object_type={q(['house', 'apartment'])}"
               f"&sort={q('date_down')}" + (f"&search_result={page}" if page > 1 else ""))
        status, h = br.get(url, ready="__NUXT_DATA__")
        root = nuxt(h)
        if root is None:
            if page == 1:
                return None
            break
        items = find_listings(root)
        if not items:
            break
        new = 0
        for l in items:
            rel = l.get("object_detail_page_relative_url")
            if not rel or (str(l.get("object_type") or "") in NOT_A_HOME):
                continue
            a = l.get("address") or {}
            price = l.get("price") or {}
            monthly = not price.get("rent_price_condition") or price.get("rent_price_condition") == "per_month"
            num, suf = a.get("house_number"), a.get("house_number_suffix")
            status_ = str(l.get("status") or "none").lower()
            photos = l.get("photo_image_id") or []
            rec = {
                "site": SITE, "url": BASE + rel, "srcId": str(l.get("id") or rel),
                "type": {"apartment": "Appartement", "house": "Huis"}.get(str(l.get("object_type")), l.get("object_type")),
                "street": a.get("street_name"), "houseNumber": f"{num or ''}{(' ' + suf) if suf else ''}".strip() or None,
                "postcode": (a.get("postal_code") or "").replace(" ", "") or None, "city": a.get("city"),
                "district": a.get("neighbourhood"),
                "rent": _num(price.get("rent_price")) if monthly else None,
                "m2": _num(l.get("floor_area")), "rooms": _num(l.get("number_of_rooms")),
                "bedrooms": _num(l.get("number_of_bedrooms")),
                "energyLabel": (str(l.get("energy_label")).upper() if l.get("energy_label") not in (None, "unknown") else None),
                "listedAt": (str(l.get("publish_date"))[:10] if l.get("publish_date") else None),
                "status": "available" if status_ in OPEN else "under_offer",
                "photo": (f"https://cloud.funda.nl/{photos[0].lstrip('/')}?options=width=720" if photos and isinstance(photos[0], str) and not photos[0].startswith("http") else (photos[0] if photos else None)),
            }
            if rec["srcId"] not in {o["srcId"] for o in out}:
                out.append(rec)
                new += 1
        if new == 0 or len(items) < 15:
            break
    return out


def details(br, url):
    status, h = br.get(url, ready="__NUXT_DATA__")
    if status in (404, 410):
        return {"gone": True}
    root = nuxt(h) or {}
    data = root.get("data") if isinstance(root, dict) else None
    d = None
    if isinstance(data, dict):
        key = next((k for k in data if k.startswith("cachedListingData")), None)
        d = data.get(key) if key else None
    if not isinstance(d, dict):
        return {}
    feats = {}

    def collect(lst):
        for f in lst or []:
            if isinstance(f, dict):
                label = f.get("Label") or f.get("Title")
                if label and f.get("Value") and str(label).lower() not in feats:
                    feats[str(label).lower()] = str(f.get("Value"))
                collect(f.get("KenmerkenList"))

    collect(d.get("features"))
    coords = d.get("coordinates") or {}
    rent_txt = feats.get("huurprijs", "")
    incl = True if re.search(r"inclusief servicekosten|servicekosten (inbegrepen|inclusief)|incl\.? ?servicekosten", rent_txt, re.I) else \
        False if re.search(r"exclusief servicekosten|excl\.? ?servicekosten", rent_txt, re.I) else None
    svc = re.search(r"servicekosten[^€\d]{0,20}€\s*([\d.,]+)", rent_txt, re.I)
    dep = re.search(r"([\d.]+)", feats.get("waarborgsom", ""))
    avail = feats.get("aanvaarding") or feats.get("beschikbaar per") or ""
    m = re.search(r"(\d{1,2})[- ](\d{1,2}|\w+)[- ](\d{4})", avail)
    interior_txt = " ".join(v for k, v in feats.items() if re.search(r"interieur|specificaties|inrichting|oplevering", k)).lower()
    desc = (d.get("description") or {}).get("content") if isinstance(d.get("description"), dict) else None
    return {
        "lat": coords.get("lat"), "lon": coords.get("lng"),
        "rentIncl": incl, "serviceCosts": int(svc.group(1).replace(".", "").split(",")[0]) if svc else None,
        "deposit": int(dep.group(1).replace(".", "")) if dep else None,
        "availableFrom": "immediately" if re.search(r"direct|in overleg", avail, re.I) and not m else (avail or None),
        "interior": "furnished" if "gemeubileerd" in interior_txt else "upholstered" if "gestoffeerd" in interior_txt
                    else "bare" if "kaal" in interior_txt else None,
        "energyLabel": (re.match(r"([A-G]\+*)", feats.get("energielabel", "")) or [None, None])[1],
        "description": (desc or "")[:4000] or None,
        "status": "let" if d.get("isSoldOrRented") else None,
        "listedAt": str(d.get("publicationDate"))[:10] if d.get("publicationDate") else None,
    }
