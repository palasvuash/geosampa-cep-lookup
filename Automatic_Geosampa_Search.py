import json
import re
import sys
import time

import requests
from pyproj import Transformer

WFS = "https://wms.geosampa.prefeitura.sp.gov.br/geoserver/ows"
ZONING = ("geoportal:zoneamento_2016_map1", "ge_poligono")
CET = ("geoportal:departamento_cet", "ge_poligono")

# BrasilAPI returns this generic point when it has no real location for a CEP
PLACEHOLDER = (-23.5475, -46.63611)

to_utm = Transformer.from_crs(4326, 31983, always_xy=True)

# Titles that GeoSampa ignores (full words and common abbreviations, no accents)
TITLES = {
    "DOUTOR", "DOUTORA", "DR", "DRA",
    "PROFESSOR", "PROFESSORA", "PROF", "PROFA",
    "CORONEL", "CEL", "GENERAL", "GEN", "CAPITAO", "CAP", "MAJOR", "MAJ",
    "TENENTE", "TEN", "SARGENTO", "SGT", "MARECHAL", "MAL", "ALMIRANTE", "ALM",
    "BRIGADEIRO", "BRIG", "COMANDANTE", "CMTE",
    "ENGENHEIRO", "ENG", "ARQUITETO", "ARQ", "DESEMBARGADOR", "DES",
    "JUIZ", "MINISTRO", "MIN", "SENADOR", "SEN", "DEPUTADO", "DEP",
    "VEREADOR", "VER", "PREFEITO", "GOVERNADOR", "GOV", "PRESIDENTE", "PRES",
    "COMENDADOR", "COM", "BARAO", "VISCONDE", "CONDE", "PADRE", "PE",
    "FREI", "MONSENHOR", "MONS", "CONEGO", "CONSELHEIRO", "CONS",
}

STREET_TYPES = {
    "RUA", "R", "AVENIDA", "AV", "ALAMEDA", "AL", "TRAVESSA", "TV", "TRAV",
    "PRACA", "PCA", "LARGO", "LGO", "ESTRADA", "EST", "VIADUTO", "VDT",
    "RODOVIA", "ROD", "BECO", "VIELA", "PARQUE", "PQE",
}


def _plain(word):
    """Uppercase, strip accents and dots, for comparison only."""
    import unicodedata
    w = unicodedata.normalize("NFKD", word).encode("ascii", "ignore").decode()
    return w.upper().replace(".", "").replace(",", "")


def strip_titles(street):
    """'Rua Doutor Carlos Norberto de Sousa Aranha' -> 'Rua Carlos Norberto de Sousa Aranha'"""
    words = street.split()
    out = []
    prefix_done = False
    for i, w in enumerate(words):
        p = _plain(w)
        if i == 0 and p in STREET_TYPES:
            out.append(w)
            continue
        if not prefix_done and p in TITLES:
            continue  # drop titles only at the start of the name, not mid-name
        prefix_done = True
        out.append(w)
    return " ".join(out)


# ---------- Geocoding ----------

def name_variants(street):
    """Spelling variants, stripped-of-titles first, original and abbreviated as fallbacks."""
    seen = []

    def add(s):
        if s and s not in seen:
            seen.append(s)

    stripped = strip_titles(street)
    abbr = re.sub(r"\bDoutor\b", "Dr.", street)
    abbr = re.sub(r"\bProfessor\b", "Prof.", abbr)
    abbr = re.sub(r"\bAvenida\b", "Av.", abbr)

    for base in (stripped, street, abbr):
        add(base)
    for s in list(seen):
        add(s.replace("Sousa", "Souza"))
        add(s.replace("Souza", "Sousa"))
    return seen


def nominatim(addr, cep, number=None):
    headers = {"User-Agent": "private-cep-lookup/0.1"}
    url = "https://nominatim.openstreetmap.org/search"
    city, state = addr.get("city"), addr.get("state")
    neighborhood = addr.get("neighborhood")

    attempts = []
    for v in name_variants(addr.get("street") or ""):
        street_param = f"{number} {v}" if number else v
        attempts.append({"street": street_param, "city": city, "state": state, "country": "Brazil"})
        attempts.append({"q": f"{street_param}, {neighborhood}, {city}, {state}, Brazil"})
    attempts.append({"postalcode": f"{cep[:5]}-{cep[5:]}", "country": "Brazil"})

    for params in attempts:
        params.update({"format": "json", "limit": 1})
        try:
            n = requests.get(url, params=params, headers=headers, timeout=15).json()
        except Exception as e:
            print(f"[nominatim] error: {e}")
            n = []
        if n:
            print(f"[nominatim] matched with: {params}")
            return float(n[0]["lat"]), float(n[0]["lon"]), n[0]["display_name"]
        time.sleep(1.1)  # Nominatim policy: max 1 request per second
    return None


def geocode(cep, number=None):
    cep = "".join(c for c in cep if c.isdigit())
    r = requests.get(f"https://brasilapi.com.br/api/cep/v2/{cep}", timeout=15)
    r.raise_for_status()
    addr = r.json()

    # With a house number, skip BrasilAPI's street-level point and use Nominatim
    if not number:
        coords = (addr.get("location") or {}).get("coordinates") or {}
        if coords.get("latitude"):
            lat, lon = float(coords["latitude"]), float(coords["longitude"])
            if (round(lat, 4), round(lon, 5)) != PLACEHOLDER:
                return lat, lon, addr, "brasilapi"

    res = nominatim(addr, cep, number)
    if not res:
        raise ValueError(f"Could not geocode CEP {cep}: {addr.get('street')}")
    lat, lon, display = res
    addr["nominatim_match"] = display
    return lat, lon, addr, "nominatim"


# ---------- GeoSampa queries ----------

def wfs(layer, cql):
    r = requests.get(WFS, params={
        "service": "WFS", "version": "2.0.0", "request": "GetFeature",
        "typeNames": layer, "outputFormat": "application/json",
        "srsName": "EPSG:31983", "CQL_FILTER": cql,
    }, timeout=30)
    r.raise_for_status()
    return r.json()["features"]


def features_near(pt, layer, geom, radii=(0, 25, 50, 100)):
    """Try an exact point-in-polygon test first, then widen the search radius."""
    for r in radii:
        cql = f"INTERSECTS({geom},{pt})" if r == 0 else f"DWITHIN({geom},{pt},{r},meters)"
        feats = wfs(layer, cql)
        if feats:
            return r, feats
    return None, []


def lookup(cep, number=None):
    lat, lon, addr, source = geocode(cep, number)
    x, y = to_utm.transform(lon, lat)
    pt = f"POINT({x} {y})"

    # Zoning
    z_layer, z_geom = ZONING
    z_radius, z = features_near(pt, z_layer, z_geom)
    zones_found = sorted({
        f["properties"].get("cd_zoneamento_perimetro")
        for f in z if f["properties"].get("cd_zoneamento_perimetro")
    })
    zone = zones_found[0] if len(zones_found) == 1 else None
    law = ""
    if z:
        p = z[0]["properties"]
        law = (f"{p.get('cd_tipo_legislacao_zoneamento')} "
               f"{p.get('cd_numero_legislacao_zoneamento')}/{p.get('an_legislacao_zoneamento')}")

    # CET
    c_layer, c_geom = CET
    c_radius, c = features_near(pt, c_layer, c_geom)
    cet_deps = sorted({
        f["properties"].get("sg_departamento")
        for f in c if f["properties"].get("sg_departamento")
    })

    street = addr.get("street") or ""
    return {
        "address": f"{street}, {addr.get('neighborhood')}, {addr.get('city')}",
        "address_without_titles": strip_titles(street),
        "house_number_used": number,
        "nominatim_match": addr.get("nominatim_match"),
        "lat": lat,
        "lon": lon,
        "geocoder": source,
        "zone": zone,
        "zones_found": zones_found,
        "search_radius_m": z_radius,  # 0 = point is inside a polygon
        "is_zer_exact": z_radius == 0 and any(c_.startswith("ZER") for c_ in zones_found),
        "zer_nearby": any(c_.startswith("ZER") for c_ in zones_found),
        "zoning_law": law,
        "cet_departamento": cet_deps[0] if len(cet_deps) == 1 else None,
        "cet_departamentos_found": cet_deps,
        "cet_search_radius_m": c_radius,
    }


if __name__ == "__main__":
    cep = sys.argv[1] if len(sys.argv) > 1 else input("CEP: ")
    number = sys.argv[2] if len(sys.argv) > 2 else input("House number (Enter to skip): ").strip() or None
    print(json.dumps(lookup(cep, number), indent=2, ensure_ascii=False))