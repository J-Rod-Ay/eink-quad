"""Private iCal feeds: a general calendar (calendar_ics) and TripIt (tripit_ics).

The feed URLs are secrets: they never appear in logs, errors, previews or the
dashboard (app.py masks them). Errors raised here carry no URL."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import requests

from sources import Cached, UA_BOT

try:
    import icalendar
    import recurring_ical_events
except ImportError:          # deps missing: panels say so calmly
    icalendar = recurring_ical_events = None


@dataclass
class CalEvent:
    start: float
    end: float
    title: str
    all_day: bool = False
    location: str = ""
    day: str = ""            # YYYY-MM-DD (local) the event falls on


# Some feed hosts (TripIt among them) answer 500 to library user agents; look like a calendar client.
FEED_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
                              "(KHTML, like Gecko) Version/17.0 Safari/605.1.15 CalendarAgent/1",
                "Accept": "text/calendar, text/plain;q=0.9, */*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9"}


def _get(url, timeout=20, tries=3):
    url = url.strip()
    if url.lower().startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    last = "feed unreachable"
    for i in range(tries):
        try:
            r = requests.get(url, headers=FEED_HEADERS, timeout=timeout, allow_redirects=True)
            if r.status_code == 200 and b"BEGIN:VCALENDAR" in r.content[:2000].upper():
                return r.content
            last = (f"feed returned HTTP {r.status_code}" if r.status_code != 200
                    else "feed did not return a calendar")
            if r.status_code in (401, 403, 404, 410):
                break                     # retrying won't help (bad or revoked link)
        except requests.RequestException as e:
            last = f"feed unreachable ({type(e).__name__})"
        time.sleep(2 * (i + 1))
    raise RuntimeError(last)              # never includes the URL


def _epoch(v):
    if isinstance(v, datetime):
        return v.timestamp() if v.tzinfo else time.mktime(v.timetuple())
    return time.mktime(datetime(v.year, v.month, v.day).timetuple())


def parse_events(raw: bytes, start: datetime, end: datetime) -> list[CalEvent]:
    if icalendar is None:
        raise RuntimeError("icalendar not installed")
    cal = icalendar.Calendar.from_ical(raw)
    out = []
    for ev in recurring_ical_events.of(cal).between(start, end):
        ds, de = ev.get("DTSTART"), ev.get("DTEND")
        if ds is None:
            continue
        s = ds.dt
        all_day = not isinstance(s, datetime)
        e = de.dt if de is not None else (s + timedelta(days=1) if all_day else s)
        st = _epoch(s)
        out.append(CalEvent(st, _epoch(e), str(ev.get("SUMMARY") or "(no title)").strip(), all_day,
                            str(ev.get("LOCATION") or "").strip(),
                            time.strftime("%Y-%m-%d", time.localtime(st)),))
        out[-1].description = str(ev.get("DESCRIPTION") or "")
    out.sort(key=lambda x: (x.start, not x.all_day))
    return out


# ------------------------------------------------------------------ calendar

_cal = {}


def calendar(cfg, days=5, force=False, raw_override=None) -> list[CalEvent] | None:
    urls = [u for u in (cfg.get("calendar_ics") or []) if str(u).strip()]
    if not urls and raw_override is None:
        return None
    key = tuple(urls)
    c = _cal.setdefault(key, Cached(900))

    def fetch_calendar():
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        evs = []
        for raw in (raw_override or [_get(u) for u in urls]):
            evs += parse_events(raw, today, today + timedelta(days=days))
        evs.sort(key=lambda x: (x.start, not x.all_day))
        return evs

    if raw_override is not None:
        return fetch_calendar()
    return c.get(fetch_calendar, force)


def calendar_status(cfg):
    urls = tuple(u for u in (cfg.get("calendar_ics") or []) if str(u).strip())
    c = _cal.get(urls)
    return c.status() if c else {"age_s": None, "error": None if urls else "not set"}


# ------------------------------------------------------------------ TripIt

# IATA -> ICAO airline designator, for live lookups by callsign (UA123 -> UAL123)
ICAO = {"UA": "UAL", "AA": "AAL", "DL": "DAL", "B6": "JBU", "WN": "SWA", "AS": "ASA", "NK": "NKS", "F9": "FFT",
        "G4": "AAY", "HA": "HAL", "SY": "SCX", "MX": "MXY", "LY": "ELY", "BA": "BAW", "VS": "VIR", "LH": "DLH",
        "AF": "AFR", "KL": "KLM", "IB": "IBE", "AC": "ACA", "WS": "WJA", "EK": "UAE", "QR": "QTR", "EY": "ETD",
        "TK": "THY", "LX": "SWR", "OS": "AUA", "SN": "BEL", "AZ": "ITY", "EI": "EIN", "TP": "TAP", "AM": "AMX",
        "CM": "CMP", "AV": "AVA", "LA": "LAN", "JL": "JAL", "NH": "ANA", "CX": "CPA", "SQ": "SIA", "QF": "QFA",
        "FI": "ICE", "SK": "SAS", "AY": "FIN", "U2": "EZY", "FR": "RYR", "YX": "RPA", "OO": "SKW", "9E": "EDV",
        "MQ": "ENY", "OH": "JIA", "YV": "ASH", "QX": "QXE", "PD": "POE", "TS": "TSC", "4O": "AIJ", "Y4": "VOI"}
AIRLINES = {"UA": "United", "AA": "American", "DL": "Delta", "B6": "JetBlue", "WN": "Southwest", "AS": "Alaska",
            "NK": "Spirit", "F9": "Frontier", "LY": "El Al", "BA": "British Airways", "LH": "Lufthansa",
            "AF": "Air France", "KL": "KLM", "AC": "Air Canada", "EK": "Emirates", "TK": "Turkish", "VS": "Virgin Atlantic"}


@dataclass
class Segment:
    kind: str            # flight | hotel | cruise | other
    start: float
    end: float
    title: str
    all_day: bool = False
    airline: str = ""
    iata: str = ""       # airline code, e.g. UA
    number: str = ""     # flight number digits
    origin: str = ""
    dest: str = ""
    name: str = ""       # hotel / cruise line
    ship: str = ""
    embark: str = ""     # cruise ports
    disembark: str = ""
    location: str = ""

    @property
    def flight_no(self):
        return f"{self.iata} {self.number}".strip()

    @property
    def callsign(self):
        return f"{ICAO.get(self.iata, '')}{self.number}" if self.iata in ICAO else ""


@dataclass
class Trip:
    name: str = ""
    start: float = 0.0
    end: float = 0.0
    segments: list = field(default_factory=list)


@dataclass
class Live:
    callsign: str
    lat: float
    lon: float
    alt_ft: int | None
    speed_kt: float | None
    track: float | None
    origin: str = ""
    dest: str = ""
    olat: float | None = None
    olon: float | None = None
    dlat: float | None = None
    dlon: float | None = None
    progress: float | None = None
    place: str = ""
    at: float = 0.0


FLIGHT_RE = re.compile(r"\b([A-Z][A-Z0-9]|[A-Z0-9][A-Z])\s?(\d{1,4})\b")
ROUTE_RE = re.compile(r"\b([A-Z]{3})\s*(?:to|→|->|-)\s*([A-Z]{3})\b")


def _field(desc, *names):
    for n in names:
        m = re.search(rf"(?im)^\s*{n}\s*[:\-]\s*(.+?)\s*$", desc)
        if m:
            return m.group(1)
    return ""


def classify(ev: CalEvent) -> Segment:
    t, desc = ev.title, getattr(ev, "description", "") or ""
    blob = f"{t}\n{desc}"
    seg = Segment("other", ev.start, ev.end, t, ev.all_day, location=ev.location)
    low = blob.lower()
    if "[cruise]" in low or re.search(r"\bcruise\b|\bembark", low):
        seg.kind = "cruise"
        seg.name = re.sub(r"(?i)^\s*(\[cruise\]\s*|cruise:\s*)", "", t).strip()
        seg.ship = _field(desc, "Ship", "Ship name", "Vessel")
        seg.embark = _field(desc, "Embark(?:ation)?(?: port)?", "Depart(?:ure)? port", "From")
        seg.disembark = _field(desc, "Disembark(?:ation)?(?: port)?", "Arriv(?:al|e) port", "To")
        return seg
    m = re.match(r"(?i)\s*check[- ]?(in|out)\s*:\s*(.+)", t)
    if m:
        seg.kind = "hotel"
        seg.name = m.group(2).strip()
        seg.title = "checkout" if m.group(1).lower() == "out" else "checkin"
        return seg
    if "[lodging]" in low or re.search(r"\bhotel\b|lodging", low) and ev.all_day:
        seg.kind = "hotel"
        seg.name = re.sub(r"(?i)^\s*(\[lodging\]\s*|hotel:\s*|check[- ]?in:?\s*)", "", t).strip()
        return seg
    if not ev.all_day:
        fm, rm = FLIGHT_RE.search(t), ROUTE_RE.search(t) or ROUTE_RE.search(desc)
        if fm and rm:
            seg.kind = "flight"
            seg.iata, seg.number = fm.group(1), fm.group(2)
            seg.origin, seg.dest = rm.group(1), rm.group(2)
            seg.airline = AIRLINES.get(seg.iata, "")
            m = re.search(r"(?i)\b([A-Z][\w .'&-]+?)\s*\(" + re.escape(seg.iata) + r"\)", desc)
            if m:
                seg.airline = m.group(1).strip()
            return seg
    if re.search(r"\bhotel\b|\binn\b|resort|lodging", low):
        seg.kind = "hotel"
        seg.name = t
    return seg


def parse_trips(raw: bytes, now=None) -> list[Trip]:
    now = now or time.time()
    start = datetime.fromtimestamp(now) - timedelta(days=1)
    evs = parse_events(raw, start, start + timedelta(days=200))
    trips, segs = [], []
    for e in evs:
        dur = e.end - e.start
        if e.all_day and dur >= 86400 and not re.search(r"(?i)hotel|lodging|cruise|check", e.title + getattr(e, "description", "")):
            trips.append(Trip(re.sub(r"(?i)^trip:\s*", "", e.title), e.start, e.end))
        else:
            segs.append(classify(e))
    # fold Check-in/Check-out pairs into one hotel stay
    merged = []
    for s in segs:
        if s.kind == "hotel" and s.title == "checkout":
            ci = next((x for x in reversed(merged) if x.kind == "hotel" and x.name == s.name and x.title == "checkin"), None)
            if ci:
                ci.end, ci.title = s.start, "stay"
                continue
            s.title = "stay"
        elif s.kind == "hotel" and s.title == "checkin":
            pass
        merged.append(s)
    for s in merged:
        if s.kind == "hotel" and s.title == "checkin":
            s.title = "stay"
    segs = merged
    for s in segs:
        home = next((t for t in trips if t.start - 86400 <= s.start <= t.end + 86400), None)
        if home is None:
            home = Trip(s.name or s.title, s.start, s.end)
            trips.append(home)
        home.segments.append(s)
        home.end = max(home.end, s.end)
    trips = [t for t in trips if t.end > now]
    trips.sort(key=lambda t: t.start)
    for t in trips:
        t.segments.sort(key=lambda s: s.start)
    return trips


_trips = {}
_live = Cached(180)


def trips(cfg, force=False) -> list[Trip] | None:
    url = str(cfg.get("tripit_ics") or "").strip()
    if not url:
        return None
    c = _trips.setdefault(url, Cached(1800))

    def fetch_trips():
        return parse_trips(_get(url))

    return c.get(fetch_trips, force)


def trips_status(cfg):
    url = str(cfg.get("tripit_ics") or "").strip()
    c = _trips.get(url)
    return c.status() if c else {"age_s": None, "error": None if url else "not set"}


def airborne(trips_list, now=None):
    now = now or time.time()
    for t in trips_list or []:
        for s in t.segments:
            if s.kind == "flight" and s.start - 900 <= now <= s.end + 1800:
                return s
    return None


_places = {}


def _place(lat, lon):
    k = (round(lat, 1), round(lon, 1))
    if k in _places:
        return _places[k]
    name = ""
    try:
        r = requests.get("https://nominatim.openstreetmap.org/reverse",
                         params={"lat": lat, "lon": lon, "format": "jsonv2", "zoom": 8},
                         headers={"User-Agent": UA_BOT}, timeout=8)
        if r.status_code == 200:
            a = r.json().get("address") or {}
            town = a.get("city") or a.get("town") or a.get("county") or a.get("state_district") or ""
            region = a.get("state") or a.get("country") or ""
            name = ", ".join(x for x in (town, region) if x)
    except requests.RequestException:
        pass
    _places[k] = name
    return name


def live_position(seg: Segment, force=False) -> Live | None:
    """adsb.lol by callsign (same network the sky panel uses) + adsbdb route coords."""
    cs = seg.callsign
    if not cs:
        return None

    def fetch_live():
        import adsb
        r = requests.get(f"https://api.adsb.lol/v2/callsign/{cs}", headers={"User-Agent": adsb.UA}, timeout=10)
        r.raise_for_status()
        ac = [a for a in r.json().get("ac", []) if a.get("lat") is not None]
        if not ac:
            return None
        a = adsb.Aircraft(ac[0])
        lv = Live(cs, a["lat"], a["lon"], a.altitude, a.get("gs"), a.get("track"), seg.origin, seg.dest, at=time.time())
        info = adsb.RouteCache().lookup(cs) or {}
        if info.get("origin_lat") is not None:
            lv.olat, lv.olon, lv.dlat, lv.dlon = info["origin_lat"], info["origin_lon"], info["dest_lat"], info["dest_lon"]
            done = adsb.haversine_nm(lv.olat, lv.olon, lv.lat, lv.lon)
            left = adsb.haversine_nm(lv.lat, lv.lon, lv.dlat, lv.dlon)
            lv.progress = done / max(1.0, done + left)
        else:
            lv.progress = min(1, max(0, (time.time() - seg.start) / max(1, seg.end - seg.start)))
        lv.place = _place(lv.lat, lv.lon)
        return lv

    fetch_live.__name__ = "flight_live"
    if _live.value is not None and getattr(_live.value, "callsign", "") != cs:
        force = True
    return _live.get(fetch_live, force)
