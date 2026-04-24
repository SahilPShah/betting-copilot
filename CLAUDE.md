# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

MLB game prediction and betting recommendation system. Predicts game winners using a hybrid ELO + calibrated logistic regression model, identifies +EV betting opportunities, and outputs top picks per day with edge, confidence, and probable starters.

## Environment

```bash
source .venv/bin/activate   # always activate before running anything
```

Required `.env` variables: `DATABASE_URL`, `ANTHROPIC_API_KEY`, `ODDS_API_KEY`.

Database: `postgresql://admin:password@localhost:5432/betting_copilot`
Connect directly: `psql postgresql://admin:password@localhost:5432/betting_copilot`

## Daily Operation

```bash
./run_daily.sh              # full pipeline for today
./run_daily.sh 2026-04-01   # specific date
```

Runs in order: ingest → predict → recommend. All steps are idempotent (safe to re-run).

Individual steps:
```bash
python ingest/run_ingest.py --date 2026-03-27
python models/predict.py --date 2026-03-27
python recs/run_recs.py --date 2026-03-27
```

## Model Training

```bash
python models/train.py   # retrains and saves to models/versions/v3_elo_logreg_starters.pkl
```

Current model is `v3_elo_logreg_starters`. Features: `elo_diff`, `era_diff`, `whip_diff`, `k9_diff`, `ops_diff`, `win_pct_diff`, `runs_diff`, `starter_era_diff`, `starter_whip_diff`, `has_starter_data`. `starter_era_diff` is the highest-weight feature. See `docs/model.md` for full training details.

Model artifacts are pickle dicts: `{model, run_diff_model, scaler, feature_cols, version, k, k_current, divisor, season_regress, decay_half_life, run_diff_residual_std}`. `models/predict.py` loads v3 and falls back to v2 automatically.

## Database Tables

The actual table name for team stats is **`team_stats_mlb`** (not `team_stats_snapshots` — the schema file name is misleading). All scripts use `team_stats_mlb`.

| Table | Purpose |
|-------|---------|
| `teams` | 30 MLB franchises |
| `games` | All games with scores, status, and external `game_pk` integer |
| `game_starters` | Per-game probable pitcher names (UNIQUE on game_id+side). ERA/WHIP NOT stored here — computed at prediction time from `pitcher_game_logs` |
| `pitcher_game_logs` | One row per starter per game — IP, ER, H, BB, K. Starters only. Used to compute rolling ERA (L5 season, L3 recent form) |
| `team_batting_logs` | One row per team per game — AB, H, 2B, 3B, HR, BB, K, R. Used to compute L7 OPS (UNIQUE on game_id+team_id) |
| `team_season_stats` | One row per team per season — ERA, WHIP, K/9, OPS, win%, runs/game from pybaseball (UNIQUE on team_id+season) |
| `team_stats_mlb` | L7 rolling stats per team per date — win%, runs scored/allowed, run diff, game count (UNIQUE on team_id+as_of_date) |
| `odds_snapshots` | Point-in-time odds (is_closing=True for historical, False for live) |
| `predictions` | Model win probabilities + cover probabilities + `predicted_margin` + `elo_diff` per game (UNIQUE on game_id) |
| `recommendations` | Rules engine output with edge, confidence, decision, context_snapshot JSONB |
| `slate_runs` | One record per daily run (UNIQUE on run_date), parent of recommendations |
| `injury_statuses` | Player injury reports |

Migrations applied to live DB: `v1_init.sql`, `v2_game_starters.sql`, `v3_cover_prob.sql`, `v4_pitcher_game_logs.sql`, `v5_team_stats_split.sql`, `v6_team_batting_logs.sql`, `v7_predictions_elo_diff.sql`.

## Pitcher ERA Architecture

ERA is **not** stored at ingest time. It is computed dynamically at prediction time from `pitcher_game_logs`.

- `ingest/mlb_games.py` — stores only pitcher name in `game_starters`, plus `game_pk` (integer) in `games`
- `ingest/mlb_boxscores.py` — fetches box scores for final games, stores **starter-only** lines in `pitcher_game_logs` and team batting stats in `team_batting_logs`
- `ingest/mlb_boxscores.compute_starter_eras(names, date_str)` — returns `{era, whip, l3_era, l3_whip, season_era, season_whip}` per pitcher. Full window = last 5 starts; L3 = last 3 starts. Requires ≥ 2 starts or returns None.
- `ingest/mlb_boxscores.compute_l7_ops(team_ids, date_str)` — computes L7 OPS from `team_batting_logs` (last 7 games before date). Returns `{ops, games}` per team.
- `models/predict.py` uses L3 ERA for starter features when available; L7 OPS/win%/runs for team features when ≥3 games exist, else falls back to season stats from `team_season_stats`
- `recs/run_recs.py` shows L3 ERA in starter output and L7 form in reasoning

Relief pitcher data is intentionally not stored. `pitcher_game_logs` contains starters only (`is_starter = TRUE` always).

## Key Conventions

- `game_id` format: `{YYYY-MM-DD}-{HOME_ABBR}-{AWAY_ABBR}-{GAME_NUM}` e.g. `2026-03-27-NYY-BOS-1`
- `games.game_pk` — integer game identifier from the external schedule API, used to fetch box scores
- Team abbreviations: ARI, ATH, ATL, BAL, BOS, CHC, CHW, CIN, CLE, COL, DET, HOU, KCR, LAA, LAD, MIA, MIL, MIN, NYM, NYY, OAK, PHI, PIT, SDP, SFG, SEA, STL, TBR, TEX, TOR, WSN
- All DB writes use `ON CONFLICT DO UPDATE` — scripts are safe to re-run
- `safe_float(val)` pattern used in predict.py to handle None/NaN from early-season stats
- `statsapi.schedule()` does **not** accept a `hydrate` argument — probable pitcher fields are returned by default
- JSON serialization: NaN must be converted to None before `json.dumps` (use `_clean_for_json` helper in run_recs.py)
- Index and table names describe the data they store, not the source API (e.g. `game_pk` not `statsapi_game_pk`)

## Locked Parameters (v1)

| Parameter | Value |
|-----------|-------|
| ELO K-factor (prior seasons) | 10 |
| ELO K-factor (current season) | 20 |
| ELO divisor | 800 |
| Season reset factor | 0.75 |
| Recency decay half-life | 365 days |
| Home field advantage | 35 ELO points |
| Min edge — moneyline | 3% |
| Min edge — run line | 4% |
| Min confidence | 5.0/10 |
| Max picks per slate | 5 |
| ERA window | Last 5 starts (current season only) |
| Min starts for ERA | 1 |
| ERA shrinkage prior IP | 15 |
| ERA league average prior | 4.50 |
| WHIP league average prior | 1.30 |

## Historical Backfill Scripts

One-time scripts in `historical/` — only needed when seeding a new database or re-backfilling:
```bash
python historical/pull_games.py                                    # game results from baseball-reference
python historical/pull_odds.py                                     # historical closing odds
python historical/pull_stats.py                                    # season team stats via pybaseball
python historical/pull_starters.py                                 # probable pitchers (name only)
python historical/pull_boxscores.py --start-date 2025-07-01 --end-date 2025-09-28  # starter game logs
```

`pull_boxscores.py` backfills `pitcher_game_logs` and also populates `games.game_pk` for any games missing it.

## Evaluation

```bash
python eval/brier.py        # Brier score + time-series CV
python eval/calibration.py  # calibration curve
python eval/clv.py          # Closing Line Value backtest (valid only with v2+ model)
```

Results saved to `eval/results/`. v3 model: in-sample Brier 0.2333, accuracy 60.2%.

## What's Not Yet Built

- `llm/` — Claude API integration for natural-language pick explanations
- `api/` — FastAPI endpoints (`/slate`, `/game/{id}`, `/chat`)
