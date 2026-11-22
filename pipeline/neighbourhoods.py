"""Neighbourhood and municipality figures: Leefbaarometer liveability, CBS key figures and registered crime.

Sources (all open data):
  - Leefbaarometer 3.0, meting 2024 (leefbaarometer.nl, CC0): liveability score per buurt/gemeente and its
    dimensions, incl. "overlast en onveiligheid" (nuisance and safety).
  - CBS Kerncijfers wijken en buurten 2024 (85984NED): inhabitants, average WOZ value, income per inhabitant.
  - Politie / CBS 47018NED: registered crimes per buurt, per year (latest full year), per 1000 inhabitants
    (inhabitants from Kerncijfers of that year, 86165NED for 2025).

Output: data/neighbourhoods.json  {code: {...}} for buurten (BU...), wijken (WK...) and gemeenten (GM...).

Run:  python -m pipeline.neighbourhoods
"""
import csv
import io
import time
import zipfile

import requests

from .common import DATA, RAW, USER_AGENT, write_json

OUT = DATA / "neighbourhoods.json"
LBM_URL = "https://www.leefbaarometer.nl/resources/open-data-leefbaarometer-meting-2024.zip"
LBM_ZIP = RAW / "leefbaarometer-2024.zip"
LBM_YEAR = "2024"
# 2024 class bounds of the total score (official legend): class 1 below the first, class 9 from the last
LBM_BOUNDS = [3.674, 3.7484, 3.8208, 3.9552, 3.9955, 4.1126, 4.1918, 4.2689]
LBM_CLASSES = ["Very poor", "Poor", "Insufficient", "Weak", "Sufficient", "Fairly good", "Good", "Very good", "Excellent"]
KERN = ("85984NED", {"inhabitants": "AantalInwoners_5", "woz": "GemiddeldeWOZWaardeVanWoningen_39",
                     "income": "GemiddeldInkomenPerInwoner_78", "density": "Bevolkingsdichtheid_34"})
CRIME_YEAR = "2025"
CRIME_INHAB = ("86165NED", "AantalInwoners_5")
http = requests.Session()
http.headers["User-Agent"] = USER_AGENT


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def lbm_class(v):
    return 1 + sum(v >= b for b in LBM_BOUNDS)


def odata(url, params):
    """All rows of a CBS OData v3 query (the ODataFeed endpoints page with odata.nextLink)."""
    url = url.replace("/ODataApi/", "/ODataFeed/")
    rows, nxt = [], None
    while True:
        r = http.get(nxt or url, params=None if nxt else {**params, "$format": "json"}, timeout=180)
        r.raise_for_status()
        d = r.json()
        rows += d["value"]
        nxt = d.get("odata.nextLink")
        if not nxt:
            return rows
        if "$format=json" not in nxt:
            nxt += ("&" if "?" in nxt else "?") + "$format=json"


def leefbaarometer(out):
    if not LBM_ZIP.exists():
        log("Downloading Leefbaarometer open data (about 500 MB, once)")
        with http.get(LBM_URL, stream=True, timeout=300) as r:
            r.raise_for_status()
            tmp = LBM_ZIP.with_suffix(".part")
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
            tmp.replace(LBM_ZIP)
    with zipfile.ZipFile(LBM_ZIP) as zf:
        for kind in ("buurten", "wijken", "gemeenten"):
            name = next((n for n in zf.namelist() if n.lower().endswith(".csv") and f"scores {kind}" in n.lower()), None)
            if not name:
                log(f"  no Leefbaarometer file for {kind}")
                continue
            n = 0
            with zf.open(name) as f:
                for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")):
                    if row.get("jaar") != LBM_YEAR:
                        continue
                    code = next((row[k] for k in ("bu_code", "wk_code", "gm_code") if k in row), None)
                    if not code or row.get("lbm") in (None, "", "NA"):
                        continue
                    num = lambda k: None if row.get(k) in (None, "", "NA") else float(row[k])
                    lbm = num("lbm")
                    out.setdefault(code, {}).update({
                        "lbm": round(lbm, 4), "lbmClass": lbm_class(lbm), "lbmLabel": LBM_CLASSES[lbm_class(lbm) - 1],
                        "safety": num("onv"), "physical": num("fys"), "social": num("soc"), "amenities": num("vrz"),
                        "housing": num("won"),
                    })
                    if row.get("bu_naam") or row.get("wk_naam") or row.get("gm_naam"):
                        out[code].setdefault("name", row.get("bu_naam") or row.get("wk_naam") or row.get("gm_naam"))
                    n += 1
            log(f"  Leefbaarometer {kind}: {n}")


def cbs(out):
    table, fields = KERN
    log(f"CBS key figures ({table})")
    rows = odata(f"https://opendata.cbs.nl/ODataApi/odata/{table}/TypedDataSet",
                 {"$select": ",".join(["WijkenEnBuurten", "Gemeentenaam_1", "SoortRegio_2"] + list(fields.values()))})
    for r in rows:
        code = r["WijkenEnBuurten"].strip()
        d = out.setdefault(code, {})
        d["kind"] = (r.get("SoortRegio_2") or "").strip()
        d["gemeente"] = (r.get("Gemeentenaam_1") or "").strip()
        for k, f in fields.items():
            v = r.get(f)
            d[k] = v
        if d.get("woz") is not None:
            d["woz"] = round(d["woz"] * 1000)
        if d.get("income") is not None:
            d["income"] = round(d["income"] * 1000)
    log(f"  {len(rows)} regions")

    log(f"Registered crime {CRIME_YEAR} (47018NED)")
    crimes = odata("https://dataderden.cbs.nl/ODataApi/odata/47018NED/TypedDataSet",
                   {"$filter": f"Perioden eq '{CRIME_YEAR}JJ00' and SoortMisdrijf eq '0.0.0 '",
                    "$select": "WijkenEnBuurten,GeregistreerdeMisdrijven_1"})
    inhab = {r["WijkenEnBuurten"].strip(): r[CRIME_INHAB[1]] for r in
             odata(f"https://opendata.cbs.nl/ODataApi/odata/{CRIME_INHAB[0]}/TypedDataSet",
                   {"$select": f"WijkenEnBuurten,{CRIME_INHAB[1]}"})}
    for r in crimes:
        code = r["WijkenEnBuurten"].strip()
        n, p = r.get("GeregistreerdeMisdrijven_1"), inhab.get(code)
        d = out.setdefault(code, {})
        d["crimes"] = n
        # small populations (city centres, business parks) make the rate meaningless: need 300+ inhabitants
        d["crimePer1000"] = round(n / p * 1000, 1) if n is not None and p and p >= 300 else None
    log(f"  {len(crimes)} regions with crime figures")


def build():
    out = {}
    leefbaarometer(out)
    cbs(out)
    write_json(OUT, {"built": time.strftime("%Y-%m-%d %H:%M"), "lbmYear": LBM_YEAR, "crimeYear": CRIME_YEAR,
                     "kernTable": KERN[0], "items": out})
    log(f"Wrote {len(out)} regions to {OUT}")


if __name__ == "__main__":
    build()
