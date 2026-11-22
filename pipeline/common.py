"""Shared paths and settings for the data pipeline and server."""
import json
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
RAW = DATA / "raw"
CACHE = DATA / "cache"
for d in (DATA, RAW, CACHE):
    d.mkdir(parents=True, exist_ok=True)

GTFS_URL = "https://gtfs.ovapi.nl/nl/gtfs-nl.zip"
GTFS_ZIP = RAW / "gtfs-nl.zip"
STATIONS_JSON = DATA / "stations.json"
GRID_JSON = DATA / "grid.json"
META_JSON = DATA / "meta.json"
LISTINGS_JSON = DATA / "listings.json"
GRID_NPZ = DATA / "grid.npz"
SUPERMARKETS_JSON = DATA / "supermarkets.json"
PARKS_JSON = DATA / "parks.json"
NOISE_NPZ = DATA / "noise.npz"
USER_JSON = DATA / "user.json"          # your stars, statuses and notes (never overwritten by a refresh)
HISTORY_JSON = DATA / "history.json"    # first/last seen and price changes per home
AREAS_JSON = CACHE / "areas.json"       # municipality and neighbourhood per listing location
EP_KEY_FILE = ROOT / "ep-online-key.txt"

USER_AGENT = "rental-finder/0.1 (personal, local use)"

# The hub everything is measured against and the final destination.
HUB_NAME = "'s-Hertogenbosch"
# Lucas Gasselstraat 2 also exists in Eindhoven and Helmond; the one in the municipality of 's-Hertogenbosch is in Rosmalen.
DESTINATION_QUERY = "Lucas Gasselstraat 2, 5246CE Rosmalen"
BIKE_SPEED_M_PER_MIN = 15000 / 60  # 15 km/h for the last stretch by bike
BIKE_PARK_MIN = 3                  # to pick up / park the bike

# Upper bounds of the sliders. The pipeline precomputes up to these; the UI filters below them.
MAX_TRAIN_MIN = 60        # stations considered at all
MAX_WALK_STATION_MIN = 60  # walk to station
MAX_WALK_SHOP_MIN = 30     # walk to supermarket
MAX_WALK_PARK_MIN = 30     # walk to a park
WALK_SPEED_M_PER_MIN = 5000 / 60  # 5 km/h
CELL_M = 50               # grid cell size (metres, Dutch RD grid)

# NS off-peak (free travel) windows, minutes after midnight. Weekends are off-peak all day.
OFFPEAK_WEEKDAY = [(0, 6 * 60 + 30), (9 * 60, 16 * 60), (18 * 60 + 30, 30 * 60)]
# Window used for the "typical off-peak" travel time that drives qualification.
TYPICAL_WINDOW = (9 * 60, 16 * 60)

HOURS = list(range(6, 24)) + [24]  # 24 = 00:00-01:00


def is_offpeak(minute: int, weekend: bool) -> bool:
    if weekend:
        return True
    m = minute % (24 * 60) if minute < 30 * 60 else minute
    return any(a <= m < b for a, b in OFFPEAK_WEEKDAY)


def write_json(path: Path, obj) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)


def read_json(path: Path, default=None):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def save_grid(ckey, sm, pk, dd, stations):
    """Cells (sorted keys ix*1e6+iy) with supermarket/park walk (min x10) and route metres to the destination,
    plus per station the cell indices and walk times (min x10)."""
    arrs = {"ckey": ckey, "sm": sm, "pk": pk, "dd": dd, "codes": np.array(list(stations))}
    for code, (idx, t) in stations.items():
        k = code.replace(":", "_")
        o = np.argsort(idx, kind="stable")  # sorted, so lookups can use binary search
        arrs["c_" + k], arrs["t_" + k] = idx[o], t[o]
    tmp = GRID_NPZ.with_name("grid.tmp.npz")
    np.savez_compressed(tmp, **arrs)
    tmp.replace(GRID_NPZ)


def load_grid():
    if not GRID_NPZ.exists():
        return None
    z = np.load(GRID_NPZ)
    codes = [str(c) for c in z["codes"]]
    st = {}
    for c in codes:
        idx, t = z["c_" + c.replace(":", "_")], z["t_" + c.replace(":", "_")]
        if len(idx) > 1 and (np.diff(idx) < 0).any():
            o = np.argsort(idx, kind="stable")
            idx, t = idx[o], t[o]
        st[c] = (idx, t)
    return {"cell": CELL_M, "ckey": z["ckey"], "sm": z["sm"], "pk": z["pk"], "dd": z["dd"], "stations": st}
