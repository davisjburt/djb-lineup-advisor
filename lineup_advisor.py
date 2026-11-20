#!/usr/bin/env python3
"""DJB lineup advisor (read-only).

Yahoo's API is read-only, so this never changes your lineup. It works out
the ideal lineup for today and pushes you a note when your current Yahoo
lineup differs (someone OUT is starting, a player with a game is benched,
etc.). Fix those in the app, or tap "Start Active Players" there.

Usage:
    python lineup_advisor.py                    # check today, notify if needed
    python lineup_advisor.py --print            # print the plan, never notify
    python lineup_advisor.py --date 2026-11-20  # check a specific day
"""
import argparse
import datetime as dt
import hashlib
import logging
import os
import sys
from zoneinfo import ZoneInfo

import requests
import yahoo_fantasy_api as yfa
from yahoo_oauth import OAuth2

# ---------------------------------------------------------------- settings
HERE = os.path.dirname(os.path.abspath(__file__))
OAUTH_FILE = os.environ.get("YAHOO_OAUTH_FILE", os.path.join(HERE, "oauth2.json"))
LEAGUE_ID = os.environ.get("YAHOO_LEAGUE_ID", "81497")
TEAM_ID = os.environ.get("YAHOO_TEAM_ID", "9")
NOTIFY_URL = os.environ.get("NOTIFY_URL", "")  # ntfy topic URL, set in .env
STATE_FILE = os.path.join(HERE, ".last_alert")

# Yahoo injury statuses that mean "don't start". GTD/DTD players still start.
OUT_STATUSES = {"O", "INJ", "SUSP", "NA"}
START_QUESTIONABLE = True  # set False to bench GTD/DTD players too

# Optional tiebreak when more players have games than starting slots.
PRIORITY = []  # e.g. ["Luka Doncic", "Bam Adebayo"]

BENCH = "BN"
NON_STARTING = {"BN", "IL", "IL+", "IR", "NA"}

ESPN_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"

TEAM_ALIASES = {
    "GS": "GSW", "GSW": "GSW", "NY": "NYK", "NYK": "NYK",
    "SA": "SAS", "SAS": "SAS", "NO": "NOP", "NOP": "NOP", "NOR": "NOP",
    "PHO": "PHX", "PHX": "PHX", "UTAH": "UTA", "UTA": "UTA",
    "WSH": "WAS", "WAS": "WAS", "BKN": "BKN", "BRK": "BKN", "BRO": "BKN",
    "CHA": "CHA", "CHO": "CHA", "ATL": "ATL", "BOS": "BOS", "CHI": "CHI",
    "CLE": "CLE", "DAL": "DAL", "DEN": "DEN", "DET": "DET", "HOU": "HOU",
    "IND": "IND", "LAC": "LAC", "LAL": "LAL", "MEM": "MEM", "MIA": "MIA",
    "MIL": "MIL", "MIN": "MIN", "OKC": "OKC", "ORL": "ORL", "PHI": "PHI",
    "POR": "POR", "SAC": "SAC", "TOR": "TOR",
}

log = logging.getLogger("lineup")


def team_code(abbr):
    return TEAM_ALIASES.get((abbr or "").strip().upper())


# ---------------------------------------------------------------- NBA games
def games_on(day):
    """Return {team_code: state} for NBA games on `day`; state is pre/in/post."""
    r = requests.get(ESPN_SCOREBOARD, params={"dates": day.strftime("%Y%m%d")}, timeout=20)
    r.raise_for_status()
    teams = {}
    for event in r.json().get("events", []):
        state = event.get("status", {}).get("type", {}).get("state", "pre")
        for comp in event.get("competitions", []):
            for side in comp.get("competitors", []):
                abbr = side.get("team", {}).get("abbreviation")
                code = team_code(abbr)
                if code:
                    teams[code] = state
                else:
                    log.warning("Unknown ESPN team code %r; add it to TEAM_ALIASES", abbr)
    return teams


# ---------------------------------------------------------------- lineup logic
def expand_eligible(positions):
    """Add the flex slots a player can fill (G, F, Util)."""
    elig = set(positions)
    if elig & {"PG", "SG"}:
        elig.add("G")
    if elig & {"SF", "PF"}:
        elig.add("F")
    if elig & {"PG", "SG", "SF", "PF", "C", "G", "F"}:
        elig.add("Util")
    return elig - NON_STARTING


def plan_lineup(roster, slot_counts, games):
    """Work out the new lineup.

    roster: list of dicts from yahoo_fantasy_api Team.roster()
    slot_counts: {position: count} from League.positions()
    games: {team_code: state} from games_on()
    Returns (moves, report): moves is [{player_id, selected_position}] for
    every player we're allowed to move; report is a list of lines.
    """
    report = []
    open_slots = []  # one entry per starting slot, e.g. ["PG", "SG", "G", ...]
    for pos, info in slot_counts.items():
        if pos not in NON_STARTING:
            open_slots += [pos] * int(info.get("count", 0))

    movable, candidates = [], []
    for p in roster:
        current = p.get("selected_position", BENCH)
        code = team_code(p.get("editorial_team_abbr"))
        state = games.get(code) if code else None
        why = None
        if current in ("IL", "IL+", "IR"):
            why = "on IL, left alone"
        elif code is None:
            why = f"unknown team code {p.get('editorial_team_abbr')!r}, left alone"
        elif state in ("in", "post"):
            why = "game already started, locked"
        if why:
            if current in open_slots:
                open_slots.remove(current)  # a locked starter keeps its slot
            report.append(f"  {p['name']}: {current} ({why})")
            continue

        movable.append(p)
        status = (p.get("status") or "").upper()
        if state is None:
            p["_why"] = "no game"
        elif status in OUT_STATUSES or (not START_QUESTIONABLE and status in {"GTD", "DTD"}):
            p["_why"] = f"status {status}"
        else:
            candidates.append(p)

    # Priority: PRIORITY list, then players already starting, then roster order.
    def rank(item):
        i, p = item
        name_rank = PRIORITY.index(p["name"]) if p["name"] in PRIORITY else len(PRIORITY)
        starting = p.get("selected_position") not in NON_STARTING
        return (name_rank, 0 if starting else 1, i)

    ordered = [p for _, p in sorted(enumerate(candidates), key=rank)]

    # Assign players to slots in priority order. Each new player may bump an
    # already-placed player into another slot they qualify for (augmenting
    # path), so position quirks never cost you a start.
    slot_owner = [None] * len(open_slots)
    elig = {p["player_id"]: expand_eligible(p.get("eligible_positions", [])) for p in ordered}

    current_pos = {p["player_id"]: p.get("selected_position") for p in ordered}

    def place(pid, seen):
        # Prefer the player's current slot, then any empty slot, and only
        # then bump someone else, so the lineup doesn't churn.
        order = sorted(range(len(open_slots)), key=lambda s: (
            open_slots[s] != current_pos[pid], slot_owner[s] is not None))
        for s in order:
            if open_slots[s] in elig[pid] and s not in seen:
                seen.add(s)
                if slot_owner[s] is None or place(slot_owner[s], seen):
                    slot_owner[s] = pid
                    return True
        return False

    for p in ordered:
        if not place(p["player_id"], set()):
            p["_why"] = "no open slot"

    new_pos = {pid: open_slots[s] for s, pid in enumerate(slot_owner) if pid is not None}
    moves = []
    for p in movable:
        pos = new_pos.get(p["player_id"], BENCH)
        moves.append({"player_id": p["player_id"], "selected_position": pos})
        old = p.get("selected_position", BENCH)
        note = "" if pos != BENCH else f" ({p.get('_why', 'bench')})"
        arrow = f"{old} -> {pos}" if old != pos else pos
        report.append(f"  {p['name']}: {arrow}{note}")
    return moves, report


# ---------------------------------------------------------------- notify
def notify(text, title="DJB lineup"):
    """POST plain text to an ntfy topic URL. No-op when NOTIFY_URL is empty."""
    if not NOTIFY_URL:
        return
    try:
        requests.post(NOTIFY_URL, data=text.encode("utf-8"),
                      headers={"Title": title}, timeout=10)
    except requests.RequestException as e:
        log.warning("Notification failed: %s", e)


def already_sent(day, text):
    """True if this exact alert already went out today (skip repeat pings)."""
    key = f"{day} {hashlib.sha1(text.encode()).hexdigest()}"
    try:
        with open(STATE_FILE) as f:
            if f.read().strip() == key:
                return True
    except OSError:
        pass
    with open(STATE_FILE, "w") as f:
        f.write(key)
    return False


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--print", action="store_true", help="print the plan, never notify")
    ap.add_argument("--date", help="YYYY-MM-DD (default: today, US Eastern)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    day = (dt.date.fromisoformat(args.date) if args.date
           else dt.datetime.now(ZoneInfo("America/New_York")).date())

    try:
        sc = OAuth2(None, None, from_file=OAUTH_FILE, browser_callback=False)
        if not sc.token_is_valid():
            sc.refresh_access_token()
        gm = yfa.Game(sc, "nba")
        league_key = f"{gm.game_id()}.l.{LEAGUE_ID}"
        lg = gm.to_league(league_key)
        tm = lg.to_team(f"{league_key}.t.{TEAM_ID}")

        games = games_on(day)
        if not games:
            print(f"No NBA games found for {day}; nothing to check.")
            return 0
        roster = tm.roster(day=day)
        moves, report = plan_lineup(roster, lg.positions(), games)
    except Exception as e:
        log.error("Check failed: %s", e)
        if not args.print:
            notify(f"Lineup check failed for {day}: {e}", title="DJB lineup: error")
        return 1

    current = {p["player_id"]: p.get("selected_position") for p in roster}
    names = {p["player_id"]: p["name"] for p in roster}
    changed = [m for m in moves if current.get(m["player_id"]) != m["selected_position"]]
    changed_names = {names[m["player_id"]] for m in changed}

    header = f"{day}: {len(games) // 2} NBA games, {len(changed)} suggested changes"
    print("\n".join([header] + report))

    if args.print:
        return 0
    if not changed:
        log.info("Lineup already matches the plan; no alert.")
        return 0

    lines = [l for l in report if l.strip().split(":")[0] in changed_names]
    alert = "\n".join([header] + lines + ["", "Fix in the Yahoo app (or tap Start Active Players)."])
    if already_sent(day, alert):
        log.info("Same alert already sent today; skipping.")
        return 0
    notify(alert)
    return 0


if __name__ == "__main__":
    sys.exit(main())
