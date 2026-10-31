"""E1001 "living gallery": an atmospheric single-image screen for the 7.5" 800x480 panel.

One full-screen category at a time, classic screensaver style. Supplemental to the E1003 big
board on the same fridge, so it NEVER shows scores or scoreboards -- only mood.

Modes, in cycle order:   onthisday -> matchup -> weather -> trip -> news -> wire
  onthisday  history / science event for today (Wikipedia "selected" feed) with a freely licensed
             Commons still (licence + author credited). Sports items are skipped.
  matchup    tonight / this-week matchup mood art: giant faint logos, "vs", day + time + TV.
             No scores even while live (the E1003 owns scores); a live game just reads "ON NOW".
  weather    atmosphere: drawn sky (rain / snow / sun rays / fog / cloud) behind a huge temperature,
             condition and a 24h temperature ribbon.
  trip       one trip or calendar beat: next TripIt trip (countdown + segments), else next event.
  news       one news photo + headline (WSJ RSS media:content), credited.
  wire       sports-wire still + headline (ESPN team news), only while no followed team is live.
             Copyrighted photos: personal, in-home display only, credited.

Mode choice (pick_mode) is a pure function of (data, time, offset):
  * modes rotate every gallery_rotate_min (default 5) minutes;
  * tap anywhere / right button = next, left button = previous (offset +-1);
  * thin modes (no data) are skipped.

Network only in fetch_*(); render_*() never fetches (images are cached to cache/gallery/).
"""
from __future__ import annotations

import hashlib
import html
import io
import os
import re
import time
import urllib.parse

import requests
from PIL import Image, ImageDraw, ImageEnhance, ImageOps, ImageFilter

import render as R

HERE = os.path.dirname(os.path.abspath(__file__))
DIR = os.path.join(HERE, "cache", "gallery")
UA = "eink-quad/0.1 (personal e-paper dashboard; hobby project)"
MODES = ["onthisday", "matchup", "weather", "trip", "news", "wire"]
ROTATE_MIN = 5
NAMES = {"onthisday": "ON THIS DAY", "matchup": "THE MATCHUP", "weather": "OUTSIDE",
         "trip": "COMING UP", "news": "THE NEWS", "wire": "THE WIRE"}

# ------------------------------------------------------------------ fetch

def _get(url, params=None, timeout=12):
    r = requests.get(url, params=params, headers={"User-Agent": UA}, timeout=timeout)
    r.raise_for_status()
    return r


def cache_image(url):
    """Download once; return local path (or None)."""
    if not url:
        return None
    os.makedirs(DIR, exist_ok=True)
    p = os.path.join(DIR, hashlib.sha1(url.encode()).hexdigest()[:16] + ".img")
    if not os.path.exists(p):
        try:
            data = _get(url, timeout=20).content
            Image.open(io.BytesIO(data)).verify()
            with open(p, "wb") as f:
                f.write(data)
        except Exception as e:
            print(f"[gallery] image {url[:80]}: {e}", flush=True)
            return None
    return p


SCIENCE = re.compile(r"\b(scien|discover|invent|patent|launch|spacecraft|satellite|orbit|moon|mars|planet|"
                     r"comet|telescope|astronom|physic|chemi|element|vaccine|medic|surg|transplant|"
                     r"dna|gene|fossil|dinosaur|computer|internet|radio|television|broadcast|electric|"
                     r"engine|aircraft|flight|rocket|nasa|nobel|expedition|bridge|canal|tunnel|"
                     r"museum|observatory|first successful|record)", re.I)
SPORTS = re.compile(r"\b(baseball|football|basketball|hockey|soccer|world series|super bowl|stanley cup|"
                    r"olympic|tournament|championship|league|nfl|nba|mlb|nhl|fifa|boxing|match|"
                    r"race|grand prix|cricket|tennis|golf|athlete)", re.I)
GRIM = re.compile(r"\b(massacre|killed|murder|shot|bomb|execut|terror|genocide|opened fire)", re.I)


def _commons_license(filename):
    """Commons extmetadata -> (license_short, artist) if freely licensed, else None."""
    d = _get("https://commons.wikimedia.org/w/api.php",
             {"action": "query", "titles": "File:" + filename, "prop": "imageinfo",
              "iiprop": "extmetadata", "format": "json"}).json()
    for pg in d.get("query", {}).get("pages", {}).values():
        md = ((pg.get("imageinfo") or [{}])[0]).get("extmetadata") or {}
        lic = re.sub("<[^>]+>", "", (md.get("LicenseShortName") or {}).get("value", "")).strip()
        art = re.sub("<[^>]+>", "", (md.get("Artist") or {}).get("value", "")).strip()
        if re.match(r"(public domain|pd|cc0|cc[- ]by)", lic, re.I):
            return lic, html.unescape(art)[:60]
    return None


def fetch_onthisday(now=None):
    """Best history/science event for today with a freely licensed Commons still."""
    t = time.localtime(now or time.time())
    d = _get(f"https://en.wikipedia.org/api/rest_v1/feed/onthisday/selected/{t.tm_mon:02d}/{t.tm_mday:02d}").json()
    cands = []
    for e in d.get("selected", []) + d.get("events", [])[:30]:
        txt = e.get("text", "")
        if SPORTS.search(txt):
            continue
        score = (3 if SCIENCE.search(txt) else 0) - (2 if GRIM.search(txt) else 0)
        cands.append((score, e))
    cands.sort(key=lambda c: -c[0])
    fallback = None
    for score, e in cands[:12]:
        for p in e.get("pages", []):
            img = ((p.get("originalimage") or {}).get("source") or "").split("?")[0]
            thumb = ((p.get("thumbnail") or {}).get("source") or "").split("?")[0]
            if "/wikipedia/commons/" not in img:      # en-local files are often non-free
                continue
            if not re.search(r"\.(jpe?g|png|tiff?)$", img, re.I):   # skip SVG diagrams / maps
                continue
            fn = urllib.parse.unquote(img.rsplit("/", 1)[-1])
            try:
                lic = _commons_license(fn)
            except Exception:
                lic = None
            if not lic:
                continue
            # a 960px thumb (a Wikimedia standard size) is plenty for 800x480 and polite
            src = re.sub(r"/\d+px-", "/960px-", thumb) if "/thumb/" in thumb else img
            path = cache_image(src)
            if not path:
                continue
            return {"year": e["year"], "text": e["text"], "title": p.get("normalizedtitle") or p.get("title", ""),
                    "image": path, "license": lic[0], "artist": lic[1],
                    "source": "Wikipedia / Wikimedia Commons"}
        if fallback is None:
            fallback = {"year": e["year"], "text": e["text"], "title": "", "image": None,
                        "license": "", "artist": "", "source": "Wikipedia"}
    return fallback


def fetch_wire(teams_cfg, keys):
    """[{headline, team, image, published}] newest first from ESPN team news (with images)."""
    out = []
    for key in keys:
        if key not in teams_cfg:
            continue
        label, sport, league, tid = teams_cfg[key]
        try:
            d = _get(f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/news",
                     {"team": tid, "limit": 8}).json()
        except Exception as e:
            print(f"[gallery] wire {key}: {e}", flush=True)
            continue
        for a in d.get("articles", []):
            imgs = [i for i in a.get("images", []) if i.get("url") and i.get("type") != "stitcher"]
            ids = {c.get("teamId") for c in a.get("categories", []) if c.get("type") == "team"}
            if a.get("type") == "Media" or not imgs or not a.get("headline") or len(ids) > 2:
                continue          # league-wide roundups (odds, rankings) tag dozens of teams
            out.append({"headline": html.unescape(a["headline"]).strip(), "team": label, "key": key,
                        "url": imgs[0]["url"], "published": a.get("published", ""),
                        "credit": (imgs[0].get("credit") or "ESPN")[:50]})
            break
    out.sort(key=lambda a: a["published"], reverse=True)
    for a in out[:3]:
        a["image"] = cache_image(a["url"])
    return [a for a in out if a.get("image")]


def fetch_news(feeds=("world", "us"), n=6):
    """Newest WSJ RSS items that carry a photo -> [{headline, section, image, summary}]."""
    import sources as S
    import xml.etree.ElementTree as ET
    out = []
    for key in feeds:
        if key not in S.WSJ_FEEDS:
            continue
        label, url = S.WSJ_FEEDS[key]
        try:
            r = requests.get(url, headers={"User-Agent": S.UA_BROWSER}, timeout=10)
            r.raise_for_status()
            root = ET.fromstring(r.content)
        except Exception as e:
            print("gallery news:", key, e)
            continue
        got = 0
        for it in root.iter("item"):
            m = it.find("{http://search.yahoo.com/mrss/}content")
            title = S._clean(it.findtext("title"))
            if m is None or not title or S.JUNK.search(title):
                continue
            img = cache_image(m.get("url"))
            if img:
                out.append({"headline": title, "section": label, "image": img,
                            "summary": S._clean(it.findtext("description"))[:240]})
                got += 1
            if got >= n:
                break
    return out


# ------------------------------------------------------------------ mode logic

def available(data, teams, weather=None, trips=None, calendar=None, now=None):
    av = []
    if data.get("onthisday"):
        av.append("onthisday")
    if any(r.next or r.live for r in teams):
        av.append("matchup")
    if weather is not None:
        av.append("weather")
    if _beat(trips, calendar, now or time.time()):
        av.append("trip")
    if data.get("news"):
        av.append("news")
    if data.get("wire") and not any(r.live for r in teams):   # wire stills only when teams are quiet
        av.append("wire")
    return av


def rotate_min(cfg):
    return max(1, int(cfg.get("gallery_rotate_min") or ROTATE_MIN))


def pick_mode(data, teams, now, cfg, offset=0, weather=None, trips=None, calendar=None):
    force = cfg.get("gallery_mode") or ""
    if force in MODES:
        return force
    av = available(data, teams, weather, trips, calendar, now)
    if not av:
        return "onthisday"
    slot = int(now // (rotate_min(cfg) * 60))
    return av[(slot + offset) % len(av)]


def _beat(trips, calendar, now):
    """The one trip / calendar beat worth a screen: current or next trip (60 d), else next event."""
    for t in sorted(trips or [], key=lambda t: t.start):
        if t.end >= now and t.start - now < 60 * 86400:
            return ("trip", t)
    for e in sorted(calendar or [], key=lambda e: e.start):
        if e.end >= now:
            return ("event", e)
    return None


# ------------------------------------------------------------------ render helpers

def _photo(path, size, gamma=1.0, contrast=1.25):
    """Cover-crop, tone for e-paper (lift shadows, boost contrast) -> L."""
    im = Image.open(path).convert("L")
    im = ImageOps.fit(im, size, Image.LANCZOS, centering=(0.5, 0.4))
    im = ImageOps.autocontrast(im, cutoff=1)
    im = ImageEnhance.Contrast(im).enhance(contrast)
    im = im.filter(ImageFilter.UnsharpMask(1.2, 60, 2))
    if gamma != 1.0:
        im = im.point(lambda v: int(255 * (v / 255) ** gamma))
    return im


def dither(img, levels):
    """Floyd-Steinberg to N evenly spaced grey levels (4 on E1001, 16 on E1003)."""
    pal = Image.new("P", (1, 1))
    step = 255 // (levels - 1)
    p = []
    for i in range(levels):
        p += [i * step] * 3
    pal.putpalette(p + [0] * (768 - len(p)))
    return img.convert("RGB").quantize(palette=pal, dither=Image.FLOYDSTEINBERG).convert("L")


def _band(c, y0, y1, fill=R.PAPER):
    ImageDraw.Draw(c).rectangle((0, y0, c.size[0], y1), fill=fill)


def _footer(d, w, h, mode, now, credit=""):
    """Bottom rail: mode dots + time + credit. Text always on solid paper, never on the photo."""
    y = h - 26
    d.rectangle((0, y, w, h), fill=R.PAPER)
    d.line((0, y, w, y), fill=R.INK, width=2)
    x = 14
    for m in MODES:
        r = 5
        if m == mode:
            d.ellipse((x, y + 13 - r, x + 2 * r, y + 13 + r), fill=R.INK)
        else:
            d.ellipse((x, y + 13 - r, x + 2 * r, y + 13 + r), outline=R.INK, width=2)
        x += 20
    R.text(d, (x + 6, y + 13), NAMES[mode], R.F("cb", 17), anchor="lm")
    R.text(d, (w - 14, y + 13), time.strftime("%a %b %-d · %-I:%M %p", time.localtime(now)), R.F("cm", 17), anchor="rm")
    if credit:
        R.text(d, (w // 2 + 70, y + 13), R.ellipsize(d, credit, R.F("cr", 14), 250), R.F("cr", 14), anchor="mm")


def _logo(url, size):
    try:
        import logos
        return logos.get(url, size)
    except Exception:
        return None


# ------------------------------------------------------------------ the six screens

def render_onthisday(data, now, size):
    w, h = size
    c = Image.new("L", size, R.PAPER)
    d = ImageDraw.Draw(c)
    e = data.get("onthisday")
    credit = ""
    if e and e.get("image"):
        ph = _photo(e["image"], (w, h - 26 - 130))
        c.paste(ph, (0, 0))
        credit = f"{e.get('artist') or 'Wikimedia Commons'} · {e.get('license')}"
    if not e:
        R.text(d, (w / 2, h / 2), "Nothing for today yet", R.F("cb", 40), anchor="mm")
        _footer(d, w, h, "onthisday", now)
        return c
    top = h - 26 - 130 if e.get("image") else 30
    _band(c, top, h - 26)
    d.line((0, top, w, top), fill=R.INK, width=2)
    R.text(d, (20, top + 12), str(e["year"]), R.F("cb", 64), anchor="la")
    yw = R.tw(d, str(e["year"]), R.F("cb", 64)) + 40
    lines = R.wrap(d, e["text"], R.F("sb", 22), w - yw - 20, 4)
    for i, ln in enumerate(lines):
        R.text(d, (yw, top + 14 + i * 27), ln, R.F("sb", 22))
    _footer(d, w, h, "onthisday", now, credit)
    return c


def render_wire(data, now, size):
    w, h = size
    c = Image.new("L", size, R.PAPER)
    d = ImageDraw.Draw(c)
    a = (data.get("wire") or [None])[(int(now // 3600)) % max(1, len(data.get("wire") or []))]
    if not a:
        R.text(d, (w / 2, h / 2), "Quiet wire", R.F("cb", 40), anchor="mm")
        _footer(d, w, h, "wire", now)
        return c
    ph_h = h - 26 - 92
    c.paste(_photo(a["image"], (w, ph_h)), (0, 0))
    _band(c, ph_h, h - 26)
    d.line((0, ph_h, w, ph_h), fill=R.INK, width=2)
    R.pill(d, 18, ph_h + 12, a["team"].upper(), R.F("cb", 18))
    lines = R.wrap(d, a["headline"], R.F("b", 26), w - 40, 2)
    for i, ln in enumerate(lines):
        R.text(d, (20, ph_h + 40 + i * 28), ln, R.F("b", 26))
    _footer(d, w, h, "wire", now, f"Photo: {a.get('credit')} (ESPN)")
    return c


def _faint(im, k=6):
    """Wash a logo to a pale backdrop tone."""
    return im.convert("L").point(lambda v: 255 - (255 - v) // k)


def render_matchup(data, teams, now, size):
    """Mood art for tonight's / this week's game. Never a score."""
    w, h = size
    c = Image.new("L", size, R.PAPER)
    d = ImageDraw.Draw(c)
    rows = sorted((r for r in teams if r.live or r.next), key=lambda r: (0 if r.live else 1, (r.next.start if r.next else 0)))
    if not rows:
        R.text(d, (w / 2, h / 2), "No games this week", R.F("cb", 40), anchor="mm")
        _footer(d, w, h, "matchup", now)
        return c
    row = rows[0]
    g = row.live or row.next
    us, them = _logo(row.logo or g.our_logo, 400), _logo(g.opp_logo, 400)
    left, right = (them, us) if g.home else (us, them)
    lname, rname = (g.opp_name or g.opp, row.label) if g.home else (row.label, g.opp_name or g.opp)
    # giant faint logos bleed off both edges: the "poster"
    for im, x in ((left, -90), (right, w - 310)):
        if im is not None:
            f = _faint(im, 5)
            c.paste(f, (x, (h - 26 - f.size[1]) // 2))
    # crisp mid-size logos + names
    ours = row.logo or g.our_logo
    lurl, rurl = (g.opp_logo, ours) if g.home else (ours, g.opp_logo)
    for im, cx, nm in ((_logo(lurl, 150), w // 4, lname), (_logo(rurl, 150), 3 * w // 4, rname)):
        if im is not None:
            c.paste(im.convert("L"), (cx - im.size[0] // 2, 120))
        else:
            R.text(d, (cx, 195), nm[:3].upper(), R.F("cb", 90), anchor="mm")
        R.text(d, (cx, 300), nm.upper(), R.F("cb", 34), anchor="mm")
    R.text(d, (w / 2, 195), "vs" if g.home else "@", R.F("cb", 72), anchor="mm")
    if row.live:
        head = "ON NOW"
    else:
        lt, ln = time.localtime(g.start), time.localtime(now)
        same = lt[:3] == ln[:3]
        day = ("TONIGHT" if lt.tm_hour >= 17 else "TODAY") if same else time.strftime("%A", lt).upper()
        head = f"{day} · {'TBD' if g.time_tbd else time.strftime('%-I:%M %p', lt)}"
    # header band on solid paper
    d.rectangle((0, 0, w, 82), fill=R.PAPER)
    d.line((0, 82, w, 82), fill=R.INK, width=2)
    R.text(d, (w / 2, 42), head, R.F("cb", 54), anchor="mm")
    bits = [b for b in (g.note, getattr(g, "venue", ""), g.tv, row.record and f"{row.label} {row.record}") if b]
    if bits:
        d.rectangle((0, 340, w, 392), fill=R.PAPER)
        R.text(d, (w / 2, 366), R.ellipsize(d, "  ·  ".join(bits), R.F("csb", 26), w - 40), R.F("csb", 26), anchor="mm")
    later = [r for r in rows[1:4] if r.next and not r.live]
    if later:
        d.rectangle((0, 398, w, h - 26), fill=R.PAPER)
        d.line((40, 400, w - 40, 400), fill=R.INK, width=1)
        for i, r in enumerate(later):
            gg = r.next
            s = f"{r.label} {'vs' if gg.home else '@'} {gg.opp} · {time.strftime('%a %-I:%M%p', time.localtime(gg.start))}"
            R.text(d, ((i + 0.5) * w / len(later), 428), R.ellipsize(d, s, R.F("cm", 21), w // len(later) - 16),
                   R.F("cm", 21), anchor="mm")
    _footer(d, w, h, "matchup", now)
    return c


def _sky(c, kind, box, now):
    """Procedural atmosphere for the weather screen (deterministic per 5-minute slot)."""
    import random
    rnd = random.Random(int(now // 300))
    d = ImageDraw.Draw(c)
    x0, y0, x1, y1 = box
    if kind in ("rain", "storm", "thunder"):
        for _ in range(260):
            x, y = rnd.randint(x0 - 40, x1), rnd.randint(y0, y1)
            L = rnd.randint(18, 46)
            d.line((x, y, x + L // 3, y + L), fill=rnd.choice((150, 190, 210)), width=rnd.choice((1, 2, 2)))
    elif kind in ("snow",):
        for _ in range(220):
            x, y, r = rnd.randint(x0, x1), rnd.randint(y0, y1), rnd.choice((2, 3, 4, 6))
            d.ellipse((x - r, y - r, x + r, y + r), fill=rnd.choice((160, 200)))
    elif kind in ("fog",):
        for i, y in enumerate(range(y0 + 20, y1, 34)):
            d.rounded_rectangle((x0 + (i % 3) * 40 - 60, y, x1 - (i % 2) * 90, y + 14), 7, fill=205)
    elif kind in ("sun", "partly"):
        import math
        cx, cy = x1 - 150, y0 + 150
        for k in range(24):
            a = k * math.pi / 12
            d.line((cx + 120 * math.cos(a), cy + 120 * math.sin(a), cx + 700 * math.cos(a), cy + 700 * math.sin(a)),
                   fill=215, width=10)
        d.ellipse((cx - 100, cy - 100, cx + 100, cy + 100), fill=200)
        if kind == "partly":
            for bx, by, s in ((cx - 260, cy + 40, 1.0), (x0 + 60, y0 + 230, 0.8)):
                _cloud(d, bx, by, s, 225)
    else:   # cloud / overcast
        for bx, by, s in ((x0 + 40, y0 + 40, 1.3), (x0 + 380, y0 + 10, 1.0), (x0 + 200, y0 + 200, 1.5), (x0 + 560, y0 + 170, 1.1)):
            _cloud(d, bx, by, s, rnd.choice((205, 220, 230)))


def _cloud(d, x, y, s, fill):
    for dx, dy, r in ((0, 40, 50), (55, 10, 70), (130, 35, 55), (60, 55, 55)):
        d.ellipse((x + dx * s - r * s, y + dy * s - r * s, x + dx * s + r * s, y + dy * s + r * s), fill=fill)


def render_weather(wx, now, size):
    w, h = size
    c = Image.new("L", size, R.PAPER)
    d = ImageDraw.Draw(c)
    if wx is None:
        R.text(d, (w / 2, h / 2), "No weather", R.F("cb", 40), anchor="mm")
        _footer(d, w, h, "weather", now)
        return c
    import sources as S
    cond, kind = S.wmo(wx.code)
    _sky(c, kind, (0, 0, w, h - 26), now)
    try:
        from PIL import ImageChops
        ic = R.icon(kind, 170, night=not wx.is_day).convert("L")
        box = (w - 210, 30, w - 40, 200)
        c.paste(ImageChops.darker(c.crop(box), ic), box[:2])
    except Exception:
        pass
    R.text(d, (28, 150), f"{wx.temp:.0f}°", R.F("cb", 220), anchor="lm")
    R.text(d, (34, 285), cond, R.F("cb", 52), anchor="lm")
    day = wx.days[0] if wx.days else None
    sub = []
    if day:
        sub.append(f"H {day.hi:.0f}°  L {day.lo:.0f}°")
    sub.append(f"feels {wx.feels:.0f}°")
    if wx.wind >= 8:
        sub.append(f"wind {wx.wind:.0f}")
    R.text(d, (36, 335), "   ".join(sub), R.F("csb", 30), anchor="lm")
    note = (wx.alerts[0][0] if wx.alerts else "") or wx.rain_note
    # 24h temperature ribbon on paper
    y0, y1 = 368, h - 30
    d.rectangle((0, y0, w, h - 26), fill=R.PAPER)
    d.line((0, y0, w, y0), fill=R.INK, width=2)
    hr = (wx.hourly or [])[:24]
    if hr:
        ts = [t for _, t, _, _ in hr]
        lo, hi = min(ts), max(ts) or 1
        span = max(1, hi - lo)
        pts = [(30 + i * (w - 60) / max(1, len(hr) - 1), y1 - 18 - (t - lo) / span * 40) for i, t in enumerate(ts)]
        d.line(pts, fill=R.INK, width=3)
        for i in range(0, len(hr), 6):
            ep, t, pop, _ = hr[i]
            x, y = pts[i]
            d.ellipse((x - 4, y - 4, x + 4, y + 4), fill=R.INK)
            R.text(d, (x, y - 16), f"{t:.0f}°", R.F("cb", 20), anchor="mm")
            R.text(d, (x, y1 - 2), time.strftime("%-I%p", time.localtime(ep)).lower(), R.F("cm", 16), anchor="mm")
    if note:
        R.text(d, (w - 20, y0 + 14), R.ellipsize(d, note, R.F("csb", 20), 420), R.F("csb", 20), anchor="rm")
    _footer(d, w, h, "weather", now, "Open-Meteo")
    return c


def render_trip(trips, calendar, now, size):
    w, h = size
    c = Image.new("L", size, R.PAPER)
    d = ImageDraw.Draw(c)
    b = _beat(trips, calendar, now)
    if not b:
        R.text(d, (w / 2, h / 2), "Nothing on the calendar", R.F("cb", 40), anchor="mm")
        _footer(d, w, h, "trip", now)
        return c
    kind, x = b
    days = (time.mktime(time.localtime(x.start)[:3] + (0, 0, 0, 0, 0, -1)) -
            time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))) / 86400
    days = round(days)
    # left: giant countdown block on ink
    d.rectangle((0, 0, 300, h - 26), fill=R.INK)
    if x.start <= now:
        big, small = "NOW", "under way"
    elif days == 0:
        big, small = "TODAY", time.strftime("%-I:%M %p", time.localtime(x.start))
    else:
        big, small = str(days), "day to go" if days == 1 else "days to go"
    R.text(d, (150, 190), big, R.F("cb", 170 if len(big) <= 2 else 96), anchor="mm", fill=R.PAPER)
    R.text(d, (150, 310), small.upper(), R.F("cb", 34), anchor="mm", fill=R.PAPER)
    R.text(d, (150, 380), time.strftime("%a %b %-d", time.localtime(x.start)).upper(), R.F("csb", 28), anchor="mm", fill=R.PAPER)
    X = 330
    if kind == "trip":
        R.text(d, (X, 40), "TRIP", R.F("cb", 26), anchor="lm")
        for i, ln in enumerate(R.wrap(d, x.name or "Trip", R.F("b", 46), w - X - 24, 2)):
            R.text(d, (X, 90 + i * 52), ln, R.F("b", 46), anchor="lm")
        y = 210
        d.line((X, y - 20, w - 24, y - 20), fill=R.INK, width=2)
        for s in x.segments[:4]:
            if s.kind == "flight":
                t = f"FLY  {s.flight_no}  {s.origin} – {s.dest}"
            elif s.kind == "hotel":
                t = f"STAY  {s.name or s.title}"
            else:
                t = s.title
            R.text(d, (X, y), R.ellipsize(d, t, R.F("csb", 28), w - X - 140), R.F("csb", 28), anchor="lm")
            R.text(d, (w - 24, y), time.strftime("%a %-I:%M%p", time.localtime(s.start)), R.F("cm", 24), anchor="rm")
            y += 50
        foot = time.strftime("%b %-d", time.localtime(x.start)) + " – " + time.strftime("%b %-d", time.localtime(x.end))
    else:
        R.text(d, (X, 40), "ON THE CALENDAR", R.F("cb", 26), anchor="lm")
        for i, ln in enumerate(R.wrap(d, x.title, R.F("b", 50), w - X - 24, 3)):
            R.text(d, (X, 100 + i * 58), ln, R.F("b", 50), anchor="lm")
        foot = "All day" if x.all_day else time.strftime("%-I:%M %p", time.localtime(x.start))
        if x.location:
            R.text(d, (X, 340), R.ellipsize(d, x.location, R.F("csb", 28), w - X - 24), R.F("csb", 28), anchor="lm")
    R.text(d, (X, h - 60), foot, R.F("cm", 26), anchor="lm")
    _footer(d, w, h, "trip", now)
    return c


def render_news(data, now, size):
    w, h = size
    c = Image.new("L", size, R.PAPER)
    d = ImageDraw.Draw(c)
    items = data.get("news") or []
    if not items:
        R.text(d, (w / 2, h / 2), "No news photo", R.F("cb", 40), anchor="mm")
        _footer(d, w, h, "news", now)
        return c
    a = items[int(now // (ROTATE_MIN * 60 * 6)) % len(items)]   # a different story each time round
    lines = R.wrap(d, a["headline"], R.F("b", 30), w - 40, 3)
    band = 46 + 34 * len(lines)
    ph_h = h - 26 - band
    c.paste(_photo(a["image"], (w, ph_h)), (0, 0))
    _band(c, ph_h, h - 26)
    d.line((0, ph_h, w, ph_h), fill=R.INK, width=2)
    R.pill(d, 18, ph_h + 10, a["section"].upper(), R.F("cb", 18))
    for i, ln in enumerate(lines):
        R.text(d, (20, ph_h + 42 + i * 34), ln, R.F("b", 30))
    _footer(d, w, h, "news", now, "Photo: WSJ")
    return c


def compose(ctx, size=None):
    """L image (W x H) for the current gallery mode."""
    size = size or (R.PW, R.PH)
    data = getattr(ctx, "gallery", None) or {}
    mode = pick_mode(data, ctx.teams, ctx.now, ctx.cfg, getattr(ctx, "gallery_offset", 0),
                     ctx.weather, ctx.trips, ctx.calendar)
    if mode == "matchup":
        return render_matchup(data, ctx.teams, ctx.now, size)
    if mode == "weather":
        return render_weather(ctx.weather, ctx.now, size)
    if mode == "trip":
        return render_trip(ctx.trips, ctx.calendar, ctx.now, size)
    if mode == "news":
        return render_news(data, ctx.now, size)
    if mode == "wire":
        return render_wire(data, ctx.now, size)
    return render_onthisday(data, ctx.now, size)


def compose_gray(ctx):
    """Device-native greys with error diffusion (photos need it; flat text stays solid)."""
    return dither(compose(ctx), R.GRAY_LEVELS)
