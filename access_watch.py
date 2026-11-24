#!/usr/bin/env python3
"""Ping ntfy once when the Yahoo Fantasy API starts answering for this app.

Run hourly by fantasy-access-watch.timer. Makes one read-only call to
/fantasy/v2/game/nba. On HTTP 200 it pushes "access is live" to NOTIFY_URL
and writes .access_live, after which every later run exits immediately.
403 "not authorized" (still waiting) is logged only, with no push.
"""
import logging
import os
import sys

import requests
from yahoo_oauth import OAuth2

HERE = os.path.dirname(os.path.abspath(__file__))
OAUTH_FILE = os.path.join(HERE, "oauth2.json")
MARKER = os.path.join(HERE, ".access_live")
NOTIFY_URL = os.environ.get("NOTIFY_URL", "")
PROBE = "https://fantasysports.yahooapis.com/fantasy/v2/game/nba"


def push(text, title):
    if not NOTIFY_URL:
        print("NOTIFY_URL not set; would have sent:", text)
        return
    requests.post(NOTIFY_URL, data=text.encode(), headers={"Title": title}, timeout=10)


def main():
    if os.path.exists(MARKER):
        return 0
    logging.disable(logging.CRITICAL)  # yahoo_oauth is noisy at DEBUG
    try:
        sc = OAuth2(None, None, from_file=OAUTH_FILE, browser_callback=False)
        if not sc.token_is_valid():
            sc.refresh_access_token()
        r = sc.session.get(PROBE, params={"format": "json"}, timeout=20)
    except Exception as e:  # network blip or token problem: try again next hour
        print(f"probe error: {e}")
        return 0

    if r.status_code == 200:
        push("Yahoo Fantasy API access is live for djb-lineup-2. "
             "Ask Claude to run the lineup test and turn the timer back on.",
             "DJB lineup: API access live")
        with open(MARKER, "w") as f:
            f.write("live\n")
        print("probe 200: access live, notified")
    else:
        print(f"probe {r.status_code}: still waiting")
    return 0


if __name__ == "__main__":
    sys.exit(main())
