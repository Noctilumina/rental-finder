# Rental finder

Local web app that finds rental homes near stations with a direct off-peak train to Den Bosch, within walking
distance of a supermarket, and scores them. See SPEC.md for the full requirements.

Runs on Windows, Linux and macOS (tested automatically on Ubuntu and macOS, see `.github/workflows/test.yml`).

## Run

- **Windows:** double-click `start.cmd`.
- **Linux / macOS:** run `./start.sh` in this folder.

Both start the server and open http://127.0.0.1:8765 in your browser. Stop it by closing the window (Windows) or
with Ctrl+C. Use **Refresh listings** (top right, or the Data screen) to fetch new listings.

## Setup (once, on a new machine)

Needs Python 3.11 or newer. The app's own data is not in the repository: after setting up, press **Refresh
listings**. On a new install it first builds the map data by itself (about 10 minutes), then searches; the first
search takes an hour or more.

**Windows** (PowerShell):

    python -m venv $env:USERPROFILE\.venvs\rental-finder
    & $env:USERPROFILE\.venvs\rental-finder\Scripts\pip install -r requirements.txt

**Linux:**

    sudo apt install python3 python3-venv     # Debian/Ubuntu; Fedora: sudo dnf install python3
    python3 -m venv ~/.venvs/rental-finder
    ~/.venvs/rental-finder/bin/pip install -r requirements.txt

**macOS** (with [Homebrew](https://brew.sh)):

    brew install python
    python3 -m venv ~/.venvs/rental-finder
    ~/.venvs/rental-finder/bin/pip install -r requirements.txt

To use a Python somewhere else, set `RF_PYTHON` to it before running `./start.sh`.

**Moving to another computer:** copy `data/user.json` (your stars, statuses and notes), `data/settings.json`
(score weights, travel times) and `ep-online-key.txt` along. On Linux and macOS the key file is readable only by you.

The optional browser-based sources (see Listings) use Microsoft Edge on Windows and Google Chrome elsewhere; set
`RF_BROWSER_CHANNEL` to pick another browser.

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
`ep-online-key.txt`) and press "Get energy labels for all listings". Without a key, labels only come from listing texts.

Your stars, statuses and notes are in `data/user.json`; refreshes never change them.

## Files

- `pipeline/` timetable (stations.py), walking areas (walk_local.py), neighbourhood figures, rebuild job
- `listings/` source adapters, refresh job, enrichment (enrich.py, energy.py, textflags.py)
- `scoring.py` the 0–10 scores; `server.py` local API and web server; `web/` the interface
- `data/` generated data (stations, grid, parks, noise, neighbourhoods, listings, your notes, caches)

Adapter approaches follow danieltyukov/nl-property-finder (MIT).
