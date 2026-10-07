"""Watches the sky and keeps a short logbook.

Runs on the server, not the tag. It polls far more often than the display
refreshes, so by the time the device wakes there is already a considered answer
waiting rather than whatever happened to be overhead at that instant.
"""

import threading
import time
from dataclasses import dataclass, field

import adsb
import aircraft
from render import Sighting

POLL_SEC = 60
HISTORY_MAX = 12
HISTORY_TTL = 3 * 3600


@dataclass
class DayStats:
    """What the quiet hours show instead of an empty screen."""
    day: str = ""
    seen: set = field(default_factory=set)
    farthest_city: str = ""
    farthest_iata: str = ""
    farthest_nm: float = 0.0

    @property
    def count(self) -> int:
        return len(self.seen)


class Sky:
    def __init__(self, lat, lon, radius_nm=60, mode="overhead"):
        self.lat, self.lon = lat, lon
        self.radius_nm = radius_nm
        self.mode = mode
        self.routes = adsb.RouteCache()

        self._lock = threading.Lock()
        self.featured = None
        self.history = []          # newest first, route-known only
        self.updated_at = 0.0
        self.heartbeat = 0.0
        self.last_error = None
        self.day = DayStats()
        # Bearing/distance of everything in range, featured or not. The 200x200
        # tag had no room to show it; the 7.5" idle screen plots it as a radar
        # scope, which is what keeps a quiet sky from being a blank one.
        self.radar = []

    # -- public ------------------------------------------------------------

    def snapshot(self):
        with self._lock:
            recent = [s for s in self.history
                      if self.featured is None or s.callsign != self.featured.callsign]
            return self.featured, list(recent), self.updated_at

    def nearby(self):
        with self._lock:
            return list(self.radar)

    def day_stats(self) -> DayStats:
        with self._lock:
            return self.day

    @property
    def stale(self) -> bool:
        return time.time() - self.updated_at > 5 * 60

    def start(self):
        t = threading.Thread(target=self._loop, daemon=True)
        t.start()
        return t

    # -- internals ---------------------------------------------------------

    def _loop(self):
        while True:
            try:
                self._poll()
                self.last_error = None
            except Exception as exc:            # network flake, adsb.lol 5xx
                self.last_error = repr(exc)
            self.heartbeat = time.time()
            time.sleep(POLL_SEC)

    def _poll(self):
        now = time.time()
        planes = adsb.fetch_nearby(self.lat, self.lon, self.radius_nm)
        ranked = adsb.rank(planes, mode=self.mode)

        featured = None
        for ac in ranked:
            s = self._to_sighting(ac, now)
            if featured is None:
                featured = s
            # Prefer a flight we can actually name a destination for; an
            # anonymous blip is the one thing this display cannot make
            # interesting.
            if s.has_route:
                featured = s
                self._remember(s)
                break

        with self._lock:
            self.featured = featured
            self.radar = [(float(ac["_dist_nm"]), float(ac["_bearing"]), ac.callsign)
                          for ac in ranked[:40]]
            self.updated_at = now
            cutoff = now - HISTORY_TTL
            self.history = [h for h in self.history if h.seen_at >= cutoff][:HISTORY_MAX]

    @staticmethod
    def _flight_no(callsign: str, iata: str) -> str:
        """ICAO callsign -> marketed flight number: BAW208 + "BA" -> BA 208.

        Usually right, not guaranteed: codeshares are sold under several
        numbers and a few carriers fly callsigns that differ from the published
        number. Good enough to look up, which is the whole point of showing it.

        The space is not decoration. Plenty of IATA codes contain a digit (F9,
        B6, 9K), so "F91047" is unreadable while "F9 1047" is the flight number
        as everyone writes and searches it.
        """
        if not iata:
            return ""
        digits = "".join(c for c in callsign if c.isdigit())
        return f"{iata} {digits.lstrip('0')}" if digits else ""

    def _progress_and_eta(self, ac, info):
        """How far along the route, and how long is left.

        Both come from the destination coordinates adsbdb hands us, so both are
        None for a flight whose route we could not resolve. Ground speed is the
        current one -- this is a friendly estimate, not a flight plan.
        """
        dlat, dlon = info.get("dest_lat"), info.get("dest_lon")
        olat, olon = info.get("origin_lat"), info.get("origin_lon")
        if dlat is None or dlon is None:
            return None, None, 0.0

        remaining = adsb.haversine_nm(ac["lat"], ac["lon"], dlat, dlon)
        dest_from_home = adsb.haversine_nm(self.lat, self.lon, dlat, dlon)

        progress = None
        if olat is not None and olon is not None:
            total = adsb.haversine_nm(olat, olon, dlat, dlon)
            if total > 50:                       # ignore pointlessly short hops
                progress = max(0.0, min(1.0, 1.0 - remaining / total))

        eta_min = None
        gs = float(ac.get("gs") or 0)
        if gs > 100:
            eta_min = int(round(remaining / gs * 60))

        return progress, eta_min, dest_from_home

    def _to_sighting(self, ac, now) -> Sighting:
        info = self.routes.lookup(ac.callsign) or {}
        if info and not adsb.route_plausible(ac, info):
            info = {}
        progress, eta_min, dest_dist = self._progress_and_eta(ac, info)
        return Sighting(
            callsign=ac.callsign,
            airline=(info.get("airline") or "").strip(),
            origin=(info.get("origin_iata") or "").strip(),
            dest=(info.get("dest_iata") or "").strip(),
            origin_city=(info.get("origin_city") or "").strip(),
            dest_city=(info.get("dest_city") or "").strip(),
            alt_ft=int(ac.altitude or 0),
            speed_kt=float(ac.get("gs") or 0),
            dist_nm=float(ac["_dist_nm"]),
            elev_deg=float(ac["_elev"]),
            seen_at=now,
            flight_no=self._flight_no(ac.callsign,
                                      (info.get("airline_iata") or "").strip()),
            aircraft=aircraft.friendly(ac.get("t") or "", ac.get("desc") or ""),
            bearing=float(ac["_bearing"]),
            progress=progress,
            eta_min=eta_min,
            dest_dist_nm=dest_dist,
        )

    def _tally(self, s: Sighting):
        """Roll the day's totals. Reset on local date change, not on a 24h
        timer, so "today" means what she would mean by it."""
        today = time.strftime("%Y-%m-%d", time.localtime(s.seen_at))
        with self._lock:
            if self.day.day != today:
                self.day = DayStats(day=today)
            self.day.seen.add(s.callsign)
            if s.dest_dist_nm > self.day.farthest_nm and s.dest:
                self.day.farthest_nm = s.dest_dist_nm
                self.day.farthest_city = s.dest_city or s.dest
                self.day.farthest_iata = s.dest

    def _remember(self, s: Sighting):
        self._tally(s)
        with self._lock:
            for i, h in enumerate(self.history):
                if h.callsign == s.callsign:
                    self.history[i] = s
                    break
            else:
                self.history.insert(0, s)
            self.history.sort(key=lambda h: -h.seen_at)
