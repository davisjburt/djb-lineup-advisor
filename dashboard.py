#!/usr/bin/env python3
"""DJB dashboard: a small private web page with live Yahoo data (read-only).

Serves one page on the Pi's Tailscale address. The page shows today's lineup
plan, roto category standings with gaps to the next team, the roster's
season averages and games left this week, and the top available players.

Yahoo rules: only read-only API calls, personal use for team DJB only. Data is
cached and refreshed at most every CACHE_MIN minutes, so the API load stays
light. Every move is a suggestion; make changes yourself in the Yahoo app.

    python dashboard.py --serve          # serve on DASH_HOST:DASH_PORT
    python dashboard.py --out page.html  # write one snapshot and exit
"""
import argparse
import datetime as dt
import html
import http.server
import logging
import os
import threading
import time
from zoneinfo import ZoneInfo

import requests
import yahoo_fantasy_api as yfa
from yahoo_oauth import OAuth2

from lineup_advisor import (BENCH, ESPN_SCOREBOARD, LEAGUE_ID, NON_STARTING,
                            OAUTH_FILE, OUT_STATUSES, TEAM_ID, plan_lineup,
                            team_code)

def tailscale_ip():
    """This machine's Tailscale IPv4, so the page is reachable only over Tailscale."""
    import subprocess
    out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, check=True)
    return out.stdout.split()[0]


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
PCT = {"FG%", "FT%"}

log = logging.getLogger("dashboard")
esc = html.escape


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


# ------------------------------------------------------------------ Yahoo
def yahoo():
    sc = OAuth2(None, None, from_file=OAUTH_FILE, browser_callback=False)
    if not sc.token_is_valid():
        sc.refresh_access_token()
    gm = yfa.Game(sc, "nba")
    lk = f"{gm.game_id()}.l.{LEAGUE_ID}"
    lg = gm.to_league(lk)
    return lg, lg.to_team(f"{lk}.t.{TEAM_ID}"), lk


def standings_table(lg, lk):
    """Roto standings with each team's category values and points."""
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
        })
    rows.sort(key=lambda r: r["rank"] or 99)
    return rows


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def available_players(lg, lk):
    """Top 25 available players by Yahoo's actual rank: one API call."""
    j = lg.yhandler.get(f"league/{lk}/players;status=A;sort=AR;count=25/percent_owned")
    _, players = lg._players_from_page(j)
    return players


def season_avgs(lg, ids):
    if not ids:
        return {}
    return {int(s["player_id"]): s for s in lg.player_stats(ids, "average_season")}


# ------------------------------------------------------------------ analysis
def category_view(rows, me):
    """Per category: my value and points, every team's value (for the ladder),
    and the gap to the next team up and down, with their names."""
    out = []
    for cat in CAT_NAMES:
        mine = me["vals"][cat]
        others = [(r["vals"][cat], r["name"]) for r in rows
                  if r is not me and r["vals"][cat] is not None]
        above = min(((v, n) for v, n in others if mine is not None and v > mine), default=None)
        below = max(((v, n) for v, n in others if mine is not None and v < mine), default=None)
        out.append({"cat": cat, "val": mine, "pts": me["pts"][cat],
                    "all": [v for v, _ in others],
                    "up": None if above is None else above[0] - mine,
                    "up_name": None if above is None else above[1],
                    "down": None if below is None else mine - below[0],
                    "down_name": None if below is None else below[1]})
    return out


def weak_cats(cview, n=3):
    scored = [c for c in cview if c["pts"] is not None]
    return [c["cat"] for c in sorted(scored, key=lambda c: c["pts"])[:n]]


def classify_moves(roster, moves, games):
    """Turn the plan into moves, flagging which actually gain you something."""
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
            kind, why = "do", "has a game; move him into the lineup"
        elif new == BENCH and status in OUT_STATUSES:
            kind, why = "do", f"status {status}; don't start him"
        elif new == BENCH:
            kind = "do" if gains else "optional"
            why = "no game today" + ("" if gains else "; nobody with a game can take the spot, so this changes nothing")
        else:
            kind, why = "do", "position swap that makes room for another starter"
        out.append({"name": p["name"], "old": old, "new": new, "kind": kind, "why": why})
    return out


def week_games(today, roster_codes):
    """Games left this week (today through Sunday) per team code."""
    days = [(today + dt.timedelta(d)) for d in range(0, 7 - today.weekday())]
    counts = {c: 0 for c in roster_codes}
    sched = {}
    for d in days:
        g = espn_day(d)
        sched[d] = g
        for c in g:
            counts[c] = counts.get(c, 0) + 1
    return counts, sched


# ------------------------------------------------------------------ build
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
    move_list = classify_moves(roster, moves, states)
    plan_pos = {m["player_id"]: m["selected_position"] for m in moves}

    codes = {team_code(p.get("editorial_team_abbr")) for p in roster} - {None}
    fa = available_players(lg, lk)
    codes |= {team_code(p.get("editorial_team_abbr")) for p in fa} - {None}
    wk_counts, _ = week_games(today, codes)

    avgs = season_avgs(lg, [int(p["player_id"]) for p in roster] + [int(p["player_id"]) for p in fa])
    cview = category_view(rows, me)
    return render(today, roster, games_today, plan_pos, move_list, rows, me, cview,
                  fa, avgs, wk_counts, positions)


def render(today, roster, games, plan_pos, move_list, rows, me, cview, fa, avgs, wk, positions):
    now = dt.datetime.now(TZ).strftime("%a %b %-d, %-I:%M %p")
    weak = weak_cats(cview)
    slot_order = {s: i for i, s in enumerate(
        [s for s in positions if s not in NON_STARTING] + ["BN", "IL", "IL+", "IR", "NA"])}

    def stat(v, cat):
        v = _num(v)  # Yahoo sends "-" when a player has no stats yet
        if v is None:
            return "–"
        if cat in PCT:
            return f"{v:.3f}".lstrip("0")
        return f"{v:.1f}"

    def total(v, cat):
        """Season totals: .464 for percentages, 3,708 for counts."""
        v = _num(v)
        if v is None:
            return "–"
        return f"{v:.3f}".lstrip("0") if cat in PCT else f"{v:,.0f}"

    def gap(v, cat):
        return f"{v:.3f}".lstrip("0") if cat in PCT else f"{v:,.0f}"

    def badge(status):
        if not status:
            return ""
        kind = "out" if status in OUT_STATUSES else "q"
        return f'<span class="badge {kind}">{esc(status)}</span>'

    def positions_of(p):
        return ", ".join(x for x in p.get("eligible_positions", []) if x not in NON_STARTING)

    def game_cell(code):
        g = games.get(code)
        if not g:
            return '<span class="muted">No game</span>'
        vs = ("vs " if g["home"] else "@ ") + g["opp"]
        if g["state"] == "in":
            return f'<span class="live" title="Game in progress"></span>{esc(vs)} <span class="sec">{esc(g["detail"])}</span>'
        if g["state"] == "post":
            return f'{esc(vs)} <span class="muted">Final</span>'
        return f'{esc(vs)} <span class="sec">{esc(g["time"])}</span>'

    # Today's moves
    if move_list:
        moves_html = '<ul class="moves">' + "".join(
            f'<li class="{m["kind"]}"><span class="mv">{esc(m["name"])}</span>'
            f'<span class="slots">{esc(m["old"])} → {esc(m["new"])}</span>'
            f'<span class="why">{esc(m["why"][:1].upper() + m["why"][1:])}</span></li>'
            for m in move_list) + "</ul>"
    else:
        moves_html = '<p class="ok">Your lineup already matches the best plan for today</p>'

    # Lineup
    def sort_key(p):
        pos = plan_pos.get(p["player_id"], p.get("selected_position", BENCH))
        return (slot_order.get(pos, 99), p["name"])

    lineup_rows = []
    for p in sorted(roster, key=sort_key):
        pid = p["player_id"]
        code = team_code(p.get("editorial_team_abbr"))
        cur = p.get("selected_position", BENCH)
        new = plan_pos.get(pid, cur)
        status = (p.get("status") or "").upper()
        slot = (f'<span class="was">{esc(cur)}</span> → <span class="to">{esc(new)}</span>'
                if new != cur else esc(cur))
        a = avgs.get(int(pid), {})
        lineup_rows.append(
            f'<tr class="{"chg" if new != cur else ""}"><td class="slot">{slot}</td>'
            f'<td><span class="name">{esc(p["name"])}</span>{badge(status)}'
            f'<div class="sub">{esc(p.get("editorial_team_abbr", ""))} · {esc(positions_of(p))}</div></td>'
            f'<td>{game_cell(code)}</td><td class="num">{wk.get(code, 0)}</td>'
            + "".join(f'<td class="num">{stat(a.get(c), c)}</td>' for c in CAT_NAMES) + "</tr>")
    stat_heads = "".join(f'<th class="num">{c}</th>' for c in CAT_NAMES)

    # Category ladder: every team's value on one track per category, DJB in the accent.
    ladder = []
    for c in cview:
        vals = [v for v in c["all"] + [c["val"]] if v is not None]
        lo, hi = (min(vals), max(vals)) if vals else (0, 1)
        span = (hi - lo) or 1

        def pos(v):
            return 2 + 96 * (v - lo) / span  # keep dots inside the track ends

        dots = "".join(f'<i style="left:{pos(v):.2f}%"></i>' for v in c["all"])
        me_dot = "" if c["val"] is None else f'<b style="left:{pos(c["val"]):.2f}%"></b>'
        pts = "–" if c["pts"] is None else f'{c["pts"]:g}'
        if c["up"] is None:
            note = "Leading the league"
        else:
            note = f'+{gap(c["up"], c["cat"])} to pass {esc(c["up_name"])}'
        lead = "" if c["down"] is None else f'{gap(c["down"], c["cat"])} ahead of {esc(c["down_name"])}'
        ladder.append(
            f'<div class="rung{" weak" if c["cat"] in weak else ""}">'
            f'<div class="cat">{esc(c["cat"])}</div>'
            f'<div class="val">{total(c["val"], c["cat"])}</div>'
            f'<div class="track"><span class="lo">{total(lo, c["cat"])}</span>{dots}{me_dot}'
            f'<span class="hi">{total(hi, c["cat"])}</span></div>'
            f'<div class="pts">{pts}<span class="of">/{len(rows)}</span></div>'
            f'<div class="note">{note}<span class="muted">{lead}</span></div></div>')

    stand_rows = "".join(
        f'<tr class="{"me" if r is me else ""}"><td class="num">{r["rank"]}</td><td>{esc(r["name"])}</td>'
        f'<td class="num strong">{r["total"]:g}</td>'
        + "".join(f'<td class="num">{"" if r["pts"][c] is None else f"{r["pts"][c]:g}"}</td>' for c in CAT_NAMES)
        + "</tr>" for r in rows)

    # Available players, tagged when they beat the roster average in a weak category.
    roster_avg = {}
    pool = [avgs[int(p["player_id"])] for p in roster if int(p["player_id"]) in avgs]
    for c in CAT_NAMES:
        vals = [float(a[c]) for a in pool if isinstance(a.get(c), (int, float))]
        roster_avg[c] = sum(vals) / len(vals) if vals else None
    fa_rows = []
    for p in fa:
        a = avgs.get(int(p["player_id"]), {})
        helps = [c for c in weak if isinstance(a.get(c), (int, float)) and roster_avg.get(c) is not None
                 and float(a[c]) > roster_avg[c]]
        code = team_code(p.get("editorial_team_abbr"))
        status = (p.get("status") or "").upper()
        tag = f'<span class="helps">Helps {esc(", ".join(helps))}</span>' if helps else ""
        fa_rows.append(
            f'<tr><td><span class="name">{esc(p["name"])}</span>{badge(status)}{tag}'
            f'<div class="sub">{esc(p.get("editorial_team_abbr", ""))} · {esc(positions_of(p))} · '
            f'{p.get("percent_owned", 0)}% owned</div></td><td class="num">{wk.get(code, 0)}</td>'
            + "".join(f'<td class="num">{stat(a.get(c), c)}</td>' for c in CAT_NAMES) + "</tr>")

    rank = me["rank"]
    suffix = "th" if 10 <= rank % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(rank % 10, "th")
    return PAGE.format(
        now=esc(now), today=esc(today.strftime("%A, %B %-d")), team=esc(me["name"]),
        place=f"{rank}{suffix}", nteams=len(rows), total=f'{me["total"]:g}',
        weak=esc(", ".join(weak)), moves=moves_html, lineup="".join(lineup_rows),
        stat_heads=stat_heads, ladder="".join(ladder), standings=stand_rows,
        fa="".join(fa_rows), cache=CACHE_MIN)


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DJB Dashboard</title>
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

header{{display:flex;flex-wrap:wrap;align-items:flex-end;justify-content:space-between;gap:16px;margin-bottom:32px}}
h1{{font-size:28px;line-height:32px;font-weight:500;margin:0}}
.meta{{color:var(--mut);font-size:13px;margin-top:4px}}
.standing{{text-align:right}}.standing .big{{font-family:"Geist Mono",monospace;font-size:28px;line-height:32px;font-weight:500;color:var(--acc)}}
.standing .meta{{margin-top:4px}}

section{{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:20px;margin-bottom:16px}}
h2{{font-size:15px;line-height:20px;font-weight:500;margin:0 0 16px}}
.note-p{{color:var(--mut);font-size:12px;line-height:16px;margin:12px 0 0}}

.moves{{list-style:none;margin:0;padding:0;display:grid;gap:8px}}
.moves li{{display:grid;grid-template-columns:auto auto 1fr;align-items:baseline;gap:4px 12px;padding:12px;border-radius:10px;background:var(--raised)}}
.moves li.optional{{background:transparent;border:1px dashed var(--line2)}}
.moves .mv{{font-weight:500}}.moves .slots{{font-family:"Geist Mono",monospace;color:var(--acc)}}
.moves li.optional .slots{{color:var(--sec)}}.moves .why{{color:var(--mut);font-size:13px}}
.ok{{margin:0;padding:12px;border-radius:10px;background:var(--raised);color:var(--sec)}}
.ok::before{{content:"";display:inline-block;width:8px;height:8px;border-radius:4px;background:var(--pos);margin-right:8px}}

.scroll{{overflow-x:auto;-webkit-overflow-scrolling:touch;margin:0 -20px;padding:0 20px}}
table{{border-collapse:collapse;width:100%}}
th,td{{padding:8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top;white-space:nowrap}}
th{{font-size:12px;line-height:16px;color:var(--mut);font-weight:400}}
tr:last-child td{{border-bottom:0}}
td.num,th.num{{text-align:right}}td.num{{font-size:13px}}
.name{{font-weight:500}}
td.slot{{color:var(--sec);font-size:13px}}td.slot .to{{color:var(--acc);font-weight:500}}td.slot .was{{color:var(--mut)}}
tr.chg td{{background:var(--accsoft)}}
tr.me td{{background:var(--raised)}}tr.me td:nth-child(2){{font-weight:500;color:var(--acc)}}
.strong{{font-weight:500}}
.badge{{display:inline-block;margin-left:8px;padding:0 4px;border-radius:4px;font:500 11px/16px "Geist Mono",monospace;vertical-align:1px}}
.badge.out{{background:var(--negsoft);color:var(--neg)}}.badge.q{{background:var(--raised);color:var(--sec);box-shadow:inset 0 0 0 1px var(--line2)}}
.helps{{margin-left:8px;font-size:12px;color:var(--pos)}}
.live{{display:inline-block;width:6px;height:6px;border-radius:3px;background:var(--pos);margin-right:8px;vertical-align:2px}}

.ladder{{display:grid;gap:4px}}
.rung{{display:grid;grid-template-columns:48px 64px 1fr 56px 232px;align-items:center;gap:16px;min-height:44px;padding:4px 0;border-bottom:1px solid var(--line)}}
.rung:last-child{{border-bottom:0}}
.rung .cat{{font-weight:500}}.rung.weak .pts{{color:var(--sec)}}
.rung .val{{text-align:right;color:var(--sec);font-size:13px}}
.track{{position:relative;height:28px;margin:0 32px}}
.track::before{{content:"";position:absolute;left:0;right:0;top:50%;height:1px;background:var(--line2)}}
.track i,.track b{{position:absolute;top:50%;border-radius:50%;transform:translate(-50%,-50%)}}
.track i{{width:8px;height:8px;background:var(--dot)}}
.track b{{width:14px;height:14px;background:var(--acc);box-shadow:0 0 0 3px var(--card);z-index:1}}
.track .lo,.track .hi{{position:absolute;top:50%;transform:translateY(-50%);font:11px/16px "Geist Mono",monospace;color:var(--mut)}}
.track .lo{{right:100%;margin-right:8px}}.track .hi{{left:100%;margin-left:8px}}
.rung .pts{{text-align:right;font-size:18px;font-weight:500}}.rung .of{{font-size:12px;color:var(--mut);font-weight:400}}
.rung .note{{font-size:12px;line-height:16px;color:var(--sec);display:flex;flex-direction:column}}

footer{{color:var(--mut);font-size:12px;line-height:16px;margin-top:24px}}

@media (max-width:760px){{
main{{padding:24px 16px 48px}}
h1{{font-size:22px;line-height:28px}}.standing{{text-align:left}}
section{{padding:16px}}.scroll{{margin:0 -16px;padding:0 16px}}
.moves li{{grid-template-columns:auto 1fr}}.moves .why{{grid-column:1/-1}}
.rung{{grid-template-columns:40px 1fr 48px;gap:4px 12px;padding:8px 0}}
.rung .val{{grid-column:1;grid-row:2;text-align:left}}
.rung .track{{grid-column:2;grid-row:1}}.rung .pts{{grid-column:3;grid-row:1}}
.rung .note{{grid-column:2/4;grid-row:2}}
}}
</style></head><body><main>
<header><div><h1>{team} · {today}</h1>
<div class="meta">Updated {now} · <a href="/refresh">Refresh</a></div></div>
<div class="standing"><div class="big">{place}</div><div class="meta">of {nteams} teams · {total} roto points</div></div></header>

<section><h2>Today's moves</h2>{moves}
<p class="note-p">Suggestions only. The Yahoo API is read-only, so make any change yourself in the Yahoo app, or tap Start Active Players.</p></section>

<section><h2>Category ladder</h2><div class="ladder">{ladder}</div>
<p class="note-p">Each dot is a team's season total, and yours is orange. Points are roto points out of 12. Weakest right now: {weak}.</p></section>

<section><h2>Lineup</h2><div class="scroll"><table><thead><tr><th>Slot</th><th>Player</th><th>Today</th><th class="num">Games left</th>{stat_heads}</tr></thead>
<tbody>{lineup}</tbody></table></div>
<p class="note-p">Highlighted rows have a suggested change. Stats are season averages per game, and games left run from today through Sunday.</p></section>

<section><h2>Standings</h2><div class="scroll"><table><thead><tr><th class="num">#</th><th>Team</th><th class="num">Points</th>{stat_heads}</tr></thead>
<tbody>{standings}</tbody></table></div></section>

<section><h2>Available players</h2><div class="scroll"><table><thead><tr><th>Player</th><th class="num">Games left</th>{stat_heads}</tr></thead>
<tbody>{fa}</tbody></table></div>
<p class="note-p">Yahoo's top 25 available players by rank. Players who beat your roster's average in a weak category are marked. Any add or drop is yours to make in the Yahoo app.</p></section>

<footer>Private page on your Tailscale network. Data from Yahoo Fantasy (read-only, personal use) and ESPN's public schedule. Refreshes at most every {cache} minutes.</footer>
</main></body></html>"""


# ------------------------------------------------------------------ server
class Cache:
    def __init__(self):
        self.html, self.at, self.err, self.lock = None, 0.0, None, threading.Lock()

    def _stale(self, force):
        age = time.time() - self.at
        return self.html is None or age > CACHE_MIN * 60 or (force and age > MIN_REFRESH_MIN * 60)

    def get(self, force=False):
        if self._stale(force):
            with self.lock:
                if self._stale(force):  # another request may have just rebuilt it
                    try:
                        self.html, self.at, self.err = build(), time.time(), None
                    except Exception as e:  # keep serving the last good page
                        log.exception("build failed")
                        self.err, self.at = str(e), time.time()
        if self.html is None:
            return f"<p>Couldn't load data from Yahoo: {esc(self.err or 'unknown error')}</p>"
        return self.html


CACHE = Cache()


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/refresh":
            CACHE.get(force=True)
            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()
            return
        if self.path not in ("/", "/index.html"):
            self.send_error(404)
            return
        body = CACHE.get().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        log.info("%s %s", self.address_string(), fmt % args)


def main():
    ap = argparse.ArgumentParser(description="DJB dashboard (read-only)")
    ap.add_argument("--serve", action="store_true")
    ap.add_argument("--out", help="write one snapshot to this file and exit")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    for noisy in ("yahoo_oauth", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(build())
        print("wrote", args.out)
        return
    host = HOST or tailscale_ip()
    srv = http.server.ThreadingHTTPServer((host, PORT), Handler)
    log.info("Serving on http://%s:%d", host, PORT)
    srv.serve_forever()


if __name__ == "__main__":
    main()
