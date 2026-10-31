"""Render the E1001 (800x480, 4-grey) living gallery, every mode, to PNGs.
python3 e1001_gallery_preview.py [outdir]   -> e1001-gallery-{onthisday,matchup,weather,trip,news,wire}.png + e1001-gallery-preview.png (3x2 sheet)
Live public sources (ESPN, Wikipedia/Commons); the trip uses a fixture when no TripIt/iCal feed is configured on this box."""
import os, sys, time
os.environ.setdefault("TZ", "America/New_York"); time.tzset()
os.environ["QUAD_MODEL"] = "e1001"
import render as R, sources as S, gallery as G
from PIL import Image

out = sys.argv[1] if len(sys.argv) > 1 else "/workspace/quad-vm"
CUSTOM = {"devils": ("Devils", "hockey", "nhl", "nj"), "jets": ("Jets", "football", "nfl", "nyj"),
          "mets": ("Mets", "baseball", "mlb", "nym"), "nets": ("Nets", "basketball", "nba", "bkn"),
          "navy_fb": ("Navy", "football", "college-football", "2426"),
          "rutgers_bb": ("Rutgers Hoops", "basketball", "mens-college-basketball", "164")}
S.TEAMS.update(CUSTOM)
CFG = {"teams": list(CUSTOM), "layout": "gallery", "gallery_rotate_min": 5}
now = time.time()
teams = S.sports(CFG)[0]
import logos
logos.prefetch([r.logo for r in teams] + [g.opp_logo for r in teams for g in (r.live, r.last, r.next) if g])
data = {"onthisday": G.fetch_onthisday(now), "wire": G.fetch_wire(S.TEAMS, CFG["teams"])}
print("onthisday:", {k: v for k, v in (data["onthisday"] or {}).items() if k != "text"})
print("wire:", [(a["team"], a["headline"]) for a in data["wire"]])

data["news"] = G.fetch_news()
print("news:", [a["headline"] for a in data["news"]][:3])
wx = S.weather(dict(CFG, lat=40.73, lon=-74.17))
import ical
trips = [ical.Trip(name="Miami · Navy at Florida Atlantic", start=now + 9 * 86400, end=now + 12 * 86400, segments=[
    ical.Segment(kind="flight", start=now + 9 * 86400, end=now + 9 * 86400 + 11000, title="UA 1234",
                 iata="UA", number="1234", origin="EWR", dest="MIA"),
    ical.Segment(kind="hotel", start=now + 9 * 86400 + 20000, end=now + 12 * 86400, title="Hotel",
                 name="The Betsy, South Beach"),
    ical.Segment(kind="flight", start=now + 12 * 86400, end=now + 12 * 86400 + 11000, title="UA 2211",
                 iata="UA", number="2211", origin="MIA", dest="EWR")])]

shots = []
for mode in G.MODES:
    cfg = dict(CFG, gallery_mode=mode)
    ctx = R.Ctx(now=now, cfg=cfg, teams=teams, weather=wx, trips=trips, gallery=data, page="quad")
    gray = R.compose_gray(ctx)
    p = os.path.join(out, f"e1001-gallery-{mode}.png")
    gray.save(p)
    shots.append(gray)
    print("wrote", p, gray.size, gray.getextrema())
sheet = Image.new("L", (3 * 810, 2 * 490), 128)
for i, s in enumerate(shots):
    sheet.paste(s, ((i % 3) * 810, (i // 3) * 490))
sheet.save(os.path.join(out, "e1001-gallery-preview.png"))
print("rotation now (real data):", G.available(data, teams, wx, trips, None, now), "->", G.pick_mode(data, teams, now, CFG, 0, wx, trips))
