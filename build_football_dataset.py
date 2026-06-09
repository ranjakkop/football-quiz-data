#!/usr/bin/env python3
"""
Football career-path dataset builder  (v3)

Anchors on a fixed list of BIG CLUBS (below). A player qualifies if they made
>= MIN_APPS_AT_LIST_CLUB appearances for AT LEAST ONE club on that list.
For every qualifying player we store their FULL club journey, with the years
at each club and the number of appearances they made for each club.

WHY THIS DESIGN: hardcoding the clubs (by Wikidata QID) avoids the messy
"league" relation entirely, and keeps the player pool to recognizable names.

PIPELINE:
  1. Look up / confirm the anchor clubs (+ crests) by QID.
  2. For each anchor club, find players with >= 80 appearances THERE.
  3. Pull each qualifying player's FULL career: club, start/end year, apps.
  4. Classify every club encountered (real club vs national team) + crest.
  5. Build a `career_paths` view with the full ordered journey.

USAGE:
  pip install requests
  python build_football_dataset.py            # build football.db
  python build_football_dataset.py --crests   # also download crests
Safe to re-run (it upserts).
"""

import argparse
import os
import sqlite3
import time

import requests

WDQS = "https://query.wikidata.org/sparql"
HEADERS = {"User-Agent": "FootballQuizDatasetBuilder/3.0 (ranjaktimble@gmail.com)"}

DB_PATH = "football.db"
CREST_DIR = "crests"

# ---- Config ----
MIN_APPS_AT_LIST_CLUB = 80     # must have >= this many apps for one anchor club
PLAYER_BATCH = 40
CLUB_BATCH = 60
SLEEP = 1.5

FOOTBALL_CLUB = "Q476028"      # to distinguish clubs from national teams

# ---- Anchor clubs: name -> Wikidata QID ----
# QIDs verified on wikidata.org. If you ever doubt one, open
# https://www.wikidata.org/wiki/<QID> to confirm it's the right club.
ANCHOR_CLUBS = {
    "Manchester United":   "Q18656",
    "Liverpool":           "Q1130849",
    "Manchester City":     "Q50602",
    "Arsenal":             "Q9617",
    "Chelsea":             "Q9616",
    "Tottenham Hotspur":   "Q18741",
    "Newcastle United":    "Q18716",
    "Everton":             "Q5794",
    "Real Madrid":         "Q8682",
    "Barcelona":           "Q7156",
    "Atletico Madrid":     "Q8701",
    "Sevilla":             "Q10329",
    "Valencia":            "Q10333",
    "Villarreal":          "Q12297",
    "Athletic Bilbao":     "Q8687",
    "Bayern Munich":       "Q15789",
    "Borussia Dortmund":   "Q41420",
    "AC Milan":            "Q1543",
    "Inter Milan":         "Q631",
    "Napoli":              "Q2641",
    "Juventus":            "Q1422",
    "Roma":                "Q2739",
}


# ----------------------------- Wikidata helpers ----------------------------- #
def sparql(query, retries=5):
    for attempt in range(retries):
        resp = requests.get(WDQS, params={"query": query, "format": "json"},
                            headers=HEADERS, timeout=120)
        if resp.status_code == 200:
            return resp.json()["results"]["bindings"]
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", 5 * (attempt + 1)))
            print(f"  429 rate-limited, sleeping {wait}s"); time.sleep(wait)
        else:
            time.sleep(2 * (attempt + 1))
    resp.raise_for_status()


def qid(uri):
    return uri.rsplit("/", 1)[-1]


def val(row, key):
    return row[key]["value"] if key in row else None


def chunked(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


def year(iso):
    return int(iso[:4]) if iso and iso[:1].isdigit() else None


# ------------------------------- Queries ------------------------------------ #
def confirm_anchor_clubs(qids):
    values = " ".join(f"wd:{q}" for q in qids)
    return sparql(f"""
      SELECT ?club ?clubLabel ?logo WHERE {{
        VALUES ?club {{ {values} }}
        OPTIONAL {{ ?club wdt:P154 ?logo. }}
        SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
      }}""")


def qualifying_players_at_club(club_qid, min_apps):
    """Players with >= min_apps appearances for this specific club."""
    return sparql(f"""
      SELECT DISTINCT ?player WHERE {{
        ?player p:P54 ?st .
        ?st ps:P54 wd:{club_qid} ;
            pq:P1350 ?apps .
        FILTER(?apps >= {min_apps})
      }}""")


def careers(player_qids):
    values = " ".join(f"wd:{q}" for q in player_qids)
    return sparql(f"""
      SELECT ?player ?playerLabel ?club ?start ?end ?apps WHERE {{
        VALUES ?player {{ {values} }}
        ?player p:P54 ?st . ?st ps:P54 ?club .
        OPTIONAL {{ ?st pq:P580 ?start. }}
        OPTIONAL {{ ?st pq:P582 ?end. }}
        OPTIONAL {{ ?st pq:P1350 ?apps. }}
        SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
      }}""")


def aliases(player_qids):
    values = " ".join(f"wd:{q}" for q in player_qids)
    return sparql(f"""
      SELECT ?player ?alias WHERE {{
        VALUES ?player {{ {values} }}
        ?player skos:altLabel ?alias . FILTER(LANG(?alias)="en")
      }}""")


def classify_clubs(club_qids):
    values = " ".join(f"wd:{q}" for q in club_qids)
    return sparql(f"""
      SELECT ?club ?clubLabel ?logo ?isclub WHERE {{
        VALUES ?club {{ {values} }}
        OPTIONAL {{ ?club wdt:P154 ?logo. }}
        BIND(EXISTS {{ ?club wdt:P31/wdt:P279* wd:{FOOTBALL_CLUB} }} AS ?isclub)
        SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
      }}""")


# ------------------------------- Database ----------------------------------- #
def init_db(conn):
    conn.executescript("""
      CREATE TABLE IF NOT EXISTS clubs (
        qid TEXT PRIMARY KEY, name TEXT, crest_url TEXT, crest_file TEXT,
        is_anchor INTEGER DEFAULT 0, is_club INTEGER DEFAULT 1);
      CREATE TABLE IF NOT EXISTS players (qid TEXT PRIMARY KEY, display_name TEXT);
      CREATE TABLE IF NOT EXISTS player_aliases (
        player_qid TEXT, alias TEXT, UNIQUE(player_qid, alias));
      CREATE TABLE IF NOT EXISTS stints (
        player_qid TEXT, club_qid TEXT, start_year INTEGER, end_year INTEGER,
        apps INTEGER, UNIQUE(player_qid, club_qid, start_year));
    """)
    conn.commit()


def upsert_club(conn, q, name=None, logo=None, is_anchor=None):
    conn.execute(
        "INSERT INTO clubs(qid,name,crest_url,is_anchor) VALUES(?,?,?,COALESCE(?,0)) "
        "ON CONFLICT(qid) DO UPDATE SET "
        "  name=COALESCE(excluded.name, clubs.name), "
        "  crest_url=COALESCE(excluded.crest_url, clubs.crest_url), "
        "  is_anchor=MAX(clubs.is_anchor, COALESCE(excluded.is_anchor,0))",
        (q, name, logo, is_anchor))


# --------------------------------- Build ------------------------------------ #
def build():
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    # 1. Confirm anchor clubs + crests.
    print(f"[1/5] confirming {len(ANCHOR_CLUBS)} anchor clubs")
    for r in confirm_anchor_clubs(list(ANCHOR_CLUBS.values())):
        upsert_club(conn, qid(val(r, "club")), val(r, "clubLabel"),
                    val(r, "logo"), is_anchor=1)
    conn.commit()

    # 2. Players with >= MIN_APPS_AT_LIST_CLUB apps at any anchor club.
    player_set = set()
    for name, cq in ANCHOR_CLUBS.items():
        try:
            ids = [qid(val(r, "player"))
                   for r in qualifying_players_at_club(cq, MIN_APPS_AT_LIST_CLUB)]
        except Exception as e:
            print(f"  skip {name}: {e}"); continue
        player_set.update(ids)
        print(f"[2/5] {name}: +{len(ids)} (total {len(player_set)})")
        time.sleep(SLEEP)

    players = sorted(player_set)
    print(f"[players qualifying] {len(players)}")

    # 3. Full journeys (club, dates, apps) + aliases.
    for i, batch in enumerate(chunked(players, PLAYER_BATCH), 1):
        for r in careers(batch):
            pq = qid(val(r, "player"))
            conn.execute("INSERT OR IGNORE INTO players(qid,display_name) VALUES(?,?)",
                         (pq, val(r, "playerLabel")))
            cq = qid(val(r, "club"))
            upsert_club(conn, cq)
            apps = int(val(r, "apps")) if val(r, "apps") else None
            conn.execute("INSERT OR IGNORE INTO stints"
                         "(player_qid,club_qid,start_year,end_year,apps) VALUES(?,?,?,?,?)",
                         (pq, cq, year(val(r, "start")), year(val(r, "end")), apps))
        for r in aliases(batch):
            conn.execute("INSERT OR IGNORE INTO player_aliases(player_qid,alias) "
                         "VALUES(?,?)", (qid(val(r, "player")), val(r, "alias")))
        conn.commit()
        print(f"[3/5] careers batch {i}/{-(-len(players)//PLAYER_BATCH)}"); time.sleep(SLEEP)

    # 4. Classify every club encountered (club vs national team) + crest + name.
    all_clubs = [row[0] for row in conn.execute("SELECT qid FROM clubs").fetchall()]
    for i, batch in enumerate(chunked(all_clubs, CLUB_BATCH), 1):
        for r in classify_clubs(batch):
            cq = qid(val(r, "club"))
            is_club = 1 if val(r, "isclub") == "true" else 0
            conn.execute("UPDATE clubs SET name=COALESCE(?,name), "
                         "crest_url=COALESCE(?,crest_url), is_club=? WHERE qid=?",
                         (val(r, "clubLabel"), val(r, "logo"), is_club, cq))
        conn.commit()
        print(f"[4/5] classify batch {i}"); time.sleep(SLEEP)

    # 5. Build the journey view: every player's full ordered path with years + apps.
    conn.executescript("""
      DROP VIEW IF EXISTS career_paths;
      CREATE VIEW career_paths AS
      SELECT p.qid, p.display_name,
             GROUP_CONCAT(
               c.name || ' (' ||
               COALESCE(s.start_year,'?') || '-' || COALESCE(s.end_year,'?') ||
               ', ' || COALESCE(s.apps,'?') || ' apps)',
               '  ->  '
               ORDER BY s.start_year) AS journey,
             COUNT(*) AS clubs
      FROM players p
      JOIN stints s ON s.player_qid = p.qid
      JOIN clubs  c ON c.qid = s.club_qid AND c.is_club = 1
      WHERE s.start_year IS NOT NULL
      GROUP BY p.qid
      ORDER BY p.display_name;
    """)
    conn.commit()
    np = conn.execute("SELECT COUNT(*) FROM players").fetchone()[0]
    ncp = conn.execute("SELECT COUNT(*) FROM career_paths").fetchone()[0]
    print(f"[5/5 done] {np} players, {ncp} with usable journeys, in {DB_PATH} "
          f"(>= {MIN_APPS_AT_LIST_CLUB} apps at an anchor club)")
    conn.close()


def download_crests():
    os.makedirs(CREST_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT DISTINCT qid, crest_url FROM clubs "
        "WHERE crest_url IS NOT NULL AND crest_file IS NULL AND is_club=1").fetchall()
    print(f"[crests] downloading {len(rows)} crests")
    for q, url in rows:
        ext = os.path.splitext(url)[1].split("?")[0] or ".img"
        path = os.path.join(CREST_DIR, f"{q}{ext}")
        try:
            with open(path, "wb") as f:
                f.write(requests.get(url, headers=HEADERS, timeout=60).content)
            conn.execute("UPDATE clubs SET crest_file=? WHERE qid=?", (path, q))
            conn.commit()
        except Exception as e:
            print(f"  fail {q}: {e}")
        time.sleep(0.3)
    conn.close()
    print("[crests] done")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--crests", action="store_true", help="download crest images")
    args = ap.parse_args()
    build()
    if args.crests:
        download_crests()
