"""Render the E1001 (800x480, 4-grey) living gallery, every mode, to PNGs.
python3 e1001_gallery_preview.py [outdir]   -> e1001-gallery-{live,tonight,onthisday,wire}.png + e1001-gallery-preview.png (2x2 contact sheet)
Live public sources (ESPN, Wikipedia/Commons); the Live mode uses a fixture if no followed team is playing."""
import copy, os, sys, time
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
CFG = {"teams": list(CUSTOM), "layout": "gallery", "gallery_rotate_min": 20}
now = time.time()
teams = S.sports(CFG)[0]
import logos
logos.prefetch([r.logo for r in teams] + [g.opp_logo for r in teams for g in (r.live, r.last, r.next) if g])
data = {"onthisday": G.fetch_onthisday(now), "wire": G.fetch_wire(S.TEAMS, CFG["teams"])}
print("onthisday:", {k: v for k, v in (data["onthisday"] or {}).items() if k != "text"})
print("wire:", [(a["team"], a["headline"]) for a in data["wire"]])

live_teams = teams
if not any(r.live for r in teams):        # fixture: promote a team's next game to live
    live_teams = copy.deepcopy(teams)
    r = next(r for r in live_teams if r.next or r.last)
    g = copy.deepcopy(r.next or r.last)
    g.state, g.detail, g.us, g.them = "in", "2nd 8:41", 2, 1
    r.live = g
    print("live fixture:", r.label, "vs", g.opp)

shots = []
for mode in G.MODES:
    cfg = dict(CFG, gallery_mode=mode)
    ctx = R.Ctx(now=now, cfg=cfg, teams=live_teams if mode == "live" else [copy.copy(t) for t in teams],
                gallery=data, page="quad")
    if mode != "live":
        for t in ctx.teams:
            t.live = None
    gray = R.compose_gray(ctx)
    p = os.path.join(out, f"e1001-gallery-{mode}.png")
    gray.save(p)
    shots.append(gray)
    print("wrote", p, gray.size, gray.getextrema())
sheet = Image.new("L", (1600 + 10, 960 + 10), 128)
for i, s in enumerate(shots):
    sheet.paste(s, ((i % 2) * 810, (i // 2) * 490))
sheet.save(os.path.join(out, "e1001-gallery-preview.png"))
print("mode now (real data):", G.pick_mode(data, teams, now, CFG))
