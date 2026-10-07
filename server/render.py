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

HERE = os.path.dirname(os.path.abspath(__file__))
FONT_DIR = os.path.join(HERE, "fonts")
INK, PAPER = 0, 255

_fonts = {}


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
    return _fonts[key]


# True while rendering the 4-level grey version: text is anti-aliased then,
# because the panel can actually show the in-between shades.
AA = False


def canvas(size):
    img = Image.new("L", size, PAPER)
    d = ImageDraw.Draw(img)
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
    ic = icon(kind, size, night)
    c.paste(ImageChops.darker(c.crop((x, y, x + size, y + size)), ic), (x, y))


def paste_logo(c, url, x, y, size, cfg=None):
    """Team logo, or False if we have none (caller falls back to text).
    Grey render keeps its shading; the 1-bit render gets it dithered so the
    fast partial updates still show a logo rather than a blob."""
    if cfg is not None and not cfg.get("sports_logos", True):
        return False
    import logos
    img = logos.get(url, size)
    if img is None:
        return False
    if not AA:
        img = img.convert("1").convert("L")
    x, y = int(x), int(y)
    c.paste(ImageChops.darker(c.crop((x, y, x + size, y + size)), img), (x, y))
    return True


def compass_arrow(c, cx, cy, r, bearing):
    """Arrow pointing where to look (0 = north = up)."""
    S = 4
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
    dn.text((R, S * 4), "N", font=F("b", 9 * S), fill=INK, anchor="mt")
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
        pill(d, 12, py, ellipsize(d, wx.rain_note, f, rx - 34), f, h=21)
    _rain_bars(d, wx.hourly, ctx.now, rx, w - 12, fy + 4, top - 7)
    # 3-day row
    days = wx.days[1:4]
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
    qs = ctx.quotes[:6]
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


PANELS = {"weather": panel_weather, "news": panel_news, "sports": panel_sports, "sky": panel_sky}


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
    """The one line under the header: sticky message > severe alert > brief."""
    c, d = canvas((W, STRIP_H))
    msg, bold = "", False
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


def slot_boxes():
    return [(0, GRID_Y, CELL_W, GRID_Y + CELL_H), (CELL_W, GRID_Y, W, GRID_Y + CELL_H),
            (0, GRID_Y + CELL_H, CELL_W, H), (CELL_W, GRID_Y + CELL_H, W, H)]


def compose_quad(ctx: Ctx, panels: dict | None = None) -> Image.Image:
    """panels: optional {name: prerendered L image} to reuse (not re-render)."""
    c = Image.new("L", (W, H), PAPER)
    c.paste(header(ctx), (0, 0))
    c.paste(strip(ctx), (0, HEAD_H))
    slots = ctx.cfg.get("slots", ["weather", "news", "sports", "sky"])
    for name, box in zip(slots, slot_boxes()):
        img = (panels or {}).get(name)
        if img is None:
            img = PANELS[name](ctx, (CELL_W, CELL_H))
        c.paste(img, box[:2])
    d = ImageDraw.Draw(c)
    d.line((CELL_W - 1, GRID_Y + 6, CELL_W - 1, H - 6), fill=INK, width=2)
    d.line((6, GRID_Y + CELL_H, W - 6, GRID_Y + CELL_H), fill=INK, width=2)
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
    c.paste(right, (x, 0))
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


DETAILS = {"weather": detail_weather, "news": detail_news, "sports": detail_sports, "sky": detail_sky}


def compose_page(ctx: Ctx, name: str) -> Image.Image:
    c = Image.new("L", (W, H), PAPER)
    c.paste(header(ctx), (0, 0))
    # the strip becomes a page title bar with position dots
    s, d = canvas((W, STRIP_H))
    titles = {"weather": "Weather", "news": "News & markets", "sports": "Scores & schedule", "sky": "Sky above"}
    text(d, (14, STRIP_H / 2 - 1), titles.get(name, name).upper(), F("cb", 19), anchor="lm")
    text(d, (W - 14, STRIP_H / 2 - 1), "middle: back  ·  right: next  ·  left: home", F("cm", 15), anchor="rm")
    d.line((0, STRIP_H - 1, W, STRIP_H - 1), fill=INK, width=2)
    c.paste(s, (0, HEAD_H))
    c.paste(DETAILS[name](ctx, BODY), (0, GRID_Y))
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
    q = img.point(lambda v: 0 if v < 60 else 85 if v < 140 else 170 if v < 215 else 255)
    if ctx.cfg.get("dark", False):
        q = q.point(lambda v: 255 - v)
    return q


def pack2(gray: Image.Image) -> bytes:
    """L image of 0/85/170/255 -> 2bpp, 4 px per byte, MSB first, 0=black..3=white."""
    v = gray.point(lambda p: p // 85).tobytes()
    return bytes((v[i] << 6) | (v[i + 1] << 4) | (v[i + 2] << 2) | v[i + 3] for i in range(0, len(v), 4))


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
            box = (0, 0, W, H)
        else:
            box = ImageChops.logical_xor(prev, img).getbbox() or (0, 0, 8, 1)
        x0 = box[0] & ~7
        x1 = min(W, (box[2] + 7) & ~7)
        y0, y1 = box[1], box[3]
        for v in (x0, y0, x1, y1, delay):
            out += int(v).to_bytes(2, "little")
        if gray is not None:
            # flags bit1: full-screen grey frame, 1-bit rows then 2bpp rows
            out += bytes([3])
            out += img.tobytes()
            out += pack2(gray)
        else:
            out += bytes([1 if full else 0])
            out += img.crop((x0, y0, x1, y1)).tobytes()
        prev = img
    return bytes(out)


def region_frames(prev: Image.Image, new: Image.Image):
    """Split one update into per-region partial refreshes (header, strip, each
    slot), so a change in two corners refreshes two small windows instead of
    one window spanning the whole screen. Each partial costs ~1s whatever its
    size, but a smaller window ghosts less."""
    boxes = [(0, 0, W, HEAD_H), (0, HEAD_H, W, GRID_Y)] + slot_boxes()
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
