"""Rebuild map and timetable data: fresh timetable, fresh OpenStreetMap extracts, walking areas, neighbourhoods.

Run:  python -m pipeline.rebuild

`ensure()` builds only what is missing; a listing refresh calls it first, so a new install needs no separate step.
"""
from .common import GRID_NPZ, META_JSON, STATIONS_JSON, SUPERMARKETS_JSON, load_grid, read_json
from . import neighbourhoods, stations, walk_local


def missing():
    """Which parts of the map data are not there yet."""
    out = []
    if not (read_json(STATIONS_JSON) or {}).get("stations"):
        out.append("timetable")
    if not GRID_NPZ.exists() or not SUPERMARKETS_JSON.exists() or not (read_json(META_JSON) or {}).get("destination") \
            or load_grid() is None:
        out.append("walking areas")
    if not neighbourhoods.OUT.exists():
        out.append("neighbourhood figures")
    return out


def ensure(log=print):
    """Builds whatever map data is missing. Returns True when something was built."""
    todo = missing()
    if not todo:
        return False
    bar = "=" * 78
    for line in (bar,
                 "MAP DATA IS MISSING: building it first (" + ", ".join(todo) + ").",
                 "This happens once on a new install and takes about 10 minutes; the listing search",
                 "starts automatically afterwards. Nothing for you to do.",
                 bar):
        log(line)
    if "timetable" in todo:
        stations.build()
    if "timetable" in todo or "walking areas" in todo:
        walk_local.build()          # downloads the OpenStreetMap extracts it doesn't have yet
    if "neighbourhood figures" in todo:
        neighbourhoods.build()
    still = missing()
    if still:
        raise SystemExit("Map data could not be built (" + ", ".join(still) + "); see the messages above.")
    for line in (bar, "MAP DATA READY: continuing with the listing search.", bar):
        log(line)
    return True


def fresh_timetable(log=print):
    """Downloads the timetable; if the site refuses (e.g. 'too many requests'), keeps the one downloaded earlier."""
    from .common import GTFS_ZIP
    try:
        stations.download()
    except Exception as e:
        if not GTFS_ZIP.exists():
            raise
        log(f"Could not download a new timetable ({e}); using the one downloaded earlier")


if __name__ == "__main__":
    fresh_timetable()
    stations.build()
    walk_local.download()
    walk_local.build()
    neighbourhoods.build()
