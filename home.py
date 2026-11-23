"""Your current home, to compare listings against. Saved only in data/settings.json (never in the repository).

A postcode is enough: it pins the location to one side of a street, which is all travel times, shops, parks,
noise and neighbourhood figures need. A house number adds the exact address (and its official energy label,
when an EP-Online key is set). Rent, floor area, rooms and label are optional and only used for the rent and
value comparisons.
"""
import re

import requests

from pipeline.common import USER_AGENT, read_json, write_json
from scoring import SETTINGS_JSON

PDOK = "https://api.pdok.nl/bzk/locatieserver/search/v3_1/free"
FL = ("centroide_ll,weergavenaam,straatnaam,woonplaatsnaam,postcode,huisnummer,huisletter,huisnummertoevoeging,"
      "adresseerbaarobject_id,buurtcode,buurtnaam,wijkcode,wijknaam,gemeentecode,gemeentenaam")
LABELS = ["A++++", "A+++", "A++", "A+", "A", "B", "C", "D", "E", "F", "G"]
HOME_ID = "__home__"


class HomeError(ValueError):
    pass


def get():
    return (read_json(SETTINGS_JSON, {}) or {}).get("home")


def clear():
    s = read_json(SETTINGS_JSON, {}) or {}
    s.pop("home", None)
    write_json(SETTINGS_JSON, s)


def _num(v, lo, hi, cast=float):
    if v in (None, ""):
        return None
    try:
        v = cast(v)
    except (TypeError, ValueError):
        raise HomeError("rent, floor area and rooms must be numbers")
    if not lo <= v <= hi:
        raise HomeError(f"{v} looks wrong (expected {lo}–{hi})")
    return v


def _point(doc):
    lon, lat = map(float, doc["centroide_ll"].removeprefix("POINT(").removesuffix(")").split())
    return lat, lon


def resolve(postcode, number=None):
    """Location and neighbourhood codes for a postcode (and optionally a house number) via PDOK."""
    pc = re.sub(r"\s+", "", str(postcode or "")).upper()
    if not re.fullmatch(r"[1-9]\d{3}[A-Z]{2}", pc):
        raise HomeError("Enter a Dutch postcode like 5246 CE")
    from listings.enrich import split_number
    num, letter, add = split_number(number) if number not in (None, "") else (None, None, None)
    if number not in (None, "") and num is None:
        raise HomeError("House number not understood (e.g. 12, 12A or 12-2)")
    q = f"postcode:{pc}" + (f" AND huisnummer:{num}" if num else "")
    try:
        r = requests.get(PDOK, params={"q": q, "fq": "type:adres", "rows": 100, "fl": FL}, timeout=20,
                         headers={"User-Agent": USER_AGENT})
        docs = r.json()["response"]["docs"]
    except Exception as e:
        raise HomeError(f"Could not look up the address right now ({e})")
    if not docs:
        raise HomeError(f"No address found for {pc}" + (f" {number}" if number else ""))
    if num:
        want = (letter or "", add or "")
        doc = next((d for d in docs if ((d.get("huisletter") or "").upper(), d.get("huisnummertoevoeging") or "") == want), docs[0])
        lat, lon = _point(doc)
        label, bag = doc["weergavenaam"], doc.get("adresseerbaarobject_id")
    else:  # middle of all addresses with this postcode
        pts = [_point(d) for d in docs]
        lat, lon = sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)
        doc = docs[0]
        label, bag = f"{doc.get('straatnaam')}, {pc[:4]} {pc[4:]} {doc.get('woonplaatsnaam')}", None
    return {"postcode": f"{pc[:4]} {pc[4:]}", "number": str(number).strip() if number not in (None, "") else None,
            "lat": round(lat, 6), "lon": round(lon, 6), "label": label, "bagId": bag,
            "street": doc.get("straatnaam"), "city": doc.get("woonplaatsnaam"),
            "gemeente": doc.get("gemeentenaam"), "gemeenteCode": "GM" + doc["gemeentecode"] if doc.get("gemeentecode") else None,
            "buurt": doc.get("buurtnaam"), "buurtCode": doc.get("buurtcode"), "wijk": doc.get("wijknaam"), "wijkCode": doc.get("wijkcode")}


def save(body):
    old = get() or {}
    place = resolve(body.get("postcode"), body.get("number"))
    label = (body.get("energyLabel") or "").strip().upper() or None
    if label and label not in LABELS:
        raise HomeError("Energy label must be one of " + ", ".join(LABELS))
    home = {**place, "rent": _num(body.get("rent"), 50, 10000), "m2": _num(body.get("m2"), 5, 1000),
            "rooms": _num(body.get("rooms"), 1, 20, int), "energyLabel": label, "energySource": "you" if label else None}
    if not label and place.get("bagId"):  # official label for the exact address, when a key is set
        from listings import energy
        if energy.has_key():
            lab, _ = energy.Lookup(log=lambda *a: None).label(place["bagId"])
            if lab:
                home.update(energyLabel=lab, energySource="EP-Online")
        elif old.get("bagId") == place["bagId"] and old.get("energySource") == "EP-Online":
            home.update(energyLabel=old["energyLabel"], energySource="EP-Online")
    s = read_json(SETTINGS_JSON, {}) or {}
    s["home"] = home
    write_json(SETTINGS_JSON, s)
    return home


def as_item(h):
    """The current home in the shape the scoring expects (kept out of medians and percentiles)."""
    return {"id": HOME_ID, "status": "removed", "address": h["label"], **{k: h.get(k) for k in (
        "lat", "lon", "rent", "m2", "rooms", "energyLabel", "energySource", "gemeente", "gemeenteCode",
        "buurt", "buurtCode", "wijk", "wijkCode")}}
