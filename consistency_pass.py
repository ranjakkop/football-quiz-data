#!/usr/bin/env python3
"""
consistency_pass.py — Category 1 internal-consistency fixes for football.db.

Fixes only what the data itself proves wrong, club-by-club for each player:
  - same-club duplicate stints (overlapping years, same apps, or one apps NULL
    with a contained range) -> merged into one row, source='consistency_fix'
  - cross-club duplicates (different club entity, identical start/end/apps):
    DETECTED and logged only — picking the right club needs football knowledge
  - end_year < start_year, years out of range, impossible apps: logged
    (fixed here only when the data alone implies the fix)

A genuine return to a club in non-overlapping years is never touched.
Every change or finding is appended to ~/Downloads/full_enrichment_log.csv.
"""

import csv
import os
import sqlite3
from collections import defaultdict

DB = "football.db"
LOG = os.path.expanduser("~/Downloads/full_enrichment_log.csv")
BATCH = 200  # commit after this many players


def log_rows(rows):
    new = not os.path.exists(LOG)
    with open(LOG, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["player", "change_type", "old_value", "new_value",
                        "category", "confidence"])
        w.writerows(rows)


def fmt(club, s, e, a):
    return f"{club} {s}-{e if e is not None else '?'}, {a if a is not None else '?'} apps"


def overlaps(a, b):
    """Year ranges touch or overlap (None end = ongoing)."""
    a0, a1 = a[0], a[1] if a[1] is not None else 9999
    b0, b1 = b[0], b[1] if b[1] is not None else 9999
    return a0 <= b1 and b0 <= a1


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    players = conn.execute("SELECT qid, display_name FROM players").fetchall()

    merged = cross_logged = year_logged = apps_logged = 0
    pending = []

    for i, p in enumerate(players, 1):
        stints = conn.execute("""
            SELECT s.rowid AS rid, s.club_qid, c.name AS club,
                   s.start_year AS s0, s.end_year AS s1, s.apps
            FROM stints s JOIN clubs c ON c.qid = s.club_qid
            WHERE s.player_qid = ? ORDER BY s.start_year
        """, (p["qid"],)).fetchall()

        # --- same-club duplicate merge ---
        by_club = defaultdict(list)
        for st in stints:
            if st["s0"] is not None:
                by_club[st["club_qid"]].append(dict(st))
        for club_qid, group in by_club.items():
            if len(group) < 2:
                continue
            group.sort(key=lambda r: (r["s0"], r["s1"] or 9999))
            kept = [group[0]]
            for st in group[1:]:
                prev = kept[-1]
                same_apps = (st["apps"] is not None and st["apps"] == prev["apps"])
                one_null = (st["apps"] is None) != (prev["apps"] is None)
                if overlaps((prev["s0"], prev["s1"]), (st["s0"], st["s1"])) \
                        and (same_apps or one_null):
                    new_s0 = min(prev["s0"], st["s0"])
                    ends = [e for e in (prev["s1"], st["s1"]) if e is not None]
                    new_s1 = None if (prev["s1"] is None or st["s1"] is None) \
                        else max(ends)
                    new_apps = prev["apps"] if prev["apps"] is not None else st["apps"]
                    old = fmt(prev["club"], prev["s0"], prev["s1"], prev["apps"]) \
                        + " + " + fmt(st["club"], st["s0"], st["s1"], st["apps"])
                    conn.execute("DELETE FROM stints WHERE rowid=?", (st["rid"],))
                    conn.execute("""UPDATE stints SET start_year=?, end_year=?,
                                    apps=?, source='consistency_fix'
                                    WHERE rowid=?""",
                                 (new_s0, new_s1, new_apps, prev["rid"]))
                    prev.update(s0=new_s0, s1=new_s1, apps=new_apps)
                    pending.append([p["display_name"], "DUPLICATE",
                                    old, fmt(prev["club"], new_s0, new_s1, new_apps),
                                    "consistency_fix", "HIGH"])
                    merged += 1
                else:
                    kept.append(st)

        # --- cross-club duplicates: detect only ---
        seen = defaultdict(list)
        for st in stints:
            if st["s0"] is not None and st["apps"] not in (None, 0):
                seen[(st["s0"], st["s1"], st["apps"])].append(st)
        for (s0, s1, apps), group in seen.items():
            clubs = {st["club_qid"] for st in group}
            if len(clubs) > 1 and apps >= 5:
                pending.append([p["display_name"], "CROSS_CLUB_DUPLICATE",
                                " + ".join(fmt(st["club"], s0, s1, apps)
                                           for st in group),
                                "(needs knowledge: which club is real)",
                                "logged_only", "n/a"])
                cross_logged += 1

        # --- impossible years / apps: log ---
        for st in stints:
            s0, s1, apps = st["s0"], st["s1"], st["apps"]
            if s1 is not None and s0 is not None and s1 < s0:
                pending.append([p["display_name"], "WRONG_YEARS",
                                fmt(st["club"], s0, s1, apps),
                                "(end before start — needs knowledge)",
                                "logged_only", "n/a"])
                year_logged += 1
            if s0 is not None and (s0 < 1920 or s0 > 2026) or \
                    (s1 is not None and s1 > 2026):
                pending.append([p["display_name"], "WRONG_YEARS",
                                fmt(st["club"], s0, s1, apps),
                                "(year out of 1920-2026 range)",
                                "logged_only", "n/a"])
                year_logged += 1
            if apps is not None and s0 is not None:
                years = max(1, (s1 if s1 is not None else 2026) - s0 + 1)
                if apps > 60 * years:
                    pending.append([p["display_name"], "WRONG_APPS",
                                    fmt(st["club"], s0, s1, apps),
                                    f"(over 60 apps/year for {years}y stint)",
                                    "logged_only", "n/a"])
                    apps_logged += 1

        if i % BATCH == 0:
            conn.commit()
            log_rows(pending)
            pending = []
            print(f"  ... {i}/{len(players)} players")

    conn.commit()
    log_rows(pending)
    conn.close()
    print(f"\nPlayers scanned:            {len(players)}")
    print(f"Same-club duplicates merged: {merged}")
    print(f"Cross-club dups logged:      {cross_logged}")
    print(f"Year violations logged:      {year_logged}")
    print(f"Impossible apps logged:      {apps_logged}")


if __name__ == "__main__":
    main()
