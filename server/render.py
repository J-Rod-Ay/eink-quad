"""Draws the board. Pure functions of data -> PIL images; no I/O here.

Everything is drawn on an 8-bit "L" canvas and thresholded to 1-bit at the
very end. Text uses FreeType's monochrome rasteriser (fontmode "1"), which is
hinted for exactly this and is much crisper than thresholding anti-aliased
glyphs. Icons are drawn 4x oversize and shrunk, so their edges are smooth
before the threshold.

Layout (800x480):
    header    y 0..38     date | sticky message | indoor + updated time
    strip     y 38..66    Claude's one-line brief (or a weather alert)
    grid      y 66..480   2x2 slots, each 400x207; any panel in any slot

Device profiles (set_profile / QUAD_MODEL env):
    e1001   800x480 panel, 4 grey levels, layouts quad | six. 1 layout unit = 1 px.
    e1003   1872x1404 panel, 16 grey levels, layouts quad | six | nine (default).
            Layout is done in units on a 1040x780 board and rasterised at
            K = 1.8 px/unit: every coordinate, stroke, font size, icon and logo
            is scaled *before* drawing (see SDraw), so glyphs are hinted and
            rendered natively at panel resolution -- nothing is upscaled.
            1.8 is roughly the dpi ratio (227 vs 125), so a 38-unit header is
            the same physical height on both panels.
"""
from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass, field

from PIL import Image, ImageChops, ImageDraw, ImageFont

W, H = 800, 480
HEAD_H = 38
STRIP_H = 28
GRID_Y = HEAD_H + STRIP_H
CELL_W, CELL_H = W // 2, (H - GRID_Y) // 2      # 400 x 207
BODY = (W, H - GRID_Y)                            # detail pages: 800 x 414

# ---- device profile. W/H above are layout units; PW/PH are panel pixels.
K = 1.0                 # panel px per layout unit
PW, PH = W, H
GRAY_LEVELS = 4
MODEL = "e1001"
DEFAULT_LAYOUT = "quad"
PROFILES = {
    "e1001": dict(W=800, H=480, K=1.0, GRAY_LEVELS=4, DEFAULT_LAYOUT="quad"),
    "e1003": dict(W=1040, H=780, K=1.8, GRAY_LEVELS=16, DEFAULT_LAYOUT="nine"),
}


def set_profile(name: str):
    """Switch every module-level geometry constant to a device. Call once at startup."""
    global W, H, K, PW, PH, GRAY_LEVELS, MODEL, DEFAULT_LAYOUT, CELL_W, CELL_H, BODY
    p = PROFILES[name]
    MODEL = name
    W, H, K, GRAY_LEVELS, DEFAULT_LAYOUT = p["W"], p["H"], p["K"], p["GRAY_LEVELS"], p["DEFAULT_LAYOUT"]
    PW, PH = round(W * K), round(H * K)
    CELL_W, CELL_H = W // 2, (H - GRID_Y) // 2
    BODY = (W, H - GRID_Y)
    _icon_cache.clear()


def px(v):
    """Layout units -> panel pixels."""
    return int(round(v * K))

HERE = os.path.dirname(os.path.abspath(__file__))
FONT_DIR = os.path.join(HERE, "fonts")
INK, PAPER = 0, 255

_fonts = {}


_fmeta = {}


def F(name: str, size: int) -> ImageFont.FreeTypeFont:
    key = (name, size)
    if key not in _fonts:
        path = os.path.join(FONT_DIR, {
            "r": "Barlow-Regular.ttf", "m": "Barlow-Medium.ttf", "sb": "Barlow-SemiBold.ttf",
            "b": "Barlow-Bold.ttf", "cr": "BarlowCondensed-Regular.ttf", "cm": "BarlowCondensed-Medium.ttf",
            "csb": "BarlowCondensed-SemiBold.ttf", "cb": "BarlowCondensed-Bold.ttf",
            "hand": "PatrickHand-Regular.ttf",
        }[name])
        _fonts[key] = ImageFont.truetype(path, size)
        _fmeta[id(_fonts[key])] = key
    return _fonts[key]


def _scaled_font(f):
    if K == 1 or f is None or id(f) not in _fmeta:
        return f
    name, size = _fmeta[id(f)]
    return F(name, max(1, round(size * K)))


def _sc(v):
    """Scale an xy argument of any PIL shape (flat tuple, list of pairs, nested)."""
    if isinstance(v, (int, float)):
        return v * K
    return type(v)(_sc(x) for x in v) if isinstance(v, (tuple, list)) else v


class SDraw:
    """ImageDraw in layout units on a K-times bigger image. Coordinates, stroke
    widths, radii and fonts are all multiplied by K before PIL sees them."""

    def __init__(self, img):
        object.__setattr__(self, "_d", ImageDraw.Draw(img))

    def __getattr__(self, k):
        return getattr(self._d, k)

    def __setattr__(self, k, v):
        setattr(self._d, k, v)

    @staticmethod
    def _kw(kw):
        if kw.get("width"):
            kw["width"] = max(1, round(kw["width"] * K))
        if kw.get("radius"):
            kw["radius"] = kw["radius"] * K
        if "font" in kw:
            kw["font"] = _scaled_font(kw["font"])
        return kw

    def text(self, xy, s, fill=None, font=None, anchor=None, **kw):
        return self._d.text(_sc(xy), s, fill=fill, font=_scaled_font(font), anchor=anchor, **kw)

    def textlength(self, s, font=None, **kw):
        return self._d.textlength(s, font=_scaled_font(font), **kw) / K

    def textbbox(self, xy, s, font=None, **kw):
        b = self._d.textbbox(_sc(xy), s, font=_scaled_font(font), **kw)
        return tuple(v / K for v in b)

    def point(self, xy, fill=None):
        x, y = xy[0] * K, xy[1] * K
        r = max(1, round(K)) - 1
        return self._d.rectangle((x, y, x + r, y + r), fill=fill)

    def __getattribute__(self, k):
        if k in ("line", "rectangle", "rounded_rectangle", "ellipse", "polygon", "arc", "pieslice", "chord"):
            fn = getattr(object.__getattribute__(self, "_d"), k)
            return lambda xy, *a, **kw: fn(_sc(xy), *a, **SDraw._kw(kw))
        return object.__getattribute__(self, k)


def draw(img):
    return ImageDraw.Draw(img) if K == 1 else SDraw(img)


# True while rendering the 4-level grey version: text is anti-aliased then,
# because the panel can actually show the in-between shades.
AA = False


def canvas(size):
    img = Image.new("L", (px(size[0]), px(size[1])), PAPER)
    d = draw(img)
    d.fontmode = "L" if AA else "1"
    return img, d


def tw(d, s, f):
    return d.textlength(s, font=f)


def text(d, xy, s, f, fill=INK, anchor="la"):
    d.text(xy, s, font=f, fill=fill, anchor=anchor)


def ellipsize(d, s, f, width):
    if tw(d, s, f) <= width:
        return s
    while s and tw(d, s + "…", f) > width:
        s = s[:-1]
    return s.rstrip(" ,.;:-") + "…"


def wrap(d, s, f, width, max_lines):
    words = s.split()
    lines, cur = [], ""
    for i, w in enumerate(words):
        t = (cur + " " + w).strip()
        if tw(d, t, f) <= width:
            cur = t
            continue
        if cur:
            lines.append(cur)
        cur = w
        if len(lines) == max_lines:
            break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    used = " ".join(lines).split()
    if len(used) < len(words):
        lines[-1] = ellipsize(d, lines[-1] + " " + words[len(used)] + "…", f, width)
        if not lines[-1].endswith("…"):
            lines[-1] += "…"
    return lines


def fit_font(d, s, name, width, sizes):
    for sz in sizes:
        f = F(name, sz)
        if tw(d, s, f) <= width:
            return f
    return F(name, sizes[-1])


def to1(img: Image.Image, invert=False) -> Image.Image:
    out = img.point(lambda v: 255 if v >= 128 else 0).convert("1")
    if invert:
        out = ImageChops.invert(out)
    return out


def pill(d, x, y, s, f, pad=5, fill=INK, fg=PAPER, h=None):
    """Inverted label. Returns right edge."""
    wd = tw(d, s, f)
    asc, desc = f.getmetrics()
    hh = h or (asc + desc // 2 + 2)
    d.rounded_rectangle((x, y, x + wd + 2 * pad, y + hh), radius=4, fill=fill)
    d.text((x + pad, y + hh / 2), s, font=f, fill=fg, anchor="lm")
    return x + wd + 2 * pad


def dotted(d, x0, x1, y, step=4):
    for x in range(int(x0), int(x1), step):
        d.point((x, y), fill=INK)


def tri(d, x, y, size, up, fill=INK):
    if up:
        d.polygon([(x, y + size), (x + size, y + size), (x + size / 2, y)], fill=fill)
    else:
        d.polygon([(x, y), (x + size, y), (x + size / 2, y + size)], fill=fill)


def arrow_right(d, x, y, w, h, width=3):
    """A drawn arrow; the fonts' U+2192 is inconsistent at mono rendering."""
    d.line((x, y, x + w - h * 0.6, y), fill=INK, width=width)
    d.polygon([(x + w, y), (x + w - h, y - h * 0.6), (x + w - h, y + h * 0.6)], fill=INK)


# ------------------------------------------------------------------ icons

_icon_cache = {}


def icon(kind: str, size: int, night=False) -> Image.Image:
    key = (kind, size, night)
    if key in _icon_cache:
        return _icon_cache[key]
    S = size * 4
    img = Image.new("L", (S, S), PAPER)
    d = ImageDraw.Draw(img)
    lw = max(4, S // 22)

    def sun(cx, cy, r, rays=True):
        if night:
            d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=INK)
            o = r * 0.55
            d.ellipse((cx - r + o, cy - r - o * 0.3, cx + r + o, cy + r - o * 0.3), fill=PAPER)
            return
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=INK)
        if rays:
            for i in range(8):
                a = i * math.pi / 4
                r0, r1 = r * 1.35, r * 1.8
                d.line((cx + r0 * math.cos(a), cy + r0 * math.sin(a),
                        cx + r1 * math.cos(a), cy + r1 * math.sin(a)), fill=INK, width=lw)

    def cloud(x0, y0, x1, y1, fill=PAPER):
        w, h = x1 - x0, y1 - y0
        parts = [(x0, y0 + h * 0.45, x0 + w * 0.45, y1),
                 (x0 + w * 0.2, y0 + h * 0.15, x0 + w * 0.65, y0 + h * 0.85),
                 (x0 + w * 0.45, y0, x1 - w * 0.05, y0 + h * 0.85),
                 (x1 - w * 0.4, y0 + h * 0.4, x1, y1)]
        base = (x0 + w * 0.2, y0 + h * 0.6, x1 - w * 0.2, y1)
        for (a, b, c, e) in parts:
            d.ellipse((a - lw, b - lw, c + lw, e + lw), fill=INK)
        d.rectangle((base[0], base[1] - lw, base[2], base[3] + lw), fill=INK)
        for (a, b, c, e) in parts:
            d.ellipse((a, b, c, e), fill=fill)
        d.rectangle(base, fill=fill)

    if kind == "sun":
        sun(S / 2, S / 2, S * 0.22)
    elif kind == "partly":
        sun(S * 0.36, S * 0.36, S * 0.17)
        cloud(S * 0.22, S * 0.38, S * 0.96, S * 0.84)
    elif kind in ("cloud", "fog"):
        cloud(S * 0.06, S * 0.2, S * 0.94, S * 0.74)
        if kind == "fog":
            for i, y in enumerate((0.82, 0.93)):
                d.line((S * (0.12 + 0.08 * i), S * y, S * (0.88 - 0.08 * i), S * y), fill=INK, width=lw)
    elif kind in ("rain", "showers", "storm", "snow"):
        if kind == "showers":
            sun(S * 0.33, S * 0.3, S * 0.15)
        cloud(S * 0.08, S * 0.12, S * 0.92, S * 0.62)
        if kind == "storm":
            d.polygon([(S * .52, S * .6), (S * .36, S * .82), (S * .5, S * .82), (S * .42, S * .99),
                       (S * .66, S * .74), (S * .52, S * .74), (S * .6, S * .6)], fill=INK)
        elif kind == "snow":
            for (x, y) in ((.3, .76), (.5, .86), (.7, .76), (.4, .96), (.6, .96)):
                r = S * 0.04
                d.ellipse((S * x - r, S * y - r, S * x + r, S * y + r), fill=INK)
        else:
            for x in (0.3, 0.5, 0.7):
                d.line((S * x, S * 0.7, S * (x - 0.07), S * 0.92), fill=INK, width=lw + 2)
    out = img.resize((size, size), Image.LANCZOS)
    _icon_cache[key] = out
    return out


def paste_icon(c, kind, x, y, size, night=False):
    x, y, size = px(x), px(y), px(size)
    ic = icon(kind, size, night)
    c.paste(ImageChops.darker(c.crop((x, y, x + size, y + size)), ic), (x, y))


def paste_logo(c, url, x, y, size, cfg=None):
    """Team logo, or False if we have none (caller falls back to text).
    Grey render keeps its shading; the 1-bit render gets it dithered so the
    fast partial updates still show a logo rather than a blob."""
    if cfg is not None and not cfg.get("sports_logos", True):
        return False
    import logos
    x, y, size = px(x), px(y), px(size)
    img = logos.get(url, size)
    if img is None:
        return False
    if not AA:
        img = img.convert("1").convert("L")
    c.paste(ImageChops.darker(c.crop((x, y, x + size, y + size)), img), (x, y))
    return True


def compass_arrow(c, cx, cy, r, bearing):
    """Arrow pointing where to look (0 = north = up)."""
    S = 4
    cx, cy, r = cx * K, cy * K, px(r)
    big = Image.new("L", (r * 2 * S, r * 2 * S), PAPER)
    d = ImageDraw.Draw(big)
    R = r * S
    d.ellipse((2, 2, 2 * R - 2, 2 * R - 2), outline=INK, width=S * 2)
    a = math.radians(bearing)

    def pt(dist, ang):
        return (R + dist * math.sin(ang), R - dist * math.cos(ang))
    tip, tail = pt(R * 0.78, a), pt(R * 0.55, a + math.pi)
    l, rr = pt(R * 0.36, a + math.radians(140)), pt(R * 0.36, a - math.radians(140))
    d.polygon([tip, l, tail, rr], fill=INK)
    dn = ImageDraw.Draw(big)
    dn.text((R, S * 4), "N", font=F("b", round(9 * S * K)), fill=INK, anchor="mt")
    sm = big.resize((2 * r, 2 * r), Image.LANCZOS)
    x, y = int(cx - r), int(cy - r)
    c.paste(ImageChops.darker(c.crop((x, y, x + 2 * r, y + 2 * r)), sm), (x, y))


# ------------------------------------------------------------------ helpers for time

def hm(ts, ampm=True):
    t = time.localtime(ts)
    s = f"{t.tm_hour % 12 or 12}:{t.tm_min:02d}"
    return s + (" AM" if t.tm_hour < 12 else " PM") if ampm else s


def when(ts, tbd=False):
    """Friendly game time: 'Tonight 7:00', 'Tomorrow 1:00 PM', 'Sun 1:00 PM', 'Oct 12'."""
    now = time.localtime()
    t = time.localtime(ts)
    days = (time.mktime(t[:3] + (0, 0, 0, 0, 0, -1)) - time.mktime(now[:3] + (0, 0, 0, 0, 0, -1))) / 86400
    days = round(days)
    clock = "" if tbd else " " + hm(ts)
    if days == 0:
        return ("Tonight" if t.tm_hour >= 17 else "Today") + (clock if not tbd else " · time TBD")
    if days == 1:
        return "Tomorrow" + clock
    if 1 < days < 7:
        return time.strftime("%a", t) + clock
    return time.strftime("%b ", t) + str(t.tm_mday) + clock


def ago(ts):
    m = int((time.time() - ts) // 60)
    if m < 1:
        return "just now"
    if m < 60:
        return f"{m} min ago"
    return f"{m // 60}h {m % 60:02d}m ago"


# ================================================================== panels

@dataclass
class Sighting:
    """One flight, already resolved -- what the layout actually draws."""
    callsign: str
    airline: str
    origin: str
    dest: str
    origin_city: str
    dest_city: str
    alt_ft: int
    speed_kt: float
    dist_nm: float
    elev_deg: float
    seen_at: float
    # The marketed flight number (BA208), not the ICAO callsign (BAW208) --
    # the former is what you can type into a search box. Blank when unknown.
    flight_no: str = ""
    aircraft: str = ""          # "Boeing 787 Dreamliner"
    bearing: float = 0.0        # degrees from the house to the aircraft
    progress: float = None      # 0..1 along the route, None if unknown
    eta_min: int = None         # minutes to destination, None if unknown
    dest_dist_nm: float = 0.0   # house -> destination, for "farthest today"

    @property
    def has_route(self) -> bool:
        return bool(self.origin and self.dest)



@dataclass
class Ctx:
    """Everything a frame needs. Built by app.py; render never fetches."""
    now: float
    cfg: dict
    weather: object = None
    heads: list = field(default_factory=list)
    quotes: list = field(default_factory=list)
    teams: list = field(default_factory=list)
    featured: object = None       # sky Sighting
    recent: list = field(default_factory=list)
    day_count: int = 0
    nearby: list = field(default_factory=list)
    event: object = None
    upcoming: list = field(default_factory=list)
    brief: str = ""
    sticky: str = ""
    indoor_f: float | None = None
    indoor_rh: float | None = None
    battery: int | None = None
    on_usb: bool = True
    cycle: int = 0                # increments every scheduled update (rotations)
    updated: float = 0.0
    page: str = "quad"
    notes_new: int = 0
    stale: dict = field(default_factory=dict)
    shabbat: object = None        # sources.Shabbat
    calendar: object = None       # list[ical.CalEvent] | None (None = no feed set)
    trips: object = None          # list[ical.Trip] | None
    live: object = None           # ical.Live for an airborne flight
    shopping: list = field(default_factory=list)   # [{"text", "done"}]
    shul: object = None           # YIOT newsletter highlights dict | None
    rot_step: int = 0             # slot_rotation position (advances on the regular beat)


def _deg(v):
    return f"{v:.0f}°"


WIND = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def panel_weather(ctx: Ctx, size):
    from sources import wmo
    c, d = canvas(size)
    w, h = size
    wx = ctx.weather
    if wx is None:
        text(d, (w / 2, h / 2), "Weather unavailable", F("m", 18), anchor="mm")
        return c
    y0 = 8
    if wx.alerts:
        ev = wx.alerts[0][0].upper()
        more = f" +{len(wx.alerts) - 1}" if len(wx.alerts) > 1 else ""
        d.rectangle((0, 0, w, 26), fill=INK)
        tri(d, 10, 5, 15, True, fill=PAPER)
        d.text((18, 16), "!", font=F("b", 11), fill=INK, anchor="mm")
        text(d, (34, 13), ellipsize(d, ev + more, F("csb", 19), w - 44), F("csb", 19), fill=PAPER, anchor="lm")
        y0 = 32
    desc, kind = wmo(wx.code)
    night = not wx.is_day and kind in ("sun", "partly")
    isz = 76 if y0 < 20 else 64
    paste_icon(c, kind, 10, y0 + 2, isz, night)
    big = F("sb", 66 if y0 < 20 else 56)
    tx = 16 + isz
    text(d, (tx, y0 - 8), _deg(wx.temp), big)
    today = wx.days[0] if wx.days else None
    cy = y0 + (66 if y0 < 20 else 56)
    text(d, (14, cy + 6), ellipsize(d, desc, F("sb", 19), 170), F("sb", 19))
    if not (wx.alerts and wx.rain_note):
        text(d, (14, cy + 28), f"Feels {_deg(wx.feels)}" + (f"  ·  H {today.hi:.0f}  L {today.lo:.0f}" if today else ""),
             F("m", 16))

    # right column of small facts
    rx = 238
    facts = []                         # (today's rain % is the chart below)
    facts.append(("Wind", f"{WIND[int((wx.wind_dir + 22.5) // 45) % 8]} {wx.wind:.0f}"
                  + (f"–{wx.gust:.0f}" if wx.gust - wx.wind >= 8 else "")))
    facts.append(("Humid", f"{wx.humidity}%"))
    if today and today.sunset:
        # after sunset, show tomorrow's sunrise instead
        tm = time.localtime(ctx.now)
        if tm.tm_hour >= 12:
            facts.append(("Sunset", today.sunset.replace(" PM", "p").replace(" AM", "a")))
        else:
            facts.append(("Sunrise", today.sunrise.replace(" AM", "a").replace(" PM", "p")))
    fy = y0 + 2
    if w < 300:                        # compact corner: no facts column or rain chart
        facts = []
    for k, v in facts[:2 if wx.alerts else 3]:
        text(d, (rx, fy), k, F("cm", 16))
        text(d, (w - 12, fy), v, F("csb", 18), anchor="ra")
        dotted(d, rx + tw(d, k, F("cm", 16)) + 5, w - 14 - tw(d, v, F("csb", 18)) - 4, fy + 14)
        fy += 23
    # rain: nowcast pill under "Feels" + next-12h chance-of-rain bars under the facts column.
    # Both live in fixed space, so the 3-day row never moves (the pill used to push it off the bottom).
    top = h - 56                       # 3-day row
    if wx.rain_note:
        f = F("csb", 17)
        py = cy + 30 if wx.alerts else cy + 50   # under an alert banner the pill replaces "Feels"
        pill(d, 12, py, ellipsize(d, wx.rain_note, f, (rx if w >= 300 else w) - 34), f, h=21)
    if w >= 300:
        _rain_bars(d, wx.hourly, ctx.now, rx, w - 12, fy + 4, top - 7)
    # 3-day row
    days = wx.days[1:4 if w >= 300 else 3]
    if days:
        d.line((10, top - 4, w - 10, top - 4), fill=INK, width=1)
        cw = (w - 20) / len(days)
        for i, day in enumerate(days):
            x = 10 + i * cw
            t = time.strptime(day.date, "%Y-%m-%d")
            dn, dk = wmo(day.code)
            text(d, (x + 4, top), time.strftime("%a", t).upper(), F("cb", 18))
            text(d, (x + cw - 8, top + 2), f"{day.pop}%", F("cm", 16), anchor="ra")
            paste_icon(c, dk, int(x + 4), int(top + 21), 32)
            hs = f"{day.hi:.0f}°"
            text(d, (x + 44, top + 24), hs, F("sb", 20))
            text(d, (x + 44 + tw(d, hs, F("sb", 20)) + 4, top + 28), f"{day.lo:.0f}°", F("m", 16))
            if i:
                d.line((x - 2, top + 4, x - 2, h - 6), fill=INK, width=1)
    return c


def _rain_bars(d, hourly, now, x0, x1, y0, y1):
    """Next 12 hours of precipitation probability as bars, 50% dotted guide, hour ticks every 3h."""
    hrs = [hh for hh in hourly if hh[0] >= now - 1800][:12]
    if len(hrs) < 2 or x1 - x0 < 60:
        return
    fl = F("cm", 13)
    by1 = y1 - 13                      # bar baseline; hour labels below it
    bh = by1 - y0 - 13                 # headroom above for the peak label
    n = len(hrs)
    cw = (x1 - x0) / n
    dotted(d, x0, x1, int(by1 - bh / 2), 3)
    d.line((x0, by1, x1, by1), fill=INK)
    peak = max(range(n), key=lambda i: hrs[i][2])
    for i, (ts, _, pop, _) in enumerate(hrs):
        bx = x0 + i * cw
        hgt = pop / 100 * bh
        if hgt >= 1:
            d.rectangle((bx + 1, by1 - hgt, bx + cw - 2, by1), fill=INK)
        if i % 3 == 0:
            lt = time.localtime(ts)
            lab = "now" if i == 0 else f"{lt.tm_hour % 12 or 12}{'a' if lt.tm_hour < 12 else 'p'}"
            text(d, (bx + 1, by1 + 1), lab, fl)
    pk = hrs[peak][2]
    if pk >= 10:
        # label the peak just above its bar
        px = x0 + peak * cw + cw / 2
        s = f"{pk}%"
        hgt = pk / 100 * bh
        lx = min(max(px, x0 + tw(d, s, fl) / 2), x1 - tw(d, s, fl) / 2)
        text(d, (lx, by1 - hgt - 2), s, fl, anchor="md")


def _quote_cell(d, x, y, cw, q, spark_h=0, c=None):
    fname = F("cm", 15)
    text(d, (x + cw / 2, y), ellipsize(d, q.name, fname, cw - 4), fname, anchor="ma")
    pct = q.chg_pct
    s = f"{abs(pct):.2f}%"
    f = F("cb", 20)
    wv = tw(d, s, f) + 13
    sx = x + (cw - wv) / 2
    if abs(pct) >= 0.005:
        tri(d, sx, y + 23, 9, pct > 0)
    text(d, (sx + 13, y + 17), s, f)
    p = q.price
    ps = f"{p:,.0f}" if p >= 1000 else f"{p:,.2f}"
    text(d, (x + cw / 2, y + 40), ps, F("cr", 15), anchor="ma")
    if spark_h and q.spark and len(q.spark) > 2:
        sy0, sy1 = y + 60, y + 60 + spark_h
        lo, hi = min(q.spark + [q.prev]), max(q.spark + [q.prev])
        rng = (hi - lo) or 1
        n = 78   # a full session is 78 five-minute bars

        def P(i, v):
            return (x + 6 + (cw - 12) * i / (n - 1), sy1 - (v - lo) / rng * (sy1 - sy0))
        py = P(0, q.prev)[1]
        dotted(d, x + 6, x + cw - 6, py, 3)
        d.line([P(i, v) for i, v in enumerate(q.spark[:n])], fill=INK, width=2)


def panel_news(ctx: Ctx, size):
    c, d = canvas(size)
    w, h = size
    y = 6
    qs = ctx.quotes[:6 if w >= 300 else 3]
    if qs:
        cw = w / len(qs)
        for i, q in enumerate(qs):
            _quote_cell(d, i * cw, y, cw, q)
            if i:
                d.line((i * cw, y + 2, i * cw, y + 54), fill=INK, width=1)
        y += 60
        d.line((8, y, w - 8, y), fill=INK, width=2)
        y += 7
    heads = ctx.heads
    f = F("m", 17)
    lh = 19
    if not heads:
        text(d, (w / 2, (y + h) / 2), "No headlines", F("m", 17), anchor="mm")
        return c
    # rotate through the list, one page per scheduled update
    avail = h - y + 2
    pages, cur, used = [], [], 0
    for hd in heads[:30]:
        lines = wrap(d, hd.title, f, w - 34, 2)
        need = len(lines) * lh + 5
        if used + need > avail and cur:
            pages.append(cur)
            cur, used = [], 0
        cur.append(lines)
        used += need
    if cur:
        pages.append(cur)
    pages = pages[:ctx.cfg.get("news_pages", 4)]
    pg = pages[ctx.cycle % len(pages)]
    for lines in pg:
        d.rectangle((12, y + 7, 18, y + 13), fill=INK)
        for ln in lines:
            text(d, (28, y), ln, f)
            y += lh
        y += 5
    # page dots
    if len(pages) > 1 and not qs:
        cx = w - 12 - (len(pages) - 1) * 10
        dy = 63 if qs else 6
        d.rectangle((cx - 8, dy - 5, w - 4, dy + 5), fill=PAPER)
        for i in range(len(pages)):
            r = 3
            box = (cx + i * 10 - r, dy - r, cx + i * 10 + r, dy + r)
            if i == ctx.cycle % len(pages):
                d.ellipse(box, fill=INK)
            else:
                d.ellipse(box, outline=INK)
    return c


def _odds_bits(g, abbr, cfg, sport, full=False):
    o = g.odds if g else None
    if not o or not cfg.get("odds", True):
        return []
    style = cfg.get("odds_style", "spread")
    main = o.line(abbr, style, sport)
    bits = [main]
    if full:
        if o.ml_us is not None and f"{o.ml_us:+d}" not in main:
            bits.append(f"ML {o.ml_us:+d}")
        if o.win_pct is not None and "to win" not in main:
            bits.append(f"{o.win_pct}% to win")
    if o.total:
        bits.append(f"O/U {o.total:g}")
    return bits


def _sport(r):
    import sources
    return sources.TEAMS.get(r.key, ("", ""))[1]


def _team_lines(r, cfg=None):
    """(badge, primary, secondary) for one TeamRow."""
    cfg = cfg or {}

    def score(g):
        return f"{g.us}–{g.them}"

    def vs(g, name=True):
        who = g.opp_name if name else g.opp
        rk = f"#{g.opp_rank} " if g.opp_rank else ""
        return ("vs " if g.home else "at ") + rk + who
    if r.live:
        g = r.live
        bits = [g.detail] + ([g.tv] if g.tv else []) + _odds_bits(g, g.abbr, cfg, _sport(r))[:1]
        return "LIVE", f"{score(g)} {vs(g)}", " · ".join(bits)
    if r.fresh_result:
        g = r.last
        bits = []
        if g.ats and cfg.get("odds", True):
            bits.append(f"{g.ats} {g.odds.spread_us:+g}".replace("+0", "PK")
                        if _sport(r) not in ("hockey", "baseball") else ("won as " if g.result == "W" else "lost as ")
                        + ("fav" if (g.odds.ml_us or 0) < 0 else "dog"))
        if r.next:
            bits.append("Next: " + when(r.next.start, r.next.time_tbd) + " " + vs(r.next, False))
        elif g.note:
            bits.append(g.note)
        return g.result, f"{score(g)} {vs(g)}", " · ".join(bits)
    if r.next:
        g = r.next
        prim = when(g.start, g.time_tbd) + " " + vs(g)
        bits = []
        if g.note:
            bits.append(g.note.replace(" - ", " "))
        if g.tv:
            bits.append(g.tv)
        odds = _odds_bits(g, g.abbr, cfg, _sport(r))
        bits += odds
        if r.last and not g.note and not odds:
            bits.append(f"last {r.last.result} {score(r.last)} {r.last.opp}")
        return None, prim, " · ".join(bits)
    return None, "No games scheduled", ""


def panel_sports(ctx: Ctx, size, rows=None):
    c, d = canvas(size)
    w, h = size
    teams = ctx.teams
    if not teams:
        text(d, (w / 2, h / 2), "No games in the next few weeks", F("m", 17), anchor="mm")
        return c
    per = rows or max(1, min(4, len(teams)))
    pages = [teams[i:i + per] for i in range(0, len(teams), per)]
    pg = pages[ctx.cycle % len(pages)]
    rh = (h - 6) / per
    y = 3
    for i, r in enumerate(pg):
        badge, prim, sec = _team_lines(r, ctx.cfg)
        g = r.live or (r.last if r.fresh_result else None) or r.next
        lsz = int(min(42, rh - 8))
        if paste_logo(c, r.logo, 10, y + (rh - 6 - lsz) / 2 + 1, lsz, ctx.cfg):
            tx = 10 + lsz + 10
            sec = " · ".join(b for b in (r.record, sec) if b)
        else:
            text(d, (12, y + 4), r.label.upper(), F("cb", 21))
            if r.record:
                text(d, (12, y + 27), r.record, F("cm", 15))
            tx = 108
        right = w - 10
        osz = 30
        if g and g.opp_logo and paste_logo(c, g.opp_logo, w - 10 - osz, y + (rh - 6 - osz) / 2 + 1, osz, ctx.cfg):
            right = w - 10 - osz - 8
        x = tx
        if badge:
            fb = F("cb", 17)
            x = pill(d, x, y + 4, badge, fb, pad=5, h=24) + 7
        text(d, (x, y + 3), ellipsize(d, prim, F("sb", 19), right - x), F("sb", 19))
        if sec:
            text(d, (tx, y + 27), ellipsize(d, sec, F("cm", 16), right - tx), F("cm", 16))
        y += rh
        if i < len(pg) - 1:
            dotted(d, 10, w - 10, int(y - 3), 3)
    if len(pages) > 1:
        text(d, (w - 8, h - 4), f"{ctx.cycle % len(pages) + 1}/{len(pages)}", F("cm", 13), anchor="rd")
    return c


def _flight_label(s):
    return s.flight_no or s.callsign


def panel_sky(ctx: Ctx, size):
    c, d = canvas(size)
    w, h = size
    if ctx.event is not None:
        return _event_card(c, d, ctx, size)
    s = ctx.featured
    if s is None:
        return _quiet_sky(c, d, ctx, size)
    # header: OVERHEAD NOW + flight number
    y = 6
    f_air = F("sb", 18)
    air = s.airline or "Private / unknown"
    fn = _flight_label(s)
    fnf = F("cb", 22)
    text(d, (12, y + 2), ellipsize(d, air, f_air, w - 40 - tw(d, fn, fnf)), f_air)
    text(d, (w - 12, y), fn, fnf, anchor="ra")
    y += 30
    if s.has_route:
        f = F("b", 46)
        ow = tw(d, s.origin, f)
        text(d, (12, y - 6), s.origin, f)
        ax = 12 + ow + 10
        arrow_right(d, ax, y + 22, 44, 11, width=4)
        text(d, (ax + 56, y - 6), s.dest, f)
        y += 50
        cities = f"{s.origin_city or s.origin} to {s.dest_city or s.dest}"
        text(d, (12, y), ellipsize(d, cities, F("m", 16), w - 24), F("m", 16))
        y += 22
        if s.progress is not None:
            bx0, bx1 = 12, w - 12
            d.rounded_rectangle((bx0, y + 2, bx1, y + 10), radius=4, outline=INK, width=2)
            px = bx0 + (bx1 - bx0) * s.progress
            d.rounded_rectangle((bx0, y + 2, max(bx0 + 8, px), y + 10), radius=4, fill=INK)
            y += 14
            if s.eta_min is not None:
                eta = f"{s.eta_min // 60}h {s.eta_min % 60:02d}m" if s.eta_min >= 60 else f"{s.eta_min} min"
                text(d, (12, y), f"{eta} to {s.dest_city or s.dest}", F("cm", 15))
                y += 18
    else:
        f = F("b", 38)
        text(d, (12, y - 4), s.callsign or "Unknown", f)
        y += 46
        text(d, (12, y), "Route unknown", F("m", 16))
        y += 24
    # bottom block: type, altitude, where to look
    by = h - 48
    d.line((10, by - 4, w - 10, by - 4), fill=INK, width=1)
    alt = f"{s.alt_ft:,} ft" if s.alt_ft else "ground"
    text(d, (12, by), ellipsize(d, s.aircraft or "Aircraft", F("sb", 17), w - 80), F("sb", 17))
    text(d, (12, by + 21), f"{alt} · {s.speed_kt:.0f} kt · {s.dist_nm * 1.15078:.1f} mi", F("cm", 16))
    from adsb import compass
    compass_arrow(c, w - 34, by + 20, 21, s.bearing)
    look = f"look {compass(s.bearing)}"
    text(d, (w - 62, by + 3), look, F("csb", 16), anchor="ra")
    text(d, (w - 62, by + 22), f"{s.elev_deg:.0f}° up", F("cm", 15), anchor="ra")
    return c


def _radar(c, d, cx, cy, r, blips, radius_nm, labels=False):
    d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=INK, width=2)
    d.ellipse((cx - r / 2, cy - r / 2, cx + r / 2, cy + r / 2), outline=INK, width=1)
    d.line((cx - r, cy, cx + r, cy), fill=INK)
    d.line((cx, cy - r, cx, cy + r), fill=INK)
    d.rectangle((cx - 3, cy - 3, cx + 3, cy + 3), fill=INK)
    text(d, (cx, cy - r - 2), "N", F("cb", 13), anchor="md")
    for b in blips:
        dist, brg = b[0], b[1]
        if dist > radius_nm:
            continue
        rr = dist / radius_nm * r
        x = cx + rr * math.sin(math.radians(brg))
        y = cy - rr * math.cos(math.radians(brg))
        d.ellipse((x - 3, y - 3, x + 3, y + 3), fill=INK)
        if labels and len(b) > 2 and b[2]:
            text(d, (x + 5, y - 7), b[2], F("cm", 13))


def _blips(ctx):
    out = []
    for b in ctx.nearby or []:
        if isinstance(b, dict):
            out.append((b.get("dist_nm", 0), b.get("bearing", 0), b.get("callsign", "")))
        else:
            out.append(tuple(b))
    return out


def _quiet_sky(c, d, ctx, size):
    w, h = size
    radius = ctx.cfg.get("radar_nm", 25)
    _radar(c, d, 80, h / 2 + 6, 66, _blips(ctx), radius)
    x = 168
    text(d, (x, 14), "QUIET SKY", F("cb", 24))
    n = len(_blips(ctx))
    text(d, (x, 44), f"{n} plane{'s' if n != 1 else ''} within {radius} nm", F("m", 16))
    text(d, (x, 66), f"{ctx.day_count} flew over today", F("m", 16))
    y = 96
    if ctx.recent:
        text(d, (x, y), "LAST OVERHEAD", F("cb", 15))
        y += 20
        for s in ctx.recent[:3]:
            route = f"{s.origin}–{s.dest}" if s.has_route else s.callsign
            text(d, (x, y), ellipsize(d, f"{_flight_label(s)}  {route}", F("sb", 16), w - x - 70), F("sb", 16))
            text(d, (w - 10, y + 1), ago(s.seen_at).replace(" ago", ""), F("cm", 15), anchor="ra")
            y += 21
    return c


def _event_card(c, d, ctx, size):
    w, h = size
    ev = ctx.event
    mins = max(0, int((ev.start - ctx.now) // 60))
    head = "SPACE STATION" if ev.kind == "iss" else "ROCKET LAUNCH"
    d.rectangle((0, 0, w, 30), fill=INK)
    text(d, (12, 15), head, F("cb", 22), fill=PAPER, anchor="lm")
    when_s = "now" if ev.start <= ctx.now else (f"in {mins} min" if mins < 90 else hm(ev.start))
    text(d, (w - 12, 15), when_s.upper(), F("cb", 22), fill=PAPER, anchor="rm")
    text(d, (12, 40), ellipsize(d, ev.title, F("b", 34), w - 110), F("b", 34))
    text(d, (12, 82), ellipsize(d, ev.subtitle or "", F("m", 17), w - 110), F("m", 17))
    lines = []
    if ev.where:
        lines.append(f"Look {ev.where}, {ev.height}".strip(", "))
    if ev.kind == "iss" and ev.duration_s:
        lines.append(f"Visible ~{max(1, ev.duration_s // 60)} min · peaks at {hm(ev.start)}")
    if ev.kind == "launch":
        lines.append(f"Liftoff {hm(ev.start)} · {ev.distance_nm:.0f} nm away · {ev.confidence}")
    y = 112
    for ln in lines:
        text(d, (12, y), ellipsize(d, ln, F("sb", 17), w - 110), F("sb", 17))
        y += 24
    compass_arrow(c, w - 52, 100, 38, ev.bearing)
    return c


def _sday(ts):
    t = time.localtime(ts)
    return f"{time.strftime('%a %b', t)} {t.tm_mday}"


JCAL_DEFAULT = ["hebdate", "shabbat", "parsha", "yomtov", "holiday", "roshchodesh", "fasts"]


def _jshow(ctx):
    v = ctx.cfg.get("jcal_show")
    return set(JCAL_DEFAULT if v is None else v)


def _jevents(ctx):
    show = _jshow(ctx)
    return [e for e in (getattr(ctx.shabbat, "events", None) or []) if e.kind in show]


def _jday(ds):
    t = time.strptime(ds, "%Y-%m-%d")
    return f"{time.strftime('%a %b', t)} {t.tm_mday}"


def _jrow(d, x, y, w, e, f, fb, dw=92):
    text(d, (x, y), _jday(e.date), fb)
    ex = e.extra
    exw = tw(d, ex, f) if ex else 0
    text(d, (x + dw, y), ellipsize(d, e.title, f, w - x - dw - exw - 18), f)
    if ex:
        text(d, (w - 12, y), ex, f, anchor="ra")


def panel_shabbat(ctx: Ctx, size):
    """Jewish calendar: Shabbat times up top, then whatever else jcal_show enables, as space allows."""
    c, d = canvas(size)
    w, h = size
    sh = ctx.shabbat
    show = _jshow(ctx)
    title = "SHABBAT" if "shabbat" in show else "JEWISH CALENDAR"
    text(d, (12, 6), title, F("cb", 22))
    if sh is None:
        text(d, (12, 50), "Times unavailable", F("sb", 20))
        text(d, (12, 78), "waiting for Hebcal", F("cm", 16))
        return c
    hx = 12 + tw(d, title, F("cb", 22)) + 14
    if "parsha" in show and sh.parsha:
        text(d, (w - 12, 8), ellipsize(d, sh.parsha, F("sb", 20), w - 12 - hx), F("sb", 20), anchor="ra")
    y = 34
    sub = [x for x in ((sh.hebdate if "hebdate" in show else ""), (sh.omer if "omer" in show else ""),
                       (f"Daf {sh.daf}" if "dafyomi" in show and sh.daf else "")) if x]
    if sub:
        text(d, (12, y + 1), ellipsize(d, " · ".join(sub), F("cm", 16), w - 24), F("cm", 16))
        y += 21
    d.line((10, y + 1, w - 10, y + 1), fill=INK, width=2)
    y += 6
    if "shabbat" in show:
        tf = F("b", 30 if sub else 34)
        rh = 44 if sub else 50
        for label, ts in (("Candle lighting", sh.candles), ("Havdalah", sh.havdalah)):
            text(d, (12, y + 1), label, F("csb", 17))
            text(d, (12, y + 21), _sday(ts) if ts else "—", F("m", 16))
            if ts:
                text(d, (w - 12, y + 2), hm(ts), tf, anchor="ra")
            y += rh
        d.line((10, y - 1, w - 10, y - 1), fill=INK, width=1)
        y += 3
    evs = _jevents(ctx)
    f, fb = F("cm", 16), F("csb", 16)
    rows = max(0, (h - 4 - y) // 21)
    if not evs and rows:
        text(d, (12, y), "Nothing else in the next few weeks", f)
    for e in evs[:rows]:
        _jrow(d, 12, y, w, e, f, fb)
        y += 21
    return c


# ================================================================== calendar (iCal)

def _day_label(ds, now):
    t0 = time.strftime("%Y-%m-%d", time.localtime(now))
    t1 = time.strftime("%Y-%m-%d", time.localtime(now + 86400))
    if ds == t0:
        return "TODAY"
    if ds == t1:
        return "TOMORROW"
    t = time.strptime(ds, "%Y-%m-%d")
    return f"{time.strftime('%A', t).upper()} {time.strftime('%b', t).upper()} {t.tm_mday}"


def _calm(d, w, y, line1, line2):
    for ln in wrap(d, line1, F("sb", 19), w - 24, 2):
        text(d, (12, y), ln, F("sb", 19))
        y += 23
    y -= 23
    text(d, (12, y + 26), ellipsize(d, line2, F("cm", 16), w - 24), F("cm", 16))


def _cal_rows(d, ctx, x, y, w, bottom, f=None, fb=None):
    """Day headings + 'time  title' rows until the space runs out. Returns y."""
    f, fb = f or F("m", 17), fb or F("cb", 15)
    evs = [e for e in ctx.calendar if e.end > ctx.now or e.all_day and e.day >= time.strftime("%Y-%m-%d", time.localtime(ctx.now))]
    last = None
    tcol = tw(d, "12:00 PM", F("csb", 16)) + 10
    for e in evs:
        need = 21 + (20 if e.day != last else 0)
        if y + need > bottom:
            break
        if e.day != last:
            text(d, (x, y + 2), _day_label(e.day, ctx.now), fb)
            y += 20
            last = e.day
        tm = "all day" if e.all_day else hm(e.start)
        text(d, (x, y), tm, F("csb", 16))
        text(d, (x + tcol, y - 1), ellipsize(d, e.title, f, w - x - tcol - 6), f)
        y += 21
    if not evs:
        text(d, (x, y), "Nothing on the calendar", f)
    return y


def panel_calendar(ctx: Ctx, size):
    c, d = canvas(size)
    w, h = size
    text(d, (12, 6), "CALENDAR", F("cb", 22))
    t = time.localtime(ctx.now)
    text(d, (w - 12, 8), f"{time.strftime('%a %b', t)} {t.tm_mday}", F("sb", 20), anchor="ra")
    d.line((10, 36, w - 10, 36), fill=INK, width=2)
    if ctx.calendar is None:
        _calm(d, w, 54, "Add your calendar link in settings", "Paste a private iCal (.ics) URL")
        return c
    _cal_rows(d, ctx, 12, 42, w - 6, h - 2)
    return c


def detail_calendar(ctx, size):
    c, d = canvas(size)
    w, h = size
    if ctx.calendar is None:
        _calm(d, w, 40, "Add your calendar link in settings", "Dashboard – Calendar feeds: paste a private iCal (.ics) URL")
        return c
    days = sorted({e.day for e in ctx.calendar})
    cols = [days[:2], days[2:5]]
    for i, ds in enumerate(cols):
        x = 14 + i * (w // 2)
        sub = Ctx(**{**ctx.__dict__, "calendar": [e for e in ctx.calendar if e.day in ds]})
        _cal_rows(d, sub, x, 8, x + w // 2 - 20, h - 4, F("m", 18), F("cb", 17))
        if i:
            d.line((x - 10, 8, x - 10, h - 8), fill=INK, width=1)
    return c


# ================================================================== trips (TripIt iCal)

def _dspan(a, b):
    ta, tb = time.localtime(a), time.localtime(b - 1)
    if ta.tm_mon == tb.tm_mon:
        return f"{time.strftime('%b', ta)} {ta.tm_mday}–{tb.tm_mday}"
    return f"{time.strftime('%b', ta)} {ta.tm_mday} – {time.strftime('%b', tb)} {tb.tm_mday}"


def _next_seg(trip, kind, now):
    return next((s for s in trip.segments if s.kind == kind and s.end > now), None)


def _map_dot(d, box, lv):
    """Tiny route map: origin, destination, the plane as a filled dot."""
    x0, y0, x1, y1 = box
    d.rectangle(box, outline=INK, width=1)
    if lv.olat is None:
        pts = [(lv.lat, lv.lon)]
    else:
        pts = [(lv.olat, lv.olon), (lv.dlat, lv.dlon), (lv.lat, lv.lon)]
    la = [p[0] for p in pts]; lo = [p[1] for p in pts]
    span = max(max(la) - min(la), max(lo) - min(lo), 1.0) * 1.25
    cla, clo = (max(la) + min(la)) / 2, (max(lo) + min(lo)) / 2
    sc = min(x1 - x0, y1 - y0) / span

    def xy(lat, lon):
        return ((x0 + x1) / 2 + (lon - clo) * sc, (y0 + y1) / 2 - (lat - cla) * sc)
    if lv.olat is not None:
        a, b = xy(lv.olat, lv.olon), xy(lv.dlat, lv.dlon)
        for i in range(0, 20, 2):     # dashed great-circle-ish line
            p = (a[0] + (b[0] - a[0]) * i / 20, a[1] + (b[1] - a[1]) * i / 20)
            q = (a[0] + (b[0] - a[0]) * (i + 1) / 20, a[1] + (b[1] - a[1]) * (i + 1) / 20)
            d.line((p, q), fill=INK, width=1)
        for (px, py), lab in ((a, lv.origin), (b, lv.dest)):
            d.rectangle((px - 2, py - 2, px + 2, py + 2), fill=INK)
            text(d, (px, py + 4), lab, F("cb", 12), anchor="ma")
    px, py = xy(lv.lat, lv.lon)
    d.ellipse((px - 5, py - 5, px + 5, py + 5), fill=INK)


def _live_block(c, d, ctx, seg, x, y, w, h, big=False):
    lv = ctx.live
    pill(d, x, y, "IN THE AIR", F("csb", 15), h=20)
    fx = x + tw(d, "IN THE AIR", F("csb", 15)) + 20
    text(d, (fx, y + 1), f"{seg.flight_no}", F("cb", 18))
    route = f"{seg.origin} – {seg.dest}"
    for sz in ((30, 26, 22) if big else (26, 22, 18)):
        f = F("b", sz)
        if fx + tw(d, seg.flight_no, F("cb", 18)) + 12 + tw(d, route, f) <= w - 12:
            break
    text(d, (w - 12, y - 4 + (26 - sz) // 2), route, f, anchor="ra")
    y += 30
    if lv is None:
        text(d, (x, y), "Live position not found yet", F("cm", 16))
        return y + 22
    alt = f"{lv.alt_ft:,} ft" if lv.alt_ft else "—"
    spd = f"{lv.speed_kt:.0f} kt" if lv.speed_kt else "—"
    text(d, (x, y), f"{alt} · {spd}", F("sb", 18))
    y += 22
    where = lv.place or f"{lv.lat:.2f}, {lv.lon:.2f}"
    text(d, (x, y), ellipsize(d, f"over {where}", F("cm", 16), w - x - 12), F("cm", 16))
    y += 22
    if lv.progress is not None:
        d.rounded_rectangle((x, y + 2, w - 12, y + 10), radius=4, outline=INK, width=2)
        px = x + (w - 12 - x) * lv.progress
        d.rounded_rectangle((x, y + 2, max(x + 8, px), y + 10), radius=4, fill=INK)
        text(d, (w - 12, y + 13), f"{lv.progress * 100:.0f}% · lands {hm(seg.end)}", F("cm", 15), anchor="ra")
        y += 32
    return y


def _seg_line(s):
    if s.kind == "flight":
        who = f"{s.airline} {s.flight_no}".strip() if s.airline else s.flight_no
        return f"{who} · {s.origin}–{s.dest}", f"{_sday(s.start)} {hm(s.start)}"
    if s.kind == "cruise":
        ports = " – ".join(x for x in (s.embark, s.disembark) if x)
        name = s.name + (f" · {s.ship}" if s.ship and s.ship not in s.name else "")
        return f"Cruise: {name}" + (f" · {ports}" if ports else ""), _dspan(s.start, s.end)
    if s.kind == "hotel":
        return f"Hotel: {s.name or s.title}", _dspan(s.start, s.end)
    return s.title, _sday(s.start)


def panel_trips(ctx: Ctx, size):
    c, d = canvas(size)
    w, h = size
    text(d, (12, 6), "NEXT TRIP", F("cb", 22))
    if ctx.trips is None:
        d.line((10, 36, w - 10, 36), fill=INK, width=2)
        _calm(d, w, 54, "Add your TripIt calendar link in settings", "TripIt – Settings – Calendar feeds (.ics)")
        return c
    if not ctx.trips:
        d.line((10, 36, w - 10, 36), fill=INK, width=2)
        _calm(d, w, 54, "No upcoming trips", "Trips added to TripIt show up here")
        return c
    tr = ctx.trips[0]
    text(d, (w - 12, 8), _dspan(tr.start, tr.end), F("sb", 20), anchor="ra")
    text(d, (12, 36), ellipsize(d, tr.name, F("sb", 20), w - 24), F("sb", 20))
    d.line((10, 62, w - 10, 62), fill=INK, width=2)
    y = 68
    air = next((s for s in tr.segments if s.kind == "flight" and s.start - 900 <= ctx.now <= s.end + 1800), None)
    if air is not None:
        y = _live_block(c, d, ctx, air, 12, y, w, h)
    else:
        fl = _next_seg(tr, "flight", ctx.now)
        if fl:
            text(d, (12, y), ellipsize(d, f"{fl.airline} {fl.flight_no}".strip(), F("csb", 18), w - 150), F("csb", 18))
            text(d, (w - 12, y - 3), f"{fl.origin} – {fl.dest}", F("b", 26), anchor="ra")
            text(d, (12, y + 24), f"{_sday(fl.start)} · departs {hm(fl.start)}", F("m", 17))
            y += 50
    for kind in ("cruise", "hotel"):
        s = _next_seg(tr, kind, ctx.now)
        if s and y + 21 <= h - 2:
            a, b = _seg_line(s)
            text(d, (12, y), ellipsize(d, a, F("cm", 16), w - 30 - tw(d, b, F("cm", 16))), F("cm", 16))
            text(d, (w - 12, y), b, F("cm", 16), anchor="ra")
            y += 21
    return c


def detail_trips(ctx, size):
    c, d = canvas(size)
    w, h = size
    if not ctx.trips:
        return panel_trips(ctx, size)
    tr = ctx.trips[0]
    air = next((s for s in tr.segments if s.kind == "flight" and s.start - 900 <= ctx.now <= s.end + 1800), None)
    lw = 470 if air else w
    text(d, (14, 6), ellipsize(d, tr.name, F("cb", 24), lw - 30), F("cb", 24))
    text(d, (14, 36), _dspan(tr.start, tr.end), F("m", 18))
    d.line((10, 62, lw - 10, 62), fill=INK, width=2)
    y = 70
    for s in tr.segments:
        if y > h - 26:
            break
        a, b = _seg_line(s)
        text(d, (14, y), b, F("csb", 17))
        text(d, (150, y - 1), ellipsize(d, a, F("m", 18), lw - 164), F("m", 18))
        y += 26
    if len(ctx.trips) > 1 and y < h - 26:
        nt = ctx.trips[1]
        text(d, (14, h - 24), ellipsize(d, f"Then: {nt.name} · {_dspan(nt.start, nt.end)}", F("cm", 16), lw - 28), F("cm", 16))
    if air:
        d.line((lw, 8, lw, h - 8), fill=INK, width=1)
        y2 = _live_block(c, d, ctx, air, lw + 12, 8, w, h, big=True)
        if ctx.live:
            _map_dot(d, (lw + 12, y2 + 2, w - 12, h - 10), ctx.live)
    return c


# ================================================================== scoreboard (page only)

def _sit_line(g):
    st = g.situation or {}
    bits = []
    if st.get("downDistanceText") or st.get("shortDownDistanceText"):
        bits.append(st.get("downDistanceText") or st.get("shortDownDistanceText"))
        if st.get("isRedZone"):
            bits.append("red zone")
    if "outs" in st:
        o = int(st.get("outs") or 0)
        bits.append(f"{o} out" + ("s" if o != 1 else ""))
        if "balls" in st:
            bits.append(f"{st.get('balls', 0)}-{st.get('strikes', 0)}")
    return " · ".join(str(b) for b in bits)


def _diamond(d, cx, cy, r, st):
    """Three bases as diamonds, filled when occupied."""
    for (dx, dy), k in (((r, 0), "onFirst"), ((0, -r), "onSecond"), ((-r, 0), "onThird")):
        x, y, q = cx + dx, cy + dy, r * 0.55
        poly = [(x, y - q), (x + q, y), (x, y + q), (x - q, y)]
        d.polygon(poly, fill=INK if st.get(k) else None, outline=INK, width=2)


def _team_side(c, d, ctx, x, y, size, logo, abbr, name, score, align):
    if not paste_logo(c, logo, x, y, size, ctx.cfg):
        d.rectangle((x, y, x + size, y + size), outline=INK, width=2)
        text(d, (x + size / 2, y + size / 2), abbr, F("cb", 20), anchor="mm")
    text(d, (x + size / 2, y + size + 4), ellipsize(d, name, F("csb", 17), size + 60), F("csb", 17), anchor="ma")


def _score_block(c, d, ctx, row, g, box, big=True):
    x0, y0, x1, y1 = box
    w = x1 - x0
    ls = 84 if big else 56
    us_name, them_name = row.label, g.opp_name
    left = (g.our_logo or row.logo, g.abbr, us_name, g.us)
    right = (g.opp_logo, g.opp, them_name, g.them)
    if g.home:   # away team on the left, like a stadium board
        left, right = right, left
    ty = y0 + 8
    _team_side(c, d, ctx, x0 + 30, ty, ls, *left, "l")
    _team_side(c, d, ctx, x1 - 30 - ls, ty, ls, *right, "r")
    sf = F("b", 76 if big else 50)
    sc = f"{'' if left[3] is None else left[3]}  –  {'' if right[3] is None else right[3]}"
    cx = (x0 + x1) / 2
    text(d, (cx, ty - 6), sc, sf, anchor="ma")
    sy = ty + (86 if big else 58)
    state = g.detail or ("Final" if g.state == "post" else "")
    text(d, (cx, sy), state, F("cb", 24 if big else 19), anchor="ma")
    sy += 30 if big else 24
    sit = _sit_line(g) if g.state == "in" else ""
    if sit:
        text(d, (cx, sy), ellipsize(d, sit, F("sb", 20), w - 2 * (ls + 60)), F("sb", 20), anchor="ma")
        sy += 26
    if g.state == "in" and any(k in (g.situation or {}) for k in ("onFirst", "onSecond", "onThird")):
        _diamond(d, cx, sy + 22, 13, g.situation)
        sy += 42
    foot = " · ".join(x for x in (g.tv, g.note) if x)
    if foot:
        text(d, (cx, y1 - 22), ellipsize(d, foot, F("cm", 16), w - 40), F("cm", 16), anchor="ma")
    lp = (g.situation or {}).get("lastPlay") if big and g.state == "in" else ""
    if lp and sy < y1 - 48:
        for i, ln in enumerate(wrap(d, lp, F("m", 16), w - 60, 2)):
            text(d, (cx, sy + 2 + i * 19), ln, F("m", 16), anchor="ma")


def panel_scoreboard(ctx: Ctx, size):
    """Corner version: the live game (or last final) as a two-line board, then what's next."""
    c, d = canvas(size)
    w, h = size
    live = [r for r in ctx.teams if r.live]
    if live:
        r = live[ctx.cycle % len(live)]
        g = r.live
        pill(d, 10, 7, "LIVE", F("csb", 15), h=20)
        hd = g.detail
    else:
        finals = [r for r in ctx.teams if r.last]
        if not finals:
            text(d, (12, 6), "SCOREBOARD", F("cb", 22))
            d.line((10, 36, w - 10, 36), fill=INK, width=2)
            nx = sorted((r for r in ctx.teams if r.next), key=lambda r: r.next.start)
            if not nx:
                text(d, (12, 50), "No games coming up", F("sb", 19))
                return c
            r = nx[0]
            text(d, (12, 46), ellipsize(d, f"{r.label} {'vs' if r.next.home else 'at'} {r.next.opp_name}", F("sb", 20), w - 24), F("sb", 20))
            text(d, (12, 72), when(r.next.start, r.next.time_tbd), F("m", 17))
            if r.next.tv:
                text(d, (12, 94), r.next.tv, F("cm", 16))
            return c
        r = max(finals, key=lambda r: r.last.start)
        g = r.last
        text(d, (12, 6), "FINAL", F("cb", 22))
        hd = _sday(g.start)
    text(d, (w - 12, 8), ellipsize(d, hd, F("csb", 18), w - 90), F("csb", 18), anchor="ra")
    d.line((10, 34, w - 10, 34), fill=INK, width=2)
    us = (g.our_logo or r.logo, r.label, g.us)
    them = (g.opp_logo, g.opp_name, g.them)
    rows = [them, us] if g.home else [us, them]
    sf = F("b", 40 if w >= 300 else 34)
    y = 42
    for logo, name, sc in rows:
        if not paste_logo(c, logo, 12, y, 38, ctx.cfg):
            d.rectangle((12, y, 50, y + 38), outline=INK, width=2)
        scs = "" if sc is None else str(sc)
        sw = tw(d, scs, sf)
        text(d, (60, y + 8), ellipsize(d, name, F("sb", 20), w - 60 - sw - 22), F("sb", 20))
        text(d, (w - 12, y - 4), scs, sf, anchor="ra")
        y += 46
    sit = _sit_line(g) if g.state == "in" else ""
    foot = " · ".join(x for x in (sit, g.tv) if x)
    if foot:
        text(d, (12, y + 2), ellipsize(d, foot, F("cm", 16), w - 24), F("cm", 16))
        y += 22
    if not live:
        nx = sorted((t for t in ctx.teams if t.next), key=lambda t: t.next.start)
        if nx and y + 20 <= h:
            n = nx[0]
            text(d, (12, y + 2), ellipsize(d, f"Next: {n.label} {'vs' if n.next.home else 'at'} {n.next.opp} · {when(n.next.start, n.next.time_tbd)}", F("cm", 16), w - 24), F("cm", 16))
    return c


def detail_scoreboard(ctx, size):
    c, d = canvas(size)
    w, h = size
    live = [r for r in ctx.teams if r.live]
    if live:
        n = len(live)
        if n == 1:
            _score_block(c, d, ctx, live[0], live[0].live, (0, 0, w, h))
        else:
            k = (ctx.cycle * 2) % n if n > 2 else 0     # >2 live: show two at a time, rotating each update
            pair = [live[k % n], live[(k + 1) % n]]
            for i, r in enumerate(pair):
                _score_block(c, d, ctx, r, r.live, (0, i * h // 2, w, (i + 1) * h // 2), big=False)
            d.line((10, h // 2, w - 10, h // 2), fill=INK, width=2)
            if n > 2:
                text(d, (w - 12, 4), f"{n} live · rotating", F("cm", 15), anchor="ra")
        return c
    finals = [r for r in ctx.teams if r.last]
    nexts = [r for r in ctx.teams if r.next]
    if not finals and not nexts:
        text(d, (w / 2, h / 2), "No games scheduled for your teams", F("sb", 24), anchor="mm")
        return c
    if finals:
        r = max(finals, key=lambda r: r.last.start)
        text(d, (14, 6), f"LAST FINAL · {_sday(r.last.start).upper()}", F("cb", 18))
        _score_block(c, d, ctx, r, r.last, (0, 26, w, 26 + 200), big=False)
    y = 236 if finals else 10
    d.line((10, y - 6, w - 10, y - 6), fill=INK, width=2)
    text(d, (14, y), "NEXT UP", F("cb", 18))
    y += 28
    for r in sorted(nexts, key=lambda r: r.next.start)[: (h - y) // 44]:
        g = r.next
        vs = "vs" if g.home else "at"
        paste_logo(c, g.opp_logo, 14, y, 36, ctx.cfg)
        text(d, (60, y), ellipsize(d, f"{r.label} {vs} {g.opp_name}", F("sb", 21), 380), F("sb", 21))
        text(d, (60, y + 22), when(g.start, g.time_tbd), F("cm", 16))
        if g.tv:
            text(d, (w - 14, y + 4), g.tv, F("csb", 19), anchor="ra")
        y += 44
    return c


# ================================================================== shopping list (page only)

def _box(d, x, y, s, checked):
    d.rectangle((x, y, x + s, y + s), outline=INK, width=2)
    if checked:
        d.line((x + 3, y + s / 2, x + s / 2 - 1, y + s - 4), fill=INK, width=3)
        d.line((x + s / 2 - 1, y + s - 4, x + s - 3, y + 3), fill=INK, width=3)


def detail_shopping(ctx, size):
    c, d = canvas(size)
    w, h = size
    items = ctx.shopping or []
    todo = [i for i in items if not i.get("done")]
    done = [i for i in items if i.get("done")]
    text(d, (14, 6), f"{len(todo)} to get" + (f" · {len(done)} in the cart" if done else ""), F("csb", 18))
    if not items:
        text(d, (14, 60), "The list is empty", F("sb", 26))
        text(d, (14, 98), "Add items from the dashboard or with Siri", F("cm", 17))
        return c
    rows = [(i, False) for i in todo] + [(i, True) for i in done]
    big = len(rows) <= 8
    rh = 40 if big else 30
    per_col = max(1, (h - 40) // rh)
    ncol = 1 if len(rows) <= per_col else 2
    cw = (w - 20) // ncol
    f_todo, f_done = F("sb", 28 if big else 21), F("m", 19 if big else 17)
    shown = 0
    for k, (it, ck) in enumerate(rows[: per_col * ncol]):
        col, row = divmod(k, per_col)
        x, y = 14 + col * cw, 36 + row * rh
        bs = 22 if big else 17
        _box(d, x, y + (rh - bs) // 2 - 3, bs, ck)
        f = f_done if ck else f_todo
        s = ellipsize(d, it.get("text", ""), f, cw - bs - 24)
        tx = x + bs + 10
        ty = y + (rh - bs) // 2 - 3 + bs / 2
        text(d, (tx, ty), s, f, anchor="lm")
        if ck:
            d.line((tx, ty, tx + tw(d, s, f), ty), fill=INK, width=2)
        shown += 1
    if ncol == 2:
        d.line((14 + cw - 8, 40, 14 + cw - 8, h - 6), fill=INK, width=1)
    if shown < len(rows):
        text(d, (w - 14, 8), f"+{len(rows) - shown} more", F("cm", 16), anchor="ra")
    return c


def panel_shopping(ctx: Ctx, size):
    """Corner summary of the shopping list: what is still to get, checked items hidden."""
    c, d = canvas(size)
    w, h = size
    items = ctx.shopping or []
    todo = [i for i in items if not i.get("done")]
    text(d, (12, 6), "SHOPPING", F("cb", 22))
    text(d, (w - 12, 8), f"{len(todo)} to get" if todo else "", F("sb", 20), anchor="ra")
    d.line((10, 36, w - 10, 36), fill=INK, width=2)
    if not todo:
        _calm(d, w, 54, "Nothing on the list", "Add items from the dashboard or with Siri")
        return c
    rh, bs = 27, 15
    per_col = max(1, (h - 46) // rh)
    ncol = 1 if len(todo) <= per_col or w < 300 else 2
    cw = (w - 20) // ncol
    f = F("m", 19)
    cap = per_col * ncol
    show = todo[:cap - 1] if len(todo) > cap else todo
    for k, it in enumerate(show):
        col, row = divmod(k, per_col)
        x, y = 12 + col * cw, 44 + row * rh
        _box(d, x, y + (rh - bs) // 2 - 2, bs, False)
        text(d, (x + bs + 8, y + rh / 2 - 2), ellipsize(d, it.get("text", ""), f, cw - bs - 18), f, anchor="lm")
    if len(show) < len(todo):
        col, row = divmod(len(show), per_col)
        text(d, (12 + col * cw, 44 + row * rh + rh / 2 - 2), f"+{len(todo) - len(show)} more", F("cm", 17), anchor="lm")
    return c


# ================================================================== Young Israel of Teaneck newsletter
# ctx.shul is shul.json from shul.py: {"issue_date", "issue_range", "personal": {kind: [items]},
#                                      "sections": [{"title", "kind", "items"}]}
# (legacy {"highlights": [(title, body), ...]} fixtures are still accepted)

SHUL_ORDER = [("mazal_tov", "MAZAL TOV"), ("birthdays", "BIRTHDAYS"), ("anniversaries", "ANNIVERSARIES"),
              ("welcome", "WELCOME"), ("refuah", "REFUAH SHLEIMAH"), ("condolences", "CONDOLENCES"),
              ("kiddush", "KIDDUSH SPONSORS")]


def _issue_label(data):
    ds = (data or {}).get("issue_date") or ""
    try:
        t = time.strptime(ds, "%Y-%m-%d")
        return f"Week of {time.strftime('%b', t)} {t.tm_mday}"
    except ValueError:
        return ""


def _shul_blocks(data, personal=True, events=True):
    """[(TITLE, [item, ...])]: personal/community news first, then what's coming up."""
    import re
    data = data or {}
    out = []
    if data.get("highlights") or data.get("items"):          # legacy simple shape
        for it in data.get("highlights") or data.get("items"):
            if isinstance(it, dict):
                t, b = str(it.get("title", "")), str(it.get("body") or it.get("text") or "")
            else:
                t, b = (list(it) + ["", ""])[:2]
            out.append((str(t).upper(), [str(b)] if b else []))
        return out
    p = data.get("personal") or {}
    if personal:
        for k, title in SHUL_ORDER:
            if p.get(k):
                items = p[k]
                if k == "kiddush":     # keep the 'in honor / in memory' lines, they're the community news
                    items = [i for i in items if re.search(r"honou?r|memory|yahrzeit|l'iluy|celebrat", i, re.I)] or items
                if k == "mazal_tov":
                    items = [re.sub(r"^Ma[zt]+[ae]l Tov to ", "", i) for i in items]
                out.append((title, items))
    if events:
        secs = data.get("sections") or []
        evs = [s for s in secs if s.get("kind") == "event"]
        if evs:
            out.append(("COMING UP", [s["title"] for s in evs]))
        for s in secs:
            if s.get("kind") in ("learning", "youth", "community") and s.get("items"):
                out.append((s["title"].upper(), s["items"]))
    return out


def _shul_flow(d, blocks, x, y, right, bottom, f=None, lines_per=3, tf=None):
    """Flow pill-headed blocks of bulleted items between x..right until bottom.
    Returns (y, blocks_started, items_drawn_in_last_block) so callers can continue in another column."""
    f = f or F("cm", 16)
    tf = tf or F("cb", 14)
    lh = f.size + 2
    nb, last = 0, 0
    for title, items in blocks:
        if y + 21 + lh > bottom:
            break
        pill(d, x, y, ellipsize(d, title, tf, right - x - 12), tf, pad=4)
        y += tf.size + 7
        nb, last = nb + 1, 0
        for it in items:
            if y + lh > bottom:
                break
            ls = wrap(d, it, f, right - x - 12, lines_per)
            ls = ls[: max(1, (bottom - y) // lh)]
            d.rectangle((x + 1, y + lh // 2 - 2, x + 4, y + lh // 2 + 1), fill=INK)
            for ln in ls:
                text(d, (x + 10, y), ln, f)
                y += lh
            y += 2
            last += 1
        y += 5
    return y, nb, last


def _nl_header(d, w, data, title, f):
    text(d, (12, 8), title, f)
    lab = (data or {}).get("issue_range") or _issue_label(data)
    if lab:
        text(d, (w - 12, 11), ellipsize(d, lab, F("cm", 14), w // 2), F("cm", 14), anchor="ra")


def panel_newsletter(ctx: Ctx, size):
    """Young Israel of Teaneck weekly newsletter (corner; tall in thirds): personal/community news, then coming up."""
    w, h = size
    c, d = canvas(size)
    data = getattr(ctx, "shul", None)
    _nl_header(d, w, data, "NEWSLETTER", F("cb", 18))
    blocks = _shul_blocks(data)
    if not blocks:
        _calm(d, w, 50, "Newsletter loads Thursday", "Fetched Thursdays 8 PM ET")
        return c
    y, nb, last = _shul_flow(d, blocks, 12, 36, w - 4, h - 22, F("cm", 15), lines_per=2)
    left = sum(len(i) for _, i in blocks[nb:]) + (len(blocks[nb - 1][1]) - last if nb else 0)
    if left > 0:
        text(d, (w - 12, h - 6), f"+{left} more  ·  newsletter page", F("cm", 13), anchor="rd")
    return c


PANELS = {"weather": panel_weather, "news": panel_news, "sports": panel_sports, "sky": panel_sky,
          "shabbat": panel_shabbat, "calendar": panel_calendar, "trips": panel_trips,
          "scoreboard": panel_scoreboard, "shopping": panel_shopping,
          "newsletter": panel_newsletter}
CORNER_PANELS = list(PANELS)          # shopping last, so short slot lists never pick it by default


# ================================================================== chrome

def header(ctx: Ctx):
    c, d = canvas((W, HEAD_H))
    d.rectangle((0, 0, W, HEAD_H), fill=INK)
    t = time.localtime(ctx.now)
    date = time.strftime("%A, %B ", t) + str(t.tm_mday)
    text(d, (14, HEAD_H / 2), date, F("sb", 22), fill=PAPER, anchor="lm")
    # right side, laid out right-to-left: updated time, battery, indoor
    f = F("m", 17)
    sep = "   ·   "
    x = W - 14
    s = f"Updated {hm(ctx.updated or ctx.now)}"
    text(d, (x, HEAD_H / 2), s, f, fill=PAPER, anchor="rm")
    x -= tw(d, s, f)
    if ctx.battery is not None:
        x -= tw(d, sep, f)
        text(d, (x, HEAD_H / 2), sep, f, fill=PAPER, anchor="lm")
        pct = f"{ctx.battery}%" + ("  plug me in" if ctx.battery <= 20 else "")
        x -= tw(d, pct, f)
        text(d, (x, HEAD_H / 2), pct, f, fill=PAPER, anchor="lm")
        # battery glyph: outline, nub, fill proportional to charge
        bw, bh = 26, 13
        x -= bw + 9
        y0 = HEAD_H / 2 - bh / 2
        d.rounded_rectangle((x, y0, x + bw, y0 + bh), radius=2, outline=PAPER, width=2)
        d.rectangle((x + bw + 1, y0 + 4, x + bw + 3, y0 + bh - 4), fill=PAPER)
        fillw = max(1, round((bw - 6) * min(100, ctx.battery) / 100))
        d.rectangle((x + 3, y0 + 3, x + 3 + fillw, y0 + bh - 3), fill=PAPER)
    if ctx.indoor_f is not None:
        x -= tw(d, sep, f)
        text(d, (x, HEAD_H / 2), sep, f, fill=PAPER, anchor="lm")
        s = f"Inside {ctx.indoor_f:.0f}°" + (f" {ctx.indoor_rh:.0f}%" if ctx.indoor_rh is not None else "")
        text(d, (x, HEAD_H / 2), s, f, fill=PAPER, anchor="rm")
    if ctx.notes_new:
        f = F("csb", 17)
        s = f"{ctx.notes_new} new note{'s' if ctx.notes_new > 1 else ''}"
        x = 14 + tw(d, date, F("sb", 22)) + 18
        pill(d, x, 8, s, f, fill=PAPER, fg=INK, h=22)
    return c


def strip(ctx: Ctx):
    """The one line under the header: sticky message > severe alert > pinned > brief."""
    c, d = canvas((W, STRIP_H))
    msg, bold = "", False
    pinned = " ".join(str(ctx.cfg.get("pinned") or "").split())
    if pinned and not ctx.sticky and not (ctx.weather is not None and ctx.weather.alerts
                                          and ctx.weather.alerts[0][2] in ("Extreme", "Severe")):
        f = F("csb", 15)
        lw = tw(d, "PINNED", f)
        pill(d, 10, 4, "PINNED", f, pad=5, h=STRIP_H - 9)
        text(d, (lw + 30, STRIP_H / 2 - 1), ellipsize(d, pinned, F("sb", 18), W - lw - 44), F("sb", 18), anchor="lm")
        d.line((0, STRIP_H - 1, W, STRIP_H - 1), fill=INK, width=2)
        return c
    if ctx.sticky:
        msg, bold = ctx.sticky, True
    elif ctx.weather is not None and ctx.weather.alerts and ctx.weather.alerts[0][2] in ("Extreme", "Severe"):
        msg, bold = ctx.weather.alerts[0][1], True
    elif ctx.brief:
        msg = ctx.brief
    if not msg:
        up = _next_up(ctx)
        msg = up or ""
    if bold:
        d.rectangle((0, 0, W, STRIP_H - 1), fill=INK)
        tri(d, 12, 6, 15, True, fill=PAPER)
        d.text((19.5, 17), "!", font=F("b", 11), fill=INK, anchor="mm")
        text(d, (36, STRIP_H / 2), ellipsize(d, msg, F("sb", 18), W - 50), F("sb", 18), fill=PAPER, anchor="lm")
    else:
        text(d, (14, STRIP_H / 2 - 1), ellipsize(d, msg, F("m", 18), W - 28), F("m", 18), anchor="lm")
        d.line((0, STRIP_H - 1, W, STRIP_H - 1), fill=INK, width=2)
    return c


def _next_up(ctx):
    for r in ctx.teams:
        if r.live:
            return f"{r.label} playing now: {r.live.us}–{r.live.them} {r.live.detail}"
    for r in ctx.teams:
        if r.next and r.next.start - ctx.now < 12 * 3600:
            return f"{r.label} {when(r.next.start, r.next.time_tbd).lower()} vs {r.next.opp_name}"
    if ctx.heads:
        return ctx.heads[0].title
    return ""


LAYOUTS = {"quad": 4, "six": 6, "nine": 9, "thirds": 7}
GRID_DIMS = {"quad": (2, 2), "six": (3, 2), "nine": (3, 3)}


def layout_of(cfg) -> str:
    lay = cfg.get("layout") or DEFAULT_LAYOUT
    return lay if lay in LAYOUTS else DEFAULT_LAYOUT


def slot_boxes(layout="quad"):
    """Slot rectangles in layout units."""
    if layout == "thirds":
        # Left column: Shabbat SHORT on top (rows 0-1 = 1/3 body height; times are compact)
        # + newsletter TALL below (rows 1-3 = 2/3 body height, reading room).
        # Middle: weather / news / trips (1/3 each).
        # Right: sports (tall) + scoreboard — two panels; room for a third later.
        xs = [0, W // 3, 2 * W // 3, W]
        ch = (H - GRID_Y) // 3
        ys = [GRID_Y, GRID_Y + ch, GRID_Y + 2 * ch, H]
        return [
            (xs[0], ys[0], xs[1], ys[1]),  # shabbat short (top 1/3)
            (xs[0], ys[1], xs[1], ys[3]),  # newsletter tall (bottom 2/3)
            (xs[1], ys[0], xs[2], ys[1]),  # weather
            (xs[1], ys[1], xs[2], ys[2]),  # news
            (xs[1], ys[2], xs[2], ys[3]),  # trips
            (xs[2], ys[0], xs[3], ys[2]),  # sports tall
            (xs[2], ys[2], xs[3], ys[3]),  # scoreboard
        ]
    if layout == "nine":
        xs = [0, W // 3, 2 * W // 3, W]
        ch = (H - GRID_Y) // 3
        ys = [GRID_Y, GRID_Y + ch, GRID_Y + 2 * ch, H]
        return [(xs[c], ys[r], xs[c + 1], ys[r + 1]) for r in range(3) for c in range(3)]
    if layout == "six":
        xs = [0, W // 3, 2 * W // 3, W]
        return [(xs[c], GRID_Y + r * CELL_H, xs[c + 1], GRID_Y + (r + 1) * CELL_H) for r in range(2) for c in range(3)]
    return [(0, GRID_Y, CELL_W, GRID_Y + CELL_H), (CELL_W, GRID_Y, W, GRID_Y + CELL_H),
            (0, GRID_Y + CELL_H, CELL_W, H), (CELL_W, GRID_Y + CELL_H, W, H)]


def _urgent(name, ctx):
    """A state worth holding a rotating corner on."""
    if name in ("sports", "scoreboard"):
        return any(r.live for r in ctx.teams)
    if name == "trips":
        tr = ctx.trips or []
        return any(s.kind == "flight" and s.start - 900 <= ctx.now <= s.end + 1800 for t in tr for s in t.segments)
    if name == "shabbat":
        sh = ctx.shabbat
        hrs = float(ctx.cfg.get("rotate_pin_hours", 3)) * 3600
        ts = [t for t in (getattr(sh, "candles", None), getattr(sh, "holiday_candles", None)) if t]
        return any(-1800 <= t - ctx.now <= hrs for t in ts)
    if name == "weather":
        wx = ctx.weather
        return bool(wx is not None and wx.alerts and wx.alerts[0][2] in ("Extreme", "Severe"))
    if name == "sky":
        return ctx.event is not None
    return False


def resolve_slots(ctx) -> list[str]:
    """Which panel each corner shows now: fixed slots, or slot_rotation cycling on rot_step,
    with live/urgent panels pinned and no panel on screen twice."""
    cfg = ctx.cfg
    layout = layout_of(cfg)
    n = LAYOUTS[layout]
    rot = cfg.get("slot_rotation") or []
    if not (isinstance(rot, list) and len(rot) == n and all(rot)):
        sl = list(cfg.get("slots") or [])
        rot = [[x] for x in sl[:n]]
        pool = [p for p in CORNER_PANELS if p not in sl]
        while len(rot) < n:                      # short slot list (e.g. switched to six): fill calmly
            rot.append([pool.pop(0)] if pool else ["weather"])
    rot = [[p for p in lst if p in PANELS] or ["weather"] for lst in rot]
    out, used = [None] * n, set()
    if cfg.get("rotate_pin_live", True):
        for i, lst in enumerate(rot):
            if len(lst) > 1:
                hit = next((p for p in lst if p not in used and _urgent(p, ctx)), None)
                if hit:
                    out[i] = hit
                    used.add(hit)
    for i, lst in enumerate(rot):            # fixed corners claim theirs first
        if out[i] is None and len(lst) == 1 and lst[0] not in used:
            out[i] = lst[0]
            used.add(lst[0])
    for i, lst in enumerate(rot):
        if out[i] is not None:
            continue
        k = ctx.rot_step % len(lst)
        pick = next((lst[(k + j) % len(lst)] for j in range(len(lst)) if lst[(k + j) % len(lst)] not in used), None)
        if pick is None:
            pick = next((p for p in CORNER_PANELS if p not in used), lst[0])
        out[i] = pick
        used.add(pick)
    return out


def compose_quad(ctx: Ctx, panels: dict | None = None) -> Image.Image:
    """panels: optional {name: prerendered L image} to reuse (not re-render)."""
    c = Image.new("L", (PW, PH), PAPER)
    c.paste(header(ctx), (0, 0))
    c.paste(strip(ctx), (0, px(HEAD_H)))
    layout = layout_of(ctx.cfg)
    boxes = slot_boxes(layout)
    for name, box in zip(resolve_slots(ctx), boxes):
        size = (box[2] - box[0], box[3] - box[1])
        img = (panels or {}).get(name) if size == (CELL_W, CELL_H) else None
        if img is None:
            img = PANELS[name](ctx, size)
        c.paste(img, (px(box[0]), px(box[1])))
    d = draw(c)
    # horizontal rules per box top edge, so tall slots (thirds) aren't cut through
    for x0, y, x1, _ in boxes:
        if y > GRID_Y:
            d.line((x0 + (6 if x0 == 0 else 0), y, x1 - (6 if x1 >= W else 0), y), fill=INK, width=2)
    for x in sorted({b[0] for b in boxes if b[0] > 0}):
        d.line((x - 1, GRID_Y + 6, x - 1, H - 6), fill=INK, width=2)
    return c


# ================================================================== detail pages

def detail_weather(ctx, size):
    from sources import wmo
    c, d = canvas(size)
    w, h = size
    left = panel_weather(ctx, (400, 207))
    c.paste(left, (0, 0))
    wx = ctx.weather
    if wx is None:
        return c
    # 24h chart on the right
    x0, x1, y0, y1 = 430, w - 20, 26, 190
    text(d, (x0, 4), "NEXT 24 HOURS", F("cb", 17))
    hrs = [hh for hh in wx.hourly if hh[0] >= ctx.now - 1800][:24]
    if len(hrs) > 2:
        temps = [t for _, t, _, _ in hrs]
        lo, hi = min(temps) - 2, max(temps) + 2
        n = len(hrs)

        def X(i):
            return x0 + (x1 - x0) * i / (n - 1)

        def Y(t):
            return y1 - 48 - (t - lo) / (hi - lo) * (y1 - 48 - y0 - 14)
        for i, (_, _, pop, _) in enumerate(hrs):
            bh = pop / 100 * 40
            if bh >= 1:
                d.rectangle((X(i) - 4, y1 - bh, X(i) + 4, y1), fill=INK)
        d.line((x0, y1, x1, y1), fill=INK)
        d.line([(X(i), Y(t)) for i, t in enumerate(temps)], fill=INK, width=3)
        for i in range(0, n, 3):
            ts, t, pop, code = hrs[i]
            text(d, (X(i), Y(t) - 6), f"{t:.0f}°", F("csb", 15), anchor="md")
            lt = time.localtime(ts)
            text(d, (X(i), y1 + 3), f"{lt.tm_hour % 12 or 12}{'a' if lt.tm_hour < 12 else 'p'}", F("cm", 14), anchor="ma")
        text(d, (x1, 4), "bars: chance of rain", F("cr", 14), anchor="ra")
    # 5-day strip
    days = wx.days[:5]
    top = 222
    d.line((10, top - 6, w - 10, top - 6), fill=INK, width=2)
    cw = (w - 20) / max(1, len(days))
    for i, day in enumerate(days):
        x = 10 + i * cw
        t = time.strptime(day.date, "%Y-%m-%d")
        dn, dk = wmo(day.code)
        text(d, (x + cw / 2, top + 4), "TODAY" if i == 0 else time.strftime("%A", t).upper(), F("cb", 19), anchor="ma")
        paste_icon(c, dk, int(x + cw / 2 - 30), top + 30, 60)
        text(d, (x + cw / 2, top + 96), f"{day.hi:.0f}°  {day.lo:.0f}°", F("sb", 24), anchor="ma")
        text(d, (x + cw / 2, top + 126), dn, F("m", 16), anchor="ma")
        text(d, (x + cw / 2, top + 148), f"{day.pop}% rain · UV {day.uv:.0f}", F("cm", 15), anchor="ma")
        if i:
            d.line((x, top + 4, x, h - 10), fill=INK, width=1)
    return c


def detail_news(ctx, size):
    c, d = canvas(size)
    w, h = size
    qs = ctx.quotes[:6]
    y = 6
    if qs:
        cw = w / len(qs)
        for i, q in enumerate(qs):
            _quote_cell(d, i * cw, y, cw, q, spark_h=28)
            if i:
                d.line((i * cw, y + 2, i * cw, y + 90), fill=INK, width=1)
        y += 98
        state = "Market open" if any(q.state == "open" for q in qs) else "Market closed"
        text(d, (w - 12, y - 8), state, F("cm", 13), anchor="rd")
        d.line((8, y, w - 8, y), fill=INK, width=2)
        y += 8
    f = F("m", 18)
    lh = 22
    colw = (w - 40) / 2
    cols = [(16, y), (16 + colw + 12, y)]
    ci = 0
    for hd in ctx.heads[:16]:
        x, cy = cols[ci]
        lines = wrap(d, hd.title, f, colw - 20, 2)
        need = len(lines) * lh + 8
        if cy + need > h - 4:
            ci += 1
            if ci >= 2:
                break
            x, cy = cols[ci]
        d.rectangle((x, cy + 8, x + 6, cy + 14), fill=INK)
        for ln in lines:
            text(d, (x + 14, cy), ln, f)
            cy += lh
        cols[ci] = (x, cy + 8)
    return c


def detail_sports(ctx, size):
    c, d = canvas(size)
    w, h = size
    teams = ctx.teams
    if not teams:
        text(d, (w / 2, h / 2), "No games in the next few weeks", F("m", 20), anchor="mm")
        return c
    n = min(6, len(teams))
    rh = (h - 10) / n
    y = 6
    for i, r in enumerate(teams[:n]):
        lsz = int(min(56, rh - 10))
        if paste_logo(c, r.logo, 12, y + 2, lsz, ctx.cfg):
            text(d, (22 + lsz, y + 4), r.label.upper(), F("cb", 24))
            text(d, (22 + lsz, y + 32), r.record, F("cm", 17))
        else:
            text(d, (16, y + 4), r.label.upper(), F("cb", 28))
            text(d, (16, y + 36), r.record, F("cm", 17))
        x = 190
        if r.live:
            g = r.live
            xx = pill(d, x, y + 4, "LIVE", F("cb", 20), h=28) + 10
            text(d, (xx, y + 2), f"{g.us}–{g.them} " + ("vs " if g.home else "at ") + g.opp_name, F("sb", 24))
            text(d, (x, y + 38), g.detail + (f" · {g.tv}" if g.tv else ""), F("m", 17))
        else:
            if r.last:
                g = r.last
                if paste_logo(c, g.opp_logo, x, y + 4, 28, ctx.cfg):
                    x += 36
                xx = pill(d, x, y + 6, g.result or "–", F("cb", 18), h=24) + 8
                s = f"{g.us}–{g.them} " + ("vs " if g.home else "at ") + g.opp_name
                text(d, (xx, y + 4), s, F("sb", 21))
                dt = time.strftime("%a %b ", time.localtime(g.start)) + str(time.localtime(g.start).tm_mday)
                if g.ats and ctx.cfg.get("odds", True) and g.odds.spread_us is not None and _sport(r) not in ("hockey", "baseball"):
                    dt += f" · {g.ats} {g.odds.spread_us:+g}"
                text(d, (xx, y + 30), dt, F("cm", 16))
            if r.next:
                g = r.next
                nx = 500
                text(d, (nx, y + 4), "NEXT", F("cb", 15))
                if paste_logo(c, g.opp_logo, w - 12 - 28, y + 2, 28, ctx.cfg):
                    pass
                s = when(g.start, g.time_tbd) + (" vs " if g.home else " at ") + g.opp_name
                text(d, (nx, y + 22), ellipsize(d, s, F("sb", 19), w - nx - 12), F("sb", 19))
                sub = " · ".join(b for b in (g.note, g.tv) if b)
                ob = _odds_bits(g, g.abbr, ctx.cfg, _sport(r), full=True)
                if ob:
                    sub = " · ".join(b for b in (sub, " · ".join(ob)) if b)
                if sub:
                    text(d, (nx, y + 44), ellipsize(d, sub, F("cm", 16), w - nx - 12), F("cm", 16))
                if ob and g.odds.book:
                    text(d, (nx, y + 63), g.odds.book, F("cr", 13))
            if r.news:
                # under the last result, stopping short of the NEXT column; no result yet = use its slot
                fn = F("cm", 16)
                hy = y + 52 if r.last else y + 6
                for j, ln in enumerate(wrap(d, "» " + r.news, fn, 300, 2 if rh - (hy - y) >= 42 or not r.last else 1)):
                    text(d, (190 + (12 if j else 0), hy + j * 18), ln, fn)
        y += rh
        if i < n - 1:
            dotted(d, 12, w - 12, int(y - 4), 3)
    return c


def detail_sky(ctx, size):
    c, d = canvas(size)
    w, h = size
    radius = ctx.cfg.get("radar_nm", 25)
    _radar(c, d, 200, h / 2 + 6, 180, _blips(ctx), radius, labels=True)
    text(d, (16, 8), f"{radius} nm", F("cm", 15))
    x = 410
    right = panel_sky(ctx, (390, 207))
    c.paste(right, (px(x), 0))
    y = 214
    d.line((x, y - 4, w - 10, y - 4), fill=INK, width=2)
    text(d, (x + 8, y), f"LOGBOOK · {ctx.day_count} TODAY", F("cb", 17))
    y += 24
    for s in ctx.recent[:6]:
        route = f"{s.origin}–{s.dest}" if s.has_route else ""
        text(d, (x + 8, y), _flight_label(s), F("sb", 17))
        text(d, (x + 100, y), route, F("m", 17))
        text(d, (x + 200, y), ellipsize(d, s.aircraft or "", F("cm", 15), 110), F("cm", 15))
        text(d, (w - 12, y + 1), hm(s.seen_at), F("cm", 15), anchor="ra")
        y += 22
    if ctx.upcoming and y < h - 24:
        ev = ctx.upcoming[0]
        text(d, (x + 8, h - 24), ellipsize(d, f"Next: {ev.title} {time.strftime('%a', time.localtime(ev.start))} {hm(ev.start)}",
                                         F("csb", 16), w - x - 20), F("csb", 16))
    return c


def detail_shabbat(ctx, size):
    c, d = canvas(size)
    w, h = size
    c.paste(panel_shabbat(ctx, (w, 207)), (0, 0))
    sh = ctx.shabbat
    y = 214
    d.line((10, y - 4, w - 10, y - 4), fill=INK, width=2)
    if sh is None:
        return c
    show = _jshow(ctx)
    lines = []
    if "hebdate" in show and sh.hebdate:
        lines.append(("Hebrew date", sh.hebdate))
    if "parsha" in show and sh.parsha:
        lines.append(("Torah reading", f"Parashat {sh.parsha}" if not sh.parsha.startswith(("Parashat", "Rosh", "Yom", "Pesach", "Sukkot", "Shavuot")) else sh.parsha))
    if "omer" in show and sh.omer:
        lines.append(("Omer", sh.omer))
    if "dafyomi" in show and sh.daf:
        lines.append(("Daf yomi", sh.daf))
    f, fb = F("m", 17), F("csb", 17)
    # two columns: facts on the left, the fuller upcoming list on the right
    ly = y + 2
    for k, v in lines:
        text(d, (16, ly), k, fb)
        text(d, (16, ly + 20), ellipsize(d, v, f, 300), f)
        ly += 46
    text(d, (16, h - 22), f"Hebcal · {sh.location or 'Teaneck'}", F("cm", 15))
    x = 340 if lines else 16
    if lines:
        d.line((x - 12, y + 2, x - 12, h - 6), fill=INK, width=1)
    text(d, (x, y + 2), "COMING UP", F("cb", 17))
    ry = y + 26
    evs = _jevents(ctx)
    if not evs:
        text(d, (x, ry), "Nothing in the next few weeks", f)
    for e in evs[: (h - 4 - ry) // 22]:
        _jrow(d, x, ry, w, e, F("cm", 16), F("csb", 16), dw=96)
        ry += 22
    return c


def detail_newsletter(ctx, size):
    """Full-page YIOT newsletter: personal news then coming-up, flowing through two reading columns."""
    c, d = canvas(size)
    w, h = size
    data = getattr(ctx, "shul", None)
    blocks = _shul_blocks(data)
    if not blocks:
        _calm(d, w, 40, "Newsletter loads Thursday", "Fetched Thursdays 8 PM ET (admin action: shul_refresh)")
        return c
    colw = (w - 48) // 2
    rest, y0, f = list(blocks), 10, F("cm", 17)
    for ci in range(2):
        x = 16 + ci * (colw + 16)
        if ci:
            d.line((x - 8, 8, x - 8, h - 8), fill=INK, width=1)
        _, nb, last = _shul_flow(d, rest, x, y0, x + colw, h - 26, f, lines_per=4, tf=F("cb", 15))
        if nb:   # carry the unfinished block into the next column (title repeated)
            t, items = rest[nb - 1]
            rest = ([(t + " (cont.)", items[last:])] if last < len(items) else []) + rest[nb:]
    left = sum(len(i) for _, i in rest)
    if left:
        text(d, (w - 14, h - 8), f"+{left} more in the newsletter", F("cm", 14), anchor="rd")
    lab = (data or {}).get("issue_range") or _issue_label(data)
    text(d, (16, h - 8), "Young Israel of Teaneck" + (f"  ·  {lab}" if lab else ""), F("cm", 14), anchor="ld")
    return c


DETAILS = {"weather": detail_weather, "news": detail_news, "sports": detail_sports, "sky": detail_sky,
           "shabbat": detail_shabbat, "calendar": detail_calendar, "trips": detail_trips,
           "shopping": detail_shopping, "scoreboard": detail_scoreboard,
           "newsletter": detail_newsletter}


def compose_page(ctx: Ctx, name: str) -> Image.Image:
    c = Image.new("L", (PW, PH), PAPER)
    c.paste(header(ctx), (0, 0))
    # the strip becomes a page title bar with position dots
    s, d = canvas((W, STRIP_H))
    titles = {"weather": "Weather", "news": "News & markets", "sports": "Scores & schedule", "sky": "Sky above",
              "shabbat": "Jewish calendar", "newsletter": "YIOT Newsletter",
              "calendar": "Calendar", "trips": "Trips", "shopping": "Shopping list",
              "scoreboard": "Scoreboard"}
    text(d, (14, STRIP_H / 2 - 1), titles.get(name, name).upper(), F("cb", 19), anchor="lm")
    text(d, (W - 14, STRIP_H / 2 - 1), "middle: back  ·  right: next  ·  left: home", F("cm", 15), anchor="rm")
    d.line((0, STRIP_H - 1, W, STRIP_H - 1), fill=INK, width=2)
    c.paste(s, (0, px(HEAD_H)))
    c.paste(DETAILS[name](ctx, BODY), (0, px(GRID_Y)))
    return c


def _compose_l(ctx: Ctx) -> Image.Image:
    return compose_page(ctx, ctx.page) if ctx.page in DETAILS else compose_quad(ctx)


def compose_gray(ctx: Ctx) -> Image.Image:
    """The same frame in 4 grey levels (L image holding only 0/85/170/255)."""
    global AA
    AA = True
    try:
        img = _compose_l(ctx)
    finally:
        AA = False
    # Snap to the 4 levels. The midpoints are skewed dark a little: e-paper
    # greys read lighter than they are on a monitor, and thin anti-aliased
    # strokes otherwise fade.
    if GRAY_LEVELS == 16:
        # 16 levels (E1003): same dark skew, finer steps. Values 0,17,..,255.
        q = img.point(lambda v: min(15, max(0, round((v - 12) * 15 / 231))) * 17)
    else:
        q = img.point(lambda v: 0 if v < 60 else 85 if v < 140 else 170 if v < 215 else 255)
    if ctx.cfg.get("dark", False):
        q = q.point(lambda v: 255 - v)
    return q


def pack2(gray: Image.Image) -> bytes:
    """L image of 0/85/170/255 -> 2bpp, 4 px per byte, MSB first, 0=black..3=white."""
    v = gray.point(lambda p: p // 85).tobytes()
    return bytes((v[i] << 6) | (v[i + 1] << 4) | (v[i + 2] << 2) | v[i + 3] for i in range(0, len(v), 4))


def pack4(gray: Image.Image) -> bytes:
    """L image of 0/17/../255 -> 4bpp, 2 px per byte, even x in the high nibble, 0=black..15=white
    (Seeed_GFX's 4bpp grey sprite layout, which the E1003 firmware copies straight in)."""
    v = gray.point(lambda p: p // 17).tobytes()
    return bytes((v[i] << 4) | v[i + 1] for i in range(0, len(v), 2))


def pack_gray(gray: Image.Image) -> bytes:
    return pack4(gray) if GRAY_LEVELS == 16 else pack2(gray)


def compose(ctx: Ctx) -> Image.Image:
    if ctx.page in DETAILS:
        img = compose_page(ctx, ctx.page)
    else:
        img = compose_quad(ctx)
    return to1(img, invert=ctx.cfg.get("dark", False))


# ================================================================== wire format

def encode_reel(frames) -> bytes:
    """REEL v1 -- identical to the note box firmware's format.

    b"REEL" u8 version u16 nframes, then per frame:
      u16 x0 y0 x1 y1 (x multiple of 8, exclusive ends), u16 delay_ms, u8 flags,
      then (x1-x0)/8 * (y1-y0) bytes of region rows. flags bit0 = full refresh.
    """
    out = bytearray(b"REEL")
    out += bytes([1])
    out += len(frames).to_bytes(2, "little")
    prev = None
    for fr in frames:
        img, delay, full = fr[:3]
        gray = fr[3] if len(fr) > 3 else None
        if prev is None or full or gray is not None:
            box = (0, 0) + img.size
        else:
            box = ImageChops.logical_xor(prev, img).getbbox() or (0, 0, 8, 1)
        x0 = box[0] & ~7
        x1 = min(img.size[0], (box[2] + 7) & ~7)
        y0, y1 = box[1], box[3]
        for v in (x0, y0, x1, y1, delay):
            out += int(v).to_bytes(2, "little")
        if gray is not None:
            # flags bit1: full-screen grey frame, 1-bit rows then 2bpp rows
            out += bytes([3])
            out += img.tobytes()
            out += pack_gray(gray)
        else:
            out += bytes([1 if full else 0])
            out += img.crop((x0, y0, x1, y1)).tobytes()
        prev = img
    return bytes(out)


def region_frames(prev: Image.Image, new: Image.Image, layout="quad"):
    """Split one update into per-region partial refreshes (header, strip, each
    slot), so a change in two corners refreshes two small windows instead of
    one window spanning the whole screen. Each partial costs ~1s whatever its
    size, but a smaller window ghosts less."""
    boxes = [tuple(px(v) for v in b) for b in
             [(0, 0, W, HEAD_H), (0, HEAD_H, W, GRID_Y)] + slot_boxes(layout)]
    frames = []
    cur = prev.copy()
    for b in boxes:
        a, n = prev.crop(b), new.crop(b)
        if ImageChops.logical_xor(a, n).getbbox() is None:
            continue
        cur = cur.copy()
        cur.paste(n, b[:2])
        frames.append(cur)
    if ImageChops.logical_xor(cur, new).getbbox() is not None:   # anything outside the boxes
        frames.append(new)
    return frames


if os.environ.get("QUAD_MODEL"):
    set_profile(os.environ["QUAD_MODEL"].lower())
