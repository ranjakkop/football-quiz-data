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


def display_sequence(stints):
    """Chronological cards; a parent spell is split around loans nested
    inside it (parent -> loan(s) -> parent again), with the return card
    shown only when the parent continues past the loan with games played."""
    out = []
    used = set()
    for i, st in enumerate(stints):
        if i in used:
            continue
        if st["loan"]:
            out.append(card(st, loan=True))
            continue
        p_end = st["end"] if st["end"] is not None else 9999
        nested = []
        for j in range(i + 1, len(stints)):
            L = stints[j]
            l_end = L["end"] if L["end"] is not None else L["start"]
            if (j not in used and L["loan"] and L["name"] != st["name"]
                    and L["start"] >= st["start"] and l_end <= p_end):
                nested.append((j, L))
        if not nested:
            out.append(card(st))
            continue
        first = nested[0][1]
        out.append(card(st, end=first["start"]))
        for j, L in nested:
            used.add(j)
            out.append(card(L, loan=True))
        last = nested[-1][1]
        last_end = last["end"] if last["end"] is not None else last["start"]
        if last_end < p_end and (st["apps"] is None or st["apps"] > 0):
            ret = card(st, start=last_end)
            ret["apps"] = None
            out.append(ret)
    return out


for qid, pl in raw.items():
    players[qid] = {
        "name": pl["name"],
        "nationality": pl["nationality"],
        "position": pl["position"],
        "aliases": [],
        "clubs": display_sequence(pl["stints"]),
    }

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
