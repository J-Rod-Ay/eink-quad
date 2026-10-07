"""Everything the board shows that comes from somebody else's server.

Each source is a small cached fetcher. Rules shared by all of them:
  - never raise into the render path; a dead API keeps its last good value
  - remember when that value was fetched, so the screen can say "as of 2:10"
    instead of passing old news off as current
  - no keys, no accounts (the one exception, Claude, is optional)
"""
from __future__ import annotations

import calendar
import email.utils
import html
import json
import math
import os
import re
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

import requests

# A full fake-Chrome UA gets 429 from Yahoo (TLS fingerprint mismatch); bare "Mozilla/5.0" does not.
UA_BROWSER = "Mozilla/5.0"
UA_BOT = "eink-quad/0.1 (personal e-paper dashboard; hobby project)"
HERE = os.path.dirname(os.path.abspath(__file__))


class Cached:
    """value + fetched_at + error, refreshed by whoever asks after ttl."""

    def __init__(self, ttl: float):
        self.ttl = ttl
        self.value = None
        self.at = 0.0
        self.tried = 0.0
        self.error = None
        self.lock = threading.Lock()

    def get(self, fn, force=False):
        with self.lock:
            now = time.time()
            due = force or (now - self.at > self.ttl and now - self.tried > min(120, self.ttl))
            if due:
                self.tried = now
                try:
                    self.value = fn()
                    self.at = time.time()
                    self.error = None
                except Exception as e:   # keep the old value
                    self.error = f"{type(e).__name__}: {e}"[:200]
                    print(f"[source] {fn.__name__}: {self.error}", flush=True)
            return self.value

    def status(self):
        return {"age_s": round(time.time() - self.at) if self.at else None, "error": self.error}


# ============================================================ weather

WMO = {
    0: ("Clear", "sun"), 1: ("Mostly clear", "sun"), 2: ("Partly cloudy", "partly"),
    3: ("Cloudy", "cloud"), 45: ("Fog", "fog"), 48: ("Fog", "fog"),
    51: ("Light drizzle", "rain"), 53: ("Drizzle", "rain"), 55: ("Drizzle", "rain"),
    56: ("Freezing drizzle", "rain"), 57: ("Freezing drizzle", "rain"),
    61: ("Light rain", "rain"), 63: ("Rain", "rain"), 65: ("Heavy rain", "rain"),
    66: ("Freezing rain", "rain"), 67: ("Freezing rain", "rain"),
    71: ("Light snow", "snow"), 73: ("Snow", "snow"), 75: ("Heavy snow", "snow"), 77: ("Snow", "snow"),
    80: ("Showers", "showers"), 81: ("Showers", "showers"), 82: ("Heavy showers", "showers"),
    85: ("Snow showers", "snow"), 86: ("Snow showers", "snow"),
    95: ("Thunderstorms", "storm"), 96: ("Storms, hail", "storm"), 99: ("Storms, hail", "storm"),
}


def wmo(code):
    return WMO.get(int(code or 0), ("—", "cloud"))


@dataclass
class Day:
    date: str            # YYYY-MM-DD
    hi: float
    lo: float
    code: int
    pop: int             # max precip probability %
    sunrise: str = ""
    sunset: str = ""
    uv: float = 0


@dataclass
class Weather:
    temp: float
    feels: float
    code: int
    is_day: bool
    humidity: int
    wind: float
    wind_dir: float
    gust: float
    days: list          # [Day] starting today
    hourly: list        # [(epoch, temp, pop, code)] next 24h
    rain_note: str = ""  # "Rain likely around 4 PM"
    alerts: list = field(default_factory=list)   # [(event, headline, severity)]
    fetched: float = 0.0


def _fetch_weather(lat, lon, units="fahrenheit"):
    r = requests.get("https://api.open-meteo.com/v1/forecast", params={
        "latitude": lat, "longitude": lon,
        "current": "temperature_2m,apparent_temperature,weather_code,is_day,relative_humidity_2m,"
                   "wind_speed_10m,wind_direction_10m,wind_gusts_10m",
        "daily": "temperature_2m_max,temperature_2m_min,weather_code,precipitation_probability_max,"
                 "sunrise,sunset,uv_index_max",
        "hourly": "temperature_2m,precipitation_probability,weather_code",
        "minutely_15": "precipitation",
        "temperature_unit": units, "wind_speed_unit": "mph",
        "timezone": "auto", "forecast_days": 5, "timeformat": "unixtime",
    }, headers={"User-Agent": UA_BOT}, timeout=10)
    r.raise_for_status()
    d = r.json()
    c = d["current"]
    dl = d["daily"]
    days = []
    for i in range(len(dl["time"])):
        days.append(Day(
            date=time.strftime("%Y-%m-%d", time.localtime(dl["time"][i] + 43200)),
            hi=dl["temperature_2m_max"][i], lo=dl["temperature_2m_min"][i],
            code=dl["weather_code"][i], pop=int(dl["precipitation_probability_max"][i] or 0),
            sunrise=_hm(dl["sunrise"][i]), sunset=_hm(dl["sunset"][i]),
            uv=dl["uv_index_max"][i] or 0))
    now = time.time()
    hr = d["hourly"]
    hourly = [(t, hr["temperature_2m"][i], int(hr["precipitation_probability"][i] or 0), hr["weather_code"][i])
              for i, t in enumerate(hr["time"]) if now - 3600 < t < now + 25 * 3600]
    # Nowcast from 15-minute precipitation: is it raining, or when does it start?
    rain_note = ""
    m = d.get("minutely_15") or {}
    pts = [(t, p or 0) for t, p in zip(m.get("time", []), m.get("precipitation", [])) if t > now - 900][:12]
    if pts:
        wet = [t for t, p in pts if p >= 0.1]
        if pts[0][1] >= 0.1:
            dry = next((t for t, p in pts if p < 0.1), None)
            rain_note = f"Raining now · easing by {_hm(dry)}" if dry else "Raining now"
        elif wet:
            rain_note = f"Rain starting around {_hm(wet[0])}"
    if not rain_note:
        nxt = next(((t, p) for t, _, p, _ in hourly if t > now and p >= 50), None)
        if nxt and nxt[0] - now < 12 * 3600:
            rain_note = f"{nxt[1]}% rain chance by {_hm(nxt[0])}"
    return Weather(temp=c["temperature_2m"], feels=c["apparent_temperature"], code=c["weather_code"],
                   is_day=bool(c["is_day"]), humidity=int(c["relative_humidity_2m"]),
                   wind=c["wind_speed_10m"], wind_dir=c["wind_direction_10m"], gust=c["wind_gusts_10m"],
                   days=days, hourly=hourly, rain_note=rain_note, fetched=time.time())


def _fetch_alerts(lat, lon):
    r = requests.get(f"https://api.weather.gov/alerts/active?point={lat:.4f},{lon:.4f}",
                     headers={"User-Agent": UA_BOT + " contact: " + os.environ.get("QUAD_CONTACT", "unset"),
                              "Accept": "application/geo+json"}, timeout=10)
    r.raise_for_status()
    out = []
    rank = {"Extreme": 0, "Severe": 1, "Moderate": 2, "Minor": 3}
    for f in r.json().get("features", []):
        p = f["properties"]
        out.append((p.get("event", ""), p.get("headline", ""), p.get("severity", "")))
    out.sort(key=lambda a: rank.get(a[2], 9))
    return out


def _hm(epoch):
    if not epoch:
        return ""
    t = time.localtime(epoch)
    return f"{t.tm_hour % 12 or 12}:{t.tm_min:02d} {'AM' if t.tm_hour < 12 else 'PM'}"


_weather = Cached(600)
_alerts = Cached(600)


def weather(cfg, force=False) -> Weather | None:
    lat, lon = cfg["lat"], cfg["lon"]
    units = "celsius" if cfg.get("units") == "C" else "fahrenheit"

    def fetch_weather():
        return _fetch_weather(lat, lon, units)

    def fetch_alerts():
        return _fetch_alerts(lat, lon)

    w = _weather.get(fetch_weather, force)
    a = _alerts.get(fetch_alerts, force)
    if w is not None:
        w.alerts = a or []
    return w


# ============================================================ news

WSJ_FEEDS = {
    "world": ("World", "https://feeds.content.dowjones.io/public/rss/RSSWorldNews"),
    "us": ("U.S.", "https://feeds.content.dowjones.io/public/rss/RSSUSnews"),
    "markets": ("Markets", "https://feeds.content.dowjones.io/public/rss/RSSMarketsMain"),
    "tech": ("Tech", "https://feeds.content.dowjones.io/public/rss/RSSWSJD"),
    "business": ("Business", "https://feeds.content.dowjones.io/public/rss/WSJcomUSBusiness"),
    "politics": ("Politics", "https://feeds.content.dowjones.io/public/rss/socialpoliticsfeed"),
    "opinion": ("Opinion", "https://feeds.content.dowjones.io/public/rss/RSSOpinion"),
}


JUNK = re.compile(r"Roundup: Market Talk|^Best .*(Accounts|Rates|Cards)|Savings Accounts|CD Rates|Mortgage Rates Today|^WSJ (News|Podcast)|^The Journal\.|Crossword|^Opinion \|", re.I)


@dataclass
class Headline:
    title: str
    section: str
    published: float
    summary: str = ""
    link: str = ""


def _clean(s):
    s = html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()
    return re.sub(r"\s+", " ", s)


def _fetch_feed(key):
    label, url = WSJ_FEEDS[key]
    r = requests.get(url, headers={"User-Agent": UA_BROWSER}, timeout=10)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    out = []
    for it in root.iter("item"):
        title = _clean(it.findtext("title"))
        if not title or JUNK.search(title):
            continue
        try:
            pub = email.utils.parsedate_to_datetime(it.findtext("pubDate")).timestamp()
        except Exception:
            pub = 0
        out.append(Headline(title=title, section=label, published=pub,
                            summary=_clean(it.findtext("description"))[:300],
                            link=(it.findtext("link") or "").strip()))
    return out


_feeds: dict[str, Cached] = {}


def headlines(cfg, force=False) -> list[Headline]:
    """Newest first across the chosen feeds, de-duplicated, within max_age_h."""
    items = []
    for key in cfg.get("feeds", ["world", "us", "markets"]):
        if key not in WSJ_FEEDS:
            continue
        c = _feeds.setdefault(key, Cached(900))

        def fetch(key=key):
            return _fetch_feed(key)
        fetch.__name__ = f"feed_{key}"
        items += c.get(fetch, force) or []
    cutoff = time.time() - cfg.get("max_age_h", 18) * 3600
    seen, out = set(), []
    for h in sorted(items, key=lambda h: -h.published):
        k = re.sub(r"\W+", "", h.title.lower())[:60]
        if k in seen or (h.published and h.published < cutoff):
            continue
        seen.add(k)
        out.append(h)
    return out


def news_status():
    return {k: c.status() for k, c in _feeds.items()}


# ============================================================ markets

TICKER_NAMES = {"^GSPC": "S&P 500", "^DJI": "Dow", "^IXIC": "Nasdaq", "^RUT": "Russell",
                "^VIX": "VIX", "^NDX": "Nasdaq 100", "BTC-USD": "Bitcoin", "CL=F": "Oil",
                "GC=F": "Gold", "^TNX": "10Y"}


@dataclass
class Quote:
    symbol: str
    name: str
    price: float
    prev: float
    spark: list          # intraday closes
    state: str           # "open" | "closed"
    at: float

    @property
    def chg_pct(self):
        return (self.price / self.prev - 1) * 100 if self.prev else 0.0


def _fetch_quotes(syms):
    """All tickers in ONE request (the spark endpoint). Per-symbol chart calls
    get 429'd quickly; one call per refresh does not."""
    last = None
    for host in ("query1", "query2"):
        try:
            r = requests.get(f"https://{host}.finance.yahoo.com/v8/finance/spark",
                             params={"symbols": ",".join(syms), "range": "1d", "interval": "5m"},
                             headers={"User-Agent": UA_BROWSER}, timeout=10)
            r.raise_for_status()
            data = r.json()
            break
        except Exception as e:
            last = e
    else:
        raise last
    now = time.time()
    out = {}
    for sym in syms:
        d = data.get(sym)
        if not d or d.get("fulldayPrice") is None:
            continue
        closes = [c for c in d.get("close") or [] if c is not None]
        ts = d.get("timestamp") or [now]
        # "open" = last print is recent and the session hasn't ended
        state = "open" if _us_market_open(now) else "closed"
        if sym.endswith("-USD"):
            state = "open"
        out[sym] = Quote(symbol=sym, name=TICKER_NAMES.get(sym, sym), price=d["fulldayPrice"],
                         prev=d.get("previousClose") or d.get("chartPreviousClose") or 0,
                         spark=closes, state=state, at=ts[-1])
    if not out:
        raise RuntimeError("no quotes in response")
    return out


def _us_market_open(now):
    t = time.localtime(now)   # TZ is America/New_York (set by app.py)
    mins = t.tm_hour * 60 + t.tm_min
    return t.tm_wday < 5 and 570 <= mins < 960


_quotes = Cached(300)


def quotes(cfg, force=False) -> list[Quote]:
    syms = [s for s in cfg.get("tickers", []) if s]

    def fetch_quotes():
        return _fetch_quotes(syms)
    got = _quotes.get(fetch_quotes, force or (_quotes.value is not None and set(syms) - set(_quotes.value))) or {}
    return [got[s] for s in syms if s in got]


def quotes_status():
    return _quotes.status()


# ============================================================ sports

# (key, label, sport, league, espn team id)
TEAMS = {
    "bucs": ("Bucs", "football", "nfl", "tb"),
    "lightning": ("Lightning", "hockey", "nhl", "tb"),
    "rays": ("Rays", "baseball", "mlb", "tb"),
    "gators_fb": ("Gators", "football", "college-football", "57"),
    "gators_bb": ("Gators Hoops", "basketball", "mens-college-basketball", "57"),
    "gators_wbb": ("Gators WBB", "basketball", "womens-college-basketball", "57"),
}


@dataclass
class Game:
    team: str            # our label, "Bucs"
    abbr: str            # our abbreviation
    opp: str             # "MIN"
    opp_name: str        # "Vikings"
    home: bool
    start: float
    state: str           # pre | in | post
    detail: str          # "Final", "Bot 7th", "2nd 12:34"
    us: int | None = None
    them: int | None = None
    tv: str = ""
    record: str = ""
    note: str = ""       # "Wild Card Game 1", "Week 5"
    time_tbd: bool = False
    our_logo: str = ""
    opp_logo: str = ""
    event_id: str = ""
    odds: "Odds | None" = None

    @property
    def ats(self):
        """Against the spread, for finals: covered | missed | push | ''."""
        if self.state != "post" or self.us is None or not self.odds or self.odds.spread_us is None:
            return ""
        m = (self.us - self.them) + self.odds.spread_us
        return "covered" if m > 0 else ("missed" if m < 0 else "push")
    our_rank: int | None = None
    opp_rank: int | None = None

    @property
    def result(self):
        if self.state != "post" or self.us is None:
            return ""
        return "W" if self.us > self.them else ("L" if self.us < self.them else "T")


def _espn(url, params=None):
    # site.api 403s a browser UA; plain requests UA is fine.
    r = requests.get(url, params=params, headers={"User-Agent": UA_BOT}, timeout=10)
    r.raise_for_status()
    return r.json()


def _parse_event(key, e, record=""):
    label = TEAMS[key][0]
    tid = TEAMS[key][3]
    comp = e["competitions"][0]
    us = them = None
    ours = theirs = None
    for c in comp["competitors"]:
        t = c.get("team", {})
        if str(t.get("id")) == tid or t.get("abbreviation", "").lower() == tid:
            ours = c
        else:
            theirs = c
    if ours is None:
        ours, theirs = comp["competitors"][0], comp["competitors"][-1]

    def score(c):
        s = c.get("score")
        if isinstance(s, dict):
            s = s.get("displayValue", s.get("value"))
        try:
            return int(float(s))
        except (TypeError, ValueError):
            return None

    def rank(c):
        r = (c.get("curatedRank") or {}).get("current")
        return r if r and r <= 25 else None

    st = (comp.get("status") or e.get("status") or {}).get("type", {})
    tv = ""
    for b in comp.get("broadcasts") or []:
        nm = (b.get("media") or {}).get("shortName") or ""
        if nm and (b.get("market", {}).get("type") in ("National", None) or not tv):
            tv = nm
            if b.get("type", {}).get("shortName") == "TV":
                break
    note = ""
    for n in comp.get("notes") or []:
        note = n.get("headline", "")
    ot = theirs.get("team", {}) if theirs else {}

    def logo(c):
        t = (c or {}).get("team", {})
        ls = t.get("logos") or []
        return (ls[0].get("href") if ls else t.get("logo")) or ""
    start = calendar.timegm(time.strptime(e["date"][:16], "%Y-%m-%dT%H:%M"))
    return Game(team=label, abbr=(ours.get("team", {}).get("abbreviation") or ""),
                opp=ot.get("abbreviation") or "TBD", opp_name=ot.get("shortDisplayName") or ot.get("name") or "TBD",
                home=ours.get("homeAway") == "home", start=start, state=st.get("state", "pre"),
                detail=st.get("shortDetail", ""), us=score(ours) if st.get("state") != "pre" else None,
                them=score(theirs) if theirs and st.get("state") != "pre" else None,
                tv=tv, record=record, note=note, time_tbd=(e.get("timeValid") is False), our_rank=rank(ours), opp_rank=rank(theirs) if theirs else None,
                our_logo=logo(ours), opp_logo=logo(theirs) if ot.get("abbreviation") not in (None, "TBD") else "",
                event_id=str(e.get("id", "")))


def _fetch_team(key):
    label, sport, league, tid = TEAMS[key]
    base = f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/teams/{tid}"
    t = _espn(base)["team"]
    rec = ""
    try:
        rec = t["record"]["items"][0]["summary"]
    except Exception:
        pass
    team_logo = next((l["href"] for l in t.get("logos") or [] if "default" in (l.get("rel") or [])),
                     (t.get("logos") or [{}])[0].get("href", ""))
    games = [_parse_event(key, e, rec) for e in t.get("nextEvent") or []]
    # Schedule for the last result. Try postseason too: team pages drop the
    # regular-season schedule's playoff games into a separate seasontype.
    for st in ("", "3"):
        try:
            sched = _espn(base + "/schedule", {"seasontype": st} if st else None)
            for e in sched.get("events", []):
                games.append(_parse_event(key, e, rec))
        except Exception:
            pass
    uniq = {}
    for g in games:
        uniq[(round(g.start / 60), g.opp)] = g
    out = sorted(uniq.values(), key=lambda g: g.start)
    for g in out:
        g.our_logo = team_logo or g.our_logo
    return out


_teams: dict[str, Cached] = {}


def _fetch_team_news(key):
    """[(epoch, headline)] newest first: this team's own stories from the last 5 days.
    The feed mixes in league-wide roundups (power rankings, bracketology: 30-88 teams tagged)
    and video clips; drop both. College stories tag team + university under one teamId."""
    _, sport, league, tid = TEAMS[key]
    d = _espn(f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/news", {"team": tid, "limit": 15})
    out = []
    for a in d.get("articles", []):
        if a.get("type") == "Media" or not a.get("headline"):
            continue
        ids = {c.get("teamId") for c in a.get("categories", []) if c.get("type") == "team"}
        if len(ids) > 2:
            continue
        try:
            ts = calendar.timegm(time.strptime(a["published"][:19], "%Y-%m-%dT%H:%M:%S"))
        except Exception:
            continue
        if time.time() - ts < 5 * 86400:
            out.append((ts, html.unescape(a["headline"]).strip()))
    out.sort(reverse=True)
    return out


_team_news: dict[str, Cached] = {}


def team_news(key, force=False):
    c = _team_news.setdefault(key, Cached(1800))

    def fetch():
        return _fetch_team_news(key)
    fetch.__name__ = f"news_{key}"
    return c.get(fetch, force) or []


def team_games(key, fast=False, force=False):
    c = _teams.setdefault(key, Cached(900))
    c.ttl = 90 if fast else 900

    def fetch():
        return _fetch_team(key)
    fetch.__name__ = f"team_{key}"
    return c.get(fetch, force) or []


@dataclass
class TeamRow:
    key: str
    label: str
    record: str = ""
    live: Game | None = None
    last: Game | None = None     # most recent final
    next: Game | None = None     # next scheduled
    logo: str = ""
    news: str = ""               # latest team-specific headline (sports page)

    @property
    def fresh_result(self):
        """Last game counts as 'last night' if it ended recently."""
        return self.last is not None and time.time() - self.last.start < 40 * 3600

    def sort_key(self):
        now = time.time()
        if self.live:
            return (0, 0)
        if self.fresh_result:
            return (1, -self.last.start)
        if self.next:
            return (2, self.next.start)
        return (3, 0)


@dataclass
class Odds:
    book: str
    details: str               # ESPN's own summary, "TB -1.5" / "TB -142"
    spread_us: float | None    # our spread, signed (negative = we're favoured)
    total: float | None
    ml_us: int | None
    ml_them: int | None
    win_pct: int | None        # vig-free implied chance we win
    at: float = 0.0

    def line(self, abbr, style, sport):
        """What the corner shows. Hockey/baseball are moneyline sports."""
        if style == "winpct" and self.win_pct is not None:
            return f"{self.win_pct}% to win"
        if (style == "moneyline" or sport in ("hockey", "baseball")) and self.ml_us is not None:
            return f"{abbr} {self.ml_us:+d}"
        if self.spread_us is not None:
            return f"{abbr} {self.spread_us:+g}".replace("+0", "PK")
        return self.details


def _implied(ml):
    return abs(ml) / (abs(ml) + 100) if ml < 0 else 100 / (ml + 100)


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _fetch_odds(sport, league, event_id, home):
    d = _espn(f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/summary", {"event": event_id})
    pcs = d.get("pickcenter") or d.get("odds") or []
    if not pcs:
        return None
    o = pcs[0]
    ours = o.get("homeTeamOdds" if home else "awayTeamOdds") or {}
    theirs = o.get("awayTeamOdds" if home else "homeTeamOdds") or {}
    ml_us, ml_them = _num(ours.get("moneyLine")), _num(theirs.get("moneyLine"))
    win = None
    if ml_us and ml_them:
        a, b = _implied(ml_us), _implied(ml_them)
        win = round(100 * a / (a + b))
    sp = _num(o.get("spread"))   # ESPN quotes the spread from the home side
    return Odds(book=(o.get("provider") or {}).get("name", ""), details=o.get("details") or "",
                spread_us=(sp if home else -sp) if sp is not None else None,
                total=_num(o.get("overUnder")),
                ml_us=int(ml_us) if ml_us else None, ml_them=int(ml_them) if ml_them else None,
                win_pct=win, at=time.time())


_odds: dict[str, Cached] = {}


def attach_odds(key, g: Game):
    if not g or not g.event_id or g.opp in ("TBD", ""):
        return
    _, sport, league, _ = TEAMS[key]
    c = _odds.setdefault(g.event_id, Cached(1800))
    c.ttl = {"in": 180, "post": 86400}.get(g.state, 1800)
    if g.state == "post" and c.value is not None:
        c.ttl = 10 ** 9           # a final's line never changes

    def fetch_odds():
        return _fetch_odds(sport, league, g.event_id, g.home)
    fetch_odds.__name__ = f"odds_{key}_{g.event_id}"
    g.odds = c.get(fetch_odds)


def sports(cfg, force=False):
    """[TeamRow] ordered live -> fresh results -> soonest next game, plus the
    keys of teams playing right now (the caller refreshes faster then)."""
    now = time.time()
    rows, live = [], []
    horizon = cfg.get("horizon_days", 21) * 86400
    for key in cfg.get("teams", []):
        if key not in TEAMS:
            continue
        games = team_games(key, fast=key in _live_keys, force=force)
        row = TeamRow(key=key, label=TEAMS[key][0], record=games[0].record if games else "",
                      logo=next((g.our_logo for g in games if g.our_logo), ""))
        row.live = next((g for g in games if g.state == "in"), None)
        done = [g for g in games if g.state == "post"]
        row.last = done[-1] if done else None
        row.next = next((g for g in games if g.state == "pre" and g.start > now - 3 * 3600), None)
        if row.live:
            live.append(key)
        if cfg.get("odds", True):
            try:
                attach_odds(key, row.live)
                if row.fresh_result:
                    attach_odds(key, row.last)
                if row.next and row.next.start - now < 7 * 86400:
                    attach_odds(key, row.next)
            except Exception as e:
                print(f"[odds] {key}: {e}", flush=True)
        if row.live or row.fresh_result or (row.next and row.next.start - now < horizon) or cfg.get("show_idle"):
            rows.append(row)
    rows.sort(key=TeamRow.sort_key)
    # one headline per row, never the same one twice (Gators FB + hoops share team id 57)
    shown = set()
    for row in rows:
        try:
            row.news = next((hl for _, hl in team_news(row.key, force) if hl not in shown), "")
            shown.add(row.news)
        except Exception as e:
            print(f"[news] {row.key}: {e}", flush=True)
    _live_keys.clear()
    _live_keys.update(live)
    return rows, live


_live_keys: set = set()


def sports_status():
    out = {k: c.status() for k, c in _teams.items()}
    if _odds:
        errs = [c.error for c in _odds.values() if c.error]
        ages = [c.status()["age_s"] for c in _odds.values() if c.at]
        out["odds"] = {"age_s": min(ages) if ages else None, "error": errs[0] if errs else None}
    if _team_news:
        errs = [c.error for c in _team_news.values() if c.error]
        ages = [c.status()["age_s"] for c in _team_news.values() if c.at]
        out["news"] = {"age_s": max(ages) if ages else None, "error": errs[0] if errs else None}
    return out


# ============================================================ LLM brief

_brief = Cached(3600)


def brief(cfg, heads, wx, games, force=False) -> str:
    """One line from Claude tying the day together. Optional; empty on failure."""
    if not cfg.get("llm_brief"):
        return ""
    _brief.ttl = cfg.get("llm_brief_min", 60) * 60

    city = cfg.get("city") or "town"

    def llm_brief():
        lines = [f"- {h.section}: {h.title}" for h in heads[:12]]
        w = ""
        if wx:
            d = wx.days[0]
            w = f"Weather in {city}: now {wx.temp:.0f}°, {wmo(wx.code)[0]}; today hi {d.hi:.0f} lo {d.lo:.0f}, rain {d.pop}%. {wx.rain_note}"
            if wx.alerts:
                w += " ALERTS: " + "; ".join(a[0] for a in wx.alerts[:2])
        parts = []
        for r in games:
            if r.live:
                parts.append(f"{r.label} playing now vs {r.live.opp}, {r.live.us}-{r.live.them} {r.live.detail}")
            if r.fresh_result:
                parts.append(f"{r.label} {r.last.result} {r.last.us}-{r.last.them} vs {r.last.opp_name}")
            if r.next:
                ln = f" ({r.next.odds.details}, O/U {r.next.odds.total:g})" if r.next.odds and r.next.odds.total else ""
                parts.append(f"{r.label} next vs {r.next.opp_name} {time.strftime('%a %I:%M %p', time.localtime(r.next.start))}{ln}")
        g = "; ".join(parts)
        prompt = (
            f"You write the one-line briefing on a small e-paper kitchen display in {city}. "
            f"It is {time.strftime('%A %I:%M %p')}. Using ONLY the facts below, write ONE plain sentence, "
            "max 115 characters, no emoji, no quotes, no preamble. Lead with what matters most today: "
            "severe weather beats everything; then a local team playing today/tonight; otherwise the most "
            "consequential news story. Don't restate routine weather unless rain or heat is notable.\n\n"
            f"{w}\nSports: {g}\nHeadlines:\n" + "\n".join(lines))
        env = dict(os.environ)
        out = subprocess.run(
            ["claude", "-p", prompt, "--strict-mcp-config", "--model", cfg.get("llm_model", "claude-haiku-4-5-20251001")],
            capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL, env=env,
            cwd=os.path.join(HERE, "cache"))
        txt = (out.stdout or "").strip().splitlines()
        if out.returncode or not txt:
            raise RuntimeError((out.stderr or out.stdout or "no output")[:150])
        s = txt[-1].strip().strip('"')
        return s[:140]

    return _brief.get(llm_brief, force) or ""


def brief_status():
    return {**_brief.status(), "text": _brief.value}
