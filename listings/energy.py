"""Official energy labels from EP-Online (RVO), looked up per address.

Needs a free API key (https://apikey.ep-online.nl), saved in ep-online-key.txt next to server.py or in the
environment variable RF_EP_KEY. Without a key nothing is looked up and labels only come from listing texts.
Results are cached in data/cache/energy.json (re-checked after 60 days).
"""
import os
import time

import requests

from pipeline.common import CACHE, EP_KEY_FILE, USER_AGENT, read_json, write_json

API = "https://public.ep-online.nl/api/v5"
CACHE_JSON = CACHE / "energy.json"
MAX_AGE_D = 60
DELAY_S = 0.3
_http = requests.Session()
_http.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})


def key():
    k = os.environ.get("RF_EP_KEY") or (EP_KEY_FILE.read_text(encoding="utf-8").strip() if EP_KEY_FILE.exists() else "")
    return k or None


def has_key():
    return bool(key())


class Lookup:
    def __init__(self, log=print, force=False):
        self.force = force  # re-check every address, ignoring earlier results
        self.key = key()
        self.cache = read_json(CACHE_JSON, {})
        self.log = log
        self.calls = 0
        self.error = None

    def label(self, bag_id):
        """(label, registration date) for a BAG addressable-object id, or (None, None)."""
        if not bag_id:
            return None, None
        hit = self.cache.get(bag_id)
        if hit and not self.force and time.time() - hit["checked"] < MAX_AGE_D * 86400:
            return hit.get("label"), hit.get("date")
        if not self.key or self.error:
            return (hit or {}).get("label"), (hit or {}).get("date")
        time.sleep(DELAY_S)
        r = _http.get(f"{API}/PandEnergielabel/AdresseerbaarObject/{bag_id}", headers={"Authorization": self.key}, timeout=30)
        self.calls += 1
        if r.status_code in (401, 403):
            self.error = "EP-Online refused the API key (check ep-online-key.txt; a new key works 5 minutes after you get it)"
            self.log(self.error)
            return None, None
        if r.status_code == 429:
            self.log("EP-Online rate limit; pausing 60 s")
            time.sleep(60)
            return self.label(bag_id)
        lab = date = None
        if r.status_code == 200:
            rows = r.json() or []
            rows = sorted(rows, key=lambda x: x.get("Registratiedatum") or "", reverse=True)
            if rows:
                lab, date = rows[0].get("Energieklasse"), (rows[0].get("Registratiedatum") or "")[:10] or None
        elif r.status_code != 404:
            self.log(f"EP-Online {r.status_code} for {bag_id}")
            return None, None
        self.cache[bag_id] = {"label": lab, "date": date, "checked": time.time()}
        if self.calls % 25 == 0:
            self.save()
        return lab, date

    def save(self):
        write_json(CACHE_JSON, self.cache)
