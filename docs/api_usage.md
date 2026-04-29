# API Usage Guide

## Starting the API

```bash
source .venv/bin/activate
python -m api.main
```

The server runs at `http://localhost:8000`. Interactive docs are at `http://localhost:8000/docs`.

---

## Generating Predictions & Recommendations

The API does not generate predictions or recommendations on its own. You must trigger the pipeline first, then read the results.

### Step 1: Trigger the pipeline

```bash
curl -X POST http://localhost:8000/run-pipeline/2026-04-27
```

This runs three steps in the background:

1. **Ingest** -- fetches today's games, odds, starters, injuries, box scores, and team stats
2. **Predict** -- runs the ELO + logistic regression model to produce win probabilities and predicted margins
3. **Recommend** -- compares model probabilities against market odds, computes edge and confidence, and outputs sized picks

The endpoint returns immediately with `202 Accepted`. The pipeline takes 1-2 minutes to complete in the background.

```json
{ "status": "started", "date": "2026-04-27" }
```

### Step 2: Read the slate

Once the pipeline finishes, fetch the results:

```bash
curl http://localhost:8000/slate/2026-04-27
```

This returns all qualifying picks for that date -- the games where the model found enough edge to recommend a bet.

To also see games the model passed on:

```bash
curl "http://localhost:8000/slate/2026-04-27?include_no_bets=true"
```

### Shortcut: Today's slate

```bash
curl http://localhost:8000/slate
```

Returns the slate for today's date. Requires the pipeline to have already run for today.

---

## Endpoints

### GET /health

Check if the server and database are running.

```bash
curl http://localhost:8000/health
```

Returns the latest game date, latest slate date, and current model version.

### GET /slate

Get today's picks. Equivalent to `GET /slate/{today}`.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `date` | query | today | Date to fetch (YYYY-MM-DD) |
| `include_no_bets` | query | false | Include games the model passed on |

### GET /slate/{date}

Get picks for a specific date.

```bash
curl http://localhost:8000/slate/2026-04-25
```

Each pick includes:

- **game_id** -- e.g. `2026-04-27-NYY-BOS-1`
- **market** -- `moneyline` or `run_line`
- **side** -- `home` or `away`
- **decision** -- `small` or `medium` (bet size)
- **edge** -- how much the model probability exceeds the market's implied probability (e.g. 0.067 = 6.7%)
- **confidence** -- composite score from 1-10 combining edge magnitude, model conviction, calibration, and injury certainty
- **model_prob** -- the model's estimated probability
- **implied_prob** -- the market's implied probability (vig-removed)
- **american_odds** -- the line (e.g. -115, +140)
- **starters** -- pitcher names with rolling ERA and WHIP
- **llm_explanation** -- natural language reasoning (if available)

Returns `404` if no slate exists for that date (pipeline hasn't run yet).

### GET /game/{game_id}

Get full detail for a single game.

```bash
curl http://localhost:8000/game/2026-04-27-NYY-BOS-1
```

Returns:

- Game metadata (date, teams, status, scores)
- Model prediction (win probabilities, predicted margin, cover probabilities, ELO differential)
- Latest odds for all markets
- Probable starters with ERA/WHIP
- Active injuries
- Latest recommendation (if any)

Returns `404` if the game doesn't exist.

### GET /history

View past pick results with win/loss settlement.

```bash
curl "http://localhost:8000/history?start_date=2026-04-01&end_date=2026-04-27"
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `start_date` | query | 30 days ago | Start of date range |
| `end_date` | query | today | End of date range |
| `decision` | query | all | Filter: `small`, `medium` |
| `market` | query | all | Filter: `moneyline`, `run_line` |
| `page` | query | 1 | Page number |
| `per_page` | query | 50 | Results per page (max 200) |
| `include_no_bets` | query | false | Include passed games |

Each item includes a `result` field: `win`, `loss`, `push`, or `pending`.

The `summary` section shows overall win/loss/push/pending counts and win rate.

### POST /run-pipeline/{date}

Trigger the full ingest-predict-recommend pipeline for a date.

```bash
curl -X POST http://localhost:8000/run-pipeline/2026-04-27
```

- Returns `202` if the pipeline started
- Returns `400` if the date format is invalid (must be YYYY-MM-DD)
- Returns `409` if the pipeline is already running for that date

The pipeline runs in the background. There is currently no status endpoint to check progress -- check server logs or poll `GET /slate/{date}` until results appear.

---

## How Data Gets Refreshed

Data comes from three sources, each refreshed differently:

### 1. Automatic daily job (9am cron)

The `capture_odds.sh` cron job runs every morning and handles background data collection:

| Data | How it refreshes |
|------|-----------------|
| Game schedule + final scores | Re-fetches last 5 days from the MLB schedule API to pick up `scheduled -> final` transitions |
| Missing game dates | Auto-detects any dates not in the DB since season start and backfills |
| Starter game logs | Fetches box scores for last 5 days of final games (IP, ER, H, BB, K) |
| Morning odds | Captures live odds for today's games from The Odds API |
| Team season stats | Pulls ERA, OPS, win%, runs/game from pybaseball |

This runs automatically. If it fails, run manually:

```bash
source .venv/bin/activate
python ingest/capture_odds.py
```

### 2. On-demand pipeline (when you want picks)

When you trigger `POST /run-pipeline/{date}` or run `./run_daily.sh`, the ingest step fetches fresh data for that specific date:

- Today's game schedule and probable starters
- Live odds from all available bookmakers
- Box scores for any recently-final games
- Team stats and injury reports

This data is then used by the predict and recommend steps.

### 3. Historical backfill (one-time setup)

Scripts in `historical/` are for seeding a new database. You don't need these for daily operation:

```bash
python historical/pull_games.py           # game results from baseball-reference
python historical/pull_odds.py            # historical closing odds
python historical/pull_stats.py           # season team stats via pybaseball
python historical/pull_starters.py        # probable pitchers
python historical/pull_boxscores.py       # starter game logs
```

### What's safe to re-run

Everything. All database writes use `ON CONFLICT DO UPDATE`, so re-running any script for the same date is safe and idempotent.

---

## Typical Daily Workflow

### Via the API

```bash
# 1. Generate today's picks
curl -X POST http://localhost:8000/run-pipeline/2026-04-27

# 2. Wait ~1-2 minutes, then fetch the slate
curl http://localhost:8000/slate

# 3. Drill into a specific game
curl http://localhost:8000/game/2026-04-27-NYY-BOS-1

# 4. Check recent track record
curl http://localhost:8000/history
```

### Via the command line

```bash
./run_daily.sh              # today
./run_daily.sh 2026-04-27   # specific date
```

Both approaches produce the same results -- the API's `/run-pipeline` endpoint runs the same three scripts as `run_daily.sh`.
