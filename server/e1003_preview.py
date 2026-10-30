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
CFG = {"lat": LAT, "lon": LON, "feeds": ["world", "us", "markets"], "tickers": ["^GSPC", "^DJI", "^IXIC", "BTC-USD"],
       "teams": list(CUSTOM), "radar_nm": 25,
       "layout": "thirds", "slots": ["shabbat", "newsletter", "weather", "trips", "calendar", "sports", "scoreboard"]}
now = time.time()


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
shul = {"highlights": [("Youth event", "Sunday morning youth minyan & brunch in the social hall, all grades welcome; parents invited to stay for coffee."),
                       ("Guest speaker", "Rabbi visit this Shabbat afternoon: a shiur on the parsha after mincha, followed by seudah shlishit."),
                       ("Chesed", "Meal train sign-ups for the M. family are open through next Thursday. Contact the chesed committee."),
                       ("Mazal tov", "To the K. family on the birth of a granddaughter, and to the B. family on their son's bar mitzvah."),
                       ("Shul dinner", "Annual dinner reservations and journal ads are due November 15. Honorees to be announced."),
                       ("Sisterhood", "Challah bake Wednesday night at 8 PM. Ingredients provided; bring an apron."),
                       ("Daf yomi", "Nightly daf continues at 9 PM in the beis medrash and on Zoom."),
                       ("Security", "Please use the main entrance on Shabbat; side doors remain locked.")]}
ctx = R.Ctx(now=now, cfg=CFG, weather=wx, heads=heads, quotes=qs, teams=teams, featured=fl, nearby=nearby,
            recent=[], day_count=12, shabbat=sh, calendar=cal, trips=[trip], shopping=shop,
            brief="E1003 home: left Shabbat + newsletter, middle weather/TripIt/calendar, right sports.",
            indoor_f=70.5, indoor_rh=44, battery=86, updated=now, shul=shul)
g = R.compose_gray(ctx)
assert g.size == (1872, 1404), g.size
g.save(out)
ctx.page = "newsletter"
R.compose_gray(ctx).save(out.replace(".png", "-detail.png"))
R.compose(ctx).save(out.replace(".png", "-1bit.png"))
print("wrote", out, g.size, "slots:", R.resolve_slots(ctx))
