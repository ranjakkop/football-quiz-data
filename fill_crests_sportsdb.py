#!/usr/bin/env python3
"""
fill_crests_sportsdb.py — fill missing club crests from TheSportsDB (free tier).

For every club with crest_file IS NULL (anchors first, then by stint count),
searches TheSportsDB, requires strSport == "Soccer" and an exact normalized
name match (against strTeam or any strAlternate entry), downloads the /small
badge to crests/, and updates clubs.crest_file. Clubs without a confident
match are logged to unmatched_clubs.csv and skipped on later runs.

Usage:
    python fill_crests_sportsdb.py [--limit N] [--retry-unmatched]

Resumable: clubs that already have crest_file are never re-queried.
Rate limit: ~2.5s between API calls (< 30 requests/minute).
"""

import argparse
import csv
import os
import re
import sqlite3
import time
import unicodedata

import requests

DB_PATH = "football.db"
CREST_DIR = "crests"
UNMATCHED_CSV = "unmatched_clubs.csv"
SEARCH_URL = "https://www.thesportsdb.com/api/v1/json/123/searchteams.php"
SLEEP = 2.1  # < 30 requests/minute

# Abbreviation tokens that carry no identity ("Portsmouth F.C." == "Portsmouth")
ABBREV_TOKENS = {
    "fc", "afc", "ac", "as", "ssc", "cf", "cd", "sl", "sc", "sk", "fk",
    "bk", "sd", "ud", "rcd", "rc", "cp", "ca", "aj", "us", "vfb", "vfl",
    "tsv", "sv", "bsc", "ogc", "psv", "nk", "gnk", "hnk", "ks", "kv",
    "kaa", "krc", "rsc", "aek", "paok",
}
# Filler words ignored only at the looser token-set comparison level
FILLER_TOKENS = {"de", "la", "el", "do", "da", "of", "and", "club", "the"}
# Generic club-type words dropped (with years) for "core name" comparison:
# "U.S. Salernitana 1919" and "Salernitana" share the core {salernitana}.
GENERIC_TOKENS = FILLER_TOKENS | {
    "calcio", "balompie", "futbol", "futebol", "clube", "regatas",
    "esporte", "sociedad", "deportiva", "deportivo", "royal", "stade",
    "sport", "if", "sk", "fk", "association", "sportive", "y", "e",
    "social", "sociedade", "societa", "esportiva", "asociacion",
    "associacao", "associazione", "unione", "sportiva", "sportivo",
}
# Identity markers for reserve/youth/women sides: if these distinguish the DB
# name from an API result, the result is the parent club — never match it.
RESERVED_TOKENS = {
    "b", "c", "ii", "iii", "iv", "u17", "u18", "u19", "u20", "u21", "u23",
    "juvenil", "junior", "youth", "reserves", "reserve", "academy",
    "women", "ladies", "femenino", "feminino", "femminile", "feminine",
    "atletic", "atletico", "castilla", "mestalla", "promesas",
}
YEAR_RE = re.compile(r"^(18|19|20)\d{2}$")

# qid -> (SportsDB strTeam, strCountry) for clubs whose names are genuinely
# ambiguous on TheSportsDB (verified by hand from unmatched_clubs.csv).
MANUAL = {
    "Q19597":   ("Rangers", "Scotland"),
    "Q131499":  ("Benfica", "Portugal"),
    "Q19482":   ("Wimbledon FC", "England"),
    "Q219703":  ("Boavista", "Portugal"),
    "Q11963":   ("Hércules", "Spain"),
    "Q208399":  ("Bastia", "France"),
    "Q980573":  ("River Plate", "Argentina"),
    "Q5014111": ("Vasco da Gama", "Brazil"),
    "Q188656":  ("Partizan Belgrade", "Serbia"),
    "Q6601875": ("Fenerbahce", "Turkey"),
}


def strip_accents(s):
    return "".join(c for c in unicodedata.normalize("NFKD", s)
                   if not unicodedata.combining(c))


def normalize(name):
    """Lowercase, accent-free, punctuation-free, abbreviation-free form."""
    n = strip_accents(name).lower()
    n = n.replace(".", "")  # "f.c." -> "fc", not "f c"
    n = re.sub(r"[^\w\s]", " ", n)
    tokens = [t for t in n.split() if t not in ABBREV_TOKENS]
    return " ".join(tokens)


def token_set(name):
    return frozenset(t for t in normalize(name).split() if t not in FILLER_TOKENS)


def core_tokens(name):
    """Identity tokens only: no fillers, generic club words, or year numbers."""
    return frozenset(t for t in normalize(name).split()
                     if t not in GENERIC_TOKENS and not YEAR_RE.match(t))


def candidate_names(team):
    """All names a SportsDB result is known by: strTeam + strAlternate parts."""
    names = [team.get("strTeam") or ""]
    alt = team.get("strAlternate") or ""
    names += [a.strip() for a in alt.split(",") if a.strip()]
    return [n for n in names if n]


def search_teams(session, query):
    for attempt in range(4):
        resp = session.get(SEARCH_URL, params={"t": query}, timeout=30)
        if resp.status_code in (429, 503) and attempt < 3:
            wait = 10 * 2 ** attempt
            print(f"      (HTTP {resp.status_code}, retrying in {wait}s)")
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp.json().get("teams") or []


def unique_or_same_badge(matches):
    """A list of matched results is safe if it boils down to one badge."""
    if len(matches) == 1 or (matches and len({t["strBadge"] for t in matches}) == 1):
        return matches[0]
    return None


def match_club(qid, club_name, teams):
    """Return (team, level) for a confident match, else (None, reason)."""
    soccer = [t for t in teams if t.get("strSport") == "Soccer"
              and t.get("strBadge")]
    if not soccer:
        return None, "no soccer results"

    if qid in MANUAL:
        want_name, want_country = MANUAL[qid]
        # Compare raw names (accents/case only) — normalize() drops
        # abbreviations, which would conflate "Benfica" and "CF Benfica".
        plain = lambda s: " ".join(strip_accents(s).lower().split())
        manual = [t for t in soccer
                  if plain(t["strTeam"]) == plain(want_name)
                  and t.get("strCountry") == want_country]
        if len(manual) == 1:
            return manual[0], "manual"

    norm = normalize(club_name)
    exact = [t for t in soccer
             if any(normalize(n) == norm for n in candidate_names(t))]
    found = unique_or_same_badge(exact)
    if found:
        return found, "exact"
    if exact:
        return None, "ambiguous: " + "; ".join(
            f"{t['strTeam']} ({t.get('strCountry')})" for t in exact[:4])

    tset = token_set(club_name)
    if tset:
        loose = [t for t in soccer
                 if any(token_set(n) == tset for n in candidate_names(t))]
        found = unique_or_same_badge(loose)
        if found:
            return found, "token-set"

    # Core-name matching: drop generic club words and years from both sides.
    # "U.S. Salernitana 1919" == "Salernitana"; never bridge a RESERVED token
    # (b, ii, women, ...) or we'd hand a reserve side its parent's badge.
    core = core_tokens(club_name)
    if core and not core & RESERVED_TOKENS:
        equal, subset = [], []
        for t in soccer:
            for n in candidate_names(t):
                tc = core_tokens(n)
                if not tc or tc & RESERVED_TOKENS:
                    continue
                if tc == core:
                    equal.append(t)
                    break
                if tc < core:  # "Feyenoord" within "Feyenoord Rotterdam"
                    subset.append(t)
                    break
        found = unique_or_same_badge(equal)
        if found:
            return found, "core"
        if not equal:
            found = unique_or_same_badge(subset)
            if found:
                return found, "core-subset"

    return None, "no confident match among: " + "; ".join(
        f"{t['strTeam']} ({t.get('strCountry')})" for t in soccer[:4])


def search_queries(club_name):
    """Query variants to try, strongest first, deduplicated."""
    queries = [club_name]
    plain = strip_accents(club_name)
    queries.append(re.sub(r"[^\w\s]", " ", plain))
    queries.append(normalize(club_name))
    # Core-name variants find clubs listed under their short name
    # ("Albacete Balompié" -> "albacete", "Fenerbahçe Istanbul" -> "fenerbahce")
    core = [t for t in normalize(club_name).split()
            if t not in GENERIC_TOKENS and not YEAR_RE.match(t)]
    if core:
        queries.append(" ".join(core))
        if len(core) > 1:
            queries.append(" ".join(core[:-1]))
            queries.append(" ".join(core[1:]))
    seen, out = set(), []
    for q in queries:
        q = re.sub(r"\s+", " ", q).strip()
        if q and q.lower() not in seen:
            seen.add(q.lower())
            out.append(q)
    return out[:6]


def load_unmatched():
    if not os.path.exists(UNMATCHED_CSV):
        return {}
    with open(UNMATCHED_CSV, newline="", encoding="utf-8") as f:
        return {row["qid"]: row for row in csv.DictReader(f)}


def save_unmatched(rows):
    with open(UNMATCHED_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["qid", "name", "reason"])
        w.writeheader()
        for row in sorted(rows.values(), key=lambda r: r["name"]):
            w.writerow(row)


def coverage(conn):
    total, have = conn.execute(
        "SELECT COUNT(*), COUNT(crest_file) FROM clubs WHERE is_club=1"
    ).fetchone()
    return total, have


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None,
                    help="process at most N clubs this run")
    ap.add_argument("--retry-unmatched", action="store_true",
                    help="re-query clubs already in unmatched_clubs.csv")
    args = ap.parse_args()

    os.makedirs(CREST_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    session = requests.Session()

    total, have_before = coverage(conn)
    unmatched = load_unmatched()
    if args.retry_unmatched:
        unmatched = {}

    candidates = conn.execute("""
        SELECT c.qid, c.name, c.is_anchor,
               (SELECT COUNT(*) FROM stints s WHERE s.club_qid = c.qid) AS n
        FROM clubs c
        WHERE c.is_club = 1 AND c.crest_file IS NULL
        ORDER BY c.is_anchor DESC, n DESC, c.name
    """).fetchall()
    skipped_known = sum(1 for q, *_ in candidates if q in unmatched)
    todo = [c for c in candidates if c[0] not in unmatched]
    if args.limit is not None:
        todo = todo[:args.limit]

    print(f"Clubs missing crests: {len(candidates)}  "
          f"(skipping {skipped_known} known-unmatched, processing {len(todo)})")

    processed = added = failed = 0
    new_unmatched = 0

    for qid, name, is_anchor, n_stints in todo:
        processed += 1
        tag = "ANCHOR" if is_anchor else f"{n_stints} stints"
        team, why = None, "no results"
        try:
            for query in search_queries(name):
                teams = search_teams(session, query)
                time.sleep(SLEEP)
                if teams:
                    team, why = match_club(qid, name, teams)
                    if team:
                        break
        except requests.RequestException as e:
            failed += 1
            print(f"  ! [{processed}/{len(todo)}] {name}: API error: {e}")
            time.sleep(SLEEP)
            continue

        if not team:
            unmatched[qid] = {"qid": qid, "name": name, "reason": why}
            new_unmatched += 1
            save_unmatched(unmatched)
            print(f"  - [{processed}/{len(todo)}] {name} ({tag}): {why}")
            continue

        badge = team["strBadge"]
        url = badge + "/small"
        path = os.path.join(CREST_DIR, f"{qid}.png")
        try:
            resp = session.get(url, timeout=30)
            if resp.status_code != 200 or not resp.content:
                resp = session.get(badge, timeout=30)
                resp.raise_for_status()
                url = badge
            with open(path, "wb") as f:
                f.write(resp.content)
            conn.execute(
                "UPDATE clubs SET crest_url=?, crest_file=? WHERE qid=?",
                (url, path, qid))
            conn.commit()
            added += 1
            print(f"  + [{processed}/{len(todo)}] {name} ({tag}) "
                  f"<- {team['strTeam']} ({team.get('strCountry')})")
        except (requests.RequestException, OSError) as e:
            failed += 1
            if os.path.exists(path):
                os.remove(path)
            print(f"  ! [{processed}/{len(todo)}] {name}: download error: {e}")
        time.sleep(0.3)

    save_unmatched(unmatched)
    _, have_after = coverage(conn)
    anchors_missing = [name for name, in conn.execute(
        "SELECT name FROM clubs WHERE is_anchor=1 AND crest_file IS NULL")]
    n_anchors = conn.execute(
        "SELECT COUNT(*) FROM clubs WHERE is_anchor=1").fetchone()[0]

    print(f"\n{'=' * 56}")
    print(f"Clubs processed this run:  {processed}")
    print(f"Crests added:              {added}")
    print(f"Errors (retryable):        {failed}")
    print(f"New unmatched this run:    {new_unmatched}")
    print(f"Total in {UNMATCHED_CSV}: {len(unmatched)}")
    print(f"Coverage before:           {have_before}/{total} "
          f"({have_before / total * 100:.1f}%)")
    print(f"Coverage after:            {have_after}/{total} "
          f"({have_after / total * 100:.1f}%)")
    if anchors_missing:
        print(f"Anchors MISSING crests:    {', '.join(anchors_missing)}")
    else:
        print(f"All {n_anchors} anchor clubs have crests.")
    print(f"Still missing overall:     {total - have_after}")

    conn.close()


if __name__ == "__main__":
    main()
