"""Facts and landlord demands read from a listing's description (Dutch and English).

read(text) returns {"energyLabel", "depositMonths", "deposit", "outdoor", "flags": [...], "suspicious": [...]}.
Each flag: {"key", "label", "penalty"} where penalty (0-4) lowers the "Conditions" score.
"""
import re

LABEL_RE = re.compile(r"energie\s*-?\s*label\s*(?:klasse)?\s*[:\-]?\s*(?:is\s*)?\b([A-G](?:\+{1,4})?)(?![a-z])"
                      r"|energy\s*(?:efficiency\s*)?(?:label|rating)\s*[:\-]?\s*(?:is\s*)?\b([A-G](?:\+{1,4})?)(?![a-z])"
                      r"|\blabel\s+([A-G](?:\+{1,4})?)\b(?=[^\n]{0,15}energ)", re.I)

FLAGS = [
    # key, label, penalty, patterns
    ("temporary", "Temporary contract / short stay", 3,
     [r"tijdelijk(?:e)?\s+(?:huur)?(?:contract|overeenkomst|verhuur)", r"\bshort\s*-?\s*stay\b", r"maximale huurperiode",
      r"(?:maximaal|max\.?|voor)\s+(?:\d+|een|twee|één)\s+(?:jaar|maanden)\b(?![^.\n]{0,30}(?:minimaal|minimum))",
      r"\btemporary (?:rental|contract|lease)\b", r"\bfixed[- ]term\b", r"\b(?:max(?:imum)?|for)\s+\d+\s+(?:months|years?)\b",
      r"leegstandswet", r"\bdiplomatenclausule\b", r"\bdiplomat clause\b"]),
    ("no_registration", "Can't register at the address", 3,
     [r"(?:geen|niet)\s+(?:mogelijk\s+)?(?:te\s+)?inschrijv", r"inschrijv\w*\s+(?:is\s+)?niet\s+(?:mogelijk|toegestaan)",
      r"registration\s+(?:at the municipality\s+)?(?:is\s+)?not\s+(?:possible|allowed)", r"\bno registration\b"]),
    ("only_group", "Only for a specific group (students, expats, seniors)", 3,
     [r"alleen\s+(?:voor\s+)?(?:studenten|expats|senioren|internationals|werkenden)", r"uitsluitend\s+(?:voor\s+)?(?:studenten|expats|senioren)",
      r"\b(?:only|exclusively)\s+(?:for\s+)?(?:students|expats|seniors|internationals)\b", r"\bexpats? only\b",
      r"\b55\s*\+\s*(?:woning|complex)", r"alleen aan u verhuren"]),
    ("income4", "Asks for income of 4x the rent or more", 2,
     [r"\b(?:[4-9]|vier|vijf)\s*(?:x|keer|maal|times)\s*(?:de\s+|the\s+)?(?:kale\s+|bruto\s+|monthly\s+)?(?:huur|rent)"]),
    ("income3", "Asks for income of 3x the rent", 1,
     [r"\b(?:3|3[.,]5|drie)\s*(?:x|keer|maal|times)\s*(?:de\s+|the\s+)?(?:kale\s+|bruto\s+|monthly\s+)?(?:huur|rent)"]),
    ("guarantor", "Asks for a guarantor", 1, [r"garantsteller", r"\bguarantor\b", r"borgsteller"]),
    ("deposit3", "Deposit of 3 months or more", 1.5, []),  # set from depositMonths
    ("agency_fee", "Agency or contract fees", 1,
     [r"bemiddelingskosten", r"contractkosten", r"administratiekosten", r"\bagency fee", r"\badministration (?:fee|costs)"]),
    ("no_pets", "No pets", 0.5, [r"(?:geen|niet)\s+huisdieren", r"huisdieren\s+(?:zijn\s+)?niet\s+toegestaan", r"\bno pets\b", r"pets (?:are )?not allowed"]),
    ("no_smoking", "No smoking", 0, [r"niet\s+roken", r"rookvrij", r"\bno smoking\b", r"non-smok"]),
    ("no_sharing", "No sharing (one person or couple only)", 0.5,
     [r"geen\s+(?:woning)?delers", r"niet\s+geschikt\s+voor\s+(?:woning)?delers", r"\bno (?:house\s*)?sharers\b", r"not suitable for sharing"]),
]
SUSPICIOUS = [
    (r"western\s*union|moneygram", "Asks for payment via Western Union / MoneyGram"),
    (r"(?:ik|wij|we)\s+(?:woon|wonen|zit|zitten|ben|zijn)\s+(?:nu\s+)?(?:in het )?buitenland|i am (?:currently )?(?:living |working )?abroad|currently abroad",
     "Landlord says they are abroad"),
    (r"sleutel\w*\s+(?:per|via de|met de)\s+post|keys? (?:by|via) (?:post|mail|courier)", "Keys sent by post"),
    (r"(?:betaal|betaling|overmaken)\w*\s+(?:voor|vóór)\s+(?:de\s+)?bezichtiging|pay\w*\s+before (?:the )?viewing", "Payment before a viewing"),
    (r"airbnb\s+(?:payment|betaling)|via airbnb", "Payment via Airbnb outside the platform"),
]
OUTDOOR = [(r"\btuin\b|\bgarden\b|achtertuin|voortuin", "garden"), (r"dakterras|roof terrace", "roof terrace"),
           (r"balkon|balcony", "balcony"), (r"\bterras\b|\bterrace\b|patio", "terrace")]


def _label(text):
    for m in LABEL_RE.finditer(text):
        lab = next(g for g in m.groups() if g)
        return lab.upper()
    return None


def read(text, rent=None):
    out = {"flags": [], "suspicious": []}
    if not text:
        return out
    low = text.lower()
    out["energyLabel"] = _label(text)
    dm = re.search(r"(?:borg|waarborgsom|deposit)\D{0,25}?(\d(?:[.,]\d)?|een|één|twee|drie|vier)\s*(?:x\s*)?(?:maand|month)", low) or \
         re.search(r"(\d(?:[.,]\d)?|een|één|twee|drie|vier)\s*(?:maand|maanden|months?)\s*(?:huur\s*)?(?:borg|waarborgsom|deposit)", low)
    if dm:
        w = dm.group(1)
        months = {"een": 1, "één": 1, "twee": 2, "drie": 3, "vier": 4}.get(w) or float(w.replace(",", "."))
        if 0 < months <= 6:
            out["depositMonths"] = months
            if rent:
                out["deposit"] = round(months * rent)
    else:
        de = re.search(r"(?:borg|waarborgsom|deposit)\D{0,20}?€\s?(\d{1,2}[.,]?\d{3}|\d{3,4})", low)
        if de:
            out["deposit"] = int(re.sub(r"[.,]", "", de.group(1)))
            if rent:
                out["depositMonths"] = round(out["deposit"] / rent, 1)
    seen = set()
    for key, label, pen, pats in FLAGS:
        if key == "income3" and "income4" in seen:
            continue
        if key == "deposit3":
            if (out.get("depositMonths") or 0) >= 3:
                out["flags"].append({"key": key, "label": f"Deposit of {out['depositMonths']:g} months", "penalty": pen})
            continue
        if any(re.search(p, low) for p in pats):
            seen.add(key)
            out["flags"].append({"key": key, "label": label, "penalty": pen})
    for pat, label in SUSPICIOUS:
        if re.search(pat, low):
            out["suspicious"].append(label)
    out["outdoor"] = sorted({name for pat, name in OUTDOOR if re.search(pat, low)}) or None
    return out
