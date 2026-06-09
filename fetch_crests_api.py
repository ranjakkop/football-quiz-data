#!/usr/bin/env python3
"""
fetch_crests_api.py — fill missing club crests from the football-data.org API.

Fetches team rosters for Premier League, La Liga, Serie A, Bundesliga,
Ligue 1, and Champions League; matches each team to the clubs table;
downloads the crest if the club has no crest_file yet; and reports coverage.

Usage:
    python fetch_crests_api.py

Requires config.py (gitignored) containing:
    FOOTBALL_DATA_TOKEN = "your-token-here"
"""

import os
import re
import sqlite3
import time
from difflib import SequenceMatcher

import requests

from config import FOOTBALL_DATA_TOKEN

DB_PATH = "football.db"
CREST_DIR = "crests"
API_BASE = "https://api.football-data.org/v4"
API_HEADERS = {"X-Auth-Token": FOOTBALL_DATA_TOKEN}
SLEEP = 0.5

COMPETITIONS = {
    "Premier League":   "PL",
    "La Liga":          "PD",
    "Serie A":          "SA",
    "Bundesliga":       "BL1",
    "Ligue 1":          "FL1",
    "Champions League": "CL",
}

# Known name variants: API name fragment (lowercase) -> DB name fragment (lowercase)
MANUAL = {
    "athletic club":            "athletic club",
    "atletico de madrid":       "atlético madrid",
    "club atletico de madrid":  "atlético madrid",
    "atletico madrid":          "atlético madrid",
    "fc barcelona":             "barcelona",
    "real madrid cf":           "real madrid",
    "manchester united":        "manchester united",
    "manchester city":          "manchester city",
    "newcastle united":         "newcastle united",
    "tottenham hotspur":        "tottenham hotspur",
    "internazionale":           "inter milan",
    "fc internazionale":        "inter milan",
    "ssc napoli":               "napoli",
    "as roma":                  "roma",
    "juventus":                 "juventus",
    "ac milan":                 "ac milan",
    "fc bayern":                "fc bayern munich",
    "borussia dortmund":        "borussia dortmund",
    "sevilla fc":               "sevilla",
    "valencia cf":              "valencia",
    "villarreal cf":            "villarreal",
}


def normalize(name):
    n = name.lower()
    n = re.sub(r'\b(f\.?c\.?|a\.?c\.?|a\.?s\.?|s\.?s\.?c\.?|c\.?f\.?|a\.?f\.?c\.?)\b', '', n)
    n = re.sub(r'[^\w\s]', ' ', n)
    n = re.sub(r'\s+', ' ', n).strip()
    return n


def similarity(a, b):
    return SequenceMatcher(None, normalize(a), normalize(b)).ratio()


def fetch_teams():
    seen = set()
    teams = []
    for comp_name, code in COMPETITIONS.items():
        resp = requests.get(f"{API_BASE}/competitions/{code}/teams",
                            headers=API_HEADERS, timeout=30)
        if resp.status_code != 200:
            print(f"  [{comp_name}] HTTP {resp.status_code} — skipping")
            continue
        for t in resp.json().get("teams", []):
            if t["id"] not in seen and t.get("crest"):
                seen.add(t["id"])
                teams.append({"name": t["name"], "crest_url": t["crest"]})
        print(f"  [{comp_name}] {len(resp.json().get('teams', []))} teams")
        time.sleep(SLEEP)
    return teams


def match_team(api_name, db_clubs):
    api_lower = api_name.lower()
    for pattern, fragment in MANUAL.items():
        if pattern in api_lower:
            for qid, db_name in db_clubs:
                if fragment in db_name.lower():
                    return qid, db_name
    best_score, best_club = 0, None
    for qid, db_name in db_clubs:
        score = similarity(api_name, db_name)
        if score > best_score:
            best_score, best_club = score, (qid, db_name)
    return best_club if best_score >= 0.72 else None


def main():
    os.makedirs(CREST_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)

    all_clubs = conn.execute("SELECT qid, name FROM clubs WHERE is_club=1").fetchall()
    needs_crest = {q for q, in conn.execute(
        "SELECT qid FROM clubs WHERE is_club=1 AND crest_file IS NULL")}

    print(f"Clubs in DB: {len(all_clubs)}  |  missing crests: {len(needs_crest)}")
    print(f"\nFetching teams from {len(COMPETITIONS)} competitions...")
    api_teams = fetch_teams()
    print(f"\n{len(api_teams)} unique teams with crests found\n")

    downloaded, skipped, no_match = 0, 0, []

    for team in api_teams:
        match = match_team(team["name"], all_clubs)
        if not match:
            no_match.append(team["name"])
            continue
        qid, db_name = match
        if qid not in needs_crest:
            skipped += 1
            continue
        url = team["crest_url"]
        ext = os.path.splitext(url)[1].split("?")[0] or ".svg"
        path = os.path.join(CREST_DIR, f"{qid}{ext}")
        try:
            with open(path, "wb") as f:
                f.write(requests.get(url, timeout=30).content)
            conn.execute("UPDATE clubs SET crest_url=?, crest_file=? WHERE qid=?",
                         (url, path, qid))
            conn.commit()
            needs_crest.discard(qid)
            downloaded += 1
            print(f"  + {db_name}")
        except Exception as e:
            print(f"  ! {db_name}: {e}")
        time.sleep(0.2)

    total = len(all_clubs)
    have = conn.execute(
        "SELECT COUNT(*) FROM clubs WHERE is_club=1 AND crest_file IS NOT NULL"
    ).fetchone()[0]

    print(f"\n{'='*50}")
    print(f"Total clubs:       {total}")
    print(f"Have crests:       {have}")
    print(f"Coverage:          {have / total * 100:.1f}%")
    print(f"Downloaded now:    {downloaded}")
    print(f"Already had crest: {skipped}")
    if no_match:
        print(f"Unmatched API teams ({len(no_match)}): {', '.join(no_match[:10])}"
              + (" ..." if len(no_match) > 10 else ""))

    print(f"\n--- Anchor clubs ---")
    missing_anchors = []
    for name, has_crest in conn.execute(
        "SELECT name, crest_file IS NOT NULL FROM clubs WHERE is_anchor=1 ORDER BY name"
    ).fetchall():
        mark = "OK" if has_crest else "MISSING"
        print(f"  [{mark}]  {name}")
        if not has_crest:
            missing_anchors.append(name)

    print()
    if missing_anchors:
        print(f"{len(missing_anchors)} anchor clubs still missing: {', '.join(missing_anchors)}")
    else:
        print("All 22 anchor clubs have crests.")

    conn.close()


if __name__ == "__main__":
    main()
