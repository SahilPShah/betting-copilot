# Operations Guide — AI Betting Co-Pilot

## Repository Layout

```
betting-copilot/
├── historical/              One-time backfill scripts (2023–2025 data)
│   ├── pull_games.py        Scrape game results from baseball-reference.com
│   ├── pull_odds.py         Fetch historical closing odds from The Odds API
│   ├── pull_stats.py        Fetch season team stats from pybaseball
│   ├── pull_starters.py     Backfill per-game probable pitcher names via statsapi
│   └── pull_boxscores.py    Backfill starter game logs (IP, ER, H, BB, K) via statsapi
│
├── ingest/                  Data ingestion scripts
│   ├── mlb_games.py         Schedule + scores → games, game_starters (name only)
│   ├── mlb_boxscores.py     Box scores → pitcher_game_logs (starters only)
│   ├── odds_api.py          Live odds → odds_snapshots
│   ├── mlb_stats.py         Season-to-date team stats → team_stats_mlb
│   ├── mlb_injuries.py      Injury reports → injury_statuses
│   ├── run_ingest.py        On-demand orchestrator: auto-backfills missing dates
│   └── capture_odds.py      Daily cron job: refreshes last 5 days + box scores + odds + stats
│
├── models/
│   ├── elo.py               ELO rating engine (training + inference)
│   ├── train.py             Training pipeline → saves .pkl to models/versions/
│   ├── predict.py           Daily inference → writes to predictions table
│   └── versions/            Serialized model artifacts (.pkl)
│       ├── v2_elo_logreg_starters.pkl
│       └── v3_elo_logreg_starters.pkl  ← current production model
│
├── recs/                    Rules engine and recommendation pipeline
│   ├── edge.py              Computes model_prob − implied_prob per game+market
│   ├── confidence.py        Composite confidence score 1–10
│   ├── rules.py             Pick qualification rules (edge/confidence thresholds)
│   └── run_recs.py          Orchestrates edges → rules → writes recommendations
│
├── eval/                    Backtesting and model evaluation scripts
│   ├── brier.py             Brier score + time-series CV
│   ├── calibration.py       Calibration curve analysis
│   ├── clv.py               Closing Line Value backtest
│   └── results/             Output CSVs and PNGs
│
├── db/
│   ├── session.py           Shared SQLAlchemy engine
│   └── migrations/
│       ├── v1_init.sql      Base schema
│       ├── v2_game_starters.sql
│       ├── v3_cover_prob.sql
│       └── v4_pitcher_game_logs.sql   ← adds pitcher_game_logs + games.game_pk
│
├── docs/
│   ├── model.md             Model training documentation
│   └── operations.md        This file
│
├── logs/
│   └── odds_capture.log     Cron job output (timestamped per run)
│
├── capture_odds.sh          Shell wrapper for cron (sets working directory)
├── run_daily.sh             On-demand pipeline: ingest → predict → recommend
└── .env                     API keys and database URL (never committed)
```

---

## Environment

**Activate the virtual environment before running anything:**
```bash
cd ~/betting-copilot
source .venv/bin/activate
```

**Required `.env` variables:**
```
DATABASE_URL=postgresql://admin:password@localhost:5432/betting_copilot
ANTHROPIC_API_KEY=sk-ant-...
ODDS_API_KEY=...
```

---

## How Data Flows

There are two independent processes:

### 1. Background data collection (automatic, 9am daily via cron)

`capture_odds.sh` runs every morning and handles all passive data collection:

| What | How |
|------|-----|
| Game results + final scores | Re-fetches last 5 days from schedule API — picks up `scheduled → final` transitions |
| Missing game dates | Auto-detects any dates not in DB since season start and backfills |
| Starter game logs | Fetches box scores for last 5 days of final games → `pitcher_game_logs` |
| Morning odds snapshot | Fetches live odds for today's games from The Odds API |
| Season-to-date team stats | Pulls ERA, OPS, win%, runs/game from pybaseball |

You never need to touch this. If the cron fails, run it manually (see below).

### 2. On-demand predictions (run whenever you want picks)

`run_daily.sh` generates picks for a given date on demand:

| Step | Script | Reads from | Writes to |
|------|--------|-----------|-----------|
| Games + starters | `ingest/mlb_games.py` | schedule API | `games`, `game_starters` |
| Box scores | `ingest/mlb_boxscores.py` | schedule API (box scores) | `pitcher_game_logs` |
| Odds | `ingest/odds_api.py` | The Odds API | `odds_snapshots` |
| Stats | `ingest/mlb_stats.py` | pybaseball | `team_stats_mlb` |
| Injuries | `ingest/mlb_injuries.py` | schedule API | `injury_statuses` |
| Predict | `models/predict.py` | DB + .pkl model | `predictions` |
| Recommend | `recs/run_recs.py` | DB | `recommendations`, `slate_runs` |

All writes use `ON CONFLICT DO UPDATE` — re-running the same date is always safe.

---

## Pitcher ERA Architecture

Pitcher ERA is **computed dynamically** at prediction time, not stored at ingest time.

- `game_starters` stores only the pitcher's **name** — no ERA column is populated by the ingest pipeline
- `pitcher_game_logs` stores one row per starter per game with raw IP, ER, H, BB, K
- `compute_starter_eras()` in `ingest/mlb_boxscores.py` computes ERA from the **last 5 starts**, crossing season boundaries naturally
- Both `models/predict.py` and `recs/run_recs.py` call this function at runtime

**Why last 5 starts:** More predictive than cumulative season ERA. A pitcher who struggled in April but settled in by June is evaluated on current form. The year boundary is irrelevant — if a pitcher only has 1 start in 2026, their last 5 starts will include 2025 data automatically.

**Minimum threshold:** 2 starts required before an ERA value is returned. Below that, ERA is treated as None and `has_starter_data = 0` in the model.

---

## Manual Operations

### Get today's picks
```bash
cd ~/betting-copilot
./run_daily.sh
```

### Get picks for a specific date
```bash
./run_daily.sh 2026-04-15
```

### Manually trigger the background data job (if cron didn't run)
```bash
source .venv/bin/activate
python ingest/capture_odds.py
```
Auto-detects missing dates, refreshes last 5 days of scores and box scores, captures today's odds, updates team stats.

### Run individual ingest steps
```bash
source .venv/bin/activate
python ingest/mlb_games.py --date 2026-04-01
python ingest/mlb_games.py --start-date 2026-04-01 --end-date 2026-04-07
python ingest/mlb_boxscores.py --date 2026-04-01
python ingest/odds_api.py --date 2026-04-01
python ingest/mlb_stats.py --date 2026-04-01
python ingest/mlb_injuries.py --date 2026-04-01
```

### Backfill pitcher game logs for a date range
```bash
source .venv/bin/activate
python historical/pull_boxscores.py --start-date 2025-07-01 --end-date 2025-09-28
```
Also updates `games.game_pk` for any games missing it.

### Retrain the model
```bash
source .venv/bin/activate
python models/train.py
```
Saves a new `.pkl` to `models/versions/`. Update the `MODEL_PATH` in `models/predict.py` to point to the new version.

### Check the cron job
```bash
crontab -l                                        # confirm it's registered
cat ~/betting-copilot/logs/odds_capture.log       # view all past runs
tail -50 ~/betting-copilot/logs/odds_capture.log  # view most recent run
```

---

## Database Access

### Connect via psql
```bash
psql postgresql://admin:password@localhost:5432/betting_copilot
```

### Key tables

| Table | Description |
|-------|-------------|
| `teams` | 30 MLB franchises |
| `games` | All games — schedule, scores, status, external `game_pk` |
| `game_starters` | Per-game probable pitcher names (ERA not stored here) |
| `pitcher_game_logs` | Starter-only game lines — IP, ER, H, BB, K per appearance |
| `odds_snapshots` | Point-in-time odds (morning captures + live) |
| `team_stats_mlb` | Season-to-date ERA, WHIP, OPS, win%, runs/game per team |
| `injury_statuses` | Active injury reports |
| `predictions` | Win probability + cover probability per game |
| `recommendations` | Pick output with edge, confidence, decision, reasoning |
| `slate_runs` | One record per pipeline run, parent of recommendations |

### Useful queries

```sql
-- Today's picks (top 5)
SELECT r.game_id, r.market, r.side, r.edge, r.confidence, r.decision
FROM recommendations r
JOIN slate_runs s ON s.slate_run_id = r.slate_run_id
WHERE s.run_date = CURRENT_DATE AND r.decision != 'no_bet'
ORDER BY r.edge DESC LIMIT 5;

-- All predictions for today
SELECT p.game_id, p.home_win_prob, p.away_win_prob,
       p.predicted_margin, p.home_cover_prob, p.away_cover_prob
FROM predictions p
JOIN games g ON g.game_id = p.game_id
WHERE g.game_date = CURRENT_DATE;

-- Starter ERA from last 5 starts for today's pitchers
SELECT gs.game_id, gs.side, gs.starter_name,
       ROUND(SUM(l.earned_runs) / NULLIF(SUM(l.innings_pitched), 0) * 9, 2) AS era_l5
FROM game_starters gs
JOIN games g ON g.game_id = gs.game_id
JOIN LATERAL (
    SELECT earned_runs, innings_pitched
    FROM pitcher_game_logs
    WHERE pitcher_name = gs.starter_name
    ORDER BY game_date DESC LIMIT 5
) l ON TRUE
WHERE g.game_date = CURRENT_DATE
GROUP BY gs.game_id, gs.side, gs.starter_name;

-- Pitcher game log counts by season
SELECT season, COUNT(*) as starts FROM pitcher_game_logs GROUP BY season ORDER BY season;

-- Game results by date
SELECT game_date,
       COUNT(*) as total,
       SUM(CASE WHEN status = 'final' THEN 1 ELSE 0 END) as final,
       SUM(CASE WHEN status = 'scheduled' THEN 1 ELSE 0 END) as scheduled
FROM games WHERE game_date >= '2026-03-27'
GROUP BY game_date ORDER BY game_date;

-- Team stats snapshot
SELECT team_id, team_win_pct, team_pitching_era, team_ops_l14
FROM team_stats_mlb
WHERE as_of_date = (SELECT MAX(as_of_date) FROM team_stats_mlb)
ORDER BY team_win_pct DESC NULLS LAST;

-- Recent pipeline run history
SELECT run_date, games_count, picks_count, model_version, ran_at
FROM slate_runs ORDER BY run_date DESC LIMIT 10;

-- Morning odds for a date
SELECT o.game_id, o.market, o.side, o.american_odds, o.implied_prob, o.captured_at
FROM odds_snapshots o
JOIN games g ON g.game_id = o.game_id
WHERE g.game_date = CURRENT_DATE
ORDER BY o.game_id, o.market, o.side, o.captured_at;
```

---

## Pick Qualification Rules (v1, frozen)

| Rule | Threshold |
|------|-----------|
| Minimum edge — moneyline | ≥ 3% |
| Minimum edge — run line | ≥ 4% |
| Minimum confidence | ≥ 5.0 / 10 |
| Injury block | Injury present AND confidence < 6 → no_bet |
| Market efficiency block | \|edge\| < 1% → no_bet |
| Max picks displayed | 5 (ranked by edge) |
| Bet sizing | small (edge × conf < 0.06), medium (≥ 0.06) |

**Confidence score components (25% each):**
1. Edge magnitude — `min(10, |edge| / 0.01)`
2. Model conviction — `min(10, |model_prob − 0.5| × 20)`
3. Historical calibration — constant 7.0 for v1
4. Injury certainty — 10 (no injury), 6 (questionable), 3 (out)

---

## Model Artifacts

Stored in `models/versions/` as pickle files. The v3 artifact contains:

```python
{
    "model":                  CalibratedClassifierCV,  # win probability model
    "run_diff_model":         LinearRegression,         # run differential model
    "scaler":                 StandardScaler,           # shared feature scaler
    "feature_cols":           [...],                    # ordered feature names
    "run_diff_residual_std":  4.3064,
    "version":                "v3_elo_logreg_starters",
    "k":                      10,
    "k_current":              20,
    "divisor":                800,
    "season_regress":         0.75,
    "decay_half_life":        365,
}
```

`models/predict.py` loads `v3_elo_logreg_starters.pkl` and falls back to v2 if absent.

---

## ELO Parameters (locked)

| Parameter | Value | Description |
|-----------|-------|-------------|
| K-factor (prior seasons) | 10 | Rating shift per game for historical seasons |
| K-factor (current season) | 20 | Higher K makes current-season results count more |
| Divisor | 800 | Controls rating gap sensitivity |
| Home field advantage | 35 pts | Applied before each game's expected result |
| Season reset | 0.75 | At each season boundary: `new = 0.75 × old + 0.25 × 1500` |
| Recency decay half-life | 365 days | Games 1 year old carry half the K-weight of today's games |

---

## External APIs

| API | Usage | Rate limits |
|-----|-------|------------|
| mlb-statsapi | Schedule, scores, starters, injuries, box scores | Free, no documented limit |
| The Odds API | Live odds | Free tier: 500 requests/month. ~1 request per daily run. |
| pybaseball | Team season stats (scrapes FanGraphs) | Scraper — `time.sleep(2)` between calls |
| baseball-reference | Historical game results (backfill only) | Scraper — `time.sleep(3)` between calls |

---

## Still Not Built

| Module | What it needs to do |
|--------|---------------------|
| `llm/` | Claude API for natural-language pick explanations stored in `recommendations.llm_explanation` |
| `api/` | FastAPI endpoints: `GET /slate?date=`, `GET /game/{id}`, `POST /chat` |
| `features/` | Rolling team stats pipeline (L7/L14 ERA, OPS) to replace season-level averages |
| `eval/roi.py` | ROI backtest with unit staking |
| `eval/clv.py` | Closing Line Value analysis (requires closing odds) |
| `eval/coverage.py` | No-bet rate tracking |
| `eval/edge_dist.py` | Edge distribution histogram |
| Deduplication rule | One pick per game across markets (top 5 can currently show same game twice) |
