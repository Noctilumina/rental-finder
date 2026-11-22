# Rental finder

Local web app that finds rental homes near stations with a direct off-peak train to Den Bosch, within walking
distance of a supermarket, and scores them. See SPEC.md for the full requirements.

## Run

Double-click `start.cmd` (Windows), or run

    %USERPROFILE%\.venvs\rental-finder\Scripts\python server.py

and open http://127.0.0.1:8765. Use **Refresh listings** (top right, or the Data screen) to fetch new listings.

## Setup (once, on a new machine)

    python -m venv %USERPROFILE%\.venvs\rental-finder
    %USERPROFILE%\.venvs\rental-finder\Scripts\pip install -r requirements.txt

(On Linux: `python3 -m venv ~/.venvs/rental-finder` and `~/.venvs/rental-finder/bin/pip ...`.)

## Map, timetable and neighbourhood data

Built once, rebuilt from the Data screen ("Rebuild map data") when the timetable changes.

    python -m pipeline.stations        # timetable -> data/stations.json (~20 s; downloads 280 MB)
    python -m pipeline.walk_local      # walking areas, parks, noise sources -> data/grid.npz etc. (~2 min; Geofabrik extracts)
    python -m pipeline.neighbourhoods  # Leefbaarometer + CBS figures -> data/neighbourhoods.json (downloads 500 MB once)

`pipeline.rebuild` does all three with fresh downloads.

Destination: Lucas Gasselstraat 2, 5246 CE Rosmalen (pipeline/common.py, DESTINATION_QUERY). Arrival stations
tried: Den Bosch CS, Den Bosch Oost, Rosmalen (pipeline/stations.py, HUBS).

## Listings

    python -m listings.refresh         # search, merge, history, enrich -> data/listings.json
    python -m listings.enrich          # only re-run municipality/neighbourhood, description facts, energy labels

- **Rentola** (rentola.nl): aggregator that re-lists homes from Pararius, Funda, Kamernet and others. Searched per
  town; each ad's page is read once (cached 30 days) for the description, photos and rooms.
- **Vesteda**: large mid-rent landlord, searched within 5 km of each station.
- Optional, browser-based (`--sites Funda,Huurwoningen,Pararius`): off by default because of frequent human checks.

The first refresh after a change of areas reads many ad pages and can take a few hours; later ones only read new
ads. Homes that disappear are re-checked: "removed" only when the site says so.

**Energy labels:** request a free key at https://apikey.ep-online.nl, then paste it on the Data screen (stored in
`ep-online-key.txt`) and press "Look up energy labels now". Without a key, labels only come from listing texts.

Your stars, statuses and notes are in `data/user.json`; refreshes never change them.

## Files

- `pipeline/` timetable (stations.py), walking areas (walk_local.py), neighbourhood figures, rebuild job
- `listings/` source adapters, refresh job, enrichment (enrich.py, energy.py, textflags.py)
- `scoring.py` the 0–10 scores; `server.py` local API and web server; `web/` the interface
- `data/` generated data (stations, grid, parks, noise, neighbourhoods, listings, your notes, caches)

Adapter approaches follow danieltyukov/nl-property-finder (MIT).
