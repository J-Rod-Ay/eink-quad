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
from html import escape as html_escape
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
import ical  # noqa: E402
import shul as SHUL  # noqa: E402

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
    "jcal_show": ["hebdate", "shabbat", "parsha", "yomtov", "holiday", "roshchodesh", "fasts"],
    "weekly_jewish": True,       # YIOT newsletter (+ jcal) refetched Thursdays 8 PM ET (Fri 8 AM retry on failure)
    "pinned": "",                # optional one-line message on the home screen strip
    "interval_min": 10,          # the main cadence: every panel refreshes on this beat
    "sky_min": 10,               # plane panel can run faster (partial refresh, no flash)
    "live_fast": True,           # while a team is playing, sports refreshes every live_min
    "live_min": 3,
    "live_sec": 30,              # live scores every N s (min 20); 0 = fall back to live_min
    "scoreboard_auto": False,    # jump to the scoreboard page while a team is live
    "calendar_ics": [],          # private iCal URLs (secret: masked in the API/dashboard)
    "tripit_ics": "",            # TripIt private iCal feed (secret)
    "layout": "quad",            # quad = 2x2 (4) | six = 3x2 (6) | nine = 3x3 (9, E1003 only)
    "rotate_pin_hours": 3,       # candle lighting within this many hours pins the jcal corner
    "slot_rotation": [],         # optional: 4 lists of panels each corner cycles through ([] = use slots)
    "rotate_min": 0,             # how often rotating corners advance (0 = every interval_min beat)
    "rotate_pin_live": True,     # hold a corner on a live game / airborne flight / near candle lighting / alert
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

# E1003 (QUAD_MODEL=e1003): the 10.3" panel defaults to the 3x3 home.
E1003_SLOTS = ["shabbat", "newsletter", "weather", "news", "trips", "sports", "scoreboard"]
if R.MODEL == "e1003":
    DEFAULTS["layout"] = "thirds"
    DEFAULTS["slots"] = [p for p in E1003_SLOTS if p in R.CORNER_PANELS][:9]


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

PAGES = ["quad", "weather", "news", "sports", "sky", "shabbat", "newsletter", "calendar", "trips", "scoreboard", "shopping"]
SECRET_KEYS = ("calendar_ics", "tripit_ics")
MASK = "•••• saved"
LIST_TOKEN = os.environ.get("QUAD_LIST_TOKEN", "")       # optional low-privilege token for the list/Siri
LIST_PATH = os.environ.get("QUAD_LIST", os.path.join(os.path.dirname(STATE_PATH), "shopping.json"))
LIST_HTML = os.path.join(HERE, "web", "list.html")
_list_lock = threading.Lock()


def public_cfg(c):
    """cfg for the dashboard/API: secret feed URLs replaced by a mask."""
    out = dict(c)
    for k in SECRET_KEYS:
        v = out.get(k)
        if isinstance(v, list):
            out[k] = [MASK] * len([x for x in v if x])
        else:
            out[k] = MASK if v else ""
    return out


def live_period(c=None):
    c = c or cfg
    if int(c.get("live_sec") or 0) > 0:
        return max(20, int(c["live_sec"]))
    return max(1, int(c.get("live_min") or 3)) * 60


def list_load():
    return load_json(LIST_PATH, {"items": []}).get("items", [])


def list_change(op, text="", item_id=""):
    import uuid
    with _list_lock:
        items = list_load()
        if op == "add":
            for t in re.split(r"[,\n]+", str(text or "")):
                t = " ".join(t.split())[:80]
                if not t:
                    continue
                hit = next((i for i in items if i["text"].lower() == t.lower()), None)
                if hit:
                    hit["done"] = False
                else:
                    items.append({"id": uuid.uuid4().hex[:8], "text": t, "done": False, "at": int(time.time())})
            items = items[-200:]
        elif op == "toggle":
            for i in items:
                if i["id"] == item_id:
                    i["done"] = not i["done"]
        elif op == "remove":
            items = [i for i in items if i["id"] != item_id]
        elif op == "clear_checked":
            items = [i for i in items if not i["done"]]
        else:
            raise ValueError("unknown op")
        save_json(LIST_PATH, {"items": items})
    board.snap["shopping"] = items
    with board.cond:
        if board.page == "shopping" or (board.page == "quad" and "shopping" in R.resolve_slots(board.ctx())):
            # only redraw when it's on the glass (the page, or a home corner)
            board.redraw("shopping list changed")
    return items


def touch_icon():
    """180x180 apple-touch-icon: a checklist, drawn once."""
    from PIL import ImageDraw as D
    im = Image.new("RGB", (180, 180), (255, 255, 255))
    d = D.Draw(im)
    d.rounded_rectangle((18, 18, 162, 162), radius=26, fill=(17, 17, 17))
    for i, y in enumerate((50, 88, 126)):
        d.rectangle((40, y - 12, 64, y + 12), outline=(255, 255, 255), width=4)
        if i < 2:
            d.line((44, y, 51, y + 8, 62, y - 9), fill=(255, 255, 255), width=5)
        d.rounded_rectangle((76, y - 5, 140, y + 5), radius=5, fill=(255, 255, 255))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


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
        self.auto_sb = False        # on the scoreboard because scoreboard_auto put us there
        self.rot_step = 0           # slot_rotation position
        self.gallery_offset = 0     # E1001 gallery: button steps between modes
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
        self.final = Image.new("1", (R.PW, R.PH), 1)
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
        elif name == "gallery":
            if R.layout_of(cfg) == "gallery":
                import gallery
                g = dict(self.snap.get("gallery") or {})
                day = time.strftime("%m-%d")
                if force or g.get("day") != day or not g.get("onthisday"):
                    g["onthisday"], g["day"] = gallery.fetch_onthisday(), day
                g["wire"] = gallery.fetch_wire(S.TEAMS, cfg.get("teams") or [])
                self.snap["gallery"] = g
        elif name == "shabbat":
            self.snap["shabbat"] = S.shabbat(cfg, force)
        elif name in ("shul", "newsletter"):
            if force:
                data, st = SHUL.refresh(force=False)
                print(f"[shul] refresh: {st} {(data or {}).get('issue_date')}", flush=True)
                if st == "none":
                    raise RuntimeError("no newsletter found")
            else:
                data = SHUL.load()
            self.snap["shul"] = data
        elif name == "calendar":
            self.snap["calendar"] = ical.calendar(cfg, force=force)
        elif name == "trips":
            tr = ical.trips(cfg, force)
            self.snap["trips"] = tr
            seg = ical.airborne(tr)
            self.snap["live_flight"] = ical.live_position(seg) if seg else None
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
                     event=sn.get("event"), upcoming=sn.get("upcoming") or [], shabbat=sn.get("shabbat"), shul=sn.get("shul"),
                     calendar=sn.get("calendar"), trips=sn.get("trips"), live=sn.get("live_flight"),
                     shopping=sn.get("shopping") or [], rot_step=self.rot_step,
                     gallery=sn.get("gallery") or {}, gallery_offset=self.gallery_offset, brief=sn.get("brief", ""),
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
        old_body = self.final.crop((0, R.px(R.HEAD_H), R.PW, R.PH))
        new_body = new.crop((0, R.px(R.HEAD_H), R.PW, R.PH))
        changed = ImageChops.logical_xor(old_body, new_body).getbbox() is not None
        self.render_ms = int((time.time() - t0) * 1000)
        full = full or self.force_full or (
            cfg["full_every_min"] and time.time() - self.last_full > cfg["full_every_min"] * 60
            and changed and not quiet_now())
        if changed and cfg.get("crisp") and (major or self.partials >= cfg.get("max_partials", 4)):
            full = True
        if changed and R.layout_of(cfg) == "gallery" and self.page == "quad":
            full = True          # photos ghost badly under partials
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
            regions = R.region_frames(self.final, new, R.layout_of(cfg))
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
            if self.page != "quad" and now - self.page_since > cfg["page_timeout_s"] and not self.auto_sb:
                self.page = "quad"
                self.redraw("page timeout", major=True)
        self.check_notes()
        self.weekly(now)
        if quiet_now():
            return
        iv = max(1, cfg["interval_min"]) * 60
        beat = int(now // iv)
        due = []
        if self.beats.get("main") != beat:
            due = ["weather", "news", "sports", "sky", "shabbat", "calendar", "trips", "brief", "gallery"]
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
            live = self.snap.get("live") or any(
                r.next and r.next.start - 300 <= now <= r.next.start + 5400 for r in (self.snap.get("teams") or []))
            if live and cfg.get("live_fast"):
                lb = int(now // live_period())
                if self.beats.get("live") != lb:
                    due.append("sports")
                    self.beats["live"] = lb
            if ical.airborne(self.snap.get("trips")):     # a flight in the air: track it every 3 min
                fb = int(now // 180)
                if self.beats.get("flight") != fb:
                    due.append("trips")
                    self.beats["flight"] = fb
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
            if "weather" in due:      # rotating corners advance on the regular beat only (no extra refreshes)
                rm = int(cfg.get("rotate_min") or 0)
                self.rot_step = int(now // (max(rm, cfg["interval_min"]) * 60)) if rm else self.cycle
            self.auto_scoreboard()
            self.redraw("scheduled " + ",".join(due), major="weather" in due)

    def weekly(self, now):
        """Thursday 8 PM ET: refetch the Jewish calendar and the YIOT newsletter (shul.json).
        Friday 8 AM ET retries only if the Thursday fetch failed."""
        if not cfg.get("weekly_jewish", True):
            return
        from datetime import datetime, timedelta
        dt = datetime.fromtimestamp(now)
        thu = (dt - timedelta(days=(dt.weekday() - 3) % 7)).replace(hour=20, minute=0, second=0, microsecond=0)
        if thu > dt:
            thu -= timedelta(days=7)
        slot = thu.strftime("%Y-%m-%d")
        st = self.beats.setdefault("weekly", {})
        if st.get("slot") != slot:
            # first boot with a newsletter already on disk: wait for the next Thursday
            if st.get("slot") is None and self.snap.get("shul") is not None and not st.get("booted"):
                st.update(slot=slot, ok=True, booted=True)
                return
            st.update(slot=slot, ok=None, retried=False, booted=True)
        elif st.get("ok") is not False or st.get("retried"):
            return
        else:
            if dt < (thu + timedelta(days=1)).replace(hour=8):
                return
            st["retried"] = True
        ok = True
        for name in ("shabbat", "shul"):
            try:
                self.take(name, force=True)
            except Exception:
                ok = False
                traceback.print_exc()
        st["ok"] = ok
        print(f"[weekly] jewish data {'ok' if ok else 'FAILED (retry Fri 8 AM)'}", flush=True)
        with self.cond:
            self.redraw("weekly jewish data")

    def auto_scoreboard(self):
        """scoreboard_auto: jump to the scoreboard when a team goes live, home when it's over."""
        live = bool(self.snap.get("live"))
        if cfg.get("scoreboard_auto") and live and self.page == "quad" and self.note is None:
            self.page, self.page_since, self.auto_sb = "scoreboard", time.time(), True
            self.force_full = True
            self.note_event("game live: scoreboard")
        elif self.auto_sb and (not live or not cfg.get("scoreboard_auto")):
            self.auto_sb = False
            if self.page == "scoreboard":
                self.page = "quad"
                self.force_full = True
                self.note_event("games over: home")

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
            for name in ("weather", "news", "sports", "sky", "shabbat", "calendar", "trips"):
                try:
                    self.take(name, force=True)
                except Exception:
                    traceback.print_exc()
            with self.cond:
                self.cycle += 1
                self.redraw("refresh button", major=True)
            return
        with self.cond:
            if R.layout_of(cfg) == "gallery" and self.page == "quad":
                self.gallery_offset += 1 if b == 2 else -1     # step modes; the timer keeps rotating from here
                return self.redraw(f"gallery step {self.gallery_offset}", major=True)
            self.auto_sb = False
            i = PAGES.index(self.page)
            self.page = PAGES[(i + (1 if b == 2 else -1)) % len(PAGES)]
            self.page_since = time.time()
            self.redraw(f"page {self.page}", major=True)

    def tap(self, tx, ty):
        """Touch (E1003). Home: tap a slot to open its page. Detail page: any tap goes home."""
        with self.cond:
            self.unread = False
            if self.note is not None or self.page != "quad":
                self.note = None
                self.page = "quad"
                self.page_since = time.time()
                return self.redraw("home (tap)", major=True)
            k = R.K or 1.0
            ux, uy = tx / k, ty / k
            boxes = R.slot_boxes(R.layout_of(cfg))
            names = R.resolve_slots(self.ctx())
            hit = None
            for (x0, y0, x1, y1), name in zip(boxes, names):
                if x0 <= ux < x1 and y0 <= uy < y1:
                    hit = name
                    break
        self.note_event(f"tap {tx},{ty} -> {hit or '(none)'}")
        if hit in PAGES and hit != "quad":
            self.goto(hit)

    def goto(self, page):
        with self.cond:
            self.auto_sb = False
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

# ---------------------------------------------------------------- auth: sessions + rate limit

import hashlib  # noqa: E402
import hmac  # noqa: E402
import secrets as _secrets  # noqa: E402
from collections import deque  # noqa: E402

SESSION_SECRET = os.environ.get("QUAD_SESSION_SECRET", "")
if not SESSION_SECRET:
    SESSION_SECRET = _secrets.token_hex(32)
    print("[auth] QUAD_SESSION_SECRET not set: sessions last until restart", flush=True)
SESSION_DAYS = 180
COOKIES = {"admin": "quad_s", "list": "quad_l"}
FAIL_MAX, FAIL_WINDOW, BLOCK_S = 10, 600, 900
_fails: dict = {}
_blocked: dict = {}
_auth_lock = threading.Lock()


def _scope_key(scope):
    tok = ADMIN_TOKEN if scope == "admin" else (LIST_TOKEN or ADMIN_TOKEN)
    # rotating the token invalidates every cookie issued for it
    return hmac.new(SESSION_SECRET.encode(), f"{scope}:{tok}".encode(), hashlib.sha256).digest()


def make_session(scope):
    exp = int(time.time()) + SESSION_DAYS * 86400
    sig = hmac.new(_scope_key(scope), f"{scope}.{exp}".encode(), hashlib.sha256).hexdigest()[:40]
    return f"{scope}.{exp}.{sig}"


def check_session(val, scope):
    try:
        sc, exp, sig = val.split(".")
        if sc != scope or int(exp) < time.time():
            return False
        good = hmac.new(_scope_key(scope), f"{sc}.{exp}".encode(), hashlib.sha256).hexdigest()[:40]
        return hmac.compare_digest(sig, good)
    except (ValueError, AttributeError):
        return False


def tok_eq(a, b):
    return bool(a) and bool(b) and hmac.compare_digest(str(a).encode(), str(b).encode())


def note_fail(ip):
    now = time.time()
    with _auth_lock:
        dq = _fails.setdefault(ip, deque())
        dq.append(now)
        while dq and now - dq[0] > FAIL_WINDOW:
            dq.popleft()
        if len(dq) >= FAIL_MAX:
            _blocked[ip] = now + BLOCK_S
            dq.clear()
            print(f"[auth] blocking {ip} for {BLOCK_S // 60} min after {FAIL_MAX} failed logins", flush=True)


def is_blocked(ip):
    with _auth_lock:
        until = _blocked.get(ip)
        if until and until > time.time():
            return True
        _blocked.pop(ip, None)
        return False


LOGIN_HTML = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,viewport-fit=cover"><title>Quad Board</title>
<style>body{font:17px -apple-system,system-ui,sans-serif;display:grid;place-items:center;min-height:90vh;margin:0;
padding:env(safe-area-inset-top) 16px env(safe-area-inset-bottom)}form{display:grid;gap:10px;width:min(320px,90vw)}
input,button{font-size:17px;padding:12px;border-radius:10px;border:1.5px solid #111}button{background:#111;color:#fff}
p{color:#a00;margin:0}</style></head><body><form method=post action="login">
<h2 style="margin:0">Quad Board</h2>%s<input type=hidden name=next value="%s">
<input type=password name=key placeholder="%s" autocomplete=current-password autofocus required>
<button>Sign in</button></form></body></html>"""


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "quad"
    sys_version = ""

    def send_header(self, keyword, value):
        if keyword.lower() == "server":       # no "quad Python/3.x" fingerprint
            return
        super().send_header(keyword, value)

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

    # -- auth
    def ip(self):
        peer = self.client_address[0]
        xff = self.headers.get("X-Forwarded-For")
        if xff and peer in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):    # only trust our own Caddy
            return xff.split(",")[-1].strip() or peer
        return peer

    def cookies(self):
        out = {}
        for part in (self.headers.get("Cookie") or "").split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                out[k] = v
        return out

    def presented(self):
        """A key in the URL or Authorization header (cookies are checked separately)."""
        return self.q().get("k") or (self.headers.get("Authorization") or "").replace("Bearer ", "").strip()

    def authed(self, scope="admin"):
        """admin: admin key or admin session. list: also the list token / list session."""
        tok = self.presented()
        ck = self.cookies()
        if tok_eq(tok, ADMIN_TOKEN) or check_session(ck.get(COOKIES["admin"], ""), "admin"):
            return "admin"
        if scope == "list" and ((LIST_TOKEN and tok_eq(tok, LIST_TOKEN)) or check_session(ck.get(COOKIES["list"], ""), "list")):
            return "list"
        if tok or ck.get(COOKIES["admin"]) or ck.get(COOKIES["list"]):
            note_fail(self.ip())                 # a wrong key or a forged/expired cookie
        return None

    def admin(self):
        return self.authed("admin") == "admin"

    def cookie_hdr(self, scope, value, max_age):
        return (f"{COOKIES[scope]}={value}; Path=/; Max-Age={max_age}; Secure; HttpOnly; SameSite=Strict")

    def redirect(self, loc, cookies=()):
        body = b""
        self.send_response(302)
        self.send_header("Location", loc)
        for c in cookies:
            self.send_header("Set-Cookie", c)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def strip_k(self):
        """Same URL without k= (after it has been swapped for a cookie)."""
        u = urllib.parse.urlsplit(self.path)
        qs = [(k, v) for k, v in urllib.parse.parse_qsl(u.query, keep_blank_values=True) if k != "k"]
        return urllib.parse.urlunsplit(("", "", u.path, urllib.parse.urlencode(qs), ""))

    def prefix(self):
        return "/quad" if urllib.parse.urlsplit(self.path).path.startswith("/quad") else ""

    def page_login(self, scope, who):
        """?k=... on an HTML page: set the session cookie and drop k from the URL."""
        if self.q().get("k") and who:
            sc = "admin" if who == "admin" else "list"
            return self.redirect(self.strip_k() or "/", [self.cookie_hdr(sc, make_session(sc), SESSION_DAYS * 86400)])
        return None

    def login_form(self, nxt, err=""):
        nxt = html_escape(nxt)
        body = LOGIN_HTML % ((f"<p>{html_escape(err)}</p>" if err else ""), nxt,
                             "List key or admin key" if "/list" in nxt else "Admin key")
        return self.send(401 if err else 200, body, "text/html; charset=utf-8")

    # -- GET
    def do_GET(self):
        p = self.route()
        try:
            if p == "/api/device/poll":
                return self.device_poll()          # device auth unchanged: its own token, its own path
            if p == "/healthz":
                return self.send(200, "ok", "text/plain")
            if p == "/apple-touch-icon.png":
                return self.send(200, touch_icon(), "image/png", {"Cache-Control": "max-age=86400"})
            if p == "/list.webmanifest":
                return self.send(200, {"name": "Quad List", "short_name": "Quad List", "display": "standalone",
                                       "start_url": "list", "background_color": "#ffffff", "theme_color": "#ffffff",
                                       "icons": [{"src": "apple-touch-icon.png", "sizes": "180x180", "type": "image/png"}]},
                                 "application/manifest+json")
            if is_blocked(self.ip()):
                return self.send(429, {"error": "too many failed attempts, try again later"}, headers={"Retry-After": BLOCK_S})
            if p == "/login":
                return self.login_form(self.q().get("next", self.prefix() + "/"))
            if p == "/logout":
                return self.redirect(self.prefix() + "/login", [self.cookie_hdr("admin", "", 0), self.cookie_hdr("list", "", 0)])
            if p in ("/list", "/list.html"):
                who = self.authed("list")
                r = self.page_login("list", who)
                if r is not None or who is None:
                    return r if r is not None else self.login_form(self.path.split("?")[0])
                with open(LIST_HTML, "rb") as f:
                    return self.send(200, f.read(), "text/html; charset=utf-8")
            if p.startswith("/api/list"):
                who = self.authed("list")
                if who is None:
                    return self.send(404, {"error": "not found"})
                if p == "/api/list":
                    return self.send(200, {"items": list_load()})
                if p == "/api/list/add":
                    return self.list_add(self.q().get("item", ""))
                return self.send(404, {"error": "not found"})
            who = self.authed("admin")
            if p in ("/", "/index.html"):
                r = self.page_login("admin", who if who == "admin" else None)
                if r is not None:
                    return r
                if who != "admin":
                    return self.send(404, {"error": "not found"})
                with open(DASH_HTML, "rb") as f:
                    return self.send(200, f.read(), "text/html; charset=utf-8")
            if who != "admin":
                return self.send(404, {"error": "not found"})
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

    def list_add(self, item):
        item = str(item or "").strip()
        if not item:
            return self.send(400, "Nothing to add", "text/plain; charset=utf-8")
        before = {i["text"].lower() for i in list_load() if not i["done"]}
        items = list_change("add", item)
        added = [i["text"] for i in items if not i["done"] and i["text"].lower() not in before]
        msg = ("Added " + ", ".join(added)) if added else "Already on the list"
        return self.send(200, msg, "text/plain; charset=utf-8")

    # -- POST
    def do_POST(self):
        p = self.route()
        if is_blocked(self.ip()):
            return self.send(429, {"error": "too many failed attempts, try again later"}, headers={"Retry-After": BLOCK_S})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(min(n, 100_000)) if n else b""
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if p == "/login":
                f = {k: v[-1] for k, v in urllib.parse.parse_qs(raw.decode("utf-8", "replace")).items()}
                key, nxt = f.get("key", ""), f.get("next", self.prefix() + "/")
                if not nxt.startswith("/") or nxt.startswith("//"):
                    nxt = self.prefix() + "/"
                if tok_eq(key, ADMIN_TOKEN):
                    sc = "admin"
                elif LIST_TOKEN and tok_eq(key, LIST_TOKEN) and "/list" in nxt:
                    sc = "list"
                else:
                    note_fail(self.ip())
                    return self.login_form(nxt, "That key didn't work")
                return self.redirect(nxt, [self.cookie_hdr(sc, make_session(sc), SESSION_DAYS * 86400)])
            if p == "/logout":
                return self.redirect(self.prefix() + "/login", [self.cookie_hdr("admin", "", 0), self.cookie_hdr("list", "", 0)])
            if p.startswith("/api/list"):
                if self.authed("list") is None:
                    return self.send(404, {"error": "not found"})
                if p == "/api/list/add":       # Siri Shortcut: text body, form, JSON, or ?item=
                    item = self.q().get("item", "")
                    if not item and raw:
                        if ctype == "application/x-www-form-urlencoded":
                            item = (urllib.parse.parse_qs(raw.decode("utf-8", "replace")).get("item") or [""])[-1]
                        elif ctype == "application/json":
                            j = json.loads(raw or b"{}")
                            item = j.get("item") or j.get("text") or "" if isinstance(j, dict) else str(j)
                        else:
                            item = raw.decode("utf-8", "replace")
                    return self.list_add(item)
                if p == "/api/list":
                    b = json.loads(raw or b"{}")
                    return self.send(200, {"items": list_change(b.get("op", ""), b.get("text", ""), b.get("id", ""))})
                return self.send(404, {"error": "not found"})
            if not self.admin():
                return self.send(404, {"error": "not found"})
            body = json.loads(raw or b"{}")
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
                "cfg": public_cfg(cfg), "defaults": DEFAULTS, "pages": PAGES, "corner_panels": R.CORNER_PANELS, "version": board.version, "page": board.page,
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
            "shabbat": S.shabbat_status(),
            "calendar": ical.calendar_status(cfg), "trips": ical.trips_status(cfg),
            "events": events.status if events else None,
        }
        st["feeds"] = {k: v[0] for k, v in S.WSJ_FEEDS.items()}
        st["teams"] = {k: v[0] for k, v in S.TEAMS.items()}
        st["panels"] = list(R.PANELS)
        st["jcal_options"] = S.JCAL_OPTIONS
        st["notes_available"] = bool(nb is not None and notes_db() is not None)
        return st

    def set_config(self, body):
        global cfg
        allowed = set(DEFAULTS)
        new = dict(cfg)
        for k, v in body.items():
            if k not in allowed:
                continue
            if k in SECRET_KEYS:
                vals = v if isinstance(v, list) else [v]
                if any(MASK in str(x) for x in vals):
                    continue                                   # untouched masked value: keep the saved link
                if any(str(x).strip().lower() == "none" for x in vals):
                    v = [] if isinstance(DEFAULTS[k], list) else ""
                elif any(str(x).strip() and not re.match(r"(?i)^(https?|webcal)://", str(x).strip()) for x in vals):
                    raise ValueError(f"{k}: expected an https:// or webcal:// link")
            if k == "slot_rotation":
                if not v:
                    new[k] = []
                    continue
                if not (isinstance(v, list) and len(v) == 4 and all(isinstance(x, list) and x for x in v)):
                    raise ValueError("slot_rotation must be 4 non-empty lists of panels")
                bad = {str(n) for x in v for n in x} - set(R.CORNER_PANELS)
                if bad:
                    raise ValueError(f"slot_rotation: not a corner panel: {', '.join(sorted(bad))}")
                new[k] = [list(dict.fromkeys(str(n) for n in x)) for x in v]
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
        if new.get("layout") not in R.LAYOUTS:
            raise ValueError("layout must be one of: " + ", ".join(R.LAYOUTS))
        n = R.LAYOUTS[new["layout"]]
        sl = new["slots"]
        if len(sl) != n or len(set(sl)) != n or any(x not in R.CORNER_PANELS for x in sl):
            raise ValueError(f"{new['layout']} needs {n} different panels from: {', '.join(R.CORNER_PANELS)}")
        if new.get("slot_rotation") and len(new["slot_rotation"]) != n:
            raise ValueError(f"slot_rotation needs {n} lists for the {new['layout']} layout")
        new["pinned"] = " ".join(str(new.get("pinned") or "").split())[:300]
        new["jcal_show"] = [x for x in dict.fromkeys(new.get("jcal_show") or []) if x in S.JCAL_OPTIONS]
        if new.get("live_sec"):
            new["live_sec"] = max(20, int(new["live_sec"]))
        changed = {k for k in DEFAULTS if new.get(k) != cfg.get(k)}
        only_visual = changed <= {"pinned", "jcal_show", "scoreboard_auto", "live_sec", "live_min", "layout", "slots",
                                  "slot_rotation", "rotate_min", "rotate_pin_live", "rotate_pin_hours"}
        if "layout" in changed:
            board.force_full = True
        moved = (new["lat"], new["lon"], new["radar_nm"], new["iss"], new["launch"]) != \
                (cfg["lat"], cfg["lon"], cfg["radar_nm"], cfg["iss"], cfg["launch"])
        cfg.clear()
        cfg.update(new)
        S.TEAMS.update({k: tuple(v) for k, v in cfg.get("custom_teams", {}).items()})
        save_json(CONFIG_PATH, {k: v for k, v in cfg.items() if DEFAULTS.get(k) != v})
        if moved:
            start_sky()
        # data-shaping changes need a refetch; purely visual ones just a redraw
        for name in (["shabbat"] if "jcal_show" in changed else []) if only_visual else \
                ("weather", "news", "sports", "sky", "shabbat", "calendar", "trips"):
            try:
                board.take(name, force=name in ("weather",) and moved)
            except Exception:
                traceback.print_exc()
        with board.cond:
            board.redraw("settings changed", major=True)
        return {"ok": True, "cfg": public_cfg(cfg)}

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
        elif a == "shul_refresh":            # manual: fetch the newest YIOT newsletter now
            board.take("shul", force=True)
            with board.cond:
                board.redraw("shul refreshed")
            return {"ok": True, "version": board.version, "issue": (board.snap.get("shul") or {}).get("issue_date")}
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
        elif q.get("tx") and q.get("ty"):
            try:
                tx, ty = int(q["tx"]), int(q["ty"])
            except ValueError:
                tx = ty = None
            if tx is not None:
                board.tap(tx, ty)
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
    board.snap["shopping"] = list_load()
    board.snap["shul"] = SHUL.load()
    if board.snap["shul"] is None:          # nothing on disk yet: one fetch now, then weekly
        try:
            board.take("shul", force=True)
        except Exception:
            traceback.print_exc()
    for name in ("weather", "news", "sports", "sky", "shabbat", "calendar", "trips"):
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
