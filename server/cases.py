"""Render every screen with live data (plus fake sky cases) to PNGs for eyeballing.
python3 cases.py  ->  case_*.png"""
import os, time
os.environ.setdefault("TZ", "America/New_York"); time.tzset()
import render as R, sources as S, skyevents

CFG = {"lat": 27.9506, "lon": -82.4572, "feeds": ["world", "us", "markets"],
       "tickers": ["^GSPC", "^DJI", "^IXIC", "SOXL"], "teams": ["bucs", "lightning", "rays", "gators_fb", "gators_bb"],
       "slots": ["weather", "news", "sports", "sky"], "radar_nm": 25}
now = time.time()
wx = S.weather(CFG); heads = S.headlines(CFG); qs = S.quotes(CFG); teams, _ = S.sports(CFG)
fl = R.Sighting(callsign="DAL2474", airline="Delta Air Lines", origin="ATL", dest="TPA", origin_city="Atlanta",
                dest_city="Tampa", alt_ft=6400, speed_kt=252, dist_nm=1.8, elev_deg=31, seen_at=now - 30,
                flight_no="DL 2474", aircraft="Airbus A321neo", bearing=315, progress=0.93, eta_min=9, dest_dist_nm=4)
recent = [R.Sighting(callsign="SWA1", airline="Southwest", origin="BWI", dest="TPA", origin_city="Baltimore", dest_city="Tampa",
                     alt_ft=3000, speed_kt=200, dist_nm=3, elev_deg=12, seen_at=now - 600 * i, flight_no=f"WN {1200+i}",
                     aircraft="Boeing 737 MAX 8", bearing=10) for i in range(1, 7)]
nearby = [(3, 40, "DAL2474"), (8, 200, "SWA12"), (15, 290, "N123AB"), (21, 100, "JBU55")]
base = dict(now=now, cfg=CFG, weather=wx, heads=heads, quotes=qs, teams=teams, recent=recent, day_count=143,
            nearby=nearby, brief="Treasury yields keep climbing as stocks slip; Rays open the ALDS Saturday and rain chances jump Thursday.",
            indoor_f=74.3, indoor_rh=52, updated=now, cycle=0)
cases = {
    "quad": R.Ctx(**base, featured=fl),
    "quad_quiet": R.Ctx(**base, featured=None, sticky="Dinner at 7 — bring the charger!"),
    "quad_iss": R.Ctx(**base, featured=fl, event=skyevents.demo_event("iss", 27.94, -82.46)),
}
for p in ("weather", "news", "sports", "sky"):
    cases["page_" + p] = R.Ctx(**base, featured=fl, page=p)
# a synthetic line on the first next game and a covered final, so both layouts are exercised
for r in teams:
    if r.next and r.next.odds is None and r.next.opp != "TBD":
        r.next.odds = S.Odds("DraftKings", f"{r.next.abbr} -3.5", -3.5, 44.5, -180, 150, 62)
        break
import logos
logos.prefetch([r.logo for r in teams] + [g.opp_logo for r in teams for g in (r.live, r.last, r.next) if g])
for k, ctx in cases.items():
    R.compose(ctx).save(f"case_{k}.png")
    if k in ("quad", "page_sports"):
        R.compose_gray(ctx).save(f"case_{k}_gray.png")
print("ok", list(cases))
