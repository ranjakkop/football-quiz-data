#!/usr/bin/env python3
"""
backfill_loans.py — set stints.is_loan from Wikidata's P1642 qualifier.

Fetches every player's P54 (member of sports team) claims in batches of 50
via wbgetentities and flags a stint as a loan when the claim carries the
qualifier P1642 (acquisition transaction) = Q2914547 ("loan").
Stints are matched on (player_qid, club_qid, start year of P580).
"""

import sqlite3
import time

import requests

DB = "football.db"
API = "https://www.wikidata.org/w/api.php"
BATCH = 50
SLEEP = 4
LOAN_QIDS = {"Q2914547"}  # "loan"


def year_of(snak):
    try:
        return int(snak["datavalue"]["value"]["time"][1:5])
    except (KeyError, TypeError, ValueError):
        return None


def main():
    conn = sqlite3.connect(DB)
    qids = [q for (q,) in conn.execute("SELECT qid FROM players ORDER BY qid")]
    print(f"{len(qids)} players, {(len(qids) + BATCH - 1) // BATCH} batches")

    session = requests.Session()
    session.headers["User-Agent"] = "football-quiz-data/1.0 (loan backfill)"
    flagged = unmatched = 0
    other_values = {}

    for i in range(0, len(qids), BATCH):
        batch = qids[i:i + BATCH]
        for attempt in range(5):
            resp = session.get(API, params={
                "action": "wbgetentities", "ids": "|".join(batch),
                "props": "claims", "format": "json"}, timeout=60)
            if resp.status_code in (429, 500, 502, 503) and attempt < 4:
                wait = 15 * 2 ** attempt
                print(f"  (HTTP {resp.status_code}, retrying in {wait}s)")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            break
        for pqid, ent in resp.json().get("entities", {}).items():
            for claim in ent.get("claims", {}).get("P54", []):
                quals = claim.get("qualifiers", {})
                trans = quals.get("P1642")
                if not trans:
                    continue
                vals = {t["datavalue"]["value"]["id"]
                        for t in trans if t.get("datavalue")}
                if not vals & LOAN_QIDS:
                    for v in vals:
                        other_values[v] = other_values.get(v, 0) + 1
                    continue
                try:
                    club = claim["mainsnak"]["datavalue"]["value"]["id"]
                except (KeyError, TypeError):
                    continue
                start = None
                if "P580" in quals:
                    start = year_of(quals["P580"][0])
                cur = conn.execute(
                    """UPDATE stints SET is_loan=1 WHERE player_qid=?
                       AND club_qid=? AND (start_year=? OR ? IS NULL)""",
                    (pqid, club, start, start))
                if cur.rowcount:
                    flagged += cur.rowcount
                else:
                    unmatched += 1
        conn.commit()
        print(f"  batch {i // BATCH + 1}: {flagged} loan stints so far")
        time.sleep(SLEEP)

    total_loans = conn.execute(
        "SELECT COUNT(*) FROM stints WHERE is_loan=1").fetchone()[0]
    print(f"\nflagged as loans:        {total_loans}")
    print(f"loan claims w/o matching stint: {unmatched}")
    if other_values:
        print(f"non-loan P1642 values seen: {other_values}")
    conn.close()


if __name__ == "__main__":
    main()
