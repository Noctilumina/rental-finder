# Rental finder: spec

A small local web app that finds rental homes in areas with a good train connection to Den Bosch.
UI mockups: https://claude.ai/artifact/3K6Ng3D2vDmEMBaJ3UAkrU

## 1. Qualifying areas (hard filter)

An address qualifies when all of these hold:

- **Station:** within *max walk time to station* (slider 5–60 min, default 15) of an NS station, measured along
  real streets and paths (OpenStreetMap walking network, 5 km/h).
- **Train:** that station has a **direct** train to Den Bosch CS or Den Bosch Oost taking at most *max train time*
  (slider 5–60 min, default 30) during **off-peak hours**, in **both directions**.
  - Off-peak (NS free travel): weekdays before 06:30, 09:00–16:00 and after 18:30; weekends all day.
  - Toggle "Direct trains only" (default on). When off, connections with one transfer count.
  - Den Bosch CS and Oost qualify by definition.
- **Supermarket:** within *walk to supermarket* (slider 2–30 min, default 15) of `shop=supermarket` (OSM).
- **Park (optional):** within *walk to a park* (slider 2–30 min; at 30 = Any, the default) of a park of 0.5 ha+.

Areas are exact polygons (50 m grid) on an OpenStreetMap base map; optionally coloured by typical off-peak
door-to-door time (colour-blind-safe scale). Hovering the map shows the place (street, neighbourhood, town,
municipality) with its walk, door-to-door, supermarket and park times.

## 2. Travel-time information (not filters)

- Destination switch: Den Bosch CS, or Lucas Gasselstraat 2, 5246 CE Rosmalen (municipality of 's-Hertogenbosch).
- To Lucas Gasselstraat the quickest of these is used, per departure hour: train to Den Bosch CS, Den Bosch Oost
  or Rosmalen plus the last stretch (bike, default, or walk); or going straight there by bike/on foot from home
  (homes in and around Den Bosch).
- Door-to-door = walk to station + average wait for the next train + ride + last stretch.
- Per station and listing: time by departure hour (06:00–00:00), weekday/Saturday/Sunday, worst case,
  direct trains per hour, last direct train home.

## 3. Listings

- Rentals only, no social housing, no rooms in shared homes, nothing under €300.
- Sources (plain, rate-limited requests for personal use; no captchas):
  - Rentola: aggregator of Pararius, Funda, Kamernet and others; searched per town around the stations; each ad's
    page read once (description, photos, rooms, pets).
  - Vesteda: mid-rent landlord; searched within 5 km of each station.
  - Funda, Pararius and Huurwoningen directly: possible through a real browser (code included), off by default
    because of frequent "verify you are human" checks.
- Duplicates merged: same postcode + house number with similar floor area (±3 m²); or within 15 m with similar
  floor area and rent (±5%); or an ad without house number on the same street within 300 m with the same size and
  rent. "Show duplicate ads" shows them separately.
- History across refreshes: first/last seen, rent changes (▼ blue / ▲ red with arrows), "removed" only when the
  site says the ad is gone or let, otherwise "not found in last scan" (filterable).
- Enrichment: municipality and neighbourhood (PDOK), energy label (EP-Online with API key, else from the
  description), landlord demands and deposit from the description, suspicious-listing warnings.
- My lists: star (shortlist), status (contacted, viewing, applied, rejected, not for me), notes, hide.
- Score 0–10 from parts that are each 0–10 on fixed scales, each with its percentile among current homes:
  connectivity, value for money (€/m² vs the municipality), neighbourhood (Leefbaarometer), safety, quiet (road
  and rail noise estimate), conditions (landlord demands), shops and green, energy label, rent vs. legal maximum
  (rough WWS points estimate).
- Filter and sort on rent, €/m², €/room, floor area, rooms, energy label, interior, available from, listed date,
  pets, score, municipality (multi-select built from the data), new in latest refresh, my lists, availability.
- Export the filtered list as CSV.

## 4. Screens

- **Map:** collapsible criteria panel, map, collapsible listing panel (drag its edge to widen; quick filters when
  wide). Pins numbered in sort order. Hovering a pin or a listing card previews it (photo, route, hourly chart);
  clicking opens it.
- **Listings table:** all filters, sortable columns, CSV export.
- **Municipalities:** per municipality (expandable to neighbourhoods): number of homes, median rent, €/m², size,
  score, liveability, crime per 1000 inhabitants, WOZ value, income, inhabitants.
- **Listing detail:** photos, properties, score breakdown, neighbourhood figures, my notes, sources and searches for
  the original ad on Funda/Pararius, travel breakdown and hourly chart.
- **Data:** refresh listings, rebuild map data, energy-label key and lookup, status and log.

## 5. Technical

- Runs locally (Windows, Linux). Python data pipeline and a small local server; the UI is a web page served by it.
- Data: national GTFS feed (gtfs.ovapi.nl); OpenStreetMap via Geofabrik province extracts; PDOK Locatieserver;
  Leefbaarometer 3.0; CBS StatLine (Kerncijfers wijken en buurten, registered crime); EP-Online.
- No cloud services.
