"""
The Rookie Room weekly newsletter.

Pulls the league from ESPN, crunches the week, has an AI write it up,
and renders a static HTML page into docs/ for GitHub Pages.

Usage:
  python newsletter.py                      # latest completed week, live ESPN + AI
  python newsletter.py --week 3             # a specific week
  python newsletter.py --fixture fixtures/week3.json --copy fixtures/week3_copy.json
                                            # offline preview, no ESPN or AI calls
Env:
  AI_PROVIDER      openai (default) or anthropic
  OPENAI_API_KEY   / OPENAI_MODEL
  ANTHROPIC_API_KEY / ANTHROPIC_MODEL
"""

import argparse
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import requests
from jinja2 import Environment, FileSystemLoader, select_autoescape

LEAGUE_ID = 596742325
SEASON = 2026
LEAGUE_NAME = "The Rookie Room"

ROOT = Path(__file__).parent
DOCS = ROOT  # site is served from the repo root
ESPN_URL = (
    "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/"
    "{season}/segments/0/leagues/{league_id}"
)
BENCH_SLOTS = {20, 21}  # 20 = bench, 21 = IR


# ---------------------------------------------------------------- ESPN

def espn_get(params):
    url = ESPN_URL.format(season=SEASON, league_id=LEAGUE_ID)
    r = requests.get(url, params=params, timeout=30,
                     headers={"User-Agent": "rookie-room-newsletter"})
    r.raise_for_status()
    return r.json()


def fetch_league(week=None):
    """Return league JSON plus the boxscore JSON for the chosen week."""
    league = espn_get([("view", "mTeam"), ("view", "mSettings"),
                       ("view", "mMatchupScore")])
    if week is None:
        week = latest_completed_week(league)
    box = espn_get([("view", "mBoxscore"), ("view", "mMatchupScore"),
                    ("scoringPeriodId", week)])
    return league, box, week


def latest_completed_week(league):
    done = {}
    for m in league.get("schedule", []):
        wk = m["matchupPeriodId"]
        done.setdefault(wk, True)
        if m.get("winner", "UNDECIDED") == "UNDECIDED":
            done[wk] = False
    finished = [wk for wk, ok in done.items() if ok]
    if not finished:
        raise SystemExit("No completed weeks yet.")
    return max(finished)


# ---------------------------------------------------------------- Stats

def clean(s):
    return (s or "").strip()


def build_week(league, box, week):
    members = {m["id"]: m for m in league.get("members", [])}
    teams = {}
    for t in league["teams"]:
        owner = members.get(t.get("primaryOwner"), {})
        first = clean(owner.get("firstName")).title()
        rec = t["record"]["overall"]
        teams[t["id"]] = {
            "id": t["id"],
            "name": clean(t.get("name")) or f"Team {t['id']}",
            "abbrev": clean(t.get("abbrev")),
            "owner": first,
            "logo": t.get("logo"),
            "wins": rec["wins"], "losses": rec["losses"], "ties": rec["ties"],
            "pf": round(rec["pointsFor"], 2), "pa": round(rec["pointsAgainst"], 2),
            "streak": f"{rec['streakType'][0]}{rec['streakLength']}" if rec.get("streakLength") else "",
            "draft_rank": t.get("draftDayProjectedRank"),
            "acquisitions": t.get("transactionCounter", {}).get("acquisitions", 0),
            "lineup_moves": t.get("transactionCounter", {}).get("moveToActive", 0),
            "trades": t.get("transactionCounter", {}).get("trades", 0),
        }

    schedule = league.get("schedule") or box.get("schedule") or []
    # merge any richer boxscore entries (rosters) for this week
    box_by_id = {m["id"]: m for m in box.get("schedule", [])}

    # weekly scores for every completed week (for all-play records)
    weekly = {}
    for m in schedule:
        if m.get("winner", "UNDECIDED") == "UNDECIDED" or "away" not in m:
            continue
        wk = m["matchupPeriodId"]
        weekly.setdefault(wk, {})
        weekly[wk][m["home"]["teamId"]] = m["home"]["totalPoints"]
        weekly[wk][m["away"]["teamId"]] = m["away"]["totalPoints"]

    allplay = {tid: [0, 0] for tid in teams}
    for wk, scores in weekly.items():
        if wk > week:
            continue
        for tid, pts in scores.items():
            others = [p for o, p in scores.items() if o != tid]
            allplay[tid][0] += sum(pts > p for p in others)
            allplay[tid][1] += sum(pts < p for p in others)

    games = []
    for m in schedule:
        if m["matchupPeriodId"] != week or "away" not in m:
            continue
        m = box_by_id.get(m["id"], m)
        h, a = m["home"], m["away"]
        win, lose = (h, a) if h["totalPoints"] >= a["totalPoints"] else (a, h)
        games.append({
            "winner": teams[win["teamId"]], "loser": teams[lose["teamId"]],
            "winner_pts": round(win["totalPoints"], 2),
            "loser_pts": round(lose["totalPoints"], 2),
            "margin": round(win["totalPoints"] - lose["totalPoints"], 2),
            "winner_players": players(win), "loser_players": players(lose),
        })

    scores = weekly.get(week, {})
    week_scores = sorted(({"team": teams[t], "pts": round(p, 2)} for t, p in scores.items()),
                         key=lambda x: -x["pts"])
    for i, s in enumerate(week_scores):
        s["allplay_wins"] = len(week_scores) - 1 - i

    standings = sorted(teams.values(), key=lambda t: (-t["wins"], t["losses"], -t["pf"]))
    for i, t in enumerate(standings, 1):
        t["rank"] = i
        ap = allplay[t["id"]]
        t["allplay"] = f"{ap[0]}-{ap[1]}"
        t["allplay_pct"] = ap[0] / max(1, sum(ap))
        t["record_pct"] = t["wins"] / max(1, t["wins"] + t["losses"] + t["ties"])

    return {
        "league": LEAGUE_NAME, "season": SEASON, "week": week,
        "games": sorted(games, key=lambda g: g["margin"]),
        "week_scores": week_scores,
        "high": week_scores[0] if week_scores else None,
        "low": week_scores[-1] if week_scores else None,
        "closest": min(games, key=lambda g: g["margin"]) if games else None,
        "blowout": max(games, key=lambda g: g["margin"]) if games else None,
        "standings": standings,
        "playoff_teams": (league.get("settings") or {}).get("scheduleSettings", {}).get("playoffTeamCount"),
    }


def players(side):
    """Starters and bench from the boxscore roster, if ESPN included it."""
    roster = (side.get("rosterForCurrentScoringPeriod") or {}).get("entries", [])
    out = {"top": [], "dud": [], "bench_pts": 0.0, "bench_best": None}
    starters = []
    for e in roster:
        pe = e.get("playerPoolEntry", {})
        p = {"name": pe.get("player", {}).get("fullName", "?"),
             "pts": round(pe.get("appliedStatTotal", 0.0), 2)}
        if e.get("lineupSlotId") in BENCH_SLOTS:
            out["bench_pts"] += p["pts"]
            if not out["bench_best"] or p["pts"] > out["bench_best"]["pts"]:
                out["bench_best"] = p
        else:
            starters.append(p)
    starters.sort(key=lambda p: -p["pts"])
    out["top"] = starters[:2]
    out["dud"] = starters[-1:] if starters else []
    out["bench_pts"] = round(out["bench_pts"], 2)
    return out


# ---------------------------------------------------------------- AI

def load_lore():
    p = ROOT / "lore.md"
    return p.read_text() if p.exists() else ""


def facts_for_ai(data):
    """Plain, compact facts so the model doesn't invent anything."""
    lines = [f"{data['league']} | {data['season']} season | Week {data['week']}", "", "RESULTS:"]
    for i, g in enumerate(data["games"]):
        w, l = g["winner"], g["loser"]
        lines.append(f"[{i}] {w['name']} ({w['owner']}) {g['winner_pts']} def. "
                     f"{l['name']} ({l['owner']}) {g['loser_pts']}, margin {g['margin']}")
        for side, label in ((g["winner_players"], w["name"]), (g["loser_players"], l["name"])):
            if side["top"]:
                tops = ", ".join(f"{p['name']} {p['pts']}" for p in side["top"])
                dud = side["dud"][0] if side["dud"] else None
                lines.append(f"    {label}: top starters {tops}; worst starter "
                             f"{dud['name'] + ' ' + str(dud['pts']) if dud else 'n/a'}; "
                             f"bench total {side['bench_pts']}"
                             + (f", best bench {side['bench_best']['name']} {side['bench_best']['pts']}"
                                if side["bench_best"] else ""))
    lines += ["", "WEEK SCORES (high to low, with how many teams each would have beaten):"]
    for s in data["week_scores"]:
        lines.append(f"  {s['team']['name']}: {s['pts']} (would beat {s['allplay_wins']})")
    lines += ["", "STANDINGS:"]
    for t in data["standings"]:
        lines.append(f"  {t['rank']}. {t['name']} ({t['owner']}) {t['wins']}-{t['losses']}"
                     f"{'-' + str(t['ties']) if t['ties'] else ''}, PF {t['pf']}, PA {t['pa']}, "
                     f"streak {t['streak']}, all-play {t['allplay']}, "
                     f"draft-day projected rank {t['draft_rank']}, "
                     f"season pickups {t['acquisitions']}, trades {t['trades']}")
    return "\n".join(lines)


SYSTEM_PROMPT = """You write the weekly newsletter for a fantasy football league of friends.
Voice: a sharp, funny commissioner who knows everyone. Roast freely, but keep it the kind
of trash talk friends laugh at, not genuinely mean. Short punchy paragraphs.

Hard rules:
- Use ONLY the numbers and facts given. Never invent stats, injuries, players, or events.
- Never use em dashes. Use periods or commas instead.
- Refer to people by team name or owner first name.
- Use the league lore for inside jokes when it fits. Don't force every joke in.

Return JSON only, in exactly this shape:
{
  "headline": "short headline for the week",
  "intro": "2-3 sentence opener",
  "recaps": [{"game": <index from RESULTS>, "title": "short title", "body": "2-4 sentences"}],
  "awards": [{"name": "award name", "team": "team name", "blurb": "1-2 sentences"}],
  "power_take": "2-3 sentences on the standings picture",
  "signoff": "one line closer"
}
Write a recap for every game. Give 3 or 4 awards."""


def write_copy(data):
    user = (f"LEAGUE LORE:\n{load_lore() or '(none yet)'}\n\n"
            f"THIS WEEK'S DATA:\n{facts_for_ai(data)}")
    provider = (os.getenv("AI_PROVIDER") or "openai").lower()
    if provider == "anthropic":
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"],
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": os.getenv("ANTHROPIC_MODEL") or "claude-sonnet-5-5",
                  "max_tokens": 3000, "system": SYSTEM_PROMPT,
                  "messages": [{"role": "user", "content": user}]},
            timeout=120)
        r.raise_for_status()
        text = r.json()["content"][0]["text"]
    else:
        r = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
            json={"model": os.getenv("OPENAI_MODEL") or "gpt-5",
                  "response_format": {"type": "json_object"},
                  "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                               {"role": "user", "content": user}]},
            timeout=120)
        r.raise_for_status()
        text = r.json()["choices"][0]["message"]["content"]
    m = re.search(r"\{.*\}", text, re.S)
    copy = json.loads(m.group(0) if m else text)
    return scrub(copy)


def scrub(obj):
    """Belt and suspenders: strip any em dashes the model sneaks in."""
    if isinstance(obj, str):
        return obj.replace(" — ", ", ").replace("—", ", ").replace("–", "-")
    if isinstance(obj, list):
        return [scrub(x) for x in obj]
    if isinstance(obj, dict):
        return {k: scrub(v) for k, v in obj.items()}
    return obj


# ---------------------------------------------------------------- Render

def render(data, copy, fragment=False):
    env = Environment(loader=FileSystemLoader(ROOT),
                      autoescape=select_autoescape(["html", "j2"]))
    archive_path = DOCS / "archive.json"
    archive = json.loads(archive_path.read_text()) if archive_path.exists() else []
    slug = f"{data['season']}-week-{data['week']:02d}"
    entry = {"slug": slug, "week": data["week"], "season": data["season"],
             "headline": copy.get("headline", "")}
    archive = [a for a in archive if a["slug"] != slug] + [entry]
    archive.sort(key=lambda a: (a["season"], a["week"]), reverse=True)

    games = data["games"]
    recaps = []
    for r in copy.get("recaps", []):
        idx = r.get("game")
        if isinstance(idx, int) and 0 <= idx < len(games):
            recaps.append({**r, "g": games[idx]})

    ctx = dict(d=data, c=copy, recaps=recaps, archive=archive,
               generated=datetime.now(timezone.utc).strftime("%b %d, %Y"))
    tpl = env.get_template("newsletter.html.j2")
    if fragment:
        return tpl.render(**ctx, fragment=True, base="")

    DOCS.mkdir(exist_ok=True)
    (DOCS / "weeks").mkdir(exist_ok=True)
    (DOCS / "weeks" / f"{slug}.html").write_text(tpl.render(**ctx, fragment=False, base="../"))
    (DOCS / "index.html").write_text(tpl.render(**ctx, fragment=False, base=""))
    archive_path.write_text(json.dumps(archive, indent=2))
    (DOCS / ".nojekyll").touch()
    return slug


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--week", type=int)
    ap.add_argument("--fixture", help="offline league JSON (skips ESPN)")
    ap.add_argument("--copy", help="pre-written copy JSON (skips AI)")
    ap.add_argument("--fragment", help="also write a body-only HTML file here")
    args = ap.parse_args()

    if args.fixture:
        league = json.loads(Path(args.fixture).read_text())
        week = args.week or latest_completed_week(league)
        box = league
    else:
        league, box, week = fetch_league(args.week)

    data = build_week(league, box, week)
    copy = scrub(json.loads(Path(args.copy).read_text())) if args.copy else write_copy(data)
    slug = render(data, copy)
    if args.fragment:
        Path(args.fragment).write_text(render(data, copy, fragment=True))
    print(f"Published {slug}")


if __name__ == "__main__":
    main()
