#!/usr/bin/env python3
"""Hamtar viltobservationer runt Hofors-Falun och skriver data.js.

Kors utan argument:  python hamta.py

Tva kallor:
  Skandobs  - varg, lo, bjorn, jarv, kungsorn. Realtid, med valideringsstatus
              fran Lansstyrelsen. Odokumenterat API - se NOT nedan.
  GBIF      - Artportalen-data for alg och ovrigt jaktbart vilt. Oppet API,
              ingen nyckel, men eftersalpning pa nagra dagar mot Artportalen.

NOT om Skandobs: API:t ar inte publicerat och kan andras utan forvarning.
Skriptet avbryter med ett tydligt fel om svaret inte ser ut som vantat,
i stallet for att tyst skriva en tom sida.
"""

import json
import math
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import date, datetime, timedelta

# --- Omradet -----------------------------------------------------------------
# Mittpunkt mellan Hofors (60.552, 16.283) och Falun (60.606, 15.626).
CENTRUM_LAT = 60.58
CENTRUM_LON = 15.95
RADIE_KM = 60

# Hur langt bakat vi hamtar.
ANTAL_DAGAR = 365

# GBIF-arter utover rovdjuren. Nyckel = vetenskapligt namn, varde = svenskt.
GBIF_ARTER = {
    "Alces alces": "Älg",
    "Capreolus capreolus": "Rådjur",
    "Sus scrofa": "Vildsvin",
    "Cervus elaphus": "Kronhjort",
    "Dama dama": "Dovhjort",
    "Vulpes vulpes": "Räv",
    "Castor fiber": "Bäver",
    "Lepus timidus": "Skogshare",
    "Tetrao urogallus": "Tjäder",
    "Lyrurus tetrix": "Orre",
    "Meles meles": "Grävling",
    "Martes martes": "Mård",
    "Nyctereutes procyonoides": "Mårdhund",
}

# Skandobs artnamn (engelska i API:t) -> svenska. Nycklarna ar gemener:
# API:t ar inkonsekvent med versaler ("Wolf" men "wolverine").
SKANDOBS_ARTER = {
    "wolf": "Varg",
    "bear": "Björn",
    "lynx": "Lo",
    "wolverine": "Järv",
    "golden eagle": "Kungsörn",
}

SKANDOBS_AKTIVITET = {
    "tracks": "Spår",
    "visual observation": "Synobservation",
    "droppings": "Spillning",
    "dead animal": "Dött djur",
    "dead or injured prey": "Dödat eller skadat bytesdjur",
    "hair": "Hår",
    "other": "Övrigt",
}

SKANDOBS_VALIDERING = {
    "verified": "Verifierad av Länsstyrelsen",
    "not judged": "Ej bedömd",
    "will not be judged": "Bedöms ej",
    "documented": "Dokumenterad",
    "uncertain": "Osäker",
    "not possible to judge": "Gick ej att bedöma",
    "other species": "Bedömd som annan art",
}

SKANDOBS_API = "https://www.skandobs.no/skandobsAPI/"
GBIF_API = "https://api.gbif.org/v1/occurrence/search"

TIMEOUT = 60


def bbox(lat, lon, radie_km):
    """Ruta som omsluter cirkeln. Vi filtrerar pa faktisk radie efterat."""
    dlat = radie_km / 111.32
    dlon = radie_km / (111.32 * math.cos(math.radians(lat)))
    return {
        "syd": lat - dlat,
        "nord": lat + dlat,
        "vast": lon - dlon,
        "ost": lon + dlon,
    }


def avstand_km(lat1, lon1, lat2, lon2):
    """Haversine."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def hamta(url, data=None, headers=None):
    ctx = ssl.create_default_context()
    req = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as svar:
        return json.loads(svar.read().decode("utf-8"))


# --- Skandobs ----------------------------------------------------------------

def hamta_skandobs(ruta, fran, till):
    """Rovdjursobservationer. Returnerar lista, kastar RuntimeError vid fel.

    Body-formatet ar egendomligt: searchCriteria ar nastlad i sig sjalv.
    Det ar inte ett misstag har - API:t svarar 500 utan den nastlingen.
    """
    sok_id = str(uuid.uuid4())
    anonym_anvandare = "00000000-0000-0000-0000-000000000000"

    kriterier = {
        "species": "", "speciesID": "",
        "fromDate": f"{fran.year}-{fran.month}-{fran.day}",
        "toDate": f"{till.year}-{till.month}-{till.day}",
        "country": "", "county": "", "municipality": "", "region": "",
        "countExpr": "", "count": "", "age": "", "sex": "", "activity": "",
        "validstatus": "", "affiliation": "",
        "myObservations": False, "hasMedia": False,
        "searchPeriod": "select", "observationId": "",
    }
    body = {
        "searchCriteria": {"searchCriteria": [kriterier]},
        "currentPosition": {
            "currentPos": {"lat": CENTRUM_LAT, "lng": CENTRUM_LON},
            "northEast": {"lat": ruta["nord"], "lng": ruta["ost"]},
            "northWest": {"lat": ruta["nord"], "lng": ruta["vast"]},
            "southEast": {"lat": ruta["syd"], "lng": ruta["ost"]},
            "southWest": {"lat": ruta["syd"], "lng": ruta["vast"]},
        },
    }

    # API:t ger 20 per sida och rapporterar totalen i NumberOfObservations,
    # men bara pa forsta sidan - efterat star det 0. Darfor sparas totalen.
    obsar = []
    totalt = None
    sida = 0
    while sida < 200:
        url = (f"{SKANDOBS_API}Area/API_Observations_Select/"
               f"{sok_id}/{anonym_anvandare}/2/false/{sida}/")
        try:
            svar = hamta(
                url,
                data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json; charset=utf-8"},
            )
        except urllib.error.HTTPError as fel:
            raise RuntimeError(
                f"Skandobs svarade {fel.code}. API:t ar odokumenterat och kan "
                f"ha andrats. Sidan byggs inte om med halva data."
            ) from fel
        except urllib.error.URLError as fel:
            raise RuntimeError(f"Nadde inte Skandobs: {fel.reason}") from fel

        if "Observations" not in svar:
            raise RuntimeError(
                "Skandobs svarade utan faltet 'Observations'. Formatet har "
                f"andrats. Fick nycklarna: {sorted(svar)}"
            )

        if totalt is None:
            totalt = svar.get("NumberOfObservations") or 0

        parti = svar["Observations"]
        if not parti:
            break
        obsar.extend(parti)
        if totalt and len(obsar) >= totalt:
            break
        sida += 1

    # Samma observation kan komma tillbaka pa flera sidor om nagon rapporterar
    # medan vi bladdrar. Nyckeln ar unik per observation.
    unika = {}
    for o in obsar:
        unika[o.get("observationID")] = o
    return list(unika.values())


def tolka_skandobs(raa):
    ut = []
    for o in raa:
        lat, lon = o.get("latitude"), o.get("longitude")
        if lat is None or lon is None:
            continue
        if avstand_km(CENTRUM_LAT, CENTRUM_LON, lat, lon) > RADIE_KM:
            continue

        # Skandobs skickar dd.mm.yyyy.
        try:
            d = datetime.strptime(o["date"], "%d.%m.%Y").date().isoformat()
        except (KeyError, ValueError):
            continue

        engelskt = o.get("speciesName") or ""
        status = o.get("validationStatus") or ""
        ut.append({
            "kalla": "Skandobs",
            "art": SKANDOBS_ARTER.get(engelskt.lower(), engelskt),
            "datum": d,
            "tid": o.get("time") or "",
            "antal": o.get("count") or 1,
            "lat": lat,
            "lon": lon,
            "kommun": o.get("municipalityName") or "",
            "plats": (o.get("localName") or "").strip(),
            "typ": SKANDOBS_AKTIVITET.get((o.get("activity") or "").lower(),
                                         o.get("activity") or ""),
            "status": SKANDOBS_VALIDERING.get(status.lower(), status),
            "verifierad": status.lower() == "verified",
            "lank": f"https://skandobs.se/#showObservation/{o.get('observationID', '')}",
        })
    return ut


# --- GBIF --------------------------------------------------------------------

def hamta_gbif(ruta, fran, till):
    ut = []
    ar_intervall = f"{fran.year},{till.year}" if fran.year != till.year else str(till.year)

    for vetenskapligt, svenskt in GBIF_ARTER.items():
        forskjutning = 0
        while forskjutning < 3000:
            fraga = urllib.parse.urlencode({
                "scientificName": vetenskapligt,
                "country": "SE",
                "decimalLatitude": f"{ruta['syd']},{ruta['nord']}",
                "decimalLongitude": f"{ruta['vast']},{ruta['ost']}",
                "year": ar_intervall,
                "hasCoordinate": "true",
                "limit": 300,
                "offset": forskjutning,
            })
            try:
                svar = hamta(f"{GBIF_API}?{fraga}")
            except urllib.error.URLError as fel:
                print(f"  ! {svenskt}: {fel}", file=sys.stderr)
                break

            for o in svar.get("results", []):
                lat, lon = o.get("decimalLatitude"), o.get("decimalLongitude")
                if lat is None or lon is None:
                    continue
                if avstand_km(CENTRUM_LAT, CENTRUM_LON, lat, lon) > RADIE_KM:
                    continue

                d = o.get("eventDate") or ""
                d = d.split("T")[0][:10]
                if not d or len(d) != 10:
                    continue
                if not (fran.isoformat() <= d <= till.isoformat()):
                    continue

                ut.append({
                    "kalla": "Artportalen",
                    "art": svenskt,
                    "datum": d,
                    "tid": "",
                    "antal": o.get("individualCount") or 1,
                    "lat": lat,
                    "lon": lon,
                    "kommun": o.get("county") or o.get("municipality") or "",
                    "plats": o.get("locality") or "",
                    "typ": "Rapporterad observation",
                    "status": "",
                    "verifierad": False,
                    "lank": f"https://www.gbif.org/occurrence/{o.get('key', '')}",
                })

            if svar.get("endOfRecords", True):
                break
            forskjutning += 300

        antal = sum(1 for x in ut if x["art"] == svenskt)
        print(f"  {svenskt:<12} {antal}")

    return ut


# --- Huvudprogram ------------------------------------------------------------

def main():
    till = date.today()
    fran = till - timedelta(days=ANTAL_DAGAR)
    ruta = bbox(CENTRUM_LAT, CENTRUM_LON, RADIE_KM)

    print(f"Omrade: {RADIE_KM} km runt {CENTRUM_LAT}, {CENTRUM_LON} (Hofors-Falun)")
    print(f"Period: {fran} till {till}\n")

    print("Skandobs (rovdjur)...")
    try:
        rovdjur = tolka_skandobs(hamta_skandobs(ruta, fran, till))
        print(f"  {len(rovdjur)} observationer")
    except RuntimeError as fel:
        print(f"\nAVBRYTER: {fel}", file=sys.stderr)
        return 1

    print("\nGBIF / Artportalen (alg och ovrigt vilt)...")
    ovrigt = hamta_gbif(ruta, fran, till)

    alla = rovdjur + ovrigt
    alla.sort(key=lambda o: (o["datum"], o["tid"]), reverse=True)

    try:
        with open("jakttider.json", encoding="utf-8") as f:
            jakttider = json.load(f)
    except FileNotFoundError:
        jakttider = {"omrade": "", "kalla": "", "arter": []}

    paket = {
        "uppdaterad": datetime.now().isoformat(timespec="seconds"),
        "centrum": {"lat": CENTRUM_LAT, "lon": CENTRUM_LON},
        "radie_km": RADIE_KM,
        "period": {"fran": fran.isoformat(), "till": till.isoformat()},
        "observationer": alla,
        "jakttider": jakttider,
    }

    # .js och inte .json: en HTML-fil oppnad med dubbelklick far inte lasa en
    # JSON-fil bredvid sig (webblasaren blockar file://), men far ladda ett skript.
    # Kompakt, utan indentering: filen laddas av webblasaren vid varje sidvisning
    # och koordinater med 15 decimaler ar meterprecision vi inte har anda.
    for o in alla:
        o["lat"] = round(o["lat"], 5)
        o["lon"] = round(o["lon"], 5)

    with open("data.js", "w", encoding="utf-8") as f:
        f.write("window.VILTDATA = ")
        json.dump(paket, f, ensure_ascii=False, separators=(",", ":"))
        f.write(";\n")

    print(f"\nSkrev data.js: {len(alla)} observationer totalt.")
    print("Oppna index.html i webblasaren.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
