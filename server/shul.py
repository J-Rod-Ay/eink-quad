"""Young Israel of Teaneck: weekly newsletter highlights (no minyan times: Zev asked to leave those off the board).

The shul posts its weekly newsletter as a PDF on ShulCloud:
  https://images.shulcloud.com/598/uploads/Newsletters/<YYYY>/<Month>/YIOT<mmddyy>_merged.pdf
(<mmddyy> = the Friday the issue starts). www.yiot.org itself answers 406 to
non-browser clients, so we find the issue by probing that pattern, download it
and parse pages 1-2 with poppler's pdftotext -bbox-layout. The result is a JSON
file the server (and render) read; nothing here invents data: whatever isn't in
the PDF is simply absent.

Run standalone:  python3 shul.py [--force] [--url PDF] [--out shul.json]
"""
import html
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import date, datetime, timedelta

import requests

UA = "Mozilla/5.0 (X11; Linux x86_64) quad-board/1.0 (+github.com/J-Rod-Ay/eink-quad)"
BASE = "https://images.shulcloud.com/598/uploads/Newsletters/{y}/{month}/YIOT{d}{suffix}.pdf"
HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.environ.get("QUAD_SHUL", os.path.join(HERE, "cache", "shul.json"))

MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
               "October", "November", "December"]

# heading keyword -> personal/community kind (order matters)
KINDS = [
    ("condolences", r"condolenc|sympath|nichum|avel|passing|baruch dayan|sad news|in memoriam"),
    ("refuah", r"refu|cholim|get well|tehillim for"),
    ("birthdays", r"birthday"),
    ("anniversaries", r"anniversar"),
    ("mazal_tov", r"mazal|mazel|simch|good news|congrat"),
    ("welcome", r"welcome|new member"),
    ("kiddush", r"kiddush|sponsor|seudah"),
    ("learning", r"learning|shiur|torah"),
    ("youth", r"youth|teen|junior"),
    ("community", r"community announce"),
]
SKIP = r"stay connected|shul payments|riddle|^schedule$"


def _kind(title):
    t = title.lower()
    if re.search(SKIP, t):
        return "skip"
    for k, rx in KINDS:
        if re.search(rx, t):
            return k
    return "event"


# ------------------------------------------------------------------ find + download

def fridays_around(today=None):
    """Upcoming Friday first (Thursday-night runs want tomorrow's issue), then the last two."""
    today = today or date.today()
    up = today + timedelta(days=(4 - today.weekday()) % 7)
    return [up, up - timedelta(days=7), up - timedelta(days=14)]


def candidates(fri):
    d = fri.strftime("%m%d%y")
    months = [fri, fri - timedelta(days=7)]          # an issue straddling months may sit in either folder
    seen = []
    for m in months:
        for suffix in ("_merged", ""):
            u = BASE.format(y=m.year, month=MONTH_NAMES[m.month - 1], d=d, suffix=suffix)
            if u not in seen:
                seen.append(u)
    return seen


def find_latest(today=None, session=None):
    s = session or requests
    for fri in fridays_around(today):
        for u in candidates(fri):
            try:
                r = s.head(u, headers={"User-Agent": UA}, timeout=15, allow_redirects=True)
                if r.status_code == 200 and "pdf" in r.headers.get("content-type", "pdf"):
                    return u, fri
            except requests.RequestException:
                continue
    return None, None


# ------------------------------------------------------------------ parse

def _words(pdf, page):
    out = subprocess.run(["pdftotext", "-f", str(page), "-l", str(page), "-bbox", pdf, "-"],
                         capture_output=True, text=True, timeout=60).stdout
    pw = float((re.search(r'<page width="([\d.]+)"', out) or [0, 612])[1])
    ws = [(float(a), float(b), float(c), float(d_), html.unescape(t)) for a, b, c, d_, t in
          re.findall(r'<word xMin="([\d.]+)" yMin="([\d.]+)" xMax="([\d.]+)" yMax="([\d.]+)">([^<]*)</word>', out)]
    # superscript ordinals ("16th" set as "16" + raised "th") come out as stray words: drop them
    ws = [w for w in ws if not (w[4] in ("th", "st", "nd", "rd") and (w[3] - w[1]) < 8)]
    return pw, ws


def _split_x(ws, pw):
    """The x (between 30% and 70% of the page) that the fewest words straddle."""
    best = None
    for x in range(int(pw * .3), int(pw * .7), 2):
        n = sum(1 for w in ws if w[0] < x < w[2])
        if best is None or n < best[0] or (n == best[0] and abs(x - pw / 2) < abs(best[1] - pw / 2)):
            best = (n, x)
    return best[1]


def _lines(ws):
    """Group words into lines (same baseline), left to right."""
    ws = sorted(ws, key=lambda w: (round(w[3]), w[0]))
    lines = []
    for w in ws:
        if lines and abs(lines[-1]["y1"] - w[3]) < 3:
            ln = lines[-1]
            ln["w"].append(w)
            ln["x1"] = max(ln["x1"], w[2])
        else:
            lines.append({"y0": w[1], "y1": w[3], "x0": w[0], "x1": w[2], "w": [w]})
    for ln in lines:
        ln["w"].sort(key=lambda w: w[0])
        ln["x0"] = ln["w"][0][0]
        ln["text"] = " ".join(w[4] for w in ln["w"]).strip()
    return [ln for ln in lines if ln["text"]]


def _clean(s):
    s = re.sub(r"\s+", " ", s).strip()
    s = s.replace(" ,", ",").replace(" .", ".")
    return s


def _sentences(txt):
    parts = re.split(r"(?<!\bJr\.)(?<!\bDr\.)(?<!\bMr\.)(?<!\bMs\.)(?<!\bMrs\.)(?<!\bSt\.)(?<=[.!])\s+(?=[A-Z“\"])", _clean(txt))
    out = []
    for p in parts:
        if out and re.match(r"(Mazal|Mazel) Tov (also )?to (his|her|their|the) ", p):
            out[-1] += " " + p            # "Mazal Tov to her parents..." belongs with the simcha before it
        elif p:
            out.append(p)
    return out


def _sections(lines, x0, x1):
    """Headings (centered, gap above, not a bullet) and their items."""
    mid, width = (x0 + x1) / 2, x1 - x0
    secs, prev = [], None
    for ln in lines:
        bullet = ln["text"].startswith("•")
        gap = ln["y0"] - prev["y1"] if prev else 99
        lmid = (ln["x0"] + ln["x1"]) / 2
        head = (not bullet and gap > 5 and abs(lmid - mid) < width * .12 and (ln["x1"] - ln["x0"]) < width * .85
                and ln["x0"] > x0 + width * .06 and len(ln["text"]) < 90)
        if head:
            secs.append({"title": _clean(ln["text"]), "lines": []})
        elif secs:
            secs[-1]["lines"].append((bullet, ln["text"]))
        prev = ln
    out = []
    for s in secs:
        items, cur = [], None
        has_bul = any(b for b, _ in s["lines"])
        for b, t in s["lines"]:
            t = t.lstrip("•").strip()
            if b or cur is None:
                if cur:
                    items.append(cur)
                cur = t
            else:
                cur += " " + t
        if cur:
            items.append(cur)
        if not has_bul:
            items = [x for it in items for x in _sentences(it)]
        items = [_clean(i) for i in items if _clean(i)]
        out.append({"title": s["title"], "kind": _kind(s["title"]), "items": items})
    return out


DAY_RX = re.compile(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(\d{1,2})\s*/\s*(\d{1,2})\s+(\w+)", re.I)


def _schedule(lines, year_hint):
    days, cur = [], None
    for ln in lines:
        t = _clean(ln["text"])
        m = DAY_RX.search(t)
        if m and ":" not in t:
            mo, dd = MONTHS[m.group(1)[:3].lower()], int(m.group(2))
            y = year_hint if not (mo == 1 and date.today().month == 12) else year_hint + 1
            ds = date(y, mo, dd)
            label = ("Shabbat" if ds.weekday() == 5 else ds.strftime("%A")) + f" {ds.strftime('%b')} {dd}"
            cur = {"date": ds.isoformat(), "label": label, "hebrew": f"{m.group(3)} {m.group(4)}", "items": []}
            days.append(cur)
            continue
        if cur is None or t.lower() == "schedule":
            continue
        if ":" in t and not re.match(r"^\d", t):
            k, v = t.split(":", 1) if not re.search(r"\d:\d", t.split(":", 1)[0]) else (t, "")
            # 'Ma’ariv (MS)/Shabbat Ends: 6:55p' -> label up to the LAST colon that ends a label
            mm = re.match(r"^(.*?[A-Za-z)’'])\s*:\s*(.*)$", t)
            k, v = (mm.group(1), mm.group(2)) if mm else (k, v)
            cur["items"].append([k.strip(), v.strip()])
        elif cur["items"] and re.match(r"^~?\d", t):
            cur["items"][-1][1] = (cur["items"][-1][1] + " " + t).strip()     # wrapped value
        else:
            cur["items"].append([t, ""])                                       # 'Followed by Seudah Shlishit'
    return days


def parse(pdf, url="", issue_fri=None):
    pw, ws = _words(pdf, 1)
    head = " ".join(w[4] for w in ws if w[1] < 112 and w[0] > pw * .45)
    rng = _clean(re.split(r"Rabbi", head)[0]) if head else ""
    rng = re.sub(r"(\d) (th|st|nd|rd)\b", r"\1\2", rng)
    year = (issue_fri or date.today()).year
    sx = _split_x([w for w in ws if w[1] > 112], pw)
    left = _lines([w for w in ws if w[1] > 112 and w[2] <= sx])
    right = _lines([w for w in ws if w[1] > 112 and w[0] >= sx])
    sched = _schedule(left, year)     # only used to recognise the schedule column, not stored
    secs = _sections(right, sx, pw - 20)
    if not sched:                     # layout changed: treat the left column as sections too
        secs = _sections(left, 20, sx) + secs
    try:
        pw2, ws2 = _words(pdf, 2)
        if ws2:
            sx2 = _split_x(ws2, pw2)
            secs += _sections(_lines([w for w in ws2 if w[2] <= sx2]), 20, sx2)
            secs += _sections(_lines([w for w in ws2 if w[0] >= sx2]), sx2, pw2 - 20)
    except Exception as e:
        print(f"[shul] page 2: {e}", flush=True)
    # merge continued sections ("Youth News cont'd")
    merged = []
    for s in secs:
        base = re.sub(r"\s*cont[’']?d\.?$", "", s["title"], flags=re.I)
        tgt = next((m for m in merged if m["title"] == base), None)
        if tgt and base != s["title"]:
            tgt["items"] += s["items"]
        elif s["kind"] != "skip":
            merged.append(s)
    personal = {}
    for s in merged:
        if s["kind"] in ("mazal_tov", "condolences", "refuah", "birthdays", "anniversaries", "welcome", "kiddush"):
            personal.setdefault(s["kind"], []).extend(s["items"])
    issue = issue_fri.isoformat() if issue_fri else (sched[0]["date"] if sched else "")
    return {
        "shul": "Young Israel of Teaneck",
        "issue_date": issue,
        "issue_range": rng,
        "source_url": url,
        "fetched_at": time.time(),
        "personal": personal,
        "sections": [s for s in merged if s["kind"] not in personal],
    }


# ------------------------------------------------------------------ public

def load(path=PATH):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save(data, path=PATH):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)
    os.replace(tmp, path)


def refresh(path=PATH, url=None, force=False):
    """Fetch the newest issue. Returns (data, status) where status is new | same | none."""
    old = load(path)
    fri = None
    if not url:
        url, fri = find_latest()
        if not url:
            return old, "none"
    elif re.search(r"YIOT(\d{6})", url):
        fri = datetime.strptime(re.search(r"YIOT(\d{6})", url).group(1), "%m%d%y").date()
    if old and old.get("source_url") == url and not force:
        old["checked_at"] = time.time()
        save(old, path)
        return old, "same"
    r = requests.get(url, headers={"User-Agent": UA}, timeout=60)
    r.raise_for_status()
    with tempfile.NamedTemporaryFile(suffix=".pdf") as tf:
        tf.write(r.content)
        tf.flush()
        data = parse(tf.name, url, fri)
    data["checked_at"] = time.time()
    save(data, path)
    return data, "new"


# ------------------------------------------------------------------ helpers for render

if __name__ == "__main__":
    a = sys.argv[1:]
    out = a[a.index("--out") + 1] if "--out" in a else PATH
    url = a[a.index("--url") + 1] if "--url" in a else None
    if "--pdf" in a:
        d = parse(a[a.index("--pdf") + 1], url or "", None)
        save(d, out)
        st = "parsed"
    else:
        d, st = refresh(out, url, "--force" in a)
    print(st, (d or {}).get("issue_date"), (d or {}).get("source_url"))
