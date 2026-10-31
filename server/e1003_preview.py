"""Render the E1003 (1872x1404, 16-grey) nine-slot home to a PNG for approval.
QUAD_MODEL=e1003 python3 e1003_preview.py [out.png]
Live public sources where reachable (weather, news, ESPN, Hebcal); fixtures for
the private feeds (calendar, TripIt, shopping) and the sky panel."""
import os, sys, time
os.environ.setdefault("TZ", "America/New_York"); time.tzset()
os.environ["QUAD_MODEL"] = "e1003"
import render as R, sources as S, ical

out = sys.argv[1] if len(sys.argv) > 1 else "/workspace/quad-vm/e1003-home-preview.png"
LAT, LON = S.SHABBAT_LAT, S.SHABBAT_LON
CUSTOM = {"devils": ("Devils", "hockey", "nhl", "nj"), "jets": ("Jets", "football", "nfl", "nyj"),
          "mets": ("Mets", "baseball", "mlb", "nym"), "nets": ("Nets", "basketball", "nba", "bkn"),
          "navy_fb": ("Navy", "football", "college-football", "2426"),
          "rutgers_fb": ("Rutgers", "football", "college-football", "164"),
          "rutgers_bb": ("Rutgers Hoops", "basketball", "mens-college-basketball", "164")}
S.TEAMS.update(CUSTOM)
CFG = {"lat": LAT, "lon": LON, "feeds": ["world", "us", "markets"], "tickers": ["^GSPC", "^DJI", "^IXIC", "SOXL"],
       "teams": list(CUSTOM), "radar_nm": 25,
       "layout": "thirds", "slots": ["shabbat", "newsletter", "weather", "news", "trips", "sports", "scoreboard"]}
now = time.time()
if os.environ.get("PREVIEW_AT"):     # e.g. "2026-11-01 15:30" local: drives the layout mode
    now = time.mktime(time.strptime(os.environ["PREVIEW_AT"], "%Y-%m-%d %H:%M"))


def tryit(fn, default):
    try:
        return fn()
    except Exception as e:      # offline / rate-limited: fall back
        print("fallback:", fn, e)
        return default


wx = tryit(lambda: S.weather(CFG), None)
heads = tryit(lambda: S.headlines(CFG), [])
qs = tryit(lambda: S.quotes(CFG), [])
teams = tryit(lambda: S.sports(CFG)[0], [])
sh = tryit(lambda: S.shabbat(CFG), None)
try:
    import logos
    logos.prefetch([r.logo for r in teams] + [g.opp_logo for r in teams for g in (r.live, r.last, r.next) if g])
except Exception as e:
    print("logos:", e)

H_ = 3600
day0 = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
def ev(title, a, b):
    return ical.CalEvent(title=title, start=a, end=b, day=time.strftime("%Y-%m-%d", time.localtime(a)))


cal = [ev("Dentist", day0 + 15.5 * H_, day0 + 16.5 * H_), ev("Family dinner", day0 + 18.5 * H_, day0 + 20 * H_),
       ev("Kids soccer", day0 + 34 * H_, day0 + 35.5 * H_)]
trip = ical.Trip(name="Miami", start=day0 + 6 * 24 * H_, end=day0 + 9 * 24 * H_, segments=[
    ical.Segment(kind="flight", start=day0 + 6 * 24 * H_ + 8 * H_, end=day0 + 6 * 24 * H_ + 11 * H_, title="UA 1234 EWR to MIA",
                 airline="United", iata="UA", number="1234", origin="EWR", dest="MIA"),
    ical.Segment(kind="hotel", start=day0 + 6 * 24 * H_ + 15 * H_, end=day0 + 9 * 24 * H_ + 11 * H_, title="Fontainebleau",
                 name="Fontainebleau Miami Beach")])
fl = R.Sighting(callsign="UAL1871", airline="United Airlines", origin="EWR", dest="ORD", origin_city="Newark",
                dest_city="Chicago", alt_ft=8200, speed_kt=290, dist_nm=2.4, elev_deg=28, seen_at=now - 30,
                flight_no="UA 1871", aircraft="Boeing 737-900", bearing=300, progress=0.06, eta_min=118, dest_dist_nm=700)
nearby = [(3, 40, "UAL1871"), (8, 200, "JBU55"), (15, 290, "N123AB"), (21, 100, "DAL402")]
shop = [{"text": t, "done": d} for t, d in [("Challah", False), ("Eggs", False), ("Grape juice", False), ("Paper towels", False),
                                            ("Bananas", False), ("Coffee beans", False), ("Olive oil", True)]]
# real shul.json shape: QUAD_SHUL fixture (e.g. `python3 shul.py --pdf YIOT101626.pdf --out x.json`), else cache
import shul as SHUL  # noqa: E402
shul = SHUL.load()
ctx = R.Ctx(now=now, cfg=CFG, weather=wx, heads=heads, quotes=qs, teams=teams, featured=fl, nearby=nearby,
            recent=[], day_count=12, shabbat=sh, calendar=cal, trips=[trip], shopping=shop,
            brief="",   # strip falls back to next-up / top headline
            indoor_f=70.5, indoor_rh=44, battery=86, updated=now, shul=shul)
from dataclasses import replace as _rp
LIVE = {"devils": ("NYR", "Rangers", True, "2nd 8:41", 2, 1, "MSG"), "nets": ("BOS", "Celtics", False, "3rd 4:12", 78, 81, "YES"),
        "jets": ("LV", "Raiders", True, "Q3 6:02", 17, 13, "CBS"),
        "navy_fb": ("AF", "Air Force", True, "3rd 7:48", 21, 14, "CBS")}
for k in (os.environ.get("GAMEDAY") or "").split(","):   # GAMEDAY=devils,nets fakes those teams live
    for r in teams:
        if r.key == k and k in LIVE:
            o, on, hm_, det, us, th, tv = LIVE[k]
            r.live = S.Game(team=r.label, abbr=k[:3].upper(), opp=o, opp_name=on, home=hm_, start=now - 5400,
                            state="in", detail=det, us=us, them=th, tv=tv, record=r.record)
            if k == "navy_fb":
                r.live.situation = {"downDistanceText": "2nd & 6 at AF 34",
                                    "lastPlay": "Blake Horvath 41-yd TD run on the triple option; Navy leads 21-14"}
                r.live.note = "Commander-in-Chief's Trophy"
                ctx.team_news = {"navy_fb": tryit(lambda: S.team_news("navy_fb"), [])}
if R.nfl_day(now):
    sl = tryit(lambda: S.league_slate(), [])
    if os.environ.get("NFL_FAKE"):   # mid-slate Sunday: 1pm games live, early finals, late games to play
        import random
        rnd = random.Random(7)
        for i, g in enumerate(sl):
            if g.away == "NYJ" or g.home == "NYJ":
                us_h = g.home == "NYJ"
                g.state, g.detail = "in", "3rd 6:02"
                g.home_score, g.away_score = (17, 13) if us_h else (13, 17)
            elif i < 6:
                g.state, g.detail = "in", rnd.choice(["3rd 2:11", "4th 9:40", "2nd 0:31", "3rd 11:05"])
                g.away_score, g.home_score = rnd.randint(3, 31), rnd.randint(3, 31)
            elif i < 9:
                g.state, g.detail = "post", "Final"
                g.away_score, g.home_score = rnd.randint(6, 38), rnd.randint(6, 38)
        o = {"in": 0, "post": 1, "pre": 2}
        sl.sort(key=lambda g: (o[g.state], g.start))
    ctx.league = sl
ctx.teams = teams
print("mode:", R.home_mode(ctx))
g = R.compose_gray(ctx)
assert g.size == (1872, 1404), g.size
g.save(out)
ctx.page = "newsletter"
R.compose_gray(ctx).save(out.replace(".png", "-detail.png"))
R.compose(ctx).save(out.replace(".png", "-1bit.png"))
print("wrote", out, g.size, "mode:", R.home_mode(ctx), R.home_layout(ctx)[1])
