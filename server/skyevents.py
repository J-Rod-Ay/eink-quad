"""Things worth walking outside for: ISS passes and rocket launches.

The flight card answers "what is over the house right now". This module answers
a rarer question -- "is something about to happen in the sky that you would
want to see" -- and when the answer is yes, it takes the screen for half an
hour. The rest of the time it is silent and the display is a flight tracker,
which is exactly how it was asked for.

The difficulty here is not prediction, it is *visibility*. Both feeds will
happily tell you about an ISS pass in broad daylight or a launch below the
horizon, and a display that says "look northwest" when there is nothing to see
does not merely fail once -- it teaches its owner that the arrow is decorative,
and takes the flight card down with it. So most of what follows is gates:
sunlit-but-observer-in-darkness for the station, and after-dark-and-close-enough
for the rocket.

No API keys anywhere. Celestrak for the orbit, The Space Devs for the launch
manifest, and the geometry is done here with sgp4 plus about sixty lines of
spherical trigonometry rather than pulling in Skyfield and a multi-megabyte
ephemeris to compute two vectors.

Everything in this module is UTC and epoch seconds, end to end. app.py sets TZ
process-wide and the renderer formats local time, but SGP4 wants UTC Julian
dates and mixing the two is the most obvious way to be silently four hours
wrong.
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from dataclasses import dataclass, field

import requests

import adsb

UA = "eink-flight/0.1 (hobby e-paper flight display)"

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
TLE_PATH = os.path.join(CACHE_DIR, "iss_tle.txt")

TLE_URL = "https://celestrak.org/NORAD/elements/gp.php?CATNR=25544&FORMAT=TLE"
LAUNCH_URL = "https://ll.thespacedevs.com/2.2.0/launch/upcoming/"

TLE_REFRESH_SEC = 6 * 3600
TLE_MAX_AGE_SEC = 3 * 86400        # past this, say nothing rather than point wrong
PASS_REFRESH_SEC = 30 * 60
PASS_WINDOW_SEC = 36 * 3600        # longer than a day so the schedule has no hole
REBUILD_SEC = 30

EARTH_R_KM = 6378.137

# sgp4 is a hard requirement for ISS and irrelevant to launches, so a missing
# install disables half the module rather than taking the server down.
try:
    from sgp4.api import Satrec
    HAVE_SGP4 = True
    SGP4_ERROR = ""
except Exception as exc:                        # pragma: no cover
    HAVE_SGP4 = False
    SGP4_ERROR = repr(exc)


# ---------------------------------------------------------------------------
# the thing the renderer draws
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Event:
    kind: str                  # "iss" | "launch"
    start: float               # epoch: ISS peak elevation, or launch NET
    end: float                 # epoch: when the card must stop showing
    title: str                 # "Space Station" | "Falcon 9"
    subtitle: str = ""         # "passes overhead" | "Starlink 12-5"
    bearing: float = 0.0       # what the LOOK arrow points at
    where: str = ""            # "NW"
    height: str = ""           # "overhead" | "high up" | "low on the horizon"
    max_elev: float | None = None
    rise_az: float | None = None
    set_az: float | None = None
    duration_s: int = 0
    distance_nm: float = 0.0
    confidence: str = "confirmed"      # confirmed | expected | approximate
    detail: str = ""


# ---------------------------------------------------------------------------
# geometry -- no dependencies beyond math
# ---------------------------------------------------------------------------

def julian(epoch: float):
    """Unix epoch -> (jd, fr) split the way sgp4 wants it for precision."""
    full = epoch / 86400.0 + 2440587.5
    jd = math.floor(full - 0.5) + 0.5
    return jd, full - jd


def gmst_rad(jd: float, fr: float) -> float:
    """Greenwich mean sidereal time. IAU-82 polynomial; polar motion ignored
    because at this scale it moves the ground track by metres."""
    t = (jd - 2451545.0 + fr) / 36525.0
    secs = (67310.54841
            + (876600.0 * 3600.0 + 8640184.812866) * t
            + 0.093104 * t * t
            - 6.2e-6 * t * t * t)
    return math.radians((secs % 86400.0) / 240.0)


def observer_ecef(lat_deg: float, lon_deg: float, alt_km: float = 0.0):
    """WGS-84 geodetic -> earth-fixed cartesian, km."""
    f = 1.0 / 298.257223563
    e2 = f * (2 - f)
    lat = math.radians(lat_deg)
    lon = math.radians(lon_deg)
    n = EARTH_R_KM / math.sqrt(1 - e2 * math.sin(lat) ** 2)
    return ((n + alt_km) * math.cos(lat) * math.cos(lon),
            (n + alt_km) * math.cos(lat) * math.sin(lon),
            (n * (1 - e2) + alt_km) * math.sin(lat))


def teme_to_ecef(r, theta: float):
    """Rotate about Z by GMST. sgp4 returns TEME; the observer lives in ECEF."""
    c, s = math.cos(theta), math.sin(theta)
    return (r[0] * c + r[1] * s, -r[0] * s + r[1] * c, r[2])


def topocentric(sat_ecef, obs_ecef, lat_deg: float, lon_deg: float):
    """-> (azimuth_deg, elevation_deg, range_km) as seen from the observer."""
    dx = sat_ecef[0] - obs_ecef[0]
    dy = sat_ecef[1] - obs_ecef[1]
    dz = sat_ecef[2] - obs_ecef[2]
    lat, lon = math.radians(lat_deg), math.radians(lon_deg)
    sl, cl = math.sin(lat), math.cos(lat)
    so, co = math.sin(lon), math.cos(lon)

    east = -so * dx + co * dy
    north = -sl * co * dx - sl * so * dy + cl * dz
    up = cl * co * dx + cl * so * dy + sl * dz

    rng = math.sqrt(dx * dx + dy * dy + dz * dz)
    az = math.degrees(math.atan2(east, north)) % 360.0
    el = math.degrees(math.atan2(up, math.hypot(east, north)))
    return az, el, rng


def sun_vector_eci(jd: float, fr: float):
    """Unit vector to the sun. Low-precision almanac formula, good to ~0.01deg,
    which is far better than anything downstream needs."""
    n = (jd - 2451545.0) + fr
    mean_long = math.radians((280.460 + 0.9856474 * n) % 360.0)
    anomaly = math.radians((357.528 + 0.9856003 * n) % 360.0)
    ecl = mean_long + math.radians(1.915 * math.sin(anomaly)
                                   + 0.020 * math.sin(2 * anomaly))
    obl = math.radians(23.439 - 4.0e-7 * n)
    return (math.cos(ecl),
            math.cos(obl) * math.sin(ecl),
            math.sin(obl) * math.sin(ecl))


def sun_elevation(lat_deg: float, lon_deg: float, epoch: float) -> float:
    """Sun altitude in degrees at a point on the ground. Negative is night."""
    jd, fr = julian(epoch)
    s_eci = sun_vector_eci(jd, fr)
    s_ecef = teme_to_ecef(s_eci, gmst_rad(jd, fr))
    lat, lon = math.radians(lat_deg), math.radians(lon_deg)
    up = (math.cos(lat) * math.cos(lon), math.cos(lat) * math.sin(lon), math.sin(lat))
    dot = sum(a * b for a, b in zip(s_ecef, up))
    return math.degrees(math.asin(max(-1.0, min(1.0, dot))))


def sunlit(r_teme, jd: float, fr: float) -> bool:
    """Is the satellite out of Earth's shadow? Cylindrical umbra -- the
    penumbra refinement moves pass edges by seconds and costs clarity."""
    s = sun_vector_eci(jd, fr)
    dot = sum(a * b for a, b in zip(r_teme, s))
    if dot > 0:
        return True                                  # sunward side, always lit
    perp = math.sqrt(max(0.0, sum(a * a for a in r_teme) - dot * dot))
    return perp > EARTH_R_KM


# ---------------------------------------------------------------------------
# ISS
# ---------------------------------------------------------------------------

def parse_tle(text: str):
    """-> (line1, line2) or None. Validated, because a Celestrak error page
    parsed as orbital elements produces confident nonsense."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    for i in range(len(lines) - 1):
        if lines[i].startswith("1 25544") and lines[i + 1].startswith("2 25544"):
            return lines[i], lines[i + 1]
    return None


def _elev_at(sat, obs, lat, lon, epoch):
    jd, fr = julian(epoch)
    err, r, _v = sat.sgp4(jd, fr)
    if err:
        return None, None, None
    ecef = teme_to_ecef(r, gmst_rad(jd, fr))
    az, el, _rng = topocentric(ecef, obs, lat, lon)
    return az, el, r


def find_passes(line1, line2, lat, lon, start, window=PASS_WINDOW_SEC,
                min_elev=25.0, sun_alt_max=-6.0, step=30.0, min_duration=120):
    """Visible ISS passes in [start, start+window].

    "Visible" is doing the heavy lifting: the station must be sunlit, the
    observer must be in darkness, and the pass must clear the rooftops. A
    geometric pass that fails any of those is not an event, it is a
    disappointment waiting to happen.
    """
    if not HAVE_SGP4:
        return []
    sat = Satrec.twoline2rv(line1, line2)
    obs = observer_ecef(lat, lon)

    passes = []
    t = start
    prev_el = None
    rise_t = None
    rise_az = None

    while t <= start + window:
        _az, el, _r = _elev_at(sat, obs, lat, lon, t)
        if el is None:
            t += step
            prev_el = None
            continue

        if prev_el is not None and prev_el < 0 <= el:
            rise_t = t
            rise_az = _az
        elif prev_el is not None and prev_el >= 0 > el and rise_t is not None:
            pk = _refine_peak(sat, obs, lat, lon, rise_t, t)
            if pk is not None:
                pk_t, pk_az, pk_el, pk_r = pk
                duration = t - rise_t
                jd, fr = julian(pk_t)
                if (pk_el >= min_elev
                        and duration >= min_duration
                        and sunlit(pk_r, jd, fr)
                        and sun_elevation(lat, lon, pk_t) < sun_alt_max):
                    passes.append({
                        "peak": pk_t, "az": pk_az, "elev": pk_el,
                        "rise": rise_t, "rise_az": rise_az,
                        "set": t, "set_az": _az,
                        "duration": int(duration),
                    })
            rise_t = None

        prev_el = el
        t += step

    return passes


def _refine_peak(sat, obs, lat, lon, lo, hi, tol=1.0):
    """Ternary search for maximum elevation. The coarse grid can miss the peak
    by 15s, which is the difference between '62 degrees' and '58 degrees'."""
    for _ in range(60):
        if hi - lo < tol:
            break
        m1 = lo + (hi - lo) / 3.0
        m2 = hi - (hi - lo) / 3.0
        _a1, e1, _r1 = _elev_at(sat, obs, lat, lon, m1)
        _a2, e2, _r2 = _elev_at(sat, obs, lat, lon, m2)
        if e1 is None or e2 is None:
            return None
        if e1 < e2:
            lo = m1
        else:
            hi = m2
    t = (lo + hi) / 2.0
    az, el, r = _elev_at(sat, obs, lat, lon, t)
    if el is None:
        return None
    return t, az, el, r


def _height_word(elev: float) -> str:
    if elev >= 60:
        return "overhead"
    if elev >= 35:
        return "high up"
    return "midway up"


def pass_to_event(p, lead_pad=0.0) -> Event:
    mins = max(1, int(round(p["duration"] / 60.0)))
    return Event(
        kind="iss",
        start=p["peak"],
        end=p["set"],
        title="Space Station",
        subtitle="passes overhead" if p["elev"] >= 60 else "passes over",
        bearing=p["az"],
        where=adsb.compass(p["az"]),
        height=_height_word(p["elev"]),
        max_elev=p["elev"],
        rise_az=p["rise_az"],
        set_az=p["set_az"],
        duration_s=p["duration"],
        confidence="confirmed",
        detail=f"appears {rise}, fades {sets} · {mins} min",
    )


# ---------------------------------------------------------------------------
# launches
# ---------------------------------------------------------------------------

# Cape Canaveral SFS and Kennedy Space Center. Both are needed: KSC alone
# returns only the far-future Falcon Heavy and SLS manifest and misses every
# near-term Falcon 9, which fly from SLC-40 at the Cape.
DEFAULT_PADS = "12,27"
CAPE_LAT, CAPE_LON = 28.5, -80.6
PAD_SANITY_NM = 50.0

# A launch is only an event if the time is real. Month- and quarter-precision
# entries are placeholders -- announcing "Falcon Heavy in 30 minutes" from a
# date that means "sometime in October" is exactly the kind of lie that teaches
# somebody to stop reading the screen.
GOOD_PRECISION = {"SEC", "MIN", "HR"}
GOOD_STATUS = {"Go", "TBC"}


def fetch_launches(pads=DEFAULT_PADS, timeout=20.0):
    r = requests.get(LAUNCH_URL, params={
        "limit": 10,
        "mode": "detailed",
        "location__ids": pads,
        "hide_recent_previous": "true",
    }, headers={"User-Agent": UA}, timeout=timeout)
    r.raise_for_status()
    return r.json().get("results", [])


def _iso_epoch(iso):
    if not iso:
        return None
    try:
        import datetime
        return datetime.datetime.fromisoformat(
            iso.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def launch_to_event(rec, lat, lon, max_nm=200.0, allow_day=False):
    """One launch record -> an Event, or None if she could not see it.

    The gates matter more than the parsing. From Tampa the Cape is about 130
    nautical miles away: after dark the vehicle is an unmistakable slow orange
    star climbing out of the east, and in daylight it is nothing at all.
    """
    net = _iso_epoch(rec.get("net"))
    if net is None:
        return None

    status = ((rec.get("status") or {}).get("abbrev") or "").strip()
    if status not in GOOD_STATUS:
        return None
    precision = ((rec.get("net_precision") or {}).get("abbrev") or "").strip().upper()
    if precision not in GOOD_PRECISION:
        return None

    pad = rec.get("pad") or {}
    try:
        pad_lat = float(pad.get("latitude"))
        pad_lon = float(pad.get("longitude"))
    except (TypeError, ValueError):
        return None

    # Belt and braces against the location ids changing under us: a numeric id
    # can be re-pointed by the API, coordinates cannot.
    if adsb.haversine_nm(pad_lat, pad_lon, CAPE_LAT, CAPE_LON) > PAD_SANITY_NM:
        return None

    dist_nm = adsb.haversine_nm(lat, lon, pad_lat, pad_lon)
    if dist_nm > max_nm:
        return None

    sun = sun_elevation(lat, lon, net)
    if sun > 0 and dist_nm > 60 and not allow_day:
        return None

    if dist_nm < 40:
        height = "overhead"
        detail = "straight up from the pad"
    elif dist_nm < 100:
        height = "midway up"
        detail = f"{round(dist_nm * 1.151)} miles away"
    else:
        height = "low on the horizon"
        detail = (f"{round(dist_nm * 1.151)} miles away · "
                  "about 2 minutes after liftoff")

    window = ((_iso_epoch(rec.get("window_end")) or net)
              - (_iso_epoch(rec.get("window_start")) or net))
    if status == "TBC":
        confidence = "expected"
    elif window > 1800:
        confidence = "approximate"
    else:
        confidence = "confirmed"

    rocket = (((rec.get("rocket") or {}).get("configuration") or {})
              .get("name") or "Rocket").strip()
    mission = ((rec.get("mission") or {}).get("name") or "").strip()
    bearing = adsb.bearing_deg(lat, lon, pad_lat, pad_lon)

    return Event(
        kind="launch",
        start=net,
        # Holds are routine, so the card expires on its own rather than leaving
        # a countdown on the wall for a rocket that is still on the pad.
        end=net + 900,
        title=rocket,
        subtitle=mission or (pad.get("location") or {}).get("name", ""),
        bearing=bearing,
        where=adsb.compass(bearing),
        height=height,
        duration_s=int(max(0, window)),
        distance_nm=dist_nm,
        confidence=confidence,
        detail=detail,
    )


# ---------------------------------------------------------------------------
# the poller
# ---------------------------------------------------------------------------

class SkyEvents:
    """Background thread. Everything the request path touches is precomputed.

    next_event() is called from next_wake(), which runs on every request
    including 304s, so it must never do I/O and never block on a lock held
    across a network call.
    """

    def __init__(self, lat, lon, *, iss=True, launch=True, min_elev=25.0,
                 sun_alt_max=-6.0, lead_sec=1800, pads=DEFAULT_PADS,
                 max_nm=200.0, allow_day=False, min_hour=0):
        self.lat, self.lon = lat, lon
        self.iss_on = iss and HAVE_SGP4
        self.launch_on = launch
        self.min_elev = min_elev
        self.sun_alt_max = sun_alt_max
        self.lead_sec = lead_sec
        self.pads = pads
        self.max_nm = max_nm
        self.allow_day = allow_day
        self.min_hour = min_hour

        self._lock = threading.Lock()
        self._events = []
        self._tle = None
        self._tle_at = 0.0
        self._iss_events = []
        self._launch_events = []
        self._iss_next = 0.0
        self._launch_next = 0.0
        self._launch_at = 0.0
        self._launch_backoff = 0
        self.last_error = None
        self.last_launch_status = None

    # -- public -----------------------------------------------------------

    def start(self):
        t = threading.Thread(target=self._loop, daemon=True)
        t.start()
        return t

    def next_event(self, now=None):
        """The soonest event we are currently inside the window of."""
        now = now or time.time()
        with self._lock:
            events = list(self._events)
        for ev in events:
            if ev.start - self.lead_sec <= now < ev.end:
                return ev
        return None

    def upcoming(self, now=None):
        now = now or time.time()
        with self._lock:
            return [e for e in self._events if e.end > now]

    @property
    def status(self):
        now = time.time()
        nxt = self.upcoming(now)
        return {
            "sgp4": HAVE_SGP4,
            "sgp4_error": SGP4_ERROR or None,
            "iss": self.iss_on,
            "launch": self.launch_on,
            "tle_age_s": round(now - self._tle_at, 1) if self._tle_at else None,
            "launch_fetch_age_s": (round(now - self._launch_at, 1)
                                   if self._launch_at else None),
            "launch_last_status": self.last_launch_status,
            "last_error": self.last_error,
            "upcoming": [
                {"kind": e.kind, "title": e.title,
                 "in_s": round(e.start - now), "confidence": e.confidence}
                for e in nxt[:4]
            ],
        }

    # -- internals ---------------------------------------------------------

    def _loop(self):
        while True:
            now = time.time()
            if self.iss_on and now >= self._iss_next:
                self._safely(self._refresh_iss, "iss")
                self._iss_next = now + PASS_REFRESH_SEC
            if self.launch_on and now >= self._launch_next:
                self._safely(self._refresh_launches, "launch")
            self._rebuild()
            time.sleep(REBUILD_SEC)

    def _safely(self, fn, what):
        try:
            fn()
        except Exception as exc:                 # network flake, bad payload
            self.last_error = f"{what}: {exc!r}"

    def _load_tle(self):
        """Disk first, so a restart during a Celestrak outage still works."""
        if self._tle:
            return
        try:
            with open(TLE_PATH) as fh:
                parsed = parse_tle(fh.read())
            if parsed:
                self._tle = parsed
                self._tle_at = os.path.getmtime(TLE_PATH)
        except OSError:
            pass

    def _refresh_iss(self):
        self._load_tle()
        now = time.time()
        if now - self._tle_at > TLE_REFRESH_SEC:
            r = requests.get(TLE_URL, headers={"User-Agent": UA}, timeout=20)
            r.raise_for_status()
            parsed = parse_tle(r.text)
            if parsed:
                self._tle = parsed
                self._tle_at = now
                try:
                    os.makedirs(CACHE_DIR, exist_ok=True)
                    tmp = TLE_PATH + ".tmp"
                    with open(tmp, "w") as fh:
                        fh.write(r.text)
                    os.replace(tmp, TLE_PATH)
                except OSError as exc:
                    self.last_error = f"tle cache: {exc!r}"

        # An orbit we have not refreshed in days will point at the wrong patch
        # of sky with total confidence. Silence is the better failure.
        if not self._tle or time.time() - self._tle_at > TLE_MAX_AGE_SEC:
            self._iss_events = []
            return

        passes = find_passes(self._tle[0], self._tle[1], self.lat, self.lon,
                             time.time(), min_elev=self.min_elev,
                             sun_alt_max=self.sun_alt_max)
        self._iss_events = [pass_to_event(p) for p in passes]

    def _launch_interval(self):
        """Tighten as a launch approaches; never exceed ~6 requests an hour
        against an anonymous limit of about fifteen."""
        nxt = min((e.start for e in self._launch_events), default=None)
        if nxt is None:
            return 2 * 3600
        lead = nxt - time.time()
        if lead < 3600:
            return 600
        if lead < 6 * 3600:
            return 1200
        if lead < 24 * 3600:
            return 3600
        return 2 * 3600

    def _refresh_launches(self):
        now = time.time()
        try:
            records = fetch_launches(self.pads)
            self.last_launch_status = "ok"
            self._launch_backoff = 0
            self._launch_at = now
        except Exception as exc:
            # Serve what we already have and back off. A rate-limit is not a
            # reason to blank a countdown that is already on the wall.
            self.last_launch_status = repr(exc)
            self._launch_backoff = min(self._launch_backoff + 1, 3)
            self._launch_next = now + 3600 * (2 ** self._launch_backoff)
            if now - self._launch_at > 6 * 3600:
                self._launch_events = []
            return

        out = []
        for rec in records:
            ev = launch_to_event(rec, self.lat, self.lon,
                                 max_nm=self.max_nm, allow_day=self.allow_day)
            if ev:
                out.append(ev)
        self._launch_events = out
        self._launch_next = now + self._launch_interval()

    def _rebuild(self):
        """Merge, sort and expire. Runs every 30s so a held rocket's countdown
        disappears without waiting for the next fetch."""
        now = time.time()
        merged = [e for e in (self._iss_events + self._launch_events)
                  if e.end > now]
        if self.min_hour:
            merged = [e for e in merged
                      if int(time.strftime("%H", time.localtime(e.start)))
                      >= self.min_hour]
        merged.sort(key=lambda e: e.start)
        with self._lock:
            self._events = merged


def demo_event(kind, lat, lon, now=None):
    """A synthetic event for eyeballing the screens.

    ISS passes come in bursts separated by quiet fortnights and Cape launches
    only firm up their times days ahead, so without this there is no way to
    look at either card until one happens for real -- by which point it is on a
    wall in another house.
    """
    now = now or time.time()
    if kind == "launch":
        bearing = adsb.bearing_deg(lat, lon, CAPE_LAT, CAPE_LON)
        dist = adsb.haversine_nm(lat, lon, CAPE_LAT, CAPE_LON)
        return Event(
            kind="launch", start=now + 26 * 60, end=now + 41 * 60,
            title="Falcon 9 Block 5", subtitle="Starlink Group 12-5",
            bearing=bearing, where=adsb.compass(bearing),
            height=("low on the horizon" if dist >= 100 else "midway up"),
            distance_nm=dist, detail="about 2 minutes after liftoff")
    return Event(
        kind="iss", start=now + 12 * 60, end=now + 18 * 60,
        title="Space Station", subtitle="passes overhead",
        bearing=315.0, where="NW", height="overhead", max_elev=64.0,
        rise_az=310.0, set_az=140.0, duration_s=600,
        detail="visible for about 10 minutes")


# ---------------------------------------------------------------------------

def _dry_run():
    """Print what we would show, so the orbital maths can be checked against
    heavens-above or NASA's Spot The Station before anyone trusts it."""
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--lat", type=float, default=float(os.environ.get("TAG_LAT", "27.94")))
    ap.add_argument("--lon", type=float, default=float(os.environ.get("TAG_LON", "-82.51")))
    ap.add_argument("--days", type=float, default=14.0)
    ap.add_argument("--min-elev", type=float, default=25.0)
    a = ap.parse_args()

    # Same zone the display renders in, so the times printed here can be read
    # straight across to the panel. Without this the dry run silently prints
    # the server's UTC and an 8pm pass looks like it is after midnight.
    tz = os.environ.get("TAG_TZ", "America/New_York")
    if tz and hasattr(time, "tzset"):
        os.environ["TZ"] = tz
        time.tzset()

    print(f"observer {a.lat}, {a.lon}   sgp4={HAVE_SGP4}   tz={tz}\n")
    r = requests.get(TLE_URL, headers={"User-Agent": UA}, timeout=20)
    tle = parse_tle(r.text)
    if not tle:
        print("no usable TLE")
        return
    print(f"TLE: {tle[0][:50]}\n")

    passes = find_passes(tle[0], tle[1], a.lat, a.lon, time.time(),
                         window=int(a.days * 86400), min_elev=a.min_elev)
    print(f"ISS visible passes, next {a.days:g} days (peak >= {a.min_elev:g} deg):")
    if not passes:
        print("  none -- the ISS pass cycle drifts ~47 min earlier per day, so")
        print("  quiet stretches of one to two weeks are normal, not a fault.")
    for p in passes:
        print("  %-26s peak %2.0f deg  %-4s -> %-4s  %d min" % (
            time.strftime("%a %d %b %I:%M %p", time.localtime(p["peak"])),
            p["elev"], adsb.compass(p["rise_az"]), adsb.compass(p["set_az"]),
            round(p["duration"] / 60)))

    print("\nCape launches:")
    try:
        records = fetch_launches()
    except Exception as exc:
        print(f"  fetch failed: {exc!r}")
        return
    print(f"  {len(records)} returned by the API")
    for rec in records:
        ev = launch_to_event(rec, a.lat, a.lon)
        label = (rec.get("name") or "?")[:44]
        if ev:
            print("  SHOW  %-44s %s  look %s" % (
                label, time.strftime("%a %d %b %I:%M %p", time.localtime(ev.start)),
                ev.where))
        else:
            st = (rec.get("status") or {}).get("abbrev")
            pr = (rec.get("net_precision") or {}).get("abbrev")
            print("  skip  %-44s status=%s precision=%s" % (label, st, pr))


if __name__ == "__main__":
    _dry_run()
