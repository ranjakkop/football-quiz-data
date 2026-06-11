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
           c.name AS club, c.crest_file, s.start_year, s.end_year, s.apps,
           s.is_loan
    FROM players p
    JOIN stints s ON s.player_qid = p.qid
    JOIN clubs  c ON c.qid = s.club_qid AND c.is_club = 1
    WHERE s.start_year IS NOT NULL
    ORDER BY p.qid, s.start_year, s.is_loan
""").fetchall()

raw = {}
for r in rows:
    raw.setdefault(r["qid"], {
        "name": r["display_name"],
        "nationality": r["nationality"],
        "position": r["position"],
    })
    raw[r["qid"]].setdefault("stints", []).append({
        "name": r["club"],
        "crest": r["crest_file"],          # e.g. "crests/Q9617.svg" or None
        "start": r["start_year"],
        "end": r["end_year"],
        "apps": r["apps"],
        "loan": r["is_loan"],
    })


def card(st, start=None, end=None, loan=False):
    c = {"name": st["name"], "crest": st["crest"],
         "start": start if start is not None else st["start"],
         "end": end if end is not None else st["end"],
         "apps": st["apps"]}
    if loan:
        c["loan"] = 1
    return c


overlap_review = []  # rows for overlap_review.csv


def display_sequence(player, stints):
    """Resolve overlapping/nested stints with loan-first precedence:

    1. Classify loans: Wikidata is_loan flag, or heuristic (<=1yr stint
       fully inside a different club's longer spell).
    2. Hide each loan nested inside a different club's spell — the parent
       shows its full range and keeps its own apps. Resolved per loan, so
       multi-loan careers work. Non-nested loans stay visible (LOAN tag).
    3. Remaining different-club strict overlaps (Fix 6): trim the earlier
       club's end to the later club's start; anything untrimmable is
       logged to overlap_review.csv.
    """
    def end_of(s, open_=9999):
        return s["end"] if s["end"] is not None else open_

    sts = sorted((dict(s) for s in stints),
                 key=lambda s: (s["start"], end_of(s)))

    # 1) loan classification (flag OR nested-short-stint heuristic)
    for s in sts:
        s["_loan"] = bool(s["loan"])
    for s in sts:
        if s["_loan"] or s["end"] is None:
            continue
        if s["end"] - s["start"] <= 1 and any(
                o is not s and not o["loan"] and o["name"] != s["name"]
                and o["start"] <= s["start"] and end_of(o) >= s["end"]
                and end_of(o, s["end"]) - o["start"] > s["end"] - s["start"]
                for o in sts):
            s["_loan"] = True

    # 2) hide loans nested inside a different club's (non-loan) spell
    visible = []
    for s in sts:
        nested_in = [o for o in sts
                     if o is not s and not o["_loan"] and o["name"] != s["name"]
                     and o["start"] <= s["start"]
                     and end_of(o) >= end_of(s, s["start"])]
        if s["_loan"] and nested_in:
            continue
        visible.append(s)

    # 3) Fix 6 — strict overlaps between different visible clubs
    for i, a in enumerate(visible):
        if a["end"] is None:
            continue
        for b in visible[i + 1:]:
            if b["name"] == a["name"] or b["start"] >= a["end"]:
                continue
            if b["start"] > a["start"]:
                overlap_review.append([player, "TRIMMED",
                    f"{a['name']} {a['start']}-{a['end']}",
                    f"{b['name']} {b['start']}-{b['end'] or '?'}",
                    f"{a['name']} end shown as {b['start']}"])
                a["end"] = b["start"]
            else:
                overlap_review.append([player, "NEEDS_REVIEW",
                    f"{a['name']} {a['start']}-{a['end']}",
                    f"{b['name']} {b['start']}-{b['end'] or '?'}",
                    "same start years; left as-is"])
            break

    return [card(s, start=s["start"], end=s["end"], loan=s["_loan"])
            for s in visible]


for qid, pl in raw.items():
    players[qid] = {
        "name": pl["name"],
        "nationality": pl["nationality"],
        "position": pl["position"],
        "aliases": [],
        "clubs": display_sequence(pl["name"], pl["stints"]),
    }

import csv
with open("overlap_review.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["player", "action", "earlier_stint", "later_stint", "resolution"])
    w.writerows(overlap_review)
print(f"{len(overlap_review)} overlaps handled -> overlap_review.csv")

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
