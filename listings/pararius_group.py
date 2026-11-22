"""Pararius and Huurwoningen.nl (same company, same page markup), read through the shared browser."""
import html as htmllib
import re

MAX_PAGES = 40
INTERIOR = {"gemeubileerd": "furnished", "gestoffeerd": "upholstered", "kaal": "bare"}


def _txt(s):
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def _int(s):
    m = re.search(r"\d[\d.]*", s or "")
    return int(m.group(0).replace(".", "")) if m else None


def _date(s):
    m = re.search(r"(\d{2})-(\d{2})-(\d{4})", s or "")
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else None


def _slug(name):
    n = name.lower().strip().replace("'", "").replace("’", "")
    n = {"s-hertogenbosch": "den-bosch", "s-hertogenbosch oost": "den-bosch"}.get(n, n)
    return re.sub(r"[^a-z0-9]+", "-", n).strip("-")


class Site:
    def __init__(self, name, base, page_url, read_details=True):
        self.SITE, self.base, self._page_url = name, base, page_url
        self.READ_DETAILS = read_details  # Pararius shows a check on listing pages; its twin Huurwoningen has them

    def slug(self, town):
        return _slug(town)

    def search_town(self, br, town, log=print):
        slug = self.slug(town)
        out = []
        for page in range(1, MAX_PAGES + 1):
            status, h = br.get(self._page_url(self.base, slug, page), ready="listing-search-item")
            if status in (404, 410):
                return None if page == 1 else out
            items = h.split('<section class="listing-search-item ')[1:]
            if not items:
                break
            for it in items:
                it = it.split("</section>")[0]
                t = re.search(r'listing-search-item__link--title"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', it, re.S)
                if not t:
                    continue
                sub = _txt((re.search(r'listing-search-item__sub-title[^"]*"[^>]*>(.*?)</div>', it, re.S) or [None, ""])[1])
                pc = re.match(r"(\d{4}\s?[A-Z]{2})\s+(.*?)(?:\s+\((.*)\))?$", sub)
                price = _txt((re.search(r'listing-search-item__price[^"]*"[^>]*>(.*?)</', it, re.S) or [None, ""])[1])
                feats = {k: _txt(v) for k, v in re.findall(r'illustrated-features__item--([a-z-]+)"[^>]*>(.*?)</li>', it, re.S)}
                title = _txt(t.group(2))
                kind, _, street = title.partition(" ")
                path = t.group(1)
                ids = [p for p in path.strip("/").split("/") if re.fullmatch(r"[0-9a-f]{8}", p)]
                label = _txt((re.search(r'listing-search-item__label[^"]*"[^>]*>(.*?)</', it, re.S) or [None, ""])[1]).lower()
                out.append({
                    "site": self.SITE, "url": self.base + path, "srcId": ids[0] if ids else path,
                    "type": kind, "street": street or title,
                    "postcode": pc.group(1).replace(" ", "") if pc else None,
                    "city": pc.group(2) if pc else None, "district": pc.group(3) if pc else None,
                    "rent": _int(price), "m2": _int(feats.get("surface-area")), "rooms": _int(feats.get("number-of-rooms")),
                    "interior": INTERIOR.get((feats.get("interior") or "").lower()),
                    "status": "under_offer" if "optie" in label or "voorbehoud" in label else "available",
                })
            if f"page-{page + 1}" not in h and f"page={page + 1}" not in h:
                break
        return out

    def details(self, br, url):
        status, h = br.get(url, ready="listing-features")
        if status in (404, 410):
            return {"gone": True}
        f = {}
        for dt, dd in re.findall(r'<dt class="listing-features__term[^"]*"[^>]*>(.*?)</dt>\s*<dd class="listing-features__description[^"]*"[^>]*>(.*?)</dd>', h, re.S):
            f.setdefault(_txt(dt), _txt(dd))
        lat = re.search(r'data-latitude="([\d.]+)"', h)
        lon = re.search(r'data-longitude="([\d.]+)"', h)
        photo = re.search(r'<meta property="og:image" content="([^"]+)"', h)
        desc = re.search(r'class="listing-detail-description__additional[^"]*"[^>]*>(.*?)</div>', h, re.S)
        price_txt = f.get("Huurprijs", "")
        st = f.get("Status", "").lower()
        pets = f.get("Huisdieren toegestaan", "").lower()
        avail = f.get("Beschikbaar", "")
        return {
            "lat": float(lat.group(1)) if lat else None, "lon": float(lon.group(1)) if lon else None,
            "rent": _int(price_txt),
            "rentIncl": True if "inclusief" in price_txt.lower() else False if "exclusief" in price_txt.lower() else None,
            "serviceCosts": _int(f.get("Servicekosten")),
            "listedAt": _date(f.get("Aangeboden sinds")),
            "availableFrom": "immediately" if "direct" in avail.lower() else (_date(avail) or avail or None),
            "deposit": _int(f.get("Borg")),
            "interior": INTERIOR.get((f.get("Interieur", "").split(" ")[0] or "").lower()),
            "m2": _int(f.get("Woonoppervlakte")), "rooms": _int(f.get("Aantal kamers")),
            "bedrooms": _int(f.get("Aantal slaapkamers")),
            "energyLabel": (f.get("Energielabel") or "").split(" ")[0] or None,
            "pets": True if pets.startswith("ja") else False if pets.startswith("nee") else None,
            "status": "let" if st.startswith("verhuurd") and "voorbehoud" not in st
                      else "under_offer" if ("voorbehoud" in st or "optie" in st) else "available",
            "photo": photo.group(1) if photo else None,
            "description": _txt(desc.group(1))[:4000] if desc else None,
        }


pararius = Site("Pararius", "https://www.pararius.nl",
                lambda b, s, p: f"{b}/huurwoningen/{s}" + (f"/page-{p}" if p > 1 else ""), read_details=False)
huurwoningen = Site("Huurwoningen", "https://www.huurwoningen.nl",
                    lambda b, s, p: f"{b}/in/{s}/" + (f"?page={p}" if p > 1 else ""))
