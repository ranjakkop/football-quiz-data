# Football Career-Path Dataset Builder

Builds a clean, queryable dataset of footballers' **club career paths** for a
guess-the-career quiz game. Data comes from [Wikidata](https://www.wikidata.org)
(free, openly licensed, CC0) and is stored in a local SQLite database.

## What it does

- Selects every player who played for a **Premier League or La Liga** club at least once.
- Pulls each player's **full club career** (all leagues, in date order).
- Keeps only players with **≥ 150 club appearances** across their career.
- Fetches each club's **crest** where Wikidata has one.
- Outputs a ready-to-use `career_paths` view, e.g.
  `Valencia  ->  Chelsea  ->  Manchester United`.

## Setup

Requires Python 3.9+.

```bash
pip install -r requirements.txt
```

Then open `build_football_dataset.py` and put a real contact email in the
`HEADERS` User-Agent line (Wikidata requires a descriptive User-Agent).

## Usage

```bash
python build_football_dataset.py            # build football.db
python build_football_dataset.py --crests   # also download crests
```

Inspect the result:

```bash
sqlite3 football.db "SELECT display_name, total_apps, path FROM career_paths LIMIT 20;"
```

The script **upserts**, so re-running it (e.g. monthly to catch transfers) is safe.

## Configuration

Edit the constants at the top of the script:

| Setting | Default | Meaning |
|---|---|---|
| `MIN_APPS` | `150` | Minimum club appearances to include a player |
| `LEAGUE_MODE` | `"OR"` | `"OR"` = PL or La Liga; `"AND"` = both |
| `LEAGUES` | PL, La Liga | Seed leagues (add Wikidata Q-numbers to widen scope) |
| `SLEEP` | `1.5` | Delay between queries (raise it if you hit rate limits) |

## Database schema

- **players** — `qid`, `display_name`
- **player_aliases** — alternate names (powers fuzzy answer matching)
- **clubs** — `qid`, `name`, `crest_url`, `crest_file`, `league`, `is_club`
- **stints** — `player_qid`, `club_qid`, `start_year`, `end_year`, `apps`
- **career_paths** (view) — qualifying players with their ordered club path

## Notes & limitations

- Appearance counts come from Wikidata and are **incomplete**; some genuinely
  qualifying players may be missing the data and get excluded. Lower `MIN_APPS`
  to recover more players.
- Crest coverage is excellent for major clubs but patchy for smaller/older ones;
  fall back to showing the club name where no crest exists.
- Respects Wikidata rate limits (serial queries, polite delays, 429 backoff).

## Data source & licensing

Player and career facts are from Wikidata (CC0). Club crests are trademarks/
copyrighted logos owned by their clubs — review usage before redistributing them.
