#!/usr/bin/env python3
"""DJB dashboard: a small private, action-first site with live Yahoo data.

Pages, all on the Pi's Tailscale address:
  /            Actions: what to do now, most urgent first
  /categories  Category ladder, plus what it takes to gain or keep each point
  /lineup      Today's lineup plan and the schedule this week and next
  /players     Available players, ranked for your team, with add/drop ideas
  /standings   Roto standings
  /activity    Recent league transactions

Yahoo rules: read-only API calls only, personal use for team DJB only. Data is
cached and refreshed at most every CACHE_MIN minutes, so the API load stays
light. Every move is a suggestion; make changes yourself in the Yahoo app.

    python dashboard.py --serve              # serve on DASH_HOST:DASH_PORT
    python dashboard.py --out page.html [/]  # write one page and exit
"""
import argparse
import datetime as dt
import html
import http.server
import logging
import math
import os
import statistics
import threading
import time
from zoneinfo import ZoneInfo

import requests
import yahoo_fantasy_api as yfa
from yahoo_oauth import OAuth2

from lineup_advisor import (BENCH, ESPN_SCOREBOARD, LEAGUE_ID, NON_STARTING,
                            OAUTH_FILE, OUT_STATUSES, TEAM_ID, plan_lineup,
                            team_code)

HOST = os.environ.get("DASH_HOST")  # default: the Tailscale address, looked up at start
PORT = int(os.environ.get("DASH_PORT", "8090"))
CACHE_MIN = 30         # normal refresh interval
MIN_REFRESH_MIN = 5    # floor for the manual "Refresh" link
TZ = ZoneInfo("America/Chicago")
ET = ZoneInfo("America/New_York")

# Yahoo stat ids for this league's 8 roto categories, in display order.
CATS = [("5", "FG%"), ("8", "FT%"), ("10", "3PTM"), ("12", "PTS"),
        ("15", "REB"), ("16", "AST"), ("17", "ST"), ("18", "BLK")]
CAT_NAMES = [name for _, name in CATS]
PCT = {"FG%": ("FGM/A", "9004003"), "FT%": ("FTM/A", "9007006")}
IL_SLOTS = {"IL", "IL+", "IR"}

# Thresholds in "player-games": one full day of starters is about 10.
CLOSE_GAMES = 20      # a point is "within reach" inside ~2 days of production
DEFEND_GAMES = 10     # a lead is "under threat" inside ~1 day of the chaser's production
SAFE_GAMES = 40

log = logging.getLogger("dashboard")
esc = html.escape


def tailscale_ip():
    """This machine's Tailscale IPv4, so the site is reachable only over Tailscale."""
    import subprocess
    out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, check=True)
    return out.stdout.split()[0]


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _ma(s):
    """'690/890' -> (690.0, 890.0); anything else -> (None, None)."""
    try:
        m, a = str(s).split("/")
        return float(m), float(a)
    except (ValueError, AttributeError):
        return None, None


# ------------------------------------------------------------------ ESPN
def espn_day(day):
    """{team_code: {opp, home, time, state, detail}} for NBA games on `day`."""
    r = requests.get(ESPN_SCOREBOARD, params={"dates": day.strftime("%Y%m%d")}, timeout=20)
    r.raise_for_status()
    out = {}
    for ev in r.json().get("events", []):
        st = ev.get("status", {}).get("type", {})
        comp = (ev.get("competitions") or [{}])[0]
        sides = comp.get("competitors", [])
        try:
            tip = dt.datetime.fromisoformat(ev["date"].replace("Z", "+00:00")).astimezone(TZ)
            tip_s = tip.strftime("%-I:%M %p")
        except (KeyError, ValueError):
            tip_s = ""
        for s in sides:
            code = team_code(s.get("team", {}).get("abbreviation"))
            other = next((o for o in sides if o is not s), {})
            if code:
                out[code] = {
                    "opp": team_code(other.get("team", {}).get("abbreviation")) or "?",
                    "home": s.get("homeAway") == "home",
                    "time": tip_s,
                    "state": st.get("state", "pre"),
                    "detail": st.get("shortDetail", ""),
                }
    return out


def schedule(today):
    """Games per team for the rest of this week (today..Sunday) and all of next week."""
    this_days = [today + dt.timedelta(d) for d in range(0, 7 - today.weekday())]
    next_mon = today + dt.timedelta(7 - today.weekday())
    next_days = [next_mon + dt.timedelta(d) for d in range(7)]
    this_wk, next_wk, per_day = {}, {}, {}
    for d in this_days + next_days:
        g = espn_day(d)
        per_day[d] = len(g) // 2
        target = this_wk if d in this_days else next_wk
        for c in g:
            target[c] = target.get(c, 0) + 1
    return this_wk, next_wk, per_day, next_mon


# ------------------------------------------------------------------ Yahoo (read-only)
def yahoo():
    sc = OAuth2(None, None, from_file=OAUTH_FILE, browser_callback=False)
    if not sc.token_is_valid():
        sc.refresh_access_token()
    gm = yfa.Game(sc, "nba")
    lk = f"{gm.game_id()}.l.{LEAGUE_ID}"
    lg = gm.to_league(lk)
    return lg, lg.to_team(f"{lk}.t.{TEAM_ID}"), lk


def standings_table(lg, lk):
    """Roto standings: each team's category totals, points, games played and makes/attempts."""
    j = lg.yhandler.get(f"league/{lk}/standings")
    teams = j["fantasy_content"]["league"][1]["standings"][0]["teams"]
    rows = []
    for k, v in teams.items():
        if k == "count":
            continue
        t = v["team"]
        meta = {}
        for part in t[0]:
            if isinstance(part, dict):
                meta.update(part)
        vals = {str(s["stat"]["stat_id"]): s["stat"]["value"] for s in t[1]["team_stats"]["stats"]}
        pts = {str(s["stat"]["stat_id"]): s["stat"]["value"] for s in t[1]["team_points"]["stats"]}
        rows.append({
            "key": meta.get("team_key", ""), "name": meta.get("name", "?"),
            "rank": int(t[2]["team_standings"].get("rank") or 0),
            "total": float(t[1]["team_points"].get("total") or 0),
            "vals": {name: _num(vals.get(sid)) for sid, name in CATS},
            "pts": {name: _num(pts.get(sid)) for sid, name in CATS},
            "gp": _num(vals.get("0")),
            "ma": {cat: _ma(vals.get(sid)) for cat, (_, sid) in PCT.items()},
        })
    rows.sort(key=lambda r: r["rank"] or 99)
    return rows


def headshots(players_obj):
    """{player_id: headshot URL} from a Yahoo 'players' JSON object.
    The URLs point at Yahoo's own image server; nothing is downloaded or stored."""
    out = {}
    for k, v in (players_obj or {}).items():
        if k == "count":
            continue
        info = {}
        for part in v["player"][0]:
            if isinstance(part, dict):
                info.update(part)
        url = (info.get("headshot") or {}).get("url") or info.get("image_url")
        if url and str(info.get("player_id", "")).isdigit():
            out[int(info["player_id"])] = url
    return out


def roster_headshots(lg, lk):
    j = lg.yhandler.get(f"team/{lk}.t.{TEAM_ID}/roster/players")
    return headshots(j["fantasy_content"]["team"][1]["roster"]["0"]["players"])


def available_players(lg, lk):
    """Top 25 available players by Yahoo's actual rank, with headshots: one API call."""
    j = lg.yhandler.get(f"league/{lk}/players;status=A;sort=AR;count=25/percent_owned")
    _, players = lg._players_from_page(j)
    return players, headshots(j["fantasy_content"]["league"][1]["players"])


def season_avgs(lg, ids):
    if not ids:
        return {}
    return {int(s["player_id"]): s for s in lg.player_stats(ids, "average_season")}


def recent_transactions(lg, count=15):
    """League adds, drops and trades, newest first, flattened for display."""
    out = []
    for tr in lg.transactions("add,drop,trade", count):
        moves = []
        for k, v in (tr.get("players") or {}).items():
            if k == "count":
                continue
            info = {}
            for part in v["player"][0]:
                if isinstance(part, dict):
                    info.update(part)
            td = v["player"][1].get("transaction_data")
            td = td[0] if isinstance(td, list) else (td or {})
            moves.append({
                "player": (info.get("name") or {}).get("full", "?"),
                "nba": info.get("editorial_team_abbr", ""),
                "type": td.get("type", ""),
                "team": td.get("destination_team_name") or td.get("source_team_name") or "",
                "team_key": td.get("destination_team_key") or td.get("source_team_key") or "",
            })
        out.append({"when": dt.datetime.fromtimestamp(int(tr.get("timestamp", 0)), TZ),
                    "type": tr.get("type", ""), "moves": moves})
    return out


# ------------------------------------------------------------------ analysis
def classify_moves(roster, moves):
    """Turn the lineup plan into moves, flagging which actually gain you something."""
    cur = {p["player_id"]: p.get("selected_position", BENCH) for p in roster}
    byid = {p["player_id"]: p for p in roster}
    changed = [m for m in moves if cur.get(m["player_id"]) != m["selected_position"]]
    gains = any(cur[m["player_id"]] in NON_STARTING and m["selected_position"] not in NON_STARTING
                for m in changed)
    out = []
    for m in changed:
        p, old, new = byid[m["player_id"]], cur[m["player_id"]], m["selected_position"]
        status = (p.get("status") or "").upper()
        if old in NON_STARTING and new not in NON_STARTING:
            kind, why = "do", "He has a game today; put him in the lineup"
        elif new == BENCH and status in OUT_STATUSES:
            kind, why = "do", f"Listed {status}; don't start him"
        elif new == BENCH:
            kind = "do" if gains else "optional"
            why = "No game today" + ("" if gains else ", and nobody with a game can take the spot, so this changes nothing")
        else:
            kind, why = "do", "Position swap that makes room for another starter"
        out.append({"pid": p["player_id"], "name": p["name"], "old": old, "new": new,
                    "kind": kind, "why": why})
    return out


def player_values(pool, avgs):
    """Per-game value of each player (sum of z-scores over the 8 categories).
    Percentages are weighted by attempts, so a 90% shooter on 1 attempt barely counts."""
    stats = {pid: avgs[pid] for pid in pool if pid in avgs}
    if len(stats) < 3:
        return {}, {}
    per_cat = {}
    for cat in CAT_NAMES:
        if cat in PCT:
            key = PCT[cat][0]
            made = sum((_ma(s.get(key))[0] or 0) for s in stats.values())
            att = sum((_ma(s.get(key))[1] or 0) for s in stats.values())
            base = made / att if att else 0
            raw = {}
            for pid, s in stats.items():
                m, a = _ma(s.get(key))
                raw[pid] = (m - base * a) if m is not None and a else 0.0
        else:
            raw = {pid: (_num(s.get(cat)) or 0.0) for pid, s in stats.items()}
        vals = list(raw.values())
        mu, sd = statistics.mean(vals), (statistics.pstdev(vals) or 1)
        per_cat[cat] = {pid: (v - mu) / sd for pid, v in raw.items()}
    value = {pid: sum(per_cat[c][pid] for c in CAT_NAMES) for pid in stats}
    return value, per_cat


def category_plans(rows, me, roster, avgs, per_cat, wk_this):
    """For each category: where you stand, what it takes to gain or keep a point,
    and which of your players matter most there."""
    plans = []
    my_gp = me["gp"] or 1
    active = [p for p in roster if p.get("selected_position") not in IL_SLOTS]
    for cat in CAT_NAMES:
        mine = me["vals"][cat]
        others = [r for r in rows if r is not me and r["vals"][cat] is not None]
        above = min((r for r in others if mine is not None and r["vals"][cat] > mine),
                    key=lambda r: r["vals"][cat], default=None)
        below = max((r for r in others if mine is not None and r["vals"][cat] < mine),
                    key=lambda r: r["vals"][cat], default=None)
        plan = {"cat": cat, "val": mine, "pts": me["pts"][cat], "above": above, "below": below,
                "all": [r["vals"][cat] for r in others], "status": "hold", "urgency": None,
                "gain": None, "defend": None, "gain_games": None, "defend_games": None}

        # Who on your roster drives this category (per game, active players only).
        lean = sorted(active, key=lambda p: per_cat.get(cat, {}).get(int(p["player_id"]), -9), reverse=True)
        plan["leaders"] = [p for p in lean[:3] if int(p["player_id"]) in avgs]
        plan["drags"] = []

        if cat in PCT:
            key = PCT[cat][0]
            m, a = me["ma"][cat]
            if m is not None and a:
                pct = m / a
                if above:
                    t = above["vals"][cat] + 0.0005
                    best = max((p for p in plan["leaders"]), default=None,
                               key=lambda p: _num(avgs[int(p["player_id"])].get(cat)) or 0)
                    r = _num(avgs[int(best["player_id"])].get(cat)) if best else None
                    plan["gain"] = f'You need +{gap_fmt(above["vals"][cat] - mine, cat)} to pass {above["name"]}'
                    if r and r > t:
                        att_needed = (t * a - m) / (r - t)
                        pa = _ma(avgs[int(best["player_id"])].get(key))[1] or 0
                        games = att_needed / pa if pa else None
                        plan["gain"] += f'. That takes about {att_needed:,.0f} attempts at {best["name"]}\'s {r:.1%}'
                        if games:
                            plan["gain"] += f', {games_txt(games, "from him")}'
                        plan["gain_games"] = games
                if below:
                    floor = below["vals"][cat] - 0.0005
                    drag_list = [p for p in active if int(p["player_id"]) in avgs
                                 and (_num(avgs[int(p["player_id"])].get(cat)) or 1) < pct
                                 and (_ma(avgs[int(p["player_id"])].get(key))[1] or 0) >= 2]
                    drag_list.sort(key=lambda p: per_cat.get(cat, {}).get(int(p["player_id"]), 0))
                    plan["drags"] = drag_list[:2]
                    if drag_list:
                        d = drag_list[0]
                        rd = _num(avgs[int(d["player_id"])].get(cat))
                        pa = _ma(avgs[int(d["player_id"])].get(key))[1] or 0
                        att_lose = (m - floor * a) / (floor - rd) if floor > rd else None
                        games = att_lose / pa if att_lose and pa else None
                        plan["defend_games"] = games
                        plan["defend"] = (f'You lead {below["name"]} by {gap_fmt(mine - below["vals"][cat], cat)}. '
                                          f'{d["name"]} shoots {rd:.1%} on {pa:.1f} attempts a game'
                                          + (f', so {games_txt(games, "from him")} would erase it' if games else "")
                                          + '. When you have a choice, sit him on nights you can spare his other stats')
                    else:
                        plan["defend"] = f'You lead {below["name"]} by {gap_fmt(mine - below["vals"][cat], cat)}'
        else:
            my_rate = (mine or 0) / my_gp
            if above:
                need = above["vals"][cat] - mine
                games = need / my_rate if my_rate else None
                their_rate = above["vals"][cat] / (above["gp"] or 1)
                pace = ("they average more per game, so you catch up by playing more games than them"
                        if their_rate > my_rate else "you average more per game, so you gain on them as games are played")
                plan["gain_games"] = games
                plan["gain"] = (f'You need +{need:,.0f} to pass {above["name"]}: {games_txt(games)} of your average '
                                f'({my_rate:.1f} {cat} per player-game). {pace[:1].upper() + pace[1:]}') if games else \
                               f'You need +{need:,.0f} to pass {above["name"]}'
            if below:
                lead = mine - below["vals"][cat]
                their_rate = below["vals"][cat] / (below["gp"] or 1)
                games = lead / their_rate if their_rate else None
                plan["defend_games"] = games
                plan["defend"] = (f'You lead {below["name"]} by {lead:,.0f}, {games_txt(games)} of their '
                                  f'production. ' + ("They average more per game, so your lead comes from playing more games. "
                                                     "Keep every active slot filled"
                                                     if their_rate > my_rate else "You out-produce them per game, so it should hold "
                                                     "if your slots stay filled"))

        g, d = plan["gain_games"], plan["defend_games"]
        # Whichever swing is closer decides what this category is about right now.
        if g is not None and g <= CLOSE_GAMES and (d is None or g <= d):
            plan["status"], plan["urgency"] = "gain", g
        elif d is not None and d <= DEFEND_GAMES:
            plan["status"], plan["urgency"] = "defend", d
        elif above is None and (d is None or d > SAFE_GAMES):
            plan["status"] = "safe"
        elif g is not None and g > 3 * CLOSE_GAMES and (d is None or d > SAFE_GAMES):
            plan["status"] = "far"
        plans.append(plan)
    return plans


def games_txt(g, whose=""):
    """'under a game', '1 game', '8 games', optionally 'of his'/'of their' style."""
    n = round(g)
    if g < 0.75:
        return "under a game" + (f" {whose}" if whose else "")
    word = "game" if n == 1 else "games"
    return f"about {n} {word}" + (f" {whose}" if whose else "")


def gap_fmt(v, cat):
    return f"{v:.3f}".lstrip("0") if cat in PCT else f"{v:,.0f}"


def waiver_ideas(roster, fa, avgs, value, per_cat, plans, wk_this):
    """Add/drop pairs worth considering. Every add needs a drop: the 3 bench spots
    are full and IL slots don't count. Players on IL are never suggested as drops."""
    targets = [p["cat"] for p in plans if p["status"] in ("gain", "defend")]
    weak = [p["cat"] for p in sorted(plans, key=lambda p: p["pts"] or 0)[:3]]
    focus = list(dict.fromkeys(targets + weak))
    droppable = [p for p in roster if p.get("selected_position") not in IL_SLOTS
                 and int(p["player_id"]) in value]
    if not droppable:
        return []
    droppable.sort(key=lambda p: value[int(p["player_id"])])
    ideas = []
    for drop in droppable[:2]:
        dv = value[int(drop["player_id"])]
        best = None
        for p in fa:
            pid = int(p["player_id"])
            if pid not in value or (p.get("status") or "").upper() in OUT_STATUSES:
                continue
            gain = value[pid] - dv
            focus_gain = sum(per_cat[c].get(pid, 0) - per_cat[c].get(int(drop["player_id"]), 0) for c in focus)
            score = gain + 0.5 * focus_gain
            if gain > 0.5 and (best is None or score > best[0]):
                best = (score, p, gain)
        if best and all(best[1]["player_id"] != i["add"]["player_id"] for i in ideas):
            add = best[1]
            pid, did = int(add["player_id"]), int(drop["player_id"])
            ups = [c for c in CAT_NAMES if per_cat[c].get(pid, 0) - per_cat[c].get(did, 0) > 0.4]
            downs = [c for c in CAT_NAMES if per_cat[c].get(pid, 0) - per_cat[c].get(did, 0) < -0.4]
            ideas.append({"add": add, "drop": drop, "gain": best[2], "ups": ups, "downs": downs,
                          "add_games": wk_this.get(team_code(add.get("editorial_team_abbr")), 0),
                          "drop_games": wk_this.get(team_code(drop.get("editorial_team_abbr")), 0)})
    return ideas


# ------------------------------------------------------------------ build (all data, one pass)
def build():
    today = dt.datetime.now(ET).date()
    lg, tm, lk = yahoo()
    roster = tm.roster(day=today)
    positions = lg.positions()
    rows = standings_table(lg, lk)
    me = next((r for r in rows if r["key"].endswith(f".t.{TEAM_ID}")), rows[0])

    games_today = espn_day(today)
    states = {c: g["state"] for c, g in games_today.items()}
    moves, _ = plan_lineup([dict(p) for p in roster], positions, states)
    plan_pos = {m["player_id"]: m["selected_position"] for m in moves}

    fa, shots = available_players(lg, lk)
    try:
        shots.update(roster_headshots(lg, lk))
    except Exception:  # headshots are a nicety; never fail the page over them
        log.warning("couldn't load roster headshots", exc_info=True)
    wk_this, wk_next, per_day, next_mon = schedule(today)

    ids = [int(p["player_id"]) for p in roster] + [int(p["player_id"]) for p in fa]
    avgs = season_avgs(lg, ids)
    value, per_cat = player_values(ids, avgs)
    plans = category_plans(rows, me, roster, avgs, per_cat, wk_this)
    try:
        activity = recent_transactions(lg)
    except Exception:
        log.warning("couldn't load transactions", exc_info=True)
        activity = []

    return {
        "today": today, "built": dt.datetime.now(TZ), "roster": roster, "positions": positions,
        "rows": rows, "me": me, "games": games_today, "plan_pos": plan_pos,
        "moves": classify_moves(roster, moves), "fa": fa, "shots": shots,
        "wk_this": wk_this, "wk_next": wk_next, "per_day": per_day, "next_mon": next_mon,
        "avgs": avgs, "value": value, "per_cat": per_cat, "plans": plans,
        "ideas": waiver_ideas(roster, fa, avgs, value, per_cat, plans, wk_this),
        "activity": activity,
    }


# ------------------------------------------------------------------ rendering helpers
def stat(v, cat):
    v = _num(v)  # Yahoo sends "-" when a player has no stats yet
    if v is None:
        return "–"
    return f"{v:.3f}".lstrip("0") if cat in PCT else f"{v:.1f}"


def total(v, cat):
    v = _num(v)
    if v is None:
        return "–"
    return f"{v:.3f}".lstrip("0") if cat in PCT else f"{v:,.0f}"


def badge(status):
    if not status:
        return ""
    kind = "out" if status in OUT_STATUSES else "q"
    return f'<span class="badge {kind}">{esc(status)}</span>'


def positions_of(p):
    return ", ".join(x for x in p.get("eligible_positions", []) if x not in NON_STARTING)


def player_cell(d, p, extra="", meta=None):
    url = d["shots"].get(int(p["player_id"]))
    img = (f'<img class="hs" src="{esc(url)}" alt="" width="32" height="40" loading="lazy" '
           f'referrerpolicy="no-referrer">' if url else '<span class="hs"></span>')
    meta = meta if meta is not None else f'{esc(p.get("editorial_team_abbr", ""))} · {esc(positions_of(p))}'
    return (f'<div class="pl">{img}<div><span class="name">{esc(p["name"])}</span>{extra}'
            f'<div class="sub">{meta}</div></div></div>')


def game_cell(d, code):
    g = d["games"].get(code)
    if not g:
        return '<span class="muted">No game</span>'
    vs = ("vs " if g["home"] else "@ ") + g["opp"]
    if g["state"] == "in":
        return f'<span class="live" title="Game in progress"></span>{esc(vs)} <span class="sec">{esc(g["detail"])}</span>'
    if g["state"] == "post":
        return f'{esc(vs)} <span class="muted">Final</span>'
    return f'{esc(vs)} <span class="sec">{esc(g["time"])}</span>'


def stat_heads():
    return "".join(f'<th class="num">{c}</th>' for c in CAT_NAMES)


def ladder_html(plans, nteams):
    out = []
    for c in plans:
        vals = [v for v in c["all"] + [c["val"]] if v is not None]
        lo, hi = (min(vals), max(vals)) if vals else (0, 1)
        span = (hi - lo) or 1

        def pos(v):
            return 2 + 96 * (v - lo) / span

        dots = "".join(f'<i style="left:{pos(v):.2f}%"></i>' for v in c["all"])
        me_dot = "" if c["val"] is None else f'<b style="left:{pos(c["val"]):.2f}%"></b>'
        pts = "–" if c["pts"] is None else f'{c["pts"]:g}'
        up = ("Leading the league" if c["above"] is None else
              f'+{gap_fmt(c["above"]["vals"][c["cat"]] - c["val"], c["cat"])} to pass {esc(c["above"]["name"])}')
        down = ("" if c["below"] is None else
                f'{gap_fmt(c["val"] - c["below"]["vals"][c["cat"]], c["cat"])} ahead of {esc(c["below"]["name"])}')
        out.append(
            f'<div class="rung"><div class="cat">{esc(c["cat"])}</div>'
            f'<div class="val">{total(c["val"], c["cat"])}</div>'
            f'<div class="track"><span class="lo">{total(lo, c["cat"])}</span>{dots}{me_dot}'
            f'<span class="hi">{total(hi, c["cat"])}</span></div>'
            f'<div class="pts">{pts}<span class="of">/{nteams}</span></div>'
            f'<div class="note">{up}<span class="muted">{down}</span></div></div>')
    return '<div class="ladder">' + "".join(out) + "</div>"


STATUS_LABEL = {"defend": "Protect", "gain": "Within reach", "safe": "Safe",
                "far": "Out of reach for now", "hold": "Hold"}


def names(players):
    return ", ".join(p["name"] for p in players)


# ------------------------------------------------------------------ pages
def page_actions(d):
    me, plans = d["me"], d["plans"]
    items = []  # (urgency, html)

    for m in d["moves"]:
        if m["kind"] == "do":
            items.append((0, "Lineup", f'Move <b>{esc(m["name"])}</b> {esc(m["old"])} → {esc(m["new"])}',
                          esc(m["why"])))

    for p in d["roster"]:
        st = (p.get("status") or "").upper()
        if not st or p.get("selected_position") in IL_SLOTS:
            continue
        code = team_code(p.get("editorial_team_abbr"))
        playing = code in d["games"]
        detail = ("Out, so keep him out of an active slot" if st in OUT_STATUSES
                  else "Game-time decision, so check news before tip-off" + ("" if playing else " (no game today)"))
        items.append((1 if st in OUT_STATUSES else 2, "Injury", f'<b>{esc(p["name"])}</b> is listed {esc(st)}', detail))

    # The closest category swings, gain or lose, ranked by how few games they are away.
    swings = sorted((c for c in plans if c["status"] in ("gain", "defend")), key=lambda c: c["urgency"])
    for c in swings[:4]:
        if c["status"] == "gain":
            help_ = f' Your best sources: {esc(names(c["leaders"]))}.' if c["leaders"] else ""
            items.append((1.5, "Gain", f'<b>{esc(c["cat"])}</b>: one more roto point is close ({c["pts"]:g}/12 now)',
                          esc(c["gain"]) + "." + help_))
        else:
            help_ = f' Lean on {esc(names(c["leaders"]))}.' if c["leaders"] and c["cat"] not in PCT else ""
            items.append((1.5, "Protect", f'<b>{esc(c["cat"])}</b>: your lead for {c["pts"]:g}/12 is thin',
                          esc(c["defend"]) + "." + help_))
    if len(swings) > 4:
        more = ", ".join(c["cat"] for c in swings[4:])
        items.append((3, "Also close", f'{esc(more)}', '<a href="/categories">See what each one takes</a>'))

    for i in d["ideas"][:1]:
        a, dr = i["add"], i["drop"]
        why = f'Better overall value (+{i["gain"]:.1f})'
        if i["ups"]:
            why += f', stronger in {", ".join(i["ups"])}'
        if i["downs"]:
            why += f', weaker in {", ".join(i["downs"])}'
        why += f'. {a["name"]} has {i["add_games"]} games left this week, {dr["name"]} has {i["drop_games"]}'
        items.append((4, "Waiver idea", f'Consider adding <b>{esc(a["name"])}</b> for <b>{esc(dr["name"])}</b>',
                      esc(why) + '. <a href="/players">See all options</a>'))

    rivals = {r["key"] for r in d["rows"] if r is not me and abs(r["rank"] - me["rank"]) <= 3}
    since = d["built"] - dt.timedelta(days=2)
    for t in d["activity"]:
        if t["when"] < since:
            continue
        rival_moves = [m for m in t["moves"] if m["team_key"] in rivals]
        if rival_moves:
            team = rival_moves[0]["team"]
            what = "; ".join(f'{m["type"]} {m["player"]} ({m["nba"]})' for m in rival_moves)
            items.append((5, "Rival", f'<b>{esc(team)}</b> made a move', esc(what[:1].upper() + what[1:])))

    items.sort(key=lambda x: x[0])
    if items:
        lst = "".join(
            f'<li class="u{int(u)}"><span class="kind">{esc(k)}</span><div><div class="what">{w}</div>'
            f'<div class="why">{why}</div></div></li>' for u, k, w, why in items)
        body = f'<ul class="actions">{lst}</ul>'
    else:
        body = '<p class="ok">Nothing needs your attention right now</p>'

    safe = [c["cat"] for c in plans if c["status"] == "safe"]
    far = [c["cat"] for c in plans if c["status"] == "far"]
    summary = []
    if safe:
        summary.append(f'Safe for now: {esc(", ".join(safe))}')
    if far:
        summary.append(f'Not worth chasing yet: {esc(", ".join(far))}')
    summary_html = f'<p class="note-p">{". ".join(summary)}</p>' if summary else ""

    return ("Actions", f'<section><h2>Do now</h2>{body}{summary_html}'
            '<p class="note-p">Suggestions only. The Yahoo API is read-only, so you make every change in the Yahoo app.</p></section>'
            f'<section><h2>Category ladder</h2>{ladder_html(plans, len(d["rows"]))}'
            '<p class="note-p">Each dot is a team\'s season total, and yours is orange. <a href="/categories">What it takes in each category</a></p></section>')


def page_categories(d):
    cards = []
    order = {"defend": 0, "gain": 1, "hold": 2, "far": 3, "safe": 4}
    for c in sorted(d["plans"], key=lambda c: order[c["status"]]):
        lines = []
        if c["gain"]:
            lines.append(f'<p><span class="lbl">To gain</span>{esc(c["gain"])}</p>')
        if c["defend"]:
            lines.append(f'<p><span class="lbl">To keep</span>{esc(c["defend"])}</p>')
        if c["leaders"]:
            lines.append(f'<p><span class="lbl">Best sources</span>{esc(names(c["leaders"]))}</p>')
        if c["drags"]:
            lines.append(f'<p><span class="lbl">Dragging it down</span>{esc(names(c["drags"]))}</p>')
        fa_help = sorted((p for p in d["fa"] if int(p["player_id"]) in d["per_cat"].get(c["cat"], {})),
                         key=lambda p: d["per_cat"][c["cat"]][int(p["player_id"])], reverse=True)[:3]
        if fa_help and c["status"] in ("gain", "defend", "hold"):
            lines.append(f'<p><span class="lbl">Available help</span>{esc(names(fa_help))}</p>')
        pts = "–" if c["pts"] is None else f'{c["pts"]:g}'
        cards.append(
            f'<div class="plan {c["status"]}"><div class="plan-h"><span class="cat">{esc(c["cat"])}</span>'
            f'<span class="tag">{STATUS_LABEL[c["status"]]}</span>'
            f'<span class="pts">{pts}<span class="of">/12</span></span></div>'
            + "".join(lines) + "</div>")
    return ("Categories", f'<section><h2>Category ladder</h2>{ladder_html(d["plans"], len(d["rows"]))}</section>'
            f'<section><h2>What it takes</h2><div class="plans">{"".join(cards)}</div>'
            '<p class="note-p">"Games" means player-games: one full day of starters is about 10. '
            'Percentages assume the other team stands still.</p></section>')


def page_lineup(d):
    slot_order = {s: i for i, s in enumerate(
        [s for s in d["positions"] if s not in NON_STARTING] + ["BN", "IL", "IL+", "IR", "NA"])}

    def sort_key(p):
        pos = d["plan_pos"].get(p["player_id"], p.get("selected_position", BENCH))
        return (slot_order.get(pos, 99), p["name"])

    rows = []
    for p in sorted(d["roster"], key=sort_key):
        pid = p["player_id"]
        code = team_code(p.get("editorial_team_abbr"))
        cur = p.get("selected_position", BENCH)
        new = d["plan_pos"].get(pid, cur)
        slot = (f'<span class="was">{esc(cur)}</span> → <span class="to">{esc(new)}</span>'
                if new != cur else esc(cur))
        a = d["avgs"].get(int(pid), {})
        rows.append(
            f'<tr class="{"chg" if new != cur else ""}"><td class="slot">{slot}</td>'
            f'<td>{player_cell(d, p, badge((p.get("status") or "").upper()))}</td>'
            f'<td>{game_cell(d, code)}</td><td class="num">{d["wk_this"].get(code, 0)}</td>'
            f'<td class="num">{d["wk_next"].get(code, 0)}</td>'
            + "".join(f'<td class="num">{stat(a.get(c), c)}</td>' for c in CAT_NAMES) + "</tr>")
    moves = "".join(
        f'<li class="{m["kind"]}"><span class="mv">{esc(m["name"])}</span>'
        f'<span class="slots">{esc(m["old"])} → {esc(m["new"])}</span><span class="why">{esc(m["why"])}</span></li>'
        for m in d["moves"])
    moves_html = f'<ul class="moves">{moves}</ul>' if moves else '<p class="ok">Your lineup already matches the best plan for today</p>'

    nm = d["next_mon"]
    days = "".join(
        f'<div class="day{" light" if n <= 5 else (" heavy" if n >= 10 else "")}"><span>{(nm + dt.timedelta(i)).strftime("%a")}</span>'
        f'<b>{n}</b></div>' for i, n in enumerate(d["per_day"].get(nm + dt.timedelta(i), 0) for i in range(7)))
    four = sorted((c for c, n in d["wk_next"].items() if n >= 4))
    return ("Lineup", f'<section><h2>Today\'s moves</h2>{moves_html}</section>'
            f'<section><h2>Lineup</h2><div class="scroll"><table><thead><tr><th>Slot</th><th>Player</th><th>Today</th>'
            f'<th class="num">This wk</th><th class="num">Next wk</th>{stat_heads()}</tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
            '<p class="note-p">Highlighted rows have a suggested change. Games are counted Monday to Sunday, from today for this week.</p></section>'
            f'<section><h2>Next week, {nm.strftime("%b %-d")} to {(nm + dt.timedelta(6)).strftime("%b %-d")}</h2>'
            f'<div class="days">{days}</div>'
            f'<p class="note-p">NBA games per night. Light nights are the easiest to fill. Teams with 4 games: {esc(", ".join(four)) or "none"}.</p></section>')


def page_players(d):
    ideas = []
    for i in d["ideas"]:
        a, dr = i["add"], i["drop"]
        a_meta = f'{esc(a.get("editorial_team_abbr", ""))} · {i["add_games"]} games left this week'
        d_meta = f'{esc(dr.get("editorial_team_abbr", ""))} · {i["drop_games"]} games left this week'
        ideas.append(
            f'<div class="idea"><div class="side"><span class="lbl">Add</span>{player_cell(d, a, meta=a_meta)}</div>'
            f'<div class="arrow">for</div>'
            f'<div class="side"><span class="lbl">Drop</span>{player_cell(d, dr, meta=d_meta)}</div>'
            f'<div class="gains">Value +{i["gain"]:.1f}'
            + (f' · better in {esc(", ".join(i["ups"]))}' if i["ups"] else "")
            + (f' · worse in {esc(", ".join(i["downs"]))}' if i["downs"] else "") + "</div></div>")
    ideas_html = "".join(ideas) or '<p class="ok">No available player beats your weakest active player right now</p>'

    weak = [c["cat"] for c in sorted(d["plans"], key=lambda c: c["pts"] or 0)[:3]]
    fa_rows = []
    for p in sorted(d["fa"], key=lambda p: d["value"].get(int(p["player_id"]), -99), reverse=True):
        pid = int(p["player_id"])
        a = d["avgs"].get(pid, {})
        helps = [c for c in weak if d["per_cat"].get(c, {}).get(pid, 0) > 0.5]
        tag = f'<span class="helps">Helps {esc(", ".join(helps))}</span>' if helps else ""
        code = team_code(p.get("editorial_team_abbr"))
        meta = (f'{esc(p.get("editorial_team_abbr", ""))} · {esc(positions_of(p))} · '
                f'{p.get("percent_owned", 0)}% owned')
        fa_rows.append(
            f'<tr><td>{player_cell(d, p, badge((p.get("status") or "").upper()) + tag, meta)}</td>'
            f'<td class="num">{d["value"].get(pid, 0):+.1f}</td><td class="num">{d["wk_this"].get(code, 0)}</td>'
            f'<td class="num">{d["wk_next"].get(code, 0)}</td>'
            + "".join(f'<td class="num">{stat(a.get(c), c)}</td>' for c in CAT_NAMES) + "</tr>")
    return ("Players", '<section><h2>Add and drop ideas</h2>'
            '<p class="note-p first">Your 3 bench spots are full, so every add needs a drop. Players on IL are never suggested as drops.</p>'
            f'<div class="ideas">{ideas_html}</div>'
            '<p class="note-p">Value compares per-game production across all 8 categories, with percentages weighted by attempts. Make any move yourself in the Yahoo app.</p></section>'
            f'<section><h2>Available players</h2><div class="scroll"><table><thead><tr><th>Player</th><th class="num">Value</th>'
            f'<th class="num">This wk</th><th class="num">Next wk</th>{stat_heads()}</tr></thead><tbody>{"".join(fa_rows)}</tbody></table></div>'
            '<p class="note-p">Yahoo\'s top 25 available players, sorted by value for your team. "Helps" marks players who are strong in your weakest categories.</p></section>')


def page_standings(d):
    me = d["me"]
    def pts(v):
        return "" if v is None else f"{v:g}"

    rows = "".join(
        f'<tr class="{"me" if r is me else ""}"><td class="num">{r["rank"]}</td><td>{esc(r["name"])}</td>'
        f'<td class="num strong">{r["total"]:g}</td>'
        + "".join(f'<td class="num">{pts(r["pts"][c])}</td>' for c in CAT_NAMES)
        + "</tr>" for r in d["rows"])
    totals = "".join(
        f'<tr class="{"me" if r is me else ""}"><td>{esc(r["name"])}</td><td class="num">{total(r["gp"], "GP")}</td>'
        + "".join(f'<td class="num">{total(r["vals"][c], c)}</td>' for c in CAT_NAMES) + "</tr>" for r in d["rows"])
    return ("Standings", f'<section><h2>Roto points</h2><div class="scroll"><table><thead><tr><th class="num">#</th><th>Team</th>'
            f'<th class="num">Points</th>{stat_heads()}</tr></thead><tbody>{rows}</tbody></table></div></section>'
            f'<section><h2>Season totals</h2><div class="scroll"><table><thead><tr><th>Team</th><th class="num">GP</th>{stat_heads()}</tr></thead>'
            f'<tbody>{totals}</tbody></table></div></section>')


def page_activity(d):
    me = d["me"]
    rivals = {r["key"] for r in d["rows"] if r is not me and abs(r["rank"] - me["rank"]) <= 3}
    items = []
    for t in d["activity"]:
        lines = "".join(
            f'<div><span class="tx {esc(m["type"])}">{esc(m["type"])}</span> {esc(m["player"])} '
            f'<span class="muted">{esc(m["nba"])}</span></div>' for m in t["moves"])
        team = next((m["team"] for m in t["moves"] if m["team"]), "")
        rival = '<span class="rival">Rival</span>' if any(m["team_key"] in rivals for m in t["moves"]) else ""
        items.append(
            f'<li><div class="when mono">{t["when"].strftime("%b %-d, %-I:%M %p")}</div>'
            f'<div class="team">{esc(team)}{rival}</div><div class="txs">{lines}</div></li>')
    body = f'<ul class="feed">{"".join(items)}</ul>' if items else '<p class="ok">No recent transactions</p>'
    return ("League activity", f'<section><h2>Recent transactions</h2>{body}'
            '<p class="note-p">Rivals are the teams within 3 places of you in the standings.</p></section>')


PAGES = {"/": page_actions, "/categories": page_categories, "/lineup": page_lineup,
         "/players": page_players, "/standings": page_standings, "/activity": page_activity}
NAV = [("/", "Actions"), ("/categories", "Categories"), ("/lineup", "Lineup"),
       ("/players", "Players"), ("/standings", "Standings"), ("/activity", "Activity")]


def render(d, path):
    title, body = PAGES[path](d)
    me = d["me"]
    rank = me["rank"]
    suffix = "th" if 10 <= rank % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(rank % 10, "th")
    nav = "".join(f'<a href="{href}"{" class=on" if href == path else ""}>{label}</a>' for href, label in NAV)
    return PAGE.format(
        title=esc(title), team=esc(me["name"]), today=esc(d["today"].strftime("%A, %B %-d")),
        now=esc(d["built"].strftime("%a %b %-d, %-I:%M %p")), place=f"{rank}{suffix}",
        nteams=len(d["rows"]), total=f'{me["total"]:g}', nav=nav, body=body, cache=CACHE_MIN,
        refresh=esc(path if path != "/" else ""))


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} · DJB</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Geist:wght@400;500;600&family=Geist+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root{{--bg:#fafafa;--card:#fff;--raised:#f4f4f5;--line:rgba(23,23,23,.08);--line2:rgba(23,23,23,.16);
--ink:#171717;--sec:#525252;--mut:#737373;--acc:#b4531a;--accsoft:rgba(180,83,26,.10);
--pos:#16a34a;--neg:#dc2626;--negsoft:rgba(220,38,38,.10);--dot:rgba(23,23,23,.28)}}
@media (prefers-color-scheme:dark){{:root{{--bg:#141415;--card:#19191b;--raised:#232325;--line:rgba(237,237,237,.08);
--line2:rgba(237,237,237,.16);--ink:#ededed;--sec:#a1a1a6;--mut:#85858b;--acc:#f0883e;--accsoft:rgba(240,136,62,.12);
--pos:#4cc38a;--neg:#e5484d;--negsoft:rgba(229,72,77,.14);--dot:rgba(237,237,237,.32)}}}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);font:14px/20px Geist,system-ui,-apple-system,"Segoe UI",sans-serif;-webkit-font-smoothing:antialiased}}
main{{max-width:1240px;margin:0 auto;padding:32px 24px 64px}}
.num,.val,.pts,.slot,.mono{{font-family:"Geist Mono",ui-monospace,monospace;font-variant-numeric:tabular-nums}}
.sec{{color:var(--sec)}}.muted{{color:var(--mut)}}
.sub{{color:var(--mut);font-size:12px;line-height:16px;margin-top:2px}}
a{{color:var(--acc);text-decoration:none;transition:color 150ms cubic-bezier(.22,1,.36,1)}}a:hover{{color:var(--ink)}}
b{{font-weight:500}}

header{{display:flex;flex-wrap:wrap;align-items:flex-end;justify-content:space-between;gap:16px;margin-bottom:20px}}
h1{{font-size:28px;line-height:32px;font-weight:500;margin:0}}
.meta{{color:var(--mut);font-size:13px;margin-top:4px}}
.standing{{text-align:right}}.standing .big{{font-family:"Geist Mono",monospace;font-size:28px;line-height:32px;font-weight:500;color:var(--acc)}}
nav{{display:flex;gap:4px;overflow-x:auto;margin:0 0 24px;padding-bottom:4px;border-bottom:1px solid var(--line)}}
nav a{{color:var(--sec);padding:8px 12px;border-radius:8px;white-space:nowrap;transition:background-color 150ms cubic-bezier(.22,1,.36,1),color 150ms cubic-bezier(.22,1,.36,1)}}
nav a:hover{{background:var(--raised);color:var(--ink)}}nav a.on{{color:var(--ink);background:var(--raised);box-shadow:inset 0 -2px 0 var(--acc)}}

section{{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:20px;margin-bottom:16px}}
h2{{font-size:15px;line-height:20px;font-weight:500;margin:0 0 16px}}
.note-p{{color:var(--mut);font-size:12px;line-height:16px;margin:12px 0 0}}.note-p.first{{margin:0 0 12px}}
.ok{{margin:0;padding:12px;border-radius:10px;background:var(--raised);color:var(--sec)}}
.ok::before{{content:"";display:inline-block;width:8px;height:8px;border-radius:4px;background:var(--pos);margin-right:8px}}

.actions{{list-style:none;margin:0;padding:0;display:grid;gap:8px}}
.actions li{{display:grid;grid-template-columns:104px 1fr;gap:12px;padding:12px;border-radius:10px;background:var(--raised)}}
.actions .kind{{font:500 12px/20px "Geist Mono",monospace;color:var(--sec)}}
.actions li.u0 .kind,.actions li.u1 .kind{{color:var(--acc)}}
.actions .what{{font-size:15px}}.actions .why{{color:var(--sec);font-size:13px;margin-top:4px}}
.actions li.u4,.actions li.u5{{background:transparent;border:1px solid var(--line)}}

.moves{{list-style:none;margin:0;padding:0;display:grid;gap:8px}}
.moves li{{display:grid;grid-template-columns:auto auto 1fr;align-items:baseline;gap:4px 12px;padding:12px;border-radius:10px;background:var(--raised)}}
.moves li.optional{{background:transparent;border:1px dashed var(--line2)}}
.moves .mv{{font-weight:500}}.moves .slots{{font-family:"Geist Mono",monospace;color:var(--acc)}}
.moves li.optional .slots{{color:var(--sec)}}.moves .why{{color:var(--mut);font-size:13px}}

.scroll{{overflow-x:auto;-webkit-overflow-scrolling:touch;margin:0 -20px;padding:0 20px}}
table{{border-collapse:collapse;width:100%}}
th,td{{padding:8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:middle;white-space:nowrap}}
th{{font-size:12px;line-height:16px;color:var(--mut);font-weight:400}}
tr:last-child td{{border-bottom:0}}td.num,th.num{{text-align:right}}td.num{{font-size:13px}}
.name{{font-weight:500}}.strong{{font-weight:500}}
.pl{{display:flex;align-items:center;gap:12px}}
.hs{{flex:none;width:32px;height:40px;border-radius:8px;background:var(--raised);object-fit:cover;object-position:top}}
td.slot{{color:var(--sec);font-size:13px}}td.slot .to{{color:var(--acc);font-weight:500}}td.slot .was{{color:var(--mut)}}
tr.chg td{{background:var(--accsoft)}}tr.me td{{background:var(--raised)}}tr.me td:nth-child(2),tr.me td:first-child{{font-weight:500}}
.badge{{display:inline-block;margin-left:8px;padding:0 4px;border-radius:4px;font:500 11px/16px "Geist Mono",monospace;vertical-align:1px}}
.badge.out{{background:var(--negsoft);color:var(--neg)}}.badge.q{{background:var(--raised);color:var(--sec);box-shadow:inset 0 0 0 1px var(--line2)}}
.helps{{margin-left:8px;font-size:12px;color:var(--pos)}}
.live{{display:inline-block;width:6px;height:6px;border-radius:3px;background:var(--pos);margin-right:8px;vertical-align:2px}}

.ladder{{display:grid;gap:4px}}
.rung{{display:grid;grid-template-columns:48px 64px 1fr 56px 232px;align-items:center;gap:16px;min-height:44px;padding:4px 0;border-bottom:1px solid var(--line)}}
.rung:last-child{{border-bottom:0}}.rung .cat{{font-weight:500}}
.rung .val{{text-align:right;color:var(--sec);font-size:13px}}
.track{{position:relative;height:28px;margin:0 32px}}
.track::before{{content:"";position:absolute;left:0;right:0;top:50%;height:1px;background:var(--line2)}}
.track i,.track b{{position:absolute;top:50%;border-radius:50%;transform:translate(-50%,-50%)}}
.track i{{width:8px;height:8px;background:var(--dot)}}
.track b{{width:14px;height:14px;background:var(--acc);box-shadow:0 0 0 3px var(--card);z-index:1}}
.track .lo,.track .hi{{position:absolute;top:50%;transform:translateY(-50%);font:11px/16px "Geist Mono",monospace;color:var(--mut)}}
.track .lo{{right:100%;margin-right:8px}}.track .hi{{left:100%;margin-left:8px}}
.rung .pts,.plan .pts{{text-align:right;font-size:18px;font-weight:500}}.of{{font-size:12px;color:var(--mut);font-weight:400}}
.rung .note{{font-size:12px;line-height:16px;color:var(--sec);display:flex;flex-direction:column}}

.plans{{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:12px}}
.plan{{border:1px solid var(--line);border-radius:12px;padding:16px}}
.plan.defend{{border-color:var(--acc)}}.plan.gain{{background:var(--raised)}}
.plan-h{{display:flex;align-items:center;gap:12px;margin-bottom:8px}}.plan-h .cat{{font-size:16px;font-weight:500}}
.plan-h .pts{{margin-left:auto}}
.tag{{font:500 11px/16px "Geist Mono",monospace;padding:2px 6px;border-radius:4px;background:var(--raised);color:var(--sec)}}
.plan.defend .tag{{background:var(--accsoft);color:var(--acc)}}.plan.gain .tag{{background:var(--card);color:var(--ink)}}
.plan p{{margin:8px 0 0;font-size:13px;color:var(--sec)}}.lbl{{display:block;font-size:12px;color:var(--mut)}}

.ideas{{display:grid;gap:8px}}
.idea{{display:grid;grid-template-columns:1fr auto 1fr;gap:8px 16px;align-items:center;padding:12px;border-radius:10px;background:var(--raised)}}
.idea .lbl{{margin-bottom:4px}}.idea .arrow{{color:var(--mut);font-size:13px}}
.idea .gains{{grid-column:1/-1;font-size:13px;color:var(--sec)}}

.days{{display:grid;grid-template-columns:repeat(7,1fr);gap:8px}}
.day{{border:1px solid var(--line);border-radius:10px;padding:8px 12px;display:flex;flex-direction:column}}
.day span{{font-size:12px;color:var(--mut)}}.day b{{font:500 20px/28px "Geist Mono",monospace}}
.day.light{{border-color:var(--pos)}}.day.heavy{{background:var(--raised)}}

.feed{{list-style:none;margin:0;padding:0}}
.feed li{{display:grid;grid-template-columns:160px 200px 1fr;gap:16px;padding:12px 0;border-bottom:1px solid var(--line)}}
.feed li:last-child{{border-bottom:0}}.feed .when{{color:var(--mut);font-size:12px}}.feed .team{{font-weight:500}}
.rival{{margin-left:8px;font:500 11px/16px "Geist Mono",monospace;padding:0 4px;border-radius:4px;background:var(--accsoft);color:var(--acc)}}
.tx{{display:inline-block;width:44px;font:500 11px/16px "Geist Mono",monospace;color:var(--mut)}}.tx.add{{color:var(--pos)}}.tx.drop{{color:var(--neg)}}

footer{{color:var(--mut);font-size:12px;line-height:16px;margin-top:24px}}

@media (max-width:760px){{
main{{padding:24px 16px 48px}}
h1{{font-size:22px;line-height:28px}}.standing{{text-align:left}}
section{{padding:16px}}.scroll{{margin:0 -16px;padding:0 16px}}
.actions li{{grid-template-columns:1fr;gap:4px}}
.moves li{{grid-template-columns:auto 1fr}}.moves .why{{grid-column:1/-1}}
.rung{{grid-template-columns:40px 1fr 48px;gap:4px 12px;padding:8px 0}}
.rung .val{{grid-column:1;grid-row:2;text-align:left}}
.rung .track{{grid-column:2;grid-row:1}}.rung .pts{{grid-column:3;grid-row:1}}
.rung .note{{grid-column:2/4;grid-row:2}}
.plans{{grid-template-columns:1fr}}
.idea{{grid-template-columns:1fr}}.idea .arrow{{display:none}}
.days{{grid-template-columns:repeat(4,1fr)}}
.feed li{{grid-template-columns:1fr;gap:4px}}
}}
</style></head><body><main>
<header><div><h1>{team} · {today}</h1>
<div class="meta">Updated {now} · <a href="/refresh{refresh}">Refresh</a></div></div>
<div class="standing"><div class="big">{place}</div><div class="meta">of {nteams} teams · {total} roto points</div></div></header>
<nav>{nav}</nav>
{body}
<footer>Private site on your Tailscale network. Data from Yahoo Fantasy (read-only, personal use) and ESPN's public schedule. Refreshes at most every {cache} minutes.</footer>
</main></body></html>"""


# ------------------------------------------------------------------ server
class Cache:
    def __init__(self):
        self.data, self.at, self.err, self.lock = None, 0.0, None, threading.Lock()

    def _stale(self, force):
        age = time.time() - self.at
        return self.data is None or age > CACHE_MIN * 60 or (force and age > MIN_REFRESH_MIN * 60)

    def get(self, force=False):
        if self._stale(force):
            with self.lock:
                if self._stale(force):  # another request may have just rebuilt it
                    try:
                        self.data, self.at, self.err = build(), time.time(), None
                    except Exception as e:  # keep serving the last good data
                        log.exception("build failed")
                        self.err, self.at = str(e), time.time()
        return self.data


CACHE = Cache()


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0]
        if path.startswith("/refresh"):
            CACHE.get(force=True)
            back = path[len("/refresh"):] or "/"
            self.send_response(303)
            self.send_header("Location", back if back in PAGES else "/")
            self.end_headers()
            return
        if path == "/index.html":
            path = "/"
        if path not in PAGES:
            self.send_error(404)
            return
        d = CACHE.get()
        if d is None:
            body = f"<p>Couldn't load data from Yahoo: {esc(CACHE.err or 'unknown error')}</p>"
        else:
            try:
                body = render(d, path)
            except Exception as e:
                log.exception("render failed for %s", path)
                body = f"<p>This page failed to render: {esc(str(e))}</p>"
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        log.info("%s %s", self.address_string(), fmt % args)


def main():
    ap = argparse.ArgumentParser(description="DJB dashboard (read-only)")
    ap.add_argument("--serve", action="store_true")
    ap.add_argument("--out", help="write one page to this file and exit")
    ap.add_argument("path", nargs="?", default="/", help="page for --out, e.g. /categories")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    for noisy in ("yahoo_oauth", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(render(build(), args.path))
        print("wrote", args.out)
        return
    host = HOST or tailscale_ip()
    srv = http.server.ThreadingHTTPServer((host, PORT), Handler)
    log.info("Serving on http://%s:%d", host, PORT)
    srv.serve_forever()


if __name__ == "__main__":
    main()
