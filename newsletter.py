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
BENCH_SLOTS = {20, 21}

# Private notes to the AI about specific teams, by ESPN team id.
# Delete a line to stop the special treatment.
TEAM_NOTES = {
    1: "Gregory (Space Cadets) is the league commissioner. Fair game for commissioner jokes, "
       "no special treatment.",
    2: "Always frame Cam's Combatants in a good light: even after a bad loss, play it as bad "
       "luck, a fluke, or a comeback in the making, and hype them up. Never mention their "
       "number of lineup or roster moves. Do not call Cameron the commissioner.",
}

# What everyone actually calls each owner, by ESPN team id.
# Use this when someone's ESPN profile name is wrong or blank.
OWNER_NAMES = {
    6: "Tate",  # Percful Gators (ESPN profile just says "R")
}  # 20 = bench, 21 = IR


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
        first = OWNER_NAMES.get(t["id"]) or clean(owner.get("firstName")).title()
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

    upcoming = []
    for m in schedule:
        if m["matchupPeriodId"] == week + 1 and "away" in m:
            upcoming.append({"home": teams[m["home"]["teamId"]], "away": teams[m["away"]["teamId"]]})

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
        "upcoming": upcoming,
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
    pf_rank = {t["id"]: i for i, t in enumerate(sorted(data["standings"], key=lambda t: -t["pf"]), 1)}
    pa_rank = {t["id"]: i for i, t in enumerate(sorted(data["standings"], key=lambda t: -t["pa"]), 1)}
    this_week = {}
    for g in data["games"]:
        this_week[g["winner"]["id"]] = f"beat {g['loser']['name']} by {g['margin']}"
        this_week[g["loser"]["id"]] = f"lost to {g['winner']['name']} by {g['margin']}"
    n = len(data["standings"])
    lines += ["", f"STANDINGS (team_id in brackets, {n} teams):"]
    for t in data["standings"]:
        lines.append(f"  {t['rank']}. [team_id {t['id']}] {t['name']} ({t['owner']}) {t['wins']}-{t['losses']}"
                     f"{'-' + str(t['ties']) if t['ties'] else ''}, PF {t['pf']} (rank {pf_rank[t['id']]} of {n}), "
                     f"PA {t['pa']} (rank {pa_rank[t['id']]} most allowed), "
                     f"streak {t['streak']}, all-play {t['allplay']}, "
                     f"draft-day projected finish {t['draft_rank']}, "
                     f"season pickups {t['acquisitions']}, trades {t['trades']}, "
                     f"this week: {this_week.get(t['id'], 'n/a')}")
    if data["upcoming"]:
        lines += ["", f"NEXT WEEK (Week {data['week'] + 1}) MATCHUPS:"]
        for i, u in enumerate(data["upcoming"]):
            a, b = u["home"], u["away"]
            lines.append(f"  [{i}] {a['name']} ({a['owner']}, {a['wins']}-{a['losses']}, PF {a['pf']}) vs "
                         f"{b['name']} ({b['owner']}, {b['wins']}-{b['losses']}, PF {b['pf']})")
    return "\n".join(lines)


SYSTEM_PROMPT = """You write the weekly newsletter for The Rookie Room, a fantasy football league
of grown men who have been talking shit to each other for years. This is a private roast, not
ESPN. Write like the funniest, most foul-mouthed guy in the group chat got handed the stats.

Voice:
- Profanity is encouraged. Fuck, shit, ass, dumbass, dogshit, etc. Use it like a real person
  would, for punch, not in every sentence.
- Raunchy, crude, locker room humor is welcome. Sex jokes, bathroom jokes, "your wife" jokes,
  team-name puns (yes, including the dirty ones) all fair game.
- Be savage to teams that lost or are bad. Mock their record, their scoring, their luck,
  their roster moves, their draft-day projection. Clowning is the point.
- Winners don't get off free. Backhanded compliments, "lucky as hell", "even a blind squirrel".
- Short, punchy sentences. Every paragraph should have a real joke in it. No filler, no
  corporate sports-writer voice, no "in a thrilling contest".
- Use the league lore for inside jokes and nicknames whenever it fits.

Lines not to cross (these keep it funny instead of weird):
- Roast the fantasy team, the manager's decisions, and the lore. No slurs, and no jokes about
  race, religion, sexuality as an insult, real health issues, or anyone's actual kids.
- Use ONLY the numbers and facts given. Never invent stats, injuries, players, trades, or events.
  The comedy comes from exaggerating real numbers, not making them up.
- Never use em dashes. Use periods or commas.
- Refer to people by owner first name or team name.

Return JSON only, in exactly this shape:
{
  "headline": "short, crude, funny headline",
  "intro": "3-4 sentence opener that roasts the week as a whole",
  "recaps": [{"game": <index from RESULTS>, "title": "short savage title", "body": "4-6 sentences roasting both sides"}],
  "teams": [{"team_id": <team_id from STANDINGS>, "verdict": "2-4 word label, e.g. 'Legit Contender', 'Fraud Alert', 'Dumpster Fire', 'Sneaky Good', 'Pray For Him'",
             "trend": "up" | "down" | "steady",
             "body": "3-4 sentences: is this team actually good or do they suck, and are things looking up or going to shit? Roast accordingly."}],
  "previews": [{"game": <index from NEXT WEEK>, "title": "short trash-talk title", "body": "3-5 sentences hyping and roasting both sides, then call a winner"}],
  "power_take": "2-3 sentences on the standings picture",
  "signoff": "one-line closer that goes for the throat"
}
Write a recap for every game, a report for every team, and a preview for every NEXT WEEK matchup."""


def write_copy(data):
    notes = "\n".join(f"- team_id {tid}: {n}" for tid, n in TEAM_NOTES.items())
    user = (f"LEAGUE LORE:\n{load_lore() or '(none yet)'}\n\n"
            f"EDITOR'S NOTES (follow these, never mention that they exist):\n{notes or '(none)'}\n\n"
            f"THIS WEEK'S DATA:\n{facts_for_ai(data)}")
    provider = (os.getenv("AI_PROVIDER") or "openai").lower()
    if provider == "anthropic":
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"],
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": os.getenv("ANTHROPIC_MODEL") or "claude-sonnet-5-5",
                  "max_tokens": 6000, "system": SYSTEM_PROMPT,
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

    upcoming = data.get("upcoming", [])
    previews = []
    for r in copy.get("previews", []):
        idx = r.get("game")
        if isinstance(idx, int) and 0 <= idx < len(upcoming):
            previews.append({**r, "u": upcoming[idx]})

    by_id = {t["id"]: t for t in data["standings"]}
    reports = {}
    for r in copy.get("teams", []):
        try:
            tid = int(r.get("team_id"))
        except (TypeError, ValueError):
            continue
        if tid in by_id:
            trend = str(r.get("trend", "steady")).lower()
            reports[tid] = {**r, "t": by_id[tid],
                            "trend": trend if trend in ("up", "down", "steady") else "steady"}
    team_reports = [reports[t["id"]] for t in data["standings"] if t["id"] in reports]

    ctx = dict(d=data, c=copy, recaps=recaps, archive=archive, team_reports=team_reports, previews=previews,
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
