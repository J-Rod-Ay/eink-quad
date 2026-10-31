"""E1001 "living gallery": an atmospheric single-image screen for the 7.5" 800x480 panel.

Modes, in cycle order:   live -> tonight -> onthisday -> wire
  live       a followed team is playing: mood image (team story photo, else big logos) + score line.
             Locks the screen while live; the board's live beat refreshes it (~1 min).
  tonight    the next matchup for a followed team (logos, time, TV, odds).
  onthisday  a history / science event for today's date (Wikipedia "selected" feed), with a
             Wikimedia Commons still -- only freely licensed (PD / CC) Commons files are shown,
             and the licence + author are credited on screen. Sports items are skipped.
  wire       one sports-news still + headline (ESPN team news). ESPN photos are copyrighted:
             personal, in-home display only, credited, never republished.

Mode choice (pick_mode) is a pure function of (data, time, button offset):
  * anything live -> "live" (lock)
  * otherwise the available modes rotate every gallery_rotate_min (default 20) minutes;
    the E1001's right/middle buttons add +1/-1 to an offset so you can step manually.
  * thin modes (no data) are skipped, so wire fills in when tonight / onthisday are empty.

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
MODES = ["live", "tonight", "onthisday", "wire"]

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


# ------------------------------------------------------------------ mode logic

def available(data, teams):
    av = []
    if any(r.live for r in teams):
        av.append("live")
    if any(r.next for r in teams):
        av.append("tonight")
    if data.get("onthisday"):
        av.append("onthisday")
    if data.get("wire"):
        av.append("wire")
    return av


def pick_mode(data, teams, now, cfg, offset=0):
    if cfg.get("gallery_lock_live", True) and any(r.live for r in teams):
        return "live"
    force = cfg.get("gallery_mode") or ""
    av = [m for m in available(data, teams) if m != "live"]
    if force in av:
        return force
    if not av:
        return "onthisday"
    slot = int(now // (max(5, int(cfg.get("gallery_rotate_min", 20))) * 60))
    return av[(slot + offset) % len(av)]


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
    names = {"live": "LIVE", "tonight": "NEXT UP", "onthisday": "ON THIS DAY", "wire": "THE WIRE"}
    R.text(d, (x + 6, y + 13), names[mode], R.F("cb", 17), anchor="lm")
    R.text(d, (w - 14, y + 13), time.strftime("%a %b %-d · %-I:%M %p", time.localtime(now)), R.F("cm", 17), anchor="rm")
    if credit:
        R.text(d, (w // 2 + 40, y + 13), R.ellipsize(d, credit, R.F("cr", 14), 300), R.F("cr", 14), anchor="mm")


def _logo(url, size):
    try:
        import logos
        return logos.get(url, size)
    except Exception:
        return None


# ------------------------------------------------------------------ the four screens

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


def _matchup(c, d, g, row, y, w, big=150):
    """Two logos with the score / time between them."""
    us = _logo(row.logo or g.our_logo, big)
    them = _logo(g.opp_logo, big)
    left, right = (them, us) if g.home else (us, them)
    lname, rname = (g.opp_name or g.opp, row.label) if g.home else (row.label, g.opp_name or g.opp)
    for im, cx, nm in ((left, w // 4, lname), (right, 3 * w // 4, rname)):
        if im is not None:
            c.paste(im.convert("L"), (cx - im.size[0] // 2, y))
        else:
            R.text(d, (cx, y + big // 2), nm[:3].upper(), R.F("cb", 80), anchor="mm")
        R.text(d, (cx, y + big + 22), nm, R.F("cb", 30), anchor="mm")


def render_live(data, teams, now, size):
    w, h = size
    c = Image.new("L", size, R.PAPER)
    d = ImageDraw.Draw(c)
    row = next((r for r in teams if r.live), None)
    if row is None:
        return render_tonight(data, teams, now, size)
    g = row.live
    # mood: that team's latest story photo, washed out so the score reads on top of a paper band
    story = next((a for a in data.get("wire") or [] if a.get("key") == row.key), None)
    if story:
        ph = _photo(story["image"], (w, h - 26), gamma=0.55, contrast=1.0)
        c.paste(ph, (0, 0))
    else:
        # no story photo: a giant, faint team logo as the mood backdrop
        lg = _logo(row.logo or g.our_logo, 420)
        if lg is not None:
            faint = lg.convert("L").point(lambda v: 255 - (255 - v) // 5)
            c.paste(faint, (w - faint.size[0] + 60, (h - faint.size[1]) // 2))
            c.paste(faint, (-60, (h - faint.size[1]) // 2))
    _band(c, 70, 360)
    d.line((0, 70, w, 70), fill=R.INK, width=2)
    d.line((0, 360, w, 360), fill=R.INK, width=2)
    R.pill(d, 18, 22, "LIVE", R.F("cb", 22))
    _matchup(c, d, g, row, 90, w, big=170)
    a, b = (g.them, g.us) if g.home else (g.us, g.them)
    R.text(d, (w / 2, 175), f"{a if a is not None else 0}–{b if b is not None else 0}", R.F("cb", 96), anchor="mm")
    R.text(d, (w / 2, 245), g.detail or "", R.F("csb", 28), anchor="mm")
    if g.tv:
        R.text(d, (w / 2, 280), g.tv, R.F("cm", 22), anchor="mm")
    others = [r for r in teams if r.live and r is not row]
    if others:
        s = "  ·  ".join(f"{r.label} {r.live.us}-{r.live.them} {r.live.detail}" for r in others[:2])
        _band(c, 370, h - 30)
        R.text(d, (w / 2, 398), R.ellipsize(d, s, R.F("cb", 24), w - 40), R.F("cb", 24), anchor="mm")
    _footer(d, w, h, "live", now, f"Photo: {story['credit']} (ESPN)" if story else "")
    return c


def render_tonight(data, teams, now, size):
    w, h = size
    c = Image.new("L", size, R.PAPER)
    d = ImageDraw.Draw(c)
    nxt = sorted((r for r in teams if r.next), key=lambda r: r.next.start)
    if not nxt:
        R.text(d, (w / 2, h / 2), "No games on the slate", R.F("cb", 40), anchor="mm")
        _footer(d, w, h, "tonight", now)
        return c
    row = nxt[0]
    g = row.next
    lt, ln = time.localtime(g.start), time.localtime(now)
    day = "TONIGHT" if lt[:3] == ln[:3] and lt.tm_hour >= 17 else (
        "TODAY" if lt[:3] == ln[:3] else time.strftime("%A", lt).upper())
    R.text(d, (w / 2, 40), f"{day} · {'TBD' if g.time_tbd else time.strftime('%-I:%M %p', lt)}",
           R.F("cb", 44), anchor="mm")
    _matchup(c, d, g, row, 88, w, big=180)
    R.text(d, (w / 2, 178), "@" if not g.home else "vs", R.F("cb", 60), anchor="mm")
    bits = [b for b in (g.note, g.tv, g.odds.details if g.odds else "", row.record) if b]
    R.text(d, (w / 2, 335), R.ellipsize(d, "  ·  ".join(bits), R.F("cm", 24), w - 40), R.F("cm", 24), anchor="mm")
    later = nxt[1:4]
    if later:
        d.line((40, 365, w - 40, 365), fill=R.INK, width=1)
        for i, r in enumerate(later):
            gg = r.next
            s = f"{r.label} {'vs' if gg.home else '@'} {gg.opp}  {time.strftime('%a %-I:%M%p', time.localtime(gg.start))}"
            R.text(d, ((i + 0.5) * w / len(later), 395), s, R.F("csb", 22), anchor="mm")
    _footer(d, w, h, "tonight", now)
    return c


def compose(ctx, size=None):
    """L image (W x H) for the current gallery mode."""
    size = size or (R.PW, R.PH)
    data = getattr(ctx, "gallery", None) or {}
    mode = pick_mode(data, ctx.teams, ctx.now, ctx.cfg, getattr(ctx, "gallery_offset", 0))
    if mode == "live":
        img = render_live(data, ctx.teams, ctx.now, size)
    elif mode == "tonight":
        img = render_tonight(data, ctx.teams, ctx.now, size)
    elif mode == "wire":
        img = render_wire(data, ctx.now, size)
    else:
        img = render_onthisday(data, ctx.now, size)
    return img


def compose_gray(ctx):
    """Device-native greys with error diffusion (photos need it; flat text stays solid)."""
    return dither(compose(ctx), R.GRAY_LEVELS)
