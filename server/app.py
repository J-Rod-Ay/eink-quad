"""Quad board server: renders the 4-panel e-paper dashboard and serves it to
the reTerminal E1001 over the note box's long-poll "reel" protocol.

The device is dumb. It long-polls /api/device/poll; when the board's version
moves on, it gets a reel of changed rectangles and plays them as partial
refreshes. Every layout decision, cadence and rotation lives here, and every
knob is editable from the web dashboard (/?k=<admin token>).

Run: python app.py   (env: see CONFIG below)
"""
from __future__ import annotations

import copy
import importlib.util
import io
import json
import os
import re
import sqlite3
import sys
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.environ.setdefault("TZ", "America/New_York")
if hasattr(time, "tzset"):
    time.tzset()

from PIL import Image, ImageChops  # noqa: E402

import logos  # noqa: E402
import render as R  # noqa: E402
import skyevents  # noqa: E402
import sources as S  # noqa: E402
from sky import Sky  # noqa: E402

# ---------------------------------------------------------------- CONFIG
HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("QUAD_PORT", "8740"))
BIND = os.environ.get("QUAD_BIND", "127.0.0.1")
DEVICE_TOKEN = os.environ.get("QUAD_TOKEN", "dev")
ADMIN_TOKEN = os.environ.get("QUAD_ADMIN", "admin")
CONFIG_PATH = os.environ.get("QUAD_CONFIG", os.path.join(HERE, "config.json"))
STATE_PATH = os.environ.get("QUAD_STATE", os.path.join(HERE, "state.json"))
NOTES_DB = os.environ.get("QUAD_NOTES_DB", "")           # the note box's sqlite, optional
NOTEBOX_DIR = os.environ.get("QUAD_NOTEBOX_DIR", "")     # to borrow its renderer
DASH_HTML = os.path.join(HERE, "web", "dashboard.html")

DEFAULTS = {
    "label": "Home",
    "city": "Tampa",             # named in the Claude brief's prompt
    "lat": 27.9506, "lon": -82.4572,
    "units": "F",
    "slots": ["weather", "news", "sports", "sky"],
    "interval_min": 10,          # the main cadence: every panel refreshes on this beat
    "sky_min": 10,               # plane panel can run faster (partial refresh, no flash)
    "live_fast": True,           # while a team is playing, sports refreshes every live_min
    "live_min": 3,
    "full_every_min": 120,       # one flashing full refresh this often, to wipe ghosting
    "quiet": "",                 # "23-6" = no scheduled updates overnight
    "dark": False,
    "feeds": ["world", "us", "markets"],
    "max_age_h": 18,
    "news_pages": 4,
    "tickers": ["^GSPC", "^DJI", "^IXIC", "SOXL"],
    "teams": ["bucs", "lightning", "rays", "gators_fb", "gators_bb"],
    # your own ESPN teams: {"key": ["Label", "sport", "league", "espn team id or abbr"]},
    # e.g. {"bears": ["Bears", "football", "nfl", "chi"]}; then add "bears" to teams
    "custom_teams": {},
    "horizon_days": 21,
    "show_idle": False,
    "sports_logos": True,
    "odds": True,
    "odds_style": "spread",      # spread | moneyline | winpct (hockey/baseball always moneyline)
    "radar_nm": 25,
    "iss": True, "launch": True,
    "llm_brief": True,
    "llm_brief_min": 60,
    "llm_model": "claude-haiku-4-5-20251001",
    "page_timeout_s": 180,       # detail pages fall back to the quad after this
    "notes": True,               # show note-box notes when they arrive (needs QUAD_NOTES_DB)
    "note_minutes": 10,
    "chime": True,
    # crisp: the regular 10-min update, page changes and the refresh button use a
    # full refresh (one ~3.5s flash, solid blacks). Only in-between nudges (plane
    # corner, live scores, messages) use the fast partial, which leaves blacks
    # lighter and speckled; after max_partials of those the next one goes full.
    "crisp": True,
    "gray": True,               # full refreshes in 4 grey levels (anti-aliased text)
    "max_partials": 4,
}


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1)
    os.replace(tmp, path)


cfg = {**DEFAULTS, **load_json(CONFIG_PATH, {})}
S.TEAMS.update({k: tuple(v) for k, v in cfg.get("custom_teams", {}).items()})


def quiet_now(t=None):
    q = cfg.get("quiet") or ""
    m = re.fullmatch(r"\s*(\d{1,2})\s*-\s*(\d{1,2})\s*", q)
    if not m:
        return False
    a, b = int(m.group(1)), int(m.group(2))
    h = time.localtime(t or time.time()).tm_hour
    return (a <= h < b) if a < b else (h >= a or h < b)


def lipo_percent(v: float) -> int:
    """Seeed's own voltage->% table for the reTerminal E series (ESPHome
    cookbook, wiki.seeedstudio.com/reterminal_e10xx_with_esphome_advanced).
    The board has a charger chip but no fuel gauge, so this is an estimate
    from resting voltage; it reads high while charging / just unplugged."""
    curve = [(3.27, 0), (3.30, 5), (3.41, 10), (3.49, 20), (3.58, 30), (3.68, 40), (3.75, 50),
             (3.80, 60), (3.85, 70), (3.91, 80), (3.96, 90), (4.15, 100)]
    if v <= curve[0][0]:
        return 0
    for (v0, p0), (v1, p1) in zip(curve, curve[1:]):
        if v <= v1:
            return int(round(p0 + (p1 - p0) * (v - v0) / (v1 - v0)))
    return 100


# ---------------------------------------------------------------- sky

sky = None
events = None


def start_sky():
    global sky, events
    sky = Sky(cfg["lat"], cfg["lon"], radius_nm=max(10, int(cfg["radar_nm"])))
    sky.start()
    events = skyevents.SkyEvents(cfg["lat"], cfg["lon"], iss=cfg["iss"], launch=cfg["launch"])
    events.start()


# ---------------------------------------------------------------- note box bridge

nb = None
if NOTEBOX_DIR and os.path.exists(os.path.join(NOTEBOX_DIR, "render.py")):
    try:
        spec = importlib.util.spec_from_file_location("nb_render", os.path.join(NOTEBOX_DIR, "render.py"))
        nb = importlib.util.module_from_spec(spec)
        sys.modules["nb_render"] = nb        # dataclasses look their module up here
        spec.loader.exec_module(nb)
    except Exception as e:
        print(f"[notes] note box renderer unavailable: {e}", flush=True)
        nb = None


def notes_db():
    if not NOTES_DB or not os.path.exists(NOTES_DB):
        return None
    db = sqlite3.connect(NOTES_DB, timeout=5)
    db.row_factory = sqlite3.Row
    return db


def row_note(r):
    doodle = None
    if r["doodle"]:
        try:
            doodle = Image.open(io.BytesIO(r["doodle"])).convert("L")
        except Exception:
            doodle = None
    return nb.Note(id=r["id"], body=r["body"] or "", name=r["name"] or "", stamp=r["stamp"] or "💌",
                   number=r["number"] or 0, created=r["created"], doodle=doodle, hearted=bool(r["hearted_at"]))


# ---------------------------------------------------------------- the board

PAGES = ["quad", "weather", "news", "sports", "sky"]


class Board:
    """What the glass shows, the reel that gets it there, and the data snapshot
    each panel was last drawn from. Panels only take fresh data on their own
    beat, so e.g. the plane corner can move every 2 min while the news corner
    sits still (and costs no refresh)."""

    def __init__(self):
        self.cond = threading.Condition(threading.RLock())
        st = load_json(STATE_PATH, {})
        self.version = st.get("version", 1)
        self.cycle = st.get("cycle", 0)
        self.sticky = st.get("sticky", "")
        self.sticky_until = st.get("sticky_until", 0)
        self.page = "quad"
        self.page_since = 0.0
        self.note = None            # nb.Note while one is on screen
        self.note_until = 0.0
        self.unread = False
        self.chime_version = -1
        self.last_full = time.time()
        self.force_full = False
        self.partials = 0
        self.updated = 0.0
        self.beats = {}             # panel -> last beat index taken
        self.snap = {}              # panel -> data
        self.device = {"last_seen": 0, "vbat": None, "t": None, "rh": None, "rssi": None, "version": 0,
                       "mode": "live", "boots": 0, "polls": 0}
        self.log = []               # recent events for the dashboard
        self.final = Image.new("1", (R.W, R.H), 1)
        self.final_gray = None      # grey rendering of final, when the last full refresh had one
        self.prev_version = self.version
        self.reel = b""
        self.render_ms = 0

    def note_event(self, s):
        self.log.insert(0, (time.time(), s))
        del self.log[40:]
        print(f"[board] {s}", flush=True)

    # -- data
    def take(self, name, force=False):
        """Refresh one panel's data snapshot from the (cached) sources."""
        if name == "weather":
            self.snap["weather"] = S.weather(cfg, force)
        elif name == "news":
            self.snap["heads"] = S.headlines(cfg, force)
            self.snap["quotes"] = S.quotes(cfg, force)
        elif name == "sports":
            teams, live = S.sports(cfg, force)
            if cfg.get("sports_logos"):
                urls = [r.logo for r in teams]
                for r in teams:
                    urls += [g.opp_logo for g in (r.live, r.last, r.next) if g]
                logos.prefetch(urls)
            self.snap["teams"], self.snap["live"] = teams, live
        elif name == "sky":
            featured, recent, _ = sky.snapshot()
            self.snap["featured"], self.snap["recent"] = featured, recent
            self.snap["nearby"] = sky.nearby()
            self.snap["day_count"] = sky.day_stats().count
            self.snap["event"] = events.next_event()
            self.snap["upcoming"] = events.upcoming()
        elif name == "brief":
            try:
                h = time.localtime().tm_hour
                if 6 <= h < 23 or force:
                    self.snap["brief"] = S.brief(cfg, self.snap.get("heads") or [], self.snap.get("weather"),
                                                 self.snap.get("teams") or [], force)
            except Exception as e:
                print(f"[brief] {e}", flush=True)

    def ctx(self, page=None):
        sn = self.snap
        now = time.time()
        sticky = self.sticky if (self.sticky and (not self.sticky_until or now < self.sticky_until)) else ""
        dv = self.device
        batt = lipo_percent(dv["vbat"]) if dv.get("vbat") else None
        t_f = dv.get("t")
        if t_f is not None and cfg.get("units") != "C":
            t_f = t_f * 9 / 5 + 32
        return R.Ctx(now=now, cfg=cfg, weather=sn.get("weather"), heads=sn.get("heads") or [],
                     quotes=sn.get("quotes") or [], teams=sn.get("teams") or [], featured=sn.get("featured"),
                     recent=sn.get("recent") or [], day_count=sn.get("day_count", 0), nearby=sn.get("nearby") or [],
                     event=sn.get("event"), upcoming=sn.get("upcoming") or [], brief=sn.get("brief", ""),
                     sticky=sticky, indoor_f=t_f, indoor_rh=dv.get("rh"), battery=batt,
                     on_usb=dv.get("mode") == "live", cycle=self.cycle, updated=self.updated or now,
                     page=page or self.page)

    # -- commit (call with cond held)
    def _commit(self, frames, why, chime=False):
        """frames: [(img1bit, delay_ms, full)] ending on the new final image."""
        self.prev_version = self.version
        prev = self.final
        self.version += 1
        self.final = frames[-1][0]
        self.final_gray = frames[-1][3] if len(frames[-1]) > 3 else None
        self.reel = R.encode_reel([(prev, 0, False)] + frames)
        if chime and cfg.get("chime") and not quiet_now():
            self.chime_version = self.version
        save_json(STATE_PATH, {"version": self.version, "cycle": self.cycle, "sticky": self.sticky,
                               "sticky_until": self.sticky_until})
        self.note_event(f"v{self.version} {why} ({len(frames)} frame{'s' if len(frames) != 1 else ''}"
                        f"{', full' if any(f[2] for f in frames) else ''})")
        self.cond.notify_all()

    def redraw(self, why, full=False, major=False):
        """Render from the current snapshot; commit if anything changed.
        major = a scheduled whole-board update, page change or button refresh."""
        t0 = time.time()
        if self.note is not None and nb is not None:
            return
        self.updated = time.time()
        new = R.compose(self.ctx())
        # "Updated 3:40" alone is not worth a refresh: compare without the header
        old_body = self.final.crop((0, R.HEAD_H, R.W, R.H))
        new_body = new.crop((0, R.HEAD_H, R.W, R.H))
        changed = ImageChops.logical_xor(old_body, new_body).getbbox() is not None
        self.render_ms = int((time.time() - t0) * 1000)
        full = full or self.force_full or (
            cfg["full_every_min"] and time.time() - self.last_full > cfg["full_every_min"] * 60
            and changed and not quiet_now())
        if changed and cfg.get("crisp") and (major or self.partials >= cfg.get("max_partials", 4)):
            full = True
        if not changed and not full:
            return
        if full:
            self.last_full = time.time()
            self.force_full = False
            self.partials = 0
            if cfg.get("gray"):
                self._commit([(new, 0, True, R.compose_gray(self.ctx()))], why)
            else:
                self._commit([(new, 0, True)], why)
        else:
            regions = R.region_frames(self.final, new)
            # Many changed regions (a page switch) read better as one sweep than
            # as five sequential ~1s partials.
            if len(regions) > 3 or not regions:
                regions = [new]
            self.partials += 1
            self._commit([(f, 0, False) for f in regions], why)

    # -- scheduler (runs every few seconds)
    def tick(self):
        now = time.time()
        with self.cond:
            if self.sticky_until and now >= self.sticky_until and self.sticky:
                self.sticky, self.sticky_until = "", 0
                self.redraw("sticky expired")
            if self.note is not None and now >= self.note_until:
                self.note = None
                self.unread = False
                self.note_event("note dwell over")
                self.redraw("back from note", full=True)
            if self.page != "quad" and now - self.page_since > cfg["page_timeout_s"]:
                self.page = "quad"
                self.redraw("page timeout", major=True)
        self.check_notes()
        if quiet_now():
            return
        iv = max(1, cfg["interval_min"]) * 60
        beat = int(now // iv)
        due = []
        if self.beats.get("main") != beat:
            due = ["weather", "news", "sports", "sky", "brief"]
            self.beats["main"] = beat
            with self.cond:
                self.cycle += 1
        else:
            sb = int(now // (max(1, cfg["sky_min"]) * 60))
            ev = events.next_event() if events else None
            if ev is not None:   # event countdown ticks every minute
                sb = int(now // 60)
            if self.beats.get("sky") != sb:
                due.append("sky")
            live = self.snap.get("live")
            if live and cfg.get("live_fast"):
                lb = int(now // (max(1, cfg["live_min"]) * 60))
                if self.beats.get("live") != lb:
                    due.append("sports")
                    self.beats["live"] = lb
        if "sky" in due:
            self.beats["sky"] = int(now // (max(1, cfg["sky_min"]) * 60))
        if not due:
            return
        for name in due:
            try:
                self.take(name)
            except Exception:
                traceback.print_exc()
        with self.cond:
            self.redraw("scheduled " + ",".join(due), major="weather" in due)

    # -- notes from the note box (optional)
    def check_notes(self):
        if not cfg.get("notes") or nb is None:
            return
        db = notes_db()
        if db is None:
            return
        try:
            r = db.execute("SELECT * FROM notes WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
            if not r:
                return
            with self.cond:
                if self.note is not None and time.time() - (self.note_until - cfg["note_minutes"] * 60) < 45:
                    return   # let the current note breathe before the next
                db.execute("UPDATE notes SET status='shown' WHERE status='showing'")
                db.execute("UPDATE notes SET status='showing', shown_at=? WHERE id=?", (time.time(), r["id"]))
                db.commit()
                note = row_note(r)
                self.note = note
                self.note_until = time.time() + cfg["note_minutes"] * 60
                self.unread = True
                scene = nb.Scene(note=note, mood="happy")
                frames = nb.reel_arrive(self.final, scene)
                self._commit(frames[1:] if len(frames) > 1 else frames, f"note from {note.name or 'someone'}",
                             chime=True)
        except Exception:
            traceback.print_exc()
        finally:
            db.close()

    def heart_note(self):
        db = notes_db()
        if db is None or self.note is None:
            return
        try:
            r = db.execute("SELECT hearted_at FROM notes WHERE id=?", (self.note.id,)).fetchone()
            new = None if (r and r["hearted_at"]) else time.time()
            db.execute("UPDATE notes SET hearted_at=? WHERE id=?", (new, self.note.id))
            db.commit()
            self.note.hearted = bool(new)
            sc = nb.Scene(note=self.note, mood="love" if new else "idle")
            frames = nb.reel_heart(self.final, sc) if new else [(nb.compose(sc), 0, False)]
            self._commit(frames[1:] if len(frames) > 1 else frames, "heart" if new else "unheart")
        finally:
            db.close()

    # -- buttons
    def button(self, b):
        """left = home / refresh (heart on a note); middle = previous page; right = next page."""
        with self.cond:
            self.unread = False
            if self.note is not None:
                if b == 0:
                    return self.heart_note()
                self.note = None
                return self.redraw("note dismissed", full=True)
            if b == 0:
                if self.page != "quad":
                    self.page = "quad"
                    return self.redraw("home", major=True)
                self.note_event("manual refresh")
        if b == 0:
            for name in ("weather", "news", "sports", "sky"):
                try:
                    self.take(name, force=True)
                except Exception:
                    traceback.print_exc()
            with self.cond:
                self.cycle += 1
                self.redraw("refresh button", major=True)
            return
        with self.cond:
            i = PAGES.index(self.page)
            self.page = PAGES[(i + (1 if b == 2 else -1)) % len(PAGES)]
            self.page_since = time.time()
            self.redraw(f"page {self.page}", major=True)

    def goto(self, page):
        with self.cond:
            self.page = page if page in PAGES else "quad"
            self.page_since = time.time()
            self.redraw(f"page {self.page} (dashboard)", major=True)


board = Board()


def scheduler():
    while True:
        try:
            board.tick()
        except Exception:
            traceback.print_exc()
        time.sleep(5)


# ---------------------------------------------------------------- HTTP

class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "quad"

    def log_message(self, fmt, *args):
        if os.environ.get("QUAD_VERBOSE"):
            print("%s %s" % (time.strftime("%H:%M:%S"), re.sub(r"k=[^&\s]+", "k=***", fmt % args)), flush=True)

    def send(self, code, body=b"", ctype="application/json", headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, default=str).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, str(v))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def q(self):
        return {k: v[-1] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).items()}

    def route(self):
        p = urllib.parse.urlsplit(self.path).path
        if p.startswith("/quad"):
            p = p[5:] or "/"
        return p

    def admin(self):
        tok = self.q().get("k") or (self.headers.get("Authorization") or "").replace("Bearer ", "")
        return tok == ADMIN_TOKEN

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        p = self.route()
        try:
            if p == "/api/device/poll":
                return self.device_poll()
            if p == "/healthz":
                return self.send(200, "ok", "text/plain")
            if not self.admin():
                return self.send(404, {"error": "not found"})
            if p in ("/", "/index.html"):
                with open(DASH_HTML, "rb") as f:
                    return self.send(200, f.read(), "text/html; charset=utf-8")
            if p == "/api/screen.png":
                with board.cond:
                    img, v = board.final, board.version
                return self.png(img, {"X-Version": v})
            if p == "/api/preview.png":
                page = self.q().get("page", "quad")
                with board.cond:
                    img = R.compose(board.ctx(page=page))
                return self.png(img)
            if p == "/api/state":
                return self.send(200, self.state())
            if p == "/api/teams":
                return self.send(200, {k: v[0] for k, v in S.TEAMS.items()})
            return self.send(404, {"error": "not found"})
        except BrokenPipeError:
            pass
        except Exception as e:
            traceback.print_exc()
            return self.send(500, {"error": str(e)})

    def do_POST(self):
        p = self.route()
        if not self.admin():
            return self.send(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(min(n, 100_000)) or b"{}")
            if p == "/api/config":
                return self.send(200, self.set_config(body))
            if p == "/api/action":
                return self.send(200, self.action(body))
            return self.send(404, {"error": "not found"})
        except Exception as e:
            traceback.print_exc()
            return self.send(400, {"error": str(e)})

    def png(self, img, headers=None):
        buf = io.BytesIO()
        img.convert("L").save(buf, "PNG", optimize=True)
        return self.send(200, buf.getvalue(), "image/png", headers)

    # -- dashboard
    def state(self):
        now = time.time()
        with board.cond:
            dv = dict(board.device)
            st = {
                "cfg": cfg, "defaults": DEFAULTS, "version": board.version, "page": board.page,
                "cycle": board.cycle, "sticky": board.sticky, "sticky_until": board.sticky_until,
                "note": (board.note.name or "someone") if board.note else None,
                "updated": board.updated, "render_ms": board.render_ms,
                "log": [(t, s) for t, s in board.log[:25]],
                "device": {**dv, "age_s": round(now - dv["last_seen"]) if dv["last_seen"] else None,
                           "battery": lipo_percent(dv["vbat"]) if dv.get("vbat") else None,
                           "in_sync": dv.get("version") == board.version},
                "quiet_now": quiet_now(),
            }
        st["sources"] = {
            "weather": S._weather.status(), "alerts": S._alerts.status(), "news": S.news_status(),
            "quotes": S.quotes_status(), "sports": S.sports_status(), "brief": S.brief_status(),
            "sky": {"age_s": round(now - sky.updated_at) if sky and sky.updated_at else None,
                    "error": sky.last_error if sky else None},
            "events": events.status if events else None,
        }
        st["feeds"] = {k: v[0] for k, v in S.WSJ_FEEDS.items()}
        st["teams"] = {k: v[0] for k, v in S.TEAMS.items()}
        st["panels"] = list(R.PANELS)
        st["notes_available"] = bool(nb is not None and notes_db() is not None)
        return st

    def set_config(self, body):
        global cfg
        allowed = set(DEFAULTS)
        new = dict(cfg)
        for k, v in body.items():
            if k not in allowed:
                continue
            dv = DEFAULTS[k]
            if isinstance(dv, bool):
                v = bool(v)
            elif isinstance(dv, int) and not isinstance(dv, bool):
                v = int(float(v))
            elif isinstance(dv, float):
                v = float(v)
            elif isinstance(dv, dict):
                v = v if isinstance(v, dict) else dv
            elif isinstance(dv, list):
                v = [str(x).strip() for x in (v if isinstance(v, list) else str(v).split(",")) if str(x).strip()]
            else:
                v = str(v)
            new[k] = v
        if sorted(new["slots"]) != sorted(R.PANELS):
            raise ValueError("slots must use each panel exactly once")
        moved = (new["lat"], new["lon"], new["radar_nm"], new["iss"], new["launch"]) != \
                (cfg["lat"], cfg["lon"], cfg["radar_nm"], cfg["iss"], cfg["launch"])
        cfg.clear()
        cfg.update(new)
        S.TEAMS.update({k: tuple(v) for k, v in cfg.get("custom_teams", {}).items()})
        save_json(CONFIG_PATH, {k: v for k, v in cfg.items() if DEFAULTS.get(k) != v})
        if moved:
            start_sky()
        # data-shaping changes need a refetch; purely visual ones just a redraw
        for name in ("weather", "news", "sports", "sky"):
            try:
                board.take(name, force=name in ("weather",) and moved)
            except Exception:
                traceback.print_exc()
        with board.cond:
            board.redraw("settings changed", major=True)
        return {"ok": True, "cfg": cfg}

    def action(self, body):
        a = body.get("action")
        if a == "refresh":
            with board.cond:
                board.note = None
                board.page = "quad"
            board.button(0)
        elif a == "full":
            with board.cond:
                board.force_full = True
                board.redraw("full refresh (dashboard)", full=True)
        elif a == "page":
            board.goto(body.get("page", "quad"))
        elif a == "sticky":
            hours = float(body.get("hours") or 0)
            with board.cond:
                board.sticky = str(body.get("text", ""))[:140]
                board.sticky_until = time.time() + hours * 3600 if hours else 0
                board.unread = bool(board.sticky) and bool(body.get("blink", True))
                board.redraw("sticky set" if board.sticky else "sticky cleared")
        elif a == "brief":
            board.take("brief", force=True)
            with board.cond:
                board.redraw("brief regenerated")
        elif a == "button":
            board.button(int(body.get("b", 0)))
        else:
            raise ValueError("unknown action")
        return {"ok": True, "version": board.version}

    # -- device
    def device_poll(self):
        q = self.q()
        if q.get("k") != DEVICE_TOKEN:
            return self.send(404, {"error": "not found"})
        v = int(q.get("v", "0") or 0)
        wait = min(int(q.get("wait", "0") or 0), 30)
        vbat = float(q["vbat"]) if q.get("vbat") else None
        btn = q.get("btn")
        mode = "live" if (vbat is None or vbat >= 3.80) else "sleep"
        dv = board.device
        dv.update(last_seen=time.time(), vbat=vbat, mode=mode, version=v,
                  rssi=int(q["rssi"]) if q.get("rssi") else dv.get("rssi"), polls=dv["polls"] + 1)
        if q.get("t"):
            dv["t"] = float(q["t"])
            dv["rh"] = float(q["rh"]) if q.get("rh") else None
        if q.get("boot"):
            dv["boots"] += 1
            board.note_event(f"device boot (v{v}, vbat {vbat})")
        if btn in ("0", "1", "2"):
            board.note_event(f"button {btn}")
            board.button(int(btn))
        deadline = time.time() + wait
        base = 0
        with board.cond:
            while v == board.version:
                left = deadline - time.time()
                if left <= 0:
                    break
                board.cond.wait(timeout=left)
            gray_ok = q.get("g") == "1"
            g = board.final_gray if gray_ok else None
            if v == board.version:
                body = None
            elif v == 0 or q.get("boot"):
                body = R.encode_reel([(board.final, 0, True, g)] if g is not None else [(board.final, 0, True)])
            elif v == board.prev_version and (gray_ok or board.final_gray is None):
                body, base = board.reel, 1
            elif g is not None:
                body = R.encode_reel([(board.final, 0, True, g)])
            else:
                body = R.encode_reel([(board.final, 0, q.get("fresh") == "1")])
            headers = {
                "X-Version": board.version, "X-Mode": mode,
                "X-Next": 600 if mode == "sleep" else 0,
                "X-Unread": int(board.unread),
                "X-Chime": int(board.chime_version == board.version and v != board.version),
                "X-Clean": 0,     # the server schedules full refreshes itself
                "X-Base": base,
            }
        if body is None:
            return self.send(204, b"", "application/octet-stream", headers)
        return self.send(200, body, "application/octet-stream", headers)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64


def warm():
    for name in ("weather", "news", "sports", "sky"):
        try:
            board.take(name)
        except Exception:
            traceback.print_exc()
    time.sleep(8)          # let the sky poller land its first answer
    board.take("sky")
    board.take("brief")
    with board.cond:
        board.beats["main"] = int(time.time() // (max(1, cfg["interval_min"]) * 60))
        board.redraw("startup", full=True)
    threading.Thread(target=scheduler, daemon=True).start()


if __name__ == "__main__":
    start_sky()
    print(f"quad board on {BIND}:{PORT} tz={os.environ['TZ']} notes={'on' if nb else 'off'}", flush=True)
    threading.Thread(target=warm, daemon=True).start()
    Server((BIND, PORT), H).serve_forever()
