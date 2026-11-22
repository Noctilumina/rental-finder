"""Train connections to and from Den Bosch CS, from the national GTFS feed.

Output: data/stations.json, one entry per station that can reach Den Bosch within MAX_TRAIN_MIN
(direct, or with one transfer), with off-peak travel times, frequencies, last train home and hourly
door-to-door profiles (wait for next train + ride, from/to the platform).

Run:  python -m pipeline.stations [--download]
"""
import bisect
import datetime as dt
import statistics
import sys
import time
import zipfile
from collections import defaultdict

import pandas as pd
import requests

from .common import (GTFS_URL, GTFS_ZIP, HOURS, MAX_TRAIN_MIN, STATIONS_JSON, TYPICAL_WINDOW,
                     USER_AGENT, write_json)

HUB = "IFF:ht"          # 's-Hertogenbosch (Den Bosch Centraal)
# Arrival stations for the trip to the destination. A station qualifies on its train time to Den Bosch CS or
# Den Bosch Oost; Rosmalen is also tried as an arrival station for the trip to Lucas Gasselstraat.
HUBS = {"IFF:ht": "Den Bosch CS", "IFF:hto": "Den Bosch Oost", "IFF:rs": "Rosmalen"}
QUALIFY_HUBS = ("IFF:ht", "IFF:hto")
ALWAYS = {"IFF:ht", "IFF:hto"}  # qualify by definition
TRANSFER_MIN = 3        # minimum changeover between trains (minutes)
MAX_WAIT = 120          # a wait longer than this counts as "no train this hour"


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def download():
    log("Downloading", GTFS_URL)
    with requests.get(GTFS_URL, stream=True, timeout=60, headers={"User-Agent": USER_AGENT}) as r:
        r.raise_for_status()
        tmp = GTFS_ZIP.with_suffix(".part")
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
        tmp.replace(GTFS_ZIP)


def to_min(series: pd.Series) -> pd.Series:
    parts = series.str.split(":", expand=True).astype(int)
    return parts[0] * 60 + parts[1] + (parts[2] >= 30).astype(int)


def pick_dates(dates: set[str]) -> dict[str, str]:
    """A representative Tuesday, Saturday and Sunday from tomorrow on, avoiding the first week."""
    start = dt.date.today() + dt.timedelta(days=7)
    want = {"wk": 1, "sa": 5, "su": 6}
    out = {}
    for key, wd in want.items():
        d = start
        for _ in range(60):
            if d.weekday() == wd and d.strftime("%Y%m%d") in dates:
                out[key] = d.strftime("%Y%m%d")
                break
            d += dt.timedelta(days=1)
    return out


def load(zf: zipfile.ZipFile):
    routes = pd.read_csv(zf.open("routes.txt"), dtype=str)
    rail_routes = set(routes.loc[routes.route_type == "2", "route_id"])
    trips = pd.read_csv(zf.open("trips.txt"), dtype=str, usecols=["route_id", "service_id", "trip_id"])
    trips = trips[trips.route_id.isin(rail_routes)]
    cal = pd.read_csv(zf.open("calendar_dates.txt"), dtype=str)
    cal = cal[cal.exception_type == "1"]
    dates = pick_dates(set(cal.date))
    log("Representative days:", dates)
    day_services = {k: set(cal.loc[cal.date == d, "service_id"]) for k, d in dates.items()}
    trip_days = defaultdict(set)
    for tid, sid in zip(trips.trip_id, trips.service_id):
        for k, sids in day_services.items():
            if sid in sids:
                trip_days[tid].add(k)
    keep = set(trip_days)
    log(f"{len(keep)} train trips on those days")

    stops = pd.read_csv(zf.open("stops.txt"), dtype=str,
                        usecols=["stop_id", "stop_name", "stop_lat", "stop_lon", "zone_id"])
    log("Reading stop times (large file, takes a few minutes)")
    parts = []
    cols = ["trip_id", "stop_sequence", "stop_id", "arrival_time", "departure_time", "pickup_type", "drop_off_type"]
    for chunk in pd.read_csv(zf.open("stop_times.txt"), dtype=str, usecols=cols, chunksize=2_000_000):
        parts.append(chunk[chunk.trip_id.isin(keep)])
    st = pd.concat(parts, ignore_index=True)
    st = st.merge(stops[["stop_id", "zone_id"]], on="stop_id", how="left")
    st = st[st.zone_id.notna() & st.zone_id.str.startswith("IFF:")]
    st["arr"] = to_min(st.arrival_time)
    st["dep"] = to_min(st.departure_time)
    st["seq"] = st.stop_sequence.astype(int)
    st["can_board"] = st.pickup_type.fillna("0") != "1"
    st["can_alight"] = st.drop_off_type.fillna("0") != "1"
    st = st.sort_values(["trip_id", "seq"])
    log(f"{len(st)} train stop events")

    used = stops[stops.stop_id.isin(set(st.stop_id))]
    info = {}
    for z, g in used.groupby("zone_id"):
        names = [n for n in g.stop_name if not n.startswith("[")] or list(g.stop_name)
        info[z] = {"name": names[0], "lat": round(g.stop_lat.astype(float).mean(), 6),
                   "lon": round(g.stop_lon.astype(float).mean(), 6)}
    return st, trip_days, info, dates


def legs_by_day(st: pd.DataFrame, trip_days):
    """Per day: list of trips, each a list of calls (station, arr, dep, can_board, can_alight)."""
    days = defaultdict(list)
    for tid, g in st.groupby("trip_id", sort=False):
        calls = list(zip(g.zone_id, g.arr, g.dep, g.can_board, g.can_alight))
        # collapse repeated platform rows of the same station
        dedup = []
        for c in calls:
            if dedup and dedup[-1][0] == c[0]:
                dedup[-1] = (c[0], dedup[-1][1], c[2], dedup[-1][3] or c[3], dedup[-1][4] or c[4])
            else:
                dedup.append(c)
        for k in trip_days[tid]:
            days[k].append(dedup)
    return days


def pareto(journeys):
    """Keep journeys not dominated by one that leaves later and arrives no later."""
    js = sorted(set(journeys), key=lambda j: (j[0], -j[1]))
    out = []
    best_arr = None
    for dep, arr, tr in sorted(js, key=lambda j: (-j[0], j[1])):
        if best_arr is None or arr < best_arr:
            out.append((dep, arr, tr))
            best_arr = arr
    return sorted(out)


def analyse_day(trips, HUB):
    direct_to, direct_from = defaultdict(list), defaultdict(list)
    pair_legs = []  # (from, to, dep, arr) for every boardable->alightable pair on a trip
    for calls in trips:
        n = len(calls)
        for i in range(n):
            si, _, di, bi, _ = calls[i]
            for j in range(i + 1, n):
                sj, aj, _, _, alj = calls[j]
                if not (bi and alj) or si == sj:
                    continue
                if sj == HUB:
                    direct_to[si].append((di, aj, 0))
                elif si == HUB:
                    direct_from[sj].append((di, aj, 0))
                else:
                    pair_legs.append((si, sj, di, aj))

    # one-transfer journeys via an intermediate station X
    hub_deps = {x: sorted(v) for x, v in direct_to.items()}          # X -> hub
    hub_dep_times = {x: [d for d, _, _ in v] for x, v in hub_deps.items()}
    from_hub = {x: sorted(v, key=lambda j: j[1]) for x, v in direct_from.items()}  # hub -> X, by arrival
    any_to, any_from = defaultdict(list), defaultdict(list)
    for s, v in direct_to.items():
        any_to[s].extend(v)
    for s, v in direct_from.items():
        any_from[s].extend(v)
    legs_from = defaultdict(list)  # X -> list of legs leaving X (for hub -> X -> S)
    for s, x, d, a in pair_legs:
        # S -> X, then X -> hub
        if x in hub_dep_times:
            times = hub_dep_times[x]
            k = bisect.bisect_left(times, a + TRANSFER_MIN)
            if k < len(times):
                hd, ha, _ = hub_deps[x][k]
                any_to[s].append((d, ha, 1))
        legs_from[s].append((d, a, x))
    for x, legs in legs_from.items():
        if x not in from_hub:
            continue
        legs.sort()
        ldeps = [d for d, _, _ in legs]
        for hd, ha, _ in from_hub[x]:
            k = bisect.bisect_left(ldeps, ha + TRANSFER_MIN)
            # take every onward leg in the next 60 minutes (different destinations)
            seen = set()
            while k < len(legs) and legs[k][0] <= ha + TRANSFER_MIN + 60:
                d, a, s = legs[k]
                if s not in seen and s != HUB:
                    any_from[s].append((hd, a, 1))
                    seen.add(s)
                k += 1
    return ({s: pareto(v) for s, v in direct_to.items()}, {s: pareto(v) for s, v in direct_from.items()},
            {s: pareto(v) for s, v in any_to.items()}, {s: pareto(v) for s, v in any_from.items()})


def typical(journeys, window):
    durs = [a - d for d, a, _ in journeys if window[0] <= d < window[1]]
    return round(statistics.median(durs)) if durs else None


def hourly(journeys):
    """Average door-to-door (wait + ride) per departure hour; None if no train within MAX_WAIT."""
    if not journeys:
        return [None] * len(HOURS)
    deps = [d for d, _, _ in journeys]
    out = []
    for h in HOURS:
        vals = []
        for m in range(h * 60, h * 60 + 60):
            k = bisect.bisect_left(deps, m)
            if k < len(deps) and deps[k] - m <= MAX_WAIT:
                d, a, _ = journeys[k]
                vals.append(a - m)
        out.append(round(sum(vals) / len(vals)) if len(vals) >= 30 else None)
    return out


def fmt(m):
    return f"{(m // 60) % 24:02d}:{m % 60:02d}" if m is not None else None


def hub_entry(code, hub, res):
    """Travel between a station and one arrival station: typical times, hourly profiles."""
    wk_dt, wk_df, wk_at, wk_af = res["wk"][hub]
    if code == hub:
        zero = [0] * len(HOURS)
        z = {"to": 0, "from": 0, "q": 0, "transfers": 0}
        return {"direct": z, "any": z, "typ": {"direct": 0, "any": 0}, "freq": None, "lastHome": None, "lastHomeAny": None,
                "hourly": {k: {"direct": {"to": zero, "from": zero}, "any": {"to": zero, "from": zero}} for k in res}}
    d_to, d_from = wk_dt.get(code, []), wk_df.get(code, [])
    a_to, a_from = wk_at.get(code, []), wk_af.get(code, [])
    dq = None
    t1, t2 = typical(d_to, TYPICAL_WINDOW), typical(d_from, TYPICAL_WINDOW)
    if t1 is not None and t2 is not None:
        dq = {"to": t1, "from": t2, "q": max(t1, t2)}
    a1, a2 = typical(a_to, TYPICAL_WINDOW), typical(a_from, TYPICAL_WINDOW)
    aq = None
    if a1 is not None and a2 is not None:
        tr = [t for d, a, t in a_to if TYPICAL_WINDOW[0] <= d < TYPICAL_WINDOW[1]]
        if dq:  # allowing transfers never makes a station worse than its direct trains
            a1, a2 = min(a1, dq["to"]), min(a2, dq["from"])
        aq = {"to": a1, "from": a2, "q": max(a1, a2),
              "transfers": 0 if dq and max(a1, a2) == dq["q"] else (round(statistics.median(tr)) if tr else 0)}
    prof = {}
    for k in res:
        dt_, df_, at_, af_ = res[k][hub]
        prof[k] = {"direct": {"to": hourly(dt_.get(code, [])), "from": hourly(df_.get(code, []))},
                   "any": {"to": hourly(at_.get(code, [])), "from": hourly(af_.get(code, []))}}
    # typical off-peak door to door from the platform (average wait + ride), 09:00-16:00 on a weekday
    window = [i for i, h in enumerate(HOURS) if 9 <= h < 16]
    def typ(m):
        v = [prof["wk"][m]["to"][i] for i in window if prof["wk"][m]["to"][i] is not None]
        return round(sum(v) / len(v)) if v else None
    return {"direct": dq, "any": aq, "typ": {"direct": typ("direct"), "any": typ("any")},
            "freq": {"offpeak": round(sum(1 for d, _, _ in d_to if 10 * 60 <= d < 15 * 60) / 5, 1),
                     "evening": round(sum(1 for d, _, _ in d_to if 20 * 60 <= d < 24 * 60) / 4, 1)},
            "lastHome": fmt(max((d for d, _, _ in d_from), default=None)),
            "lastHomeAny": fmt(max((d for d, _, _ in a_from), default=None)),
            "hourly": prof}


def best(entries, m):
    """The qualifying arrival station with the shortest typical trip for mode m ('direct' or 'any')."""
    opts = [(e[m]["q"], h) for h, e in entries.items() if h in QUALIFY_HUBS and e[m]]
    if not opts:
        return None
    q, h = min(opts)
    return {**entries[h][m], "via": h}


def build():
    if not GTFS_ZIP.exists():
        download()
    with zipfile.ZipFile(GTFS_ZIP) as zf:
        st, trip_days, info, dates = load(zf)
    days = legs_by_day(st, trip_days)
    res = {k: {h: analyse_day(v, h) for h in HUBS} for k, v in days.items()}
    log("Connections analysed")

    out = []
    candidates = set(ALWAYS) | set(HUBS)
    for h in QUALIFY_HUBS:
        candidates |= set(res["wk"][h][2]) | set(res["wk"][h][3])
    for code in sorted(candidates):
        if code not in info:
            continue
        entries = {h: hub_entry(code, h, res) for h in HUBS}
        dq, aq = best(entries, "direct"), best(entries, "any")
        if (aq is None or aq["q"] > MAX_TRAIN_MIN) and code not in ALWAYS:
            continue
        via = (dq or aq or {"via": HUB})["via"]
        e = entries[via]
        out.append({
            "code": code, "name": info[code]["name"], "lat": info[code]["lat"], "lon": info[code]["lon"],
            "hub": code == HUB, "always": code in ALWAYS, "arrival": code in HUBS,
            "direct": dq, "any": aq,
            "freq": e["freq"] or {"offpeak": 0, "evening": 0},
            "lastHome": e["lastHome"], "lastHomeAny": e["lastHomeAny"],
            "hubs": entries,
        })
    write_json(STATIONS_JSON, {"generated": dt.datetime.now().isoformat(timespec="minutes"),
                               "dates": dates, "hours": HOURS, "hubNames": HUBS, "stations": out})
    log(f"Wrote {len(out)} stations to {STATIONS_JSON}")
    return out


if __name__ == "__main__":
    if "--download" in sys.argv:
        download()
    build()
