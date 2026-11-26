# DJB Lineup Advisor

A small, private, **read-only** helper for my own Yahoo Fantasy Basketball team.
Personal, single-league, non-commercial use, 1 user.

## What it does
Twice a day, a script on my home Raspberry Pi:

1. Reads my own team's roster for the day from the Yahoo Fantasy Sports API (read-only, `fspt-r`).
   It needs each player's selected position, eligible positions and injury status, plus my league's roster slot settings.
2. Checks that day's NBA schedule (ESPN's public scoreboard).
3. Works out the ideal lineup. If my current lineup differs from it (an OUT/injured player is starting, or a player with a game is on the bench), it sends me a private push notification through ntfy.

I make any lineup changes myself in the Yahoo app. The script never writes to Yahoo.

## What it doesn't do
- No write access: it never changes a lineup.
- No other users, teams or leagues.
- It stores nothing beyond the current run, except a one-line "already alerted today" marker.
- Nothing is shared, published or resold.

## Files
- `lineup_advisor.py`: the advisor (`--print` to preview, `--date YYYY-MM-DD` to check a given day).
- `install.sh`: installs it on a Raspberry Pi with a venv and a systemd timer (09:00 and 16:30 America/Chicago).
- `dashboard.py`: a small private site, reachable only over Tailscale. Pages: Actions (the most urgent things to do: lineup fixes, injuries, the closest category points to gain or protect, a waiver idea, rival moves), Categories (what it takes to gain or keep each roto point), Lineup (today plus this week's and next week's schedule), Players (available players ranked for your team, with add and drop pairs), Standings, and league Activity. It makes read-only calls, refreshes at most every 30 minutes, and lists every move as a suggestion.
- `install_dashboard.sh`: installs `dashboard.py` as a systemd service on the Pi.
- `access_watch.py`: a temporary hourly check that makes one read-only Yahoo call and sends a single ntfy push when API access goes live (HTTP 200), then stays quiet.

Credentials (`oauth2.json`) and the notification topic (`.env`) stay on the device and are never committed.
