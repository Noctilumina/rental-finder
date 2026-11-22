"""Rebuild map and timetable data: fresh timetable, fresh OpenStreetMap extracts, walking areas, neighbourhoods.

Run:  python -m pipeline.rebuild
"""
from . import neighbourhoods, stations, walk_local

if __name__ == "__main__":
    stations.download()
    stations.build()
    walk_local.download()
    walk_local.build()
    neighbourhoods.build()
