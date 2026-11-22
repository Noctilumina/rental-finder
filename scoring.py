"""Home scores: an overall 0-10 made of separate 0-10 parts, each with its percentile among all current homes.

Scores are absolute (fixed scales below), so they don't shift when other listings come and go; the percentile
says how a home compares with all homes currently in the data set.
"""
import bisect
import math
import statistics

from pipeline.common import DATA, read_json, write_json

NB_JSON = DATA / "neighbourhoods.json"
_nb = {"mtime": None, "items": {}}


def neighbourhoods():
    m = NB_JSON.stat().st_mtime if NB_JSON.exists() else None
    if m != _nb["mtime"]:
        _nb["items"] = (read_json(NB_JSON, {}) or {}).get("items", {})
        _nb["mtime"] = m
    return _nb["items"]


NB_SOURCES = "Leefbaarometer 3.0 (2024), CBS Kerncijfers wijken en buurten 2024, Politie/CBS registered crime 2025"

INFO = {
    "connectivity": {"label": "Connectivity", "weight": 3,
                     "how": "Typical off-peak door to door to Lucas Gasselstraat 2 (walk, wait, train, bike; or straight there by bike). 25 min or less = 10, 85 min or more = 0."},
    "value": {"label": "Value for money", "weight": 2,
              "how": "Rent per m² against the median of homes in the same municipality. Half the median = 10, the median = 5, 40% above = 0."},
    "neighbourhood": {"label": "Neighbourhood", "weight": 1.5,
                      "how": "Leefbaarometer 2024 liveability score of the neighbourhood (buurt), from 'very poor' (0) to 'excellent' (10)."},
    "safety": {"label": "Safety", "weight": 1,
               "how": "Leefbaarometer 'nuisance and safety' dimension of the neighbourhood (based on crime, nuisance and disturbance figures). National average = 5. Registered crimes per 1000 inhabitants shown for reference."},
    "noise": {"label": "Quiet", "weight": 1.5,
              "how": "Estimated road and rail noise from the distance to motorways, main roads and railways (OpenStreetMap). About 50 dB or less = 10, 70 dB or more = 0. An estimate, not a measurement."},
    "conditions": {"label": "Conditions", "weight": 1.5,
                   "how": "Landlord demands found in the description: temporary contract, no registration, only for certain groups, income demands, guarantor, deposit of 3+ months, fees. 10 = none found."},
    "amenities": {"label": "Shops and green", "weight": 1,
                  "how": "Walk to the nearest supermarket (3 min = 10, 20 min = 0) and park (5 min = 10, 25 min = 0), averaged."},
    "energy": {"label": "Energy label", "weight": 1,
               "how": "A++++ = 10, A = 8, B = 6.5, C = 5, D = 3.5, E = 2, F = 1, G = 0. Official EP-Online label where available, else the listing's."},
    "legal": {"label": "Rent vs. legal maximum", "weight": 0.5,
              "how": "Rough points estimate under the rent points system (WWS 2026) from floor area, energy label and the neighbourhood's average WOZ value. Up to 186 points the rent has a legal maximum: 80% of it or less = 10, 30% above = 0. Above 186 points (free sector) this part doesn't count."},
}

LABEL_SCORE = {"A++++": 10, "A+++": 9.5, "A++": 9, "A+": 8.5, "A": 8, "B": 6.5, "C": 5, "D": 3.5, "E": 2, "F": 1, "G": 0}
# WWS 2026 energy label points: (single-family, apartment)
WWS_LABEL = {"A++++": (62, 58), "A+++": (57, 53), "A++": (52, 48), "A+": (47, 43), "A": (41, 37), "B": (34, 30),
             "C": (22, 15), "D": (14, 11), "E": (-5, -5), "F": (-9, -9), "G": (-15, -15)}
LBM_STEPS = [3.60, 3.674, 3.7484, 3.8208, 3.9552, 3.9955, 4.1126, 4.1918, 4.2689, 4.34]


def clamp(v, lo=0.0, hi=10.0):
    return max(lo, min(hi, v))


def lbm_score(v):
    if v is None:
        return None
    if v <= LBM_STEPS[0]:
        return 0.0
    if v >= LBM_STEPS[-1]:
        return 10.0
    i = bisect.bisect_right(LBM_STEPS, v) - 1
    frac = (v - LBM_STEPS[i]) / (LBM_STEPS[i + 1] - LBM_STEPS[i])
    return (i + frac) / (len(LBM_STEPS) - 1) * 10


def wws(h, nb):
    """Rough points estimate and the legal maximum rent (2026 table)."""
    m2 = h.get("m2")
    if not m2 or m2 < 12:
        return None
    house = (h.get("type") or "").lower() in ("huis", "house", "woonhuis", "eengezinswoning")
    kind = "single-family home" if house else "apartment"
    label = h.get("energyLabel")
    label_known = label in WWS_LABEL
    lp = WWS_LABEL[label if label_known else "D"][0 if house else 1]
    avg_woz = (nb or {}).get("woz")
    woz_known = bool(avg_woz)
    size_factor = min(2.0, max(0.4, m2 / 90)) ** 0.75
    woz = max(85_806, (avg_woz or 300_000) * size_factor)
    wp_abs, wp_m2 = woz / 16_954, (woz / m2) / 268
    wp = wp_abs + wp_m2
    rooms_known = bool(h.get("rooms"))
    rooms = h.get("rooms") or max(1, round(m2 / 25))
    outdoor = 2 if h.get("outdoor") else 0
    # each line: (what, points, how it's worked out, known from the listing?)
    items = [
        ("Living area", m2, f"{m2} m² × 1 point (rooms; storage and other spaces would add 0.75/m², not in the listing)", True),
        ("Energy label", lp, f"label {label if label_known else 'unknown, assumed D'} for {'a' if house else 'an'} {kind}", label_known),
        ("Heating", 2 * rooms, f"{rooms} heated room{'s' if rooms != 1 else ''} × 2" + ("" if rooms_known else " (rooms estimated from the size)"), rooms_known),
        ("Kitchen", 7, "standard kitchen counter of 2 m or more (assumed)", False),
        ("Bathroom and toilet", 7, "shower 4 + toilet 3 (assumed)", False),
        ("Outdoor space", outdoor, ", ".join(h.get("outdoor") or []) + " mentioned (rough)" if outdoor else "none mentioned", bool(outdoor)),
        ("WOZ value", wp, f"€{round(woz, -3):,.0f} / 16,954 = {wp_abs:.1f}, plus €{woz / m2:,.0f} per m² / 268 = {wp_m2:.1f}. "
         f"WOZ estimated from the neighbourhood average (€{avg_woz:,.0f}) adjusted for size" if woz_known else
         f"neighbourhood WOZ unknown; assumed €300,000 adjusted for size (€{round(woz, -3):,.0f})", False),
    ]
    total = sum(p for _, p, _, _ in items)
    capped = None
    if total >= 187 and wp > 0.33 * total:
        wp_c = 0.33 / 0.67 * (total - wp)
        capped = round(wp - wp_c, 1)
        total = max(186.0, total - wp + wp_c)
    pts = round(total)
    base = {"points": pts, "items": [{"what": w, "points": round(p, 1), "how": t, "known": k} for w, p, t, k in items],
            "capped": capped, "labelAssumed": not label_known, "wozKnown": woz_known, "table": "WWS 2026 (1 January 2026)"}
    if pts > 186:
        return {**base, "maxRent": None, "sector": "free",
                "maxHow": "Above 186 points the home is in the free sector: there is no legal maximum rent."}
    if pts < 40:
        mx, how = 250.26, "Under 40 points the minimum of the table applies: €250.26."
    elif pts <= 143:
        mx = 250.26 + (pts - 40) * (932.93 - 250.26) / 103
        how = f"Social range (40–143 points): €250.26 + ({pts} − 40) × €6.63 = €{mx:,.2f}."
    else:
        mx = 932.93 + (pts - 143) * 6.864
        how = f"Mid-rent range (144–186 points): €932.93 + ({pts} − 143) × €6.86 = €{mx:,.2f}."
    rent = h.get("rent")
    return {**base, "maxRent": round(mx), "sector": "social" if pts <= 143 else "mid", "maxHow": how,
            "over": round(rent - mx) if rent else None}


def percentile_fn(values):
    vals = sorted(v for v in values if v is not None)

    def pct(v):
        if v is None or not vals:
            return None
        lo = bisect.bisect_left(vals, v)
        hi = bisect.bisect_right(vals, v)
        return round((lo + (hi - lo) / 2) / len(vals) * 100)
    return pct


SETTINGS_JSON = DATA / "settings.json"
DEFAULT_WEIGHTS = {k: v["weight"] for k, v in INFO.items()}


def weights():
    """Your weights (Data screen), falling back to the defaults for any part not set."""
    saved = (read_json(SETTINGS_JSON, {}) or {}).get("weights") or {}
    return {k: float(saved.get(k, d)) for k, d in DEFAULT_WEIGHTS.items()}


def save_weights(w):
    s = read_json(SETTINGS_JSON, {}) or {}
    s["weights"] = {k: max(0.0, min(10.0, float(w.get(k, d)))) for k, d in DEFAULT_WEIGHTS.items()}
    write_json(SETTINGS_JSON, s)
    return s["weights"]


DEFAULT_SPEEDS = {"walk": 5.0, "bike": 15.0, "bikePark": 3.0}  # km/h, km/h, minutes to pick up/park a bike


def travel_settings():
    """Your own last-stretch minutes per arrival station and speeds for going straight there (Data screen)."""
    s = (read_json(SETTINGS_JSON, {}) or {}).get("travel") or {}
    return {"arrivals": s.get("arrivals") or {}, "speeds": {**DEFAULT_SPEEDS, **(s.get("speeds") or {})}}


def save_travel(t):
    s = read_json(SETTINGS_JSON, {}) or {}
    arr = {}
    for code, v in (t.get("arrivals") or {}).items():
        clean = {m: max(0, min(180, int(round(float(v[m]))))) for m in ("bike", "walk") if v.get(m) not in (None, "")}
        if clean:
            arr[str(code)] = clean
    sp = {}
    for k, lo, hi in (("walk", 2, 10), ("bike", 5, 35), ("bikePark", 0, 15)):
        v = (t.get("speeds") or {}).get(k)
        if v not in (None, ""):
            sp[k] = max(lo, min(hi, float(v)))
    s["travel"] = {"arrivals": arr, "speeds": sp}
    write_json(SETTINGS_JSON, s)
    return travel_settings()


def info():
    w = weights()
    return {k: {**v, "weight": w[k], "default": v["weight"]} for k, v in INFO.items()}


def score_all(items, ctx):
    W = weights()
    nbs = neighbourhoods()
    current = [i for i, h in enumerate(items) if h.get("status") != "removed"]
    # local median rent per m² per municipality
    by_gm = {}
    for i in current:
        h = items[i]
        if h.get("rent") and h.get("m2") and h["m2"] >= 15:
            by_gm.setdefault(h.get("gemeente"), []).append(h["rent"] / h["m2"])
    all_ppm = [v for vs in by_gm.values() for v in vs]
    overall = statistics.median(all_ppm) if all_ppm else None
    local = {g: statistics.median(v) for g, v in by_gm.items() if len(v) >= 8}

    out = {}
    for h, c in zip(items, ctx):
        nb = nbs.get(h.get("buurtCode") or "") or nbs.get(h.get("wijkCode") or "") or {}
        gm = nbs.get(h.get("gemeenteCode") or "") or {}
        parts = {}
        door = c["door"]
        parts["connectivity"] = (clamp((85 - door) / 6) if math.isfinite(door) else None, f"{round(door)} min door to door" if math.isfinite(door) else "no route")
        ppm = h["rent"] / h["m2"] if h.get("rent") and h.get("m2") else None
        ref = local.get(h.get("gemeente")) or overall
        if ppm and ref:
            r = ppm / ref
            where = f"€{ppm:.1f}/m² vs €{ref:.1f} {'in ' + h['gemeente'] if h.get('gemeente') in local else 'overall'}"
            if r < 0.5 and (h.get("m2") or 0) >= 20:  # implausibly cheap: likely an error or a scam, don't reward it
                parts["value"] = (None, where + ": too cheap to be plausible, not counted")
            else:
                parts["value"] = (clamp(5 + (1 - r) * 10 if r <= 1 else 5 - (r - 1) * 12.5), where)
        lb = nb.get("lbm") or gm.get("lbm")
        if lb:
            parts["neighbourhood"] = (lbm_score(lb), f"{nb.get('lbmLabel') or gm.get('lbmLabel')} ({h.get('buurt') or h.get('gemeente') or ''})")
        onv = nb.get("safety") if nb.get("safety") is not None else gm.get("safety")
        if onv is not None:
            crime = nb.get("crimePer1000")
            parts["safety"] = (clamp(5 + onv / 0.2351 * 5), f"{crime:g} crimes per 1000 inhabitants (2025)" if crime else
                               f"{'better' if onv > 0 else 'worse'} than the national average")
        if math.isfinite(c["noiseDb"]):
            parts["noise"] = (clamp((70 - c["noiseDb"]) / 2), f"≈{round(c['noiseDb'])} dB" +
                              (f", motorway {round(c['motorwayM'])} m" if c["motorwayM"] < 1500 else "") +
                              (f", railway {round(c['railM'])} m" if c["railM"] < 500 else ""))
        if h.get("description"):
            pen = sum(f["penalty"] for f in h.get("flags") or [])
            names = [f["label"] for f in h.get("flags") or [] if f["penalty"] > 0]
            parts["conditions"] = (clamp(10 - pen), "; ".join(names) or "no demands found")
        s_sm = clamp((20 - c["shop"]) / 1.7) if math.isfinite(c["shop"]) else 0
        s_pk = clamp((25 - c["park"]) / 2) if math.isfinite(c["park"]) else 0
        parts["amenities"] = ((s_sm + s_pk) / 2, f"supermarket {round(c['shop']) if math.isfinite(c['shop']) else '30+'} min, park {round(c['park']) if math.isfinite(c['park']) else '30+'} min")
        if h.get("energyLabel") in LABEL_SCORE:
            parts["energy"] = (LABEL_SCORE[h["energyLabel"]], f"label {h['energyLabel']}" + (f" ({h['energySource']})" if h.get("energySource") else ""))
        w = wws(h, nb or gm)
        if w and w["maxRent"] and h.get("rent"):
            ratio = h["rent"] / w["maxRent"]
            parts["legal"] = (clamp((1.3 - ratio) / 0.5 * 10), f"≈{w['points']} points, max ≈€{w['maxRent']}" + (" (label assumed D)" if w["labelAssumed"] else ""))
        elif w:
            parts["legal"] = (None, f"≈{w['points']} points: free sector, no legal maximum")
        sus = list(h.get("suspicious") or [])
        if ppm and ref and ppm < 0.5 * ref and (h.get("m2") or 0) >= 20:
            sus.append(f"Rent per m² (€{ppm:.0f}) is less than half the local median (€{ref:.0f})")
        out[h["id"]] = {"parts": {k: {"score": None if v[0] is None else round(v[0], 1), "text": v[1]} for k, v in parts.items()},
                        "ppm2": round(ppm, 1) if ppm else None, "localPpm2": round(ref, 1) if ref else None,
                        "wws": w, "suspicious": sus, "noiseDb": round(c["noiseDb"]) if math.isfinite(c["noiseDb"]) else None,
                        "nb": {k: nb.get(k) for k in ("lbm", "lbmLabel", "crimePer1000", "woz", "income", "inhabitants")} if nb else None}
    for o in out.values():
        num = sum(W[k] * p["score"] for k, p in o["parts"].items() if p["score"] is not None)
        den = sum(W[k] for k, p in o["parts"].items() if p["score"] is not None)
        o["total"] = round(num / den, 1) if den else None
    # percentiles among current (not removed) homes
    cur_ids = {items[i]["id"] for i in current}
    cur = [o for k, o in out.items() if k in cur_ids]
    for key in list(INFO) + ["total"]:
        get = (lambda o: o["total"]) if key == "total" else (lambda o, key=key: (o["parts"].get(key) or {}).get("score"))
        pct = percentile_fn([get(o) for o in cur])
        for o in out.values():
            v = get(o)
            if key == "total":
                o["totalPct"] = pct(v)
            elif key in o["parts"]:
                o["parts"][key]["pct"] = pct(v)
    return out
