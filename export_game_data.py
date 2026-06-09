#!/usr/bin/env python3
"""
Exports the dataset from football.db into players.js, which the local game
page (index.html) reads. Run this whenever you rebuild the database.

  python export_game_data.py

Produces players.js with:  window.GAME_PLAYERS = [ {name, aliases, clubs:[{name,crest}]} ]
Only clubs (is_club=1) with a known year are included, ordered chronologically.
Only players with >= 2 such clubs are kept (so the path question makes sense).
"""

import json
import sqlite3

conn = sqlite3.connect("football.db")
conn.row_factory = sqlite3.Row

players = {}

# Ordered club stints per player (clubs only, dated), chronological.
rows = conn.execute("""
    SELECT p.qid, p.display_name, p.nationality, p.position,
           c.name AS club, c.crest_file, s.start_year, s.end_year, s.apps
    FROM players p
    JOIN stints s ON s.player_qid = p.qid
    JOIN clubs  c ON c.qid = s.club_qid AND c.is_club = 1
    WHERE s.start_year IS NOT NULL
    ORDER BY p.qid, s.start_year
""").fetchall()

for r in rows:
    pl = players.setdefault(r["qid"], {
        "name": r["display_name"],
        "nationality": r["nationality"],
        "position": r["position"],
        "aliases": [],
        "clubs": [],
    })
    pl["clubs"].append({
        "name": r["club"],
        "crest": r["crest_file"],          # e.g. "crests/Q9617.svg" or None
        "start": r["start_year"],
        "end": r["end_year"],
        "apps": r["apps"],
    })

# Aliases (for the fuzzy answer matcher).
for r in conn.execute("SELECT player_qid, alias FROM player_aliases"):
    if r[0] in players:
        players[r[0]]["aliases"].append(r[1])

conn.close()

# Keep players with at least 2 clubs.
data = [v for v in players.values() if len(v["clubs"]) >= 2]

with open("players.js", "w", encoding="utf-8") as f:
    f.write("window.GAME_PLAYERS = " + json.dumps(data, ensure_ascii=False) + ";")

print(f"Exported {len(data)} players to players.js")
