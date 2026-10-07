"""Data layer: live aircraft, callsign->route lookup, airline logos.

All three sources are free and keyless:

  adsb.lol      /v2/point/{lat}/{lon}/{radius_nm}  -- live traffic
  adsbdb.com    /v0/callsign/{cs}, /v0/aircraft/{hex} -- route + airframe
  logo pack     c0wsaysmoo/plane-tracker-rgb-pi, 16x16 PNGs keyed by ICAO
  kiwi.com      64x64 PNGs keyed by IATA (nicer, used when we know the IATA)

airplanes.live is deliberately not used: it rejects anonymous clients and
asks you to email them about your project first.
"""

from __future__ import annotations

import hashlib
import json
import math
import os

import requests
from PIL import Image

UA = "flightwall-tui/0.1 (personal LED matrix flight display; hobby project)"
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
LOGO_DIR = os.path.join(CACHE_DIR, "logos")
ROUTE_CACHE = os.path.join(CACHE_DIR, "routes.json")

NM_PER_KM = 0.539957
EARTH_R_KM = 6371.0088


# --------------------------------------------------------------------------
# geometry
# --------------------------------------------------------------------------
def haversine_nm(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R_KM * math.asin(math.sqrt(a)) * NM_PER_KM


def bearing_deg(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def elevation_deg(ground_nm: float, alt_ft: float) -> float:
    """Angle above the horizon. 90 deg = directly overhead."""
    alt_nm = (alt_ft or 0) / 6076.12
    if ground_nm <= 0.0001:
        return 90.0
    return math.degrees(math.atan2(alt_nm, ground_nm))


COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
           "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def compass(deg: float) -> str:
    return COMPASS[int((deg % 360) / 22.5 + 0.5) % 16]


EARTH_R_NM = EARTH_R_KM * NM_PER_KM


def cross_track_nm(plat, plon, alat, alon, blat, blon):
    """Signed-magnitude distance from P to the great circle through A and B,
    plus how far along that circle P sits, both in nautical miles."""
    d13 = haversine_nm(alat, alon, plat, plon) / EARTH_R_NM
    t13 = math.radians(bearing_deg(alat, alon, plat, plon))
    t12 = math.radians(bearing_deg(alat, alon, blat, blon))
    xt = math.asin(max(-1.0, min(1.0, math.sin(d13) * math.sin(t13 - t12))))
    at = math.acos(max(-1.0, min(1.0, math.cos(d13) / math.cos(xt))))
    if math.cos(t13 - t12) < 0:
        at = -at
    return abs(xt) * EARTH_R_NM, at * EARTH_R_NM


def route_plausible(ac, route):
    """Does the claimed route match where this aircraft actually is?

    adsbdb keys routes by callsign, but airlines reuse a flight number for
    several legs a day, so the stored route is frequently a *different* leg
    than the one overhead -- a Frontier jet on the ground at Tampa comes back
    as LGA-ATL. Since adsbdb also hands us airport coordinates, we can check
    the claim: an aircraft flying A to B has to be near the great circle
    between them, and between the two ends of it.

    Returns True when we have no coordinates to check against -- absence of
    evidence is not grounds to throw the route away.
    """
    if not route:
        return True
    try:
        alat, alon = route["origin_lat"], route["origin_lon"]
        blat, blon = route["dest_lat"], route["dest_lon"]
    except (KeyError, TypeError):
        return True
    if None in (alat, alon, blat, blon):
        return True

    leg = haversine_nm(alat, alon, blat, blon)
    if leg < 1:
        return True
    # Airliners wander off the great circle for weather and airways; 15% of
    # the leg (floored at 75nm) is comfortably wider than real routings and
    # still an order of magnitude tighter than a wrong-leg mismatch.
    tol = max(75.0, 0.15 * leg)
    xt, at = cross_track_nm(ac["lat"], ac["lon"], alat, alon, blat, blon)
    if xt > tol:
        return False
    # Also reject aircraft sitting well beyond either end of the route, which
    # catches the reverse/onward leg sharing a flight number.
    return -tol <= at <= leg + tol


# --------------------------------------------------------------------------
# aircraft feed
# --------------------------------------------------------------------------
class Aircraft(dict):
    """Thin wrapper so the UI can use attribute-ish access with defaults."""

    @property
    def callsign(self) -> str:
        return (self.get("flight") or "").strip() or self.get("r") or "??????"

    @property
    def icao_airline(self) -> str:
        cs = (self.get("flight") or "").strip().upper()
        if len(cs) >= 4 and cs[:3].isalpha() and any(c.isdigit() for c in cs[3:]):
            return cs[:3]
        return ""

    @property
    def altitude(self):
        a = self.get("alt_baro")
        if a == "ground":
            return 0
        if a is None:
            a = self.get("alt_geom")
        return a

    @property
    def on_ground(self) -> bool:
        return self.get("alt_baro") == "ground"


def fetch_nearby(lat: float, lon: float, radius_nm: int = 25, timeout: float = 8.0):
    """Live aircraft within radius. Raises on network/API failure."""
    url = f"https://api.adsb.lol/v2/point/{lat:.5f}/{lon:.5f}/{int(radius_nm)}"
    r = requests.get(url, headers={"User-Agent": UA}, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    out = []
    for raw in data.get("ac", []):
        if raw.get("lat") is None or raw.get("lon") is None:
            continue
        ac = Aircraft(raw)
        ac["_dist_nm"] = haversine_nm(lat, lon, ac["lat"], ac["lon"])
        ac["_bearing"] = bearing_deg(lat, lon, ac["lat"], ac["lon"])
        ac["_elev"] = elevation_deg(ac["_dist_nm"], ac.altitude or 0)
        out.append(ac)
    return out


def rank(aircraft, mode: str = "overhead", include_ground: bool = False):
    """Sort candidates best-first.

    overhead -- highest elevation angle: what you'd actually crane your neck at.
                A 737 at 4000ft two miles out beats a widebody at 38000ft.
    nearest  -- plain ground distance.
    """
    pool = [a for a in aircraft if include_ground or not a.on_ground]
    if mode == "nearest":
        return sorted(pool, key=lambda a: a["_dist_nm"])
    return sorted(pool, key=lambda a: -a["_elev"])


# --------------------------------------------------------------------------
# route / airline lookup (adsbdb)
# --------------------------------------------------------------------------
ROUTE_CACHE_VERSION = 2  # bumped when airport coordinates were added


class RouteCache:
    """Disk-backed callsign -> route cache, including negative results.

    adsbdb is a volunteer service; we hit it once per callsign, ever.
    """

    def __init__(self, path: str = ROUTE_CACHE):
        self.path = path
        self.data = {}
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path):
            try:
                with open(path) as f:
                    self.data = json.load(f)
            except (json.JSONDecodeError, OSError):
                self.data = {}
        # Drop entries from before the coordinates were stored; without them
        # route_plausible() has nothing to check and would pass everything.
        self.data = {k: v for k, v in self.data.items()
                     if not v or v.get("_v") == ROUTE_CACHE_VERSION}

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.data, f)
        os.replace(tmp, self.path)

    def lookup(self, callsign: str, timeout: float = 8.0):
        cs = (callsign or "").strip().upper()
        if not cs:
            return None
        if cs in self.data:
            return self.data[cs]
        info = None
        try:
            r = requests.get(f"https://api.adsbdb.com/v0/callsign/{cs}",
                             headers={"User-Agent": UA}, timeout=timeout)
            if r.status_code == 200:
                fr = r.json().get("response", {}).get("flightroute") or {}
                airline = fr.get("airline") or {}
                origin = fr.get("origin") or {}
                dest = fr.get("destination") or {}
                info = {
                    "_v": ROUTE_CACHE_VERSION,
                    "airline": airline.get("name"),
                    "airline_icao": airline.get("icao"),
                    "airline_iata": airline.get("iata"),
                    "origin_iata": origin.get("iata_code"),
                    "origin_city": origin.get("municipality"),
                    "origin_name": origin.get("name"),
                    "origin_lat": origin.get("latitude"),
                    "origin_lon": origin.get("longitude"),
                    "dest_iata": dest.get("iata_code"),
                    "dest_city": dest.get("municipality"),
                    "dest_name": dest.get("name"),
                    "dest_lat": dest.get("latitude"),
                    "dest_lon": dest.get("longitude"),
                }
            elif r.status_code == 404:
                info = {}  # known-unknown: cache the miss
        except requests.RequestException:
            return None  # transient, don't poison the cache
        self.data[cs] = info
        self.save()
        return info


# --------------------------------------------------------------------------
# logos
# --------------------------------------------------------------------------
_LOGO_MEM = {}

_PACK_BASE = ("https://raw.githubusercontent.com/c0wsaysmoo/"
              "plane-tracker-rgb-pi/main")

# Kiwi serves stand-in art rather than a 404 when it has no logo for a code:
# a grey aircraft silhouette (5X, FX, ZZ, ...) and, for Southwest, its own
# "K" roundel. Both are identified by content hash and discarded.
_KIWI_PLACEHOLDERS = {
    "c5749468ef41070431eafcf3762fcc85",  # generic plane silhouette
    "054b5b442506149ebe7643a5213116a4",  # kiwi "K" roundel
}


def _logo_urls(icao: str, iata: str):
    # The ICAO pack is tried first: it is keyed by the code we already have
    # from the callsign, it is pre-trimmed for LED matrices, and it spans
    # ~1950 carriers. GitHub caps a folder at 1000 files, hence logo/logo2.
    if icao:
        yield f"pack_{icao}", f"{_PACK_BASE}/logo/{icao}.png", False
        yield f"pack2_{icao}", f"{_PACK_BASE}/logo2/{icao}.png", False
    if iata:
        yield f"kiwi_{iata}", f"https://images.kiwi.com/airlines/64/{iata}.png", True


def get_logo(icao: str, iata: str = "", size: int = 32, strip_bg: bool = True,
             timeout: float = 8.0):
    """Return a square RGBA PIL logo at `size`, or None if nothing is found."""
    key = (icao, iata, size, strip_bg)
    if key in _LOGO_MEM:
        return _LOGO_MEM[key]
    os.makedirs(LOGO_DIR, exist_ok=True)
    img = None
    for name, url, check_placeholder in _logo_urls(icao, iata):
        path = os.path.join(LOGO_DIR, name + ".png")
        miss = path + ".miss"
        if os.path.exists(miss):
            continue
        if not os.path.exists(path):
            try:
                r = requests.get(url, headers={"User-Agent": UA}, timeout=timeout)
            except requests.RequestException:
                continue
            bad = (r.status_code != 200
                   or not r.content.startswith(b"\x89PNG")
                   or (check_placeholder
                       and hashlib.md5(r.content).hexdigest() in _KIWI_PLACEHOLDERS))
            if bad:
                open(miss, "wb").close()  # remember the miss, don't refetch
                continue
            with open(path, "wb") as f:
                f.write(r.content)
        try:
            src = Image.open(path).convert("RGBA")
        except OSError:
            os.remove(path)
            continue
        if strip_bg:
            src = _strip_background(src)
        # Nearest-neighbour when upscaling keeps the pixel art crisp; Lanczos
        # when downscaling keeps the mark readable.
        resample = Image.NEAREST if src.width < size else Image.LANCZOS
        img = _fit_square(src, size, resample)
        break
    _LOGO_MEM[key] = img
    return img


def _strip_background(src, thresh: int = 190):
    """Flood the pale neutral border of a logo to transparent.

    Plenty of the packaged logos ship on a white or light-grey card. A bright
    square is the loudest thing an LED panel can do and it swamps everything
    else in a dark room, so we knock it out from the edges inward.

    Two guards keep it from eating the artwork: it only spreads from the
    border, so white *inside* a mark (Delta's highlights, the jetBlue
    wordmark) survives; and it only matches near-neutral pixels, so coloured
    brand backgrounds (United's blue, Frontier's green) are left alone.
    """
    w, h = src.size
    px = src.load()

    def is_bg(x, y):
        r, g, b, a = px[x, y]
        return a > 0 and min(r, g, b) >= thresh and max(r, g, b) - min(r, g, b) < 24

    corners = [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)]
    if not any(is_bg(x, y) for x, y in corners):
        return src
    src = src.copy()
    px = src.load()
    stack = [(x, y) for x, y in corners if is_bg(x, y)]
    seen = set(stack)
    while stack:
        x, y = stack.pop()
        px[x, y] = (0, 0, 0, 0)
        for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
            if 0 <= nx < w and 0 <= ny < h and (nx, ny) not in seen and is_bg(nx, ny):
                seen.add((nx, ny))
                stack.append((nx, ny))
    return src


def _fit_square(src, size, resample):
    """Scale preserving aspect ratio, centred on a transparent square."""
    src = _autocrop(src)
    w, h = src.size
    scale = min(size / w, size / h)
    nw, nh = max(1, round(w * scale)), max(1, round(h * scale))
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(src.resize((nw, nh), resample), ((size - nw) // 2, (size - nh) // 2))
    return out


def _autocrop(src):
    """Trim fully transparent margin so small logos use every pixel."""
    bbox = src.split()[3].getbbox()
    return src.crop(bbox) if bbox else src


# Fallback brand colours for carriers whose logo we cannot fetch.
BRAND = {
    "AAL": (0xC8, 0x10, 0x2E), "DAL": (0xC8, 0x10, 0x2E), "UAL": (0x00, 0x53, 0xA0),
    "SWA": (0xF9, 0xB1, 0x1D), "JBU": (0x00, 0x33, 0xA0), "FFT": (0x00, 0x8F, 0x3E),
    "NKS": (0xFF, 0xEB, 0x00), "ASA": (0x01, 0x42, 0x6A), "SKW": (0x00, 0x53, 0xA0),
    "RPA": (0x00, 0x3A, 0x70), "ENY": (0xC8, 0x10, 0x2E), "JIA": (0xC8, 0x10, 0x2E),
    "EDV": (0xC8, 0x10, 0x2E), "AAY": (0x00, 0x53, 0xA0), "SCX": (0x00, 0x2D, 0x62),
    "FDX": (0x4D, 0x14, 0x8C), "UPS": (0x35, 0x1C, 0x15), "ACA": (0xD1, 0x1E, 0x2E),
    "BAW": (0x07, 0x5A, 0xAA), "DLH": (0x05, 0x16, 0x4D), "AFR": (0x00, 0x2D, 0x62),
    "KLM": (0x00, 0xA1, 0xDE), "VIR": (0xE1, 0x00, 0x1A), "UAE": (0xD7, 0x1A, 0x21),
}


def dominant_color(img, default=(0xE0, 0xE0, 0xE0)):
    """Most-used opaque, non-near-black/white pixel -- the accent colour."""
    if img is None:
        return default
    counts = {}
    for r, g, b, a in img.convert("RGBA").getdata():
        if a < 200:
            continue
        if max(r, g, b) < 40 or min(r, g, b) > 225:
            continue
        k = (r // 24 * 24, g // 24 * 24, b // 24 * 24)
        counts[k] = counts.get(k, 0) + 1
    if not counts:
        return default
    c = max(counts, key=counts.get)
    # push it up to a display-friendly brightness
    m = max(c) or 1
    boost = min(2.4, 210 / m)
    return tuple(min(255, int(v * boost)) for v in c)


# --------------------------------------------------------------------------
# location
# --------------------------------------------------------------------------
def geolocate(timeout: float = 6.0):
    """Coarse lat/lon from the IP, for first run. Returns (lat, lon, label)."""
    try:
        r = requests.get("https://ipinfo.io/json",
                         headers={"User-Agent": UA}, timeout=timeout)
        r.raise_for_status()
        d = r.json()
        lat, lon = (float(v) for v in d["loc"].split(","))
        label = ", ".join(x for x in (d.get("city"), d.get("region")) if x)
        return lat, lon, label or "IP location"
    except (requests.RequestException, KeyError, ValueError):
        return None
