# AGENTS.md

Repository guidance for Codex CLI.

## Scope

These instructions apply to the entire repository.

## Project Overview

MLB game prediction and betting recommendation system. The pipeline predicts game winners with a hybrid ELO + calibrated logistic regression model, identifies positive expected value betting opportunities, and outputs top daily picks with edge, confidence, and probable starters.

## Environment

- Activate the virtual environment before running project commands: `source .venv/bin/activate`
- Required `.env` variables: `DATABASE_URL`, `ANTHROPIC_API_KEY`, `ODDS_API_KEY`
- Default local database: `postgresql://admin:password@localhost:5432/betting_copilot`
- Direct database access: `psql postgresql://admin:password@localhost:5432/betting_copilot`

## Primary Workflows

- Full daily pipeline for today: `./run_daily.sh`
- Full daily pipeline for a specific date: `./run_daily.sh 2026-04-01`
- Ingest only: `python ingest/run_ingest.py --date 2026-03-27`
- Predict only: `python models/predict.py --date 2026-03-27`
- Recommend only: `python recs/run_recs.py --date 2026-03-27`
- Retrain the model: `python models/train.py`

All pipeline stages are idempotent and safe to re-run.

## Model Notes

- Current model version: `v3_elo_logreg_starters`
- Main artifact path: `models/versions/v3_elo_logreg_starters.pkl`
- Feature columns:
  - `elo_diff`
  - `era_diff`
  - `whip_diff`
  - `k9_diff`
  - `ops_diff`
  - `win_pct_diff`
  - `runs_diff`
  - `starter_era_diff`
  - `starter_whip_diff`
  - `has_starter_data`
- `starter_era_diff` is the highest-weight feature.
- `models/predict.py` loads v3 and falls back to v2 automatically.
- Artifact payloads are pickle dictionaries with model objects, scaler, feature metadata, and ELO parameters.
- Full training detail lives in `docs/model.md`.

## Database Notes

- Use `team_stats_mlb` as the actual team stats table name. Do not use `team_stats_snapshots`.
- Migrations already applied to the live database:
  - `v1_init.sql`
  - `v2_game_starters.sql`
  - `v3_cover_prob.sql`
  - `v4_pitcher_game_logs.sql`
  - `v5_team_stats_split.sql`
  - `v6_team_batting_logs.sql`

Key tables:

- `teams`: 30 MLB franchises
- `games`: game records, scores, status, and external `game_pk`
- `game_starters`: probable starters by game and side
- `pitcher_game_logs`: starter-only pitching logs
- `team_batting_logs`: per-team batting logs by game
- `team_season_stats`: per-team season stats
- `team_stats_mlb`: rolling team stats by date
- `odds_snapshots`: point-in-time odds snapshots
- `predictions`: model output probabilities
- `recommendations`: rules-engine recommendations
- `slate_runs`: one record per daily run
- `injury_statuses`: injury reports

## Prediction Data Conventions

- `game_id` format: `{YYYY-MM-DD}-{HOME_ABBR}-{AWAY_ABBR}-{GAME_NUM}`
- `games.game_pk` is the external integer game identifier used for box score fetches.
- Team abbreviations:
  - `ARI`, `ATH`, `ATL`, `BAL`, `BOS`, `CHC`, `CHW`, `CIN`, `CLE`, `COL`
  - `DET`, `HOU`, `KCR`, `LAA`, `LAD`, `MIA`, `MIL`, `MIN`, `NYM`, `NYY`
  - `OAK`, `PHI`, `PIT`, `SDP`, `SFG`, `SEA`, `STL`, `TBR`, `TEX`, `TOR`, `WSN`
- All database writes use `ON CONFLICT DO UPDATE`.
- Use the `safe_float(val)` pattern in prediction code when stats may be `None` or `NaN`.
- `statsapi.schedule()` does not accept `hydrate`.
- Convert `NaN` to `None` before JSON serialization. Use `_clean_for_json` in `recs/run_recs.py`.

## Pitcher And Team Form Architecture

- ERA is computed dynamically at prediction time from `pitcher_game_logs`.
- `ingest/mlb_games.py` stores starter names in `game_starters` and `game_pk` in `games`.
- `ingest/mlb_boxscores.py` stores starter-only pitching lines and team batting stats.
- `ingest/mlb_boxscores.compute_starter_eras(names, date_str)` returns season and recent ERA and WHIP values.
- `ingest/mlb_boxscores.compute_l7_ops(team_ids, date_str)` computes trailing seven-game OPS.
- `models/predict.py` prefers recent-form starter and team metrics when enough history exists, otherwise it falls back to season stats.
- `recs/run_recs.py` surfaces recent starter ERA and team form in recommendation output.
- Relief pitcher data is intentionally excluded from `pitcher_game_logs`.

## Locked Parameters

- Prior-season ELO K-factor: `10`
- Current-season ELO K-factor: `20`
- ELO divisor: `800`
- Season reset factor: `0.75`
- Recency half-life: `365 days`
- Home-field advantage: `35 ELO points`
- Minimum moneyline edge: `3%`
- Minimum run line edge: `4%`
- Minimum confidence: `5.0/10`
- Maximum picks per slate: `5`
- ERA window: `last 5 starts`
- Minimum starts for ERA: `1`
- ERA shrinkage prior innings: `15`
- ERA league average prior: `4.50`
- WHIP league average prior: `1.30`

## Historical Backfill

One-time scripts under `historical/` are for initial seeding or re-backfills:

- `python historical/pull_games.py`
- `python historical/pull_odds.py`
- `python historical/pull_stats.py`
- `python historical/pull_starters.py`
- `python historical/pull_boxscores.py --start-date 2025-07-01 --end-date 2025-09-28`

`pull_boxscores.py` also fills `games.game_pk` when it is missing.

## Evaluation

- `python eval/brier.py`
- `python eval/calibration.py`
- `python eval/clv.py`

Evaluation outputs are written to `eval/results/`. The v3 model's documented in-sample metrics are Brier `0.2333` and accuracy `60.2%`.

## Current Gaps

- `llm/`: natural-language pick explanations
- `api/`: FastAPI endpoints such as `/slate`, `/game/{id}`, and `/chat`

## Codex Execution Notes

- Prefer the local virtual environment binaries for repeatable execution.
- Favor targeted commands over broad repository-wide operations.
- Treat the common commands in `.codex/skills/repo-operations/SKILL.md` as the approved workflow set for routine project work.
