"""Adds to each home: municipality and neighbourhood (PDOK), facts and demands from the description, and the
official energy label (EP-Online, when an API key is present).

Runs at the end of every refresh; can also run on its own:  python -m listings.enrich
"""
import re
import time

import requests

from pipeline.common import AREAS_JSON, LISTINGS_JSON, USER_AGENT, read_json, write_json

from . import energy, textflags

PDOK = "https://api.pdok.nl/bzk/locatieserver/search/v3_1"
FL = "buurtcode,buurtnaam,wijkcode,wijknaam,gemeentecode,gemeentenaam,adresseerbaarobject_id,weergavenaam"
_http = requests.Session()
_http.headers["User-Agent"] = USER_AGENT


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def split_number(hn):
    """'2F' -> (2, 'F', None); '12-2' -> (12, None, '2'); '33 A' -> (33, 'A', None)."""
    m = re.match(r"\s*(\d+)\s*[- ]?\s*([A-Za-z])?(?:\s*[- ]\s*|\s*)([A-Za-z0-9]{1,4})?\s*$", str(hn or ""))
    if not m:
        return None, None, None
    return int(m.group(1)), (m.group(2) or "").upper() or None, m.group(3) or None


def area_info(home, cache):
    """PDOK address (exact house number if known, else the nearest address) with its BAG id, buurt and gemeente."""
    num, letter, add = split_number(home.get("houseNumber"))
    key = f"{home.get('postcode')}|{home.get('houseNumber')}" if num and home.get("postcode") else f"{home['lat']:.5f},{home['lon']:.5f}"
    if key in cache:
        return cache[key]
    doc, exact = None, False
    try:
        if num and home.get("postcode"):
            q = f"postcode:{home['postcode']} AND huisnummer:{num}"
            r = _http.get(f"{PDOK}/free", params={"q": q, "fq": "type:adres", "rows": 20, "fl": FL + ",huisletter,huisnummertoevoeging"}, timeout=20)
            docs = r.json()["response"]["docs"]
            want = (letter or "", add or "")
            for d in docs:
                if ((d.get("huisletter") or "").upper(), d.get("huisnummertoevoeging") or "") == want:
                    doc, exact = d, True
                    break
            if not doc and docs:
                doc = next((d for d in docs if (d.get("huisletter") or "").upper() == (letter or "") or (d.get("huisnummertoevoeging") or "") == (letter or "")), None)
                exact = doc is not None
                doc = doc or docs[0]
        if not doc:
            r = _http.get(f"{PDOK}/reverse", params={"lat": home["lat"], "lon": home["lon"], "type": "adres", "rows": 1, "fl": FL}, timeout=20)
            docs = r.json()["response"]["docs"]
            doc = docs[0] if docs else {}
        time.sleep(0.1)
    except Exception as e:
        log(f"PDOK lookup failed for {home.get('address')}: {e}")
        return None
    val = {"gemeente": doc.get("gemeentenaam"), "gemeenteCode": "GM" + doc["gemeentecode"] if doc.get("gemeentecode") else None,
           "buurt": doc.get("buurtnaam"), "buurtCode": doc.get("buurtcode"), "wijk": doc.get("wijknaam"),
           "wijkCode": doc.get("wijkcode"), "bagId": doc.get("adresseerbaarobject_id") if exact else None}
    cache[key] = val
    return val


def run(items, log=log, force_energy=False):
    cache = read_json(AREAS_JSON, {})
    ep = energy.Lookup(log=log, force=force_energy)
    if not ep.key:
        log("No EP-Online API key (ep-online-key.txt): energy labels only from listing texts")
    n_area = n_ep = n_text = 0
    for i, h in enumerate(items, 1):
        if h.get("lat") is None:
            continue
        a = area_info(h, cache)
        if a:
            h.update(a)
            n_area += 1
        t = textflags.read(h.get("description"), h.get("rent"))
        h["flags"], h["suspicious"], h["outdoor"] = t["flags"], t["suspicious"], t.get("outdoor")
        for k in ("depositMonths", "deposit"):
            if t.get(k) is not None and h.get(k) is None:
                h[k] = t[k]
        lab, date = ep.label(h.get("bagId")) if h.get("bagId") else (None, None)
        if lab:
            h["energyLabel"], h["energySource"], h["energyDate"] = lab, "EP-Online", date
            n_ep += 1
        elif t.get("energyLabel"):
            h["energyLabel"], h["energySource"] = t["energyLabel"], "listing text"
            n_text += 1
        elif h.get("energyLabel") and not h.get("energySource"):
            h["energySource"] = "listing"
        if i % 100 == 0:
            write_json(AREAS_JSON, cache)
            log(f"  enriched {i}/{len(items)}")
    write_json(AREAS_JSON, cache)
    ep.save()
    log(f"Areas for {n_area} homes; energy labels: {n_ep} from EP-Online, {n_text} from listing texts")
    return {"epOnline": n_ep, "fromText": n_text, "keyPresent": bool(ep.key), "error": ep.error}


if __name__ == "__main__":
    data = read_json(LISTINGS_JSON, {"items": []})
    import sys
    data["energy"] = run([h for h in data["items"] if h.get("status") != "removed"], force_energy="--force" in sys.argv)
    write_json(LISTINGS_JSON, data)
