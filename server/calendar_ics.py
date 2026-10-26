"""Calendar corner: busy times from published iCalendar (ICS) links.

The links live in quad.env as QUAD_CALENDAR_ICS (space separated), never in config.json: the
settings page shows config.json, and a published-calendar link is as good as a password.

Built for Outlook / Microsoft 365 "Can view when I'm busy" publishing, where every event is
just Busy / Tentative / Out of office. If a feed does carry titles they are only shown when the
calendar_titles setting is on; otherwise the board shows the busy status alone.
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from dateutil.rrule import rrulestr

UA = {"User-Agent": "eink-quad/0.1 (personal e-paper dashboard; hobby project)"}

# Outlook writes Windows zone names. The common ones; anything else falls back to the server's TZ.
WINDOWS_TZ = {
    "Eastern Standard Time": "America/New_York", "US Eastern Standard Time": "America/Indianapolis",
    "Central Standard Time": "America/Chicago", "Mountain Standard Time": "America/Denver",
    "US Mountain Standard Time": "America/Phoenix", "Pacific Standard Time": "America/Los_Angeles",
    "Alaskan Standard Time": "America/Anchorage", "Hawaiian Standard Time": "Pacific/Honolulu",
    "Atlantic Standard Time": "America/Halifax", "GMT Standard Time": "Europe/London",
    "Greenwich Standard Time": "Atlantic/Reykjavik", "W. Europe Standard Time": "Europe/Berlin",
    "Romance Standard Time": "Europe/Paris", "Central Europe Standard Time": "Europe/Budapest",
    "India Standard Time": "Asia/Kolkata", "China Standard Time": "Asia/Shanghai",
    "Tokyo Standard Time": "Asia/Tokyo", "AUS Eastern Standard Time": "Australia/Sydney",
    "UTC": "UTC", "Coordinated Universal Time": "UTC",
}

# X-MICROSOFT-CDO-BUSYSTATUS and the SUMMARY values Outlook uses when only free/busy is published
STATUS = {"BUSY": "Busy", "TENTATIVE": "Tentative", "OOF": "Out of office",
          "WORKINGELSEWHERE": "Working elsewhere", "FREE": None}
SUMMARY_STATUS = {"busy": "Busy", "tentative": "Tentative", "out of office": "Out of office",
                  "away": "Out of office", "working elsewhere": "Working elsewhere", "free": None}


@dataclass
class Busy:
    start: float          # epoch seconds
    end: float
    all_day: bool
    label: str            # "Busy", "Tentative", ... or the title when calendar_titles is on
    cal: str = ""


def local_tz():
    try:
        return ZoneInfo(os.environ.get("TZ") or "America/New_York")
    except ZoneInfoNotFoundError:
        return timezone.utc


def _zone(name):
    if not name:
        return local_tz()
    name = name.strip('"')
    try:
        return ZoneInfo(WINDOWS_TZ.get(name, name))
    except (ZoneInfoNotFoundError, ValueError):
        return local_tz()


def _unescape(s):
    return re.sub(r"\\([nN,;\\])", lambda m: "\n" if m.group(1) in "nN" else m.group(1), s)


def _prop(line):
    q, i = False, 0
    for i, ch in enumerate(line):
        if ch == '"':
            q = not q
        elif ch == ":" and not q:
            break
    else:
        return None
    head = line[:i].split(";")
    params = {}
    for p in head[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            params[k.upper()] = v.strip('"')
    return head[0].upper(), params, line[i + 1:]


def _when(value, params):
    """date for all-day values, aware datetime otherwise."""
    v = value.strip()
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", v):
        return date(int(v[:4]), int(v[4:6]), int(v[6:8]))
    m = re.fullmatch(r"(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})(Z?)", v)
    if not m:
        return None
    dt = datetime(*map(int, m.groups()[:6]))
    return dt.replace(tzinfo=timezone.utc) if m.group(7) else dt.replace(tzinfo=_zone(params.get("TZID")))


def _events(text):
    lines = re.sub(r"\r?\n[ \t]", "", text).splitlines()
    out, cur, nested = [], None, 0
    for line in lines:
        if line == "BEGIN:VEVENT":
            cur, nested = {"exdate": []}, 0
            continue
        if cur is None:
            continue
        if line == "END:VEVENT":
            out.append(cur)
            cur = None
            continue
        if line.startswith("BEGIN:"):
            nested += 1
            continue
        if line.startswith("END:"):
            nested -= 1
            continue
        if nested:
            continue
        p = _prop(line)
        if not p:
            continue
        name, params, value = p
        if name in ("DTSTART", "DTEND", "RECURRENCE-ID"):
            cur[name] = _when(value, params)
        elif name == "EXDATE":
            cur["exdate"] += [_when(v, params) for v in value.split(",")]
        elif name in ("UID", "RRULE", "DURATION", "STATUS", "TRANSP", "X-MICROSOFT-CDO-BUSYSTATUS"):
            cur[name] = value.strip()
        elif name == "SUMMARY":
            cur[name] = _unescape(value).strip()
    return [e for e in out if e.get("DTSTART") is not None]


def _duration(e):
    s, end = e["DTSTART"], e.get("DTEND")
    if end is not None:
        return end - s
    m = re.fullmatch(r"P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", e.get("DURATION", ""))
    if m and any(m.groups()):
        w, d, h, mi, se = (int(x or 0) for x in m.groups())
        return timedelta(weeks=w, days=d, hours=h, minutes=mi, seconds=se)
    return timedelta(days=1) if isinstance(s, date) and not isinstance(s, datetime) else timedelta(0)


def _key(v):
    if isinstance(v, datetime):
        return ("t", v.timestamp())
    return ("d", v.isoformat()) if v else None


def _status(e, titles):
    st = STATUS.get(e.get("X-MICROSOFT-CDO-BUSYSTATUS", "").upper(), "Busy")
    summary = e.get("SUMMARY", "")
    by_summary = SUMMARY_STATUS.get(summary.lower(), "missing")
    if by_summary is None or st is None or e.get("TRANSP", "").upper() == "TRANSPARENT":
        return None                     # free time: not shown
    if by_summary != "missing":
        st = by_summary
    return summary if (titles and summary and by_summary == "missing") else st


def _occurrences(e, lo, hi):
    """Start values of e that may overlap [lo, hi)."""
    s = e["DTSTART"]
    if not e.get("RRULE") or e.get("RECURRENCE-ID") is not None:
        return [s]
    all_day = not isinstance(s, datetime)
    rule_txt = e["RRULE"]
    if all_day:   # date-based rule: iterate naive midnights; a UTC UNTIL has to lose its Z
        start = datetime(s.year, s.month, s.day)
        rule_txt = re.sub(r"(UNTIL=\d{8}T\d{6})Z", r"\1", rule_txt)
        a, b = datetime.combine(lo.date(), datetime.min.time()), hi.replace(tzinfo=None)
    else:
        start, a, b = s, lo, hi
    try:
        rule = rrulestr(rule_txt, dtstart=start)
        hits = rule.between(a - _duration(e), b, inc=True)
    except (ValueError, TypeError):
        return [s]
    return [h.date() for h in hits] if all_day else hits


def parse(text, lo, hi, cal="", titles=False):
    tz = local_tz()
    evs = _events(text)
    moved = {}
    for e in evs:
        if e.get("RECURRENCE-ID") is not None:
            moved.setdefault(e.get("UID"), set()).add(_key(e["RECURRENCE-ID"]))
    out = []
    for e in evs:
        if e.get("STATUS", "").upper() == "CANCELLED":
            continue
        label = _status(e, titles)
        if label is None:
            continue
        dur = _duration(e)
        skip = {_key(x) for x in e["exdate"] if x} | (moved.get(e.get("UID"), set()) if e.get("RECURRENCE-ID") is None else set())
        for s in _occurrences(e, lo, hi):
            if e.get("RRULE") and _key(s) in skip:
                continue
            if isinstance(s, datetime):
                a, b = s.timestamp(), (s + dur).timestamp()
                all_day = False
            else:   # all-day: local midnights
                a = datetime(s.year, s.month, s.day, tzinfo=tz).timestamp()
                end = s + (dur if dur.days else timedelta(days=1))
                b = datetime(end.year, end.month, end.day, tzinfo=tz).timestamp()
                all_day = True
            if b <= lo.timestamp() or a >= hi.timestamp():
                continue
            out.append(Busy(a, max(b, a + 900) if not all_day else b, all_day, label, cal))
    return sorted(out, key=lambda x: (x.start, not x.all_day))


def merge(blocks):
    """Join overlapping timed blocks with the same label, so back-to-back meetings read as one."""
    out = []
    for x in blocks:
        if out and not x.all_day and not out[-1].all_day and x.label == out[-1].label and x.start <= out[-1].end:
            out[-1].end = max(out[-1].end, x.end)
        else:
            out.append(Busy(x.start, x.end, x.all_day, x.label, x.cal))
    return out


def fetch(urls, titles=False, days=4):
    now = datetime.now(timezone.utc)
    lo, hi = now - timedelta(days=1), now + timedelta(days=days)
    names = (os.environ.get("QUAD_CALENDAR_NAMES") or "").split(",")
    blocks = []
    for i, u in enumerate(urls):
        r = requests.get(u.replace("webcal://", "https://", 1), headers=UA, timeout=20)
        r.raise_for_status()
        blocks += parse(r.text, lo, hi, (names[i].strip() if i < len(names) else ""), titles)
    blocks.sort(key=lambda x: (x.start, not x.all_day))
    return merge(blocks)


def urls_from_env():
    return [u for u in (os.environ.get("QUAD_CALENDAR_ICS") or "").split() if re.match(r"(https|webcal)://", u)]


if __name__ == "__main__":
    # Quick check from a shell: QUAD_CALENDAR_ICS=... python calendar_ics.py
    for b in fetch(urls_from_env()):
        t = time.strftime("%a %d %b %H:%M", time.localtime(b.start))
        print(f"{t}  {'all day' if b.all_day else time.strftime('%H:%M', time.localtime(b.end))}  {b.label}")
