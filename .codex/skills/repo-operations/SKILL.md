# Repo Operations

Use this skill when working on the betting-copilot repository and you need the canonical local execution flow.

## Goal

Run, inspect, and validate the MLB prediction pipeline with deterministic repository-specific commands.

## Environment Setup

Activate the repository virtual environment before project commands:

```bash
source .venv/bin/activate
```

Required environment variables:

- `DATABASE_URL`
- `ANTHROPIC_API_KEY`
- `ODDS_API_KEY`

Default local database:

```bash
psql postgresql://admin:password@localhost:5432/betting_copilot
```

## Standard Commands

Daily pipeline:

```bash
./run_daily.sh
./run_daily.sh 2026-04-01
```

Individual stages:

```bash
python ingest/run_ingest.py --date 2026-03-27
python models/predict.py --date 2026-03-27
python recs/run_recs.py --date 2026-03-27
```

Training:

```bash
python models/train.py
```

Evaluation:

```bash
python eval/brier.py
python eval/calibration.py
python eval/clv.py
python eval/roi.py
```

Backfills:

```bash
python historical/pull_games.py
python historical/pull_odds.py
python historical/pull_stats.py
python historical/pull_starters.py
python historical/pull_boxscores.py --start-date 2025-07-01 --end-date 2025-09-28
```

Operational ingestion and support commands commonly used in this repo:

```bash
python ingest/mlb_stats.py --seed
python ingest/mlb_stats.py --backfill --backfill-start 2026-03-25 --date 2026-04-03
python ingest/mlb_boxscores.py --start-date 2026-03-25 --end-date 2026-04-01
python ingest/capture_odds.py
```

## Data Model Constraints

- Use `team_stats_mlb` for rolling team stats.
- `game_starters` stores starter names only.
- `pitcher_game_logs` contains starter-only rows.
- `games.game_pk` is the external integer game identifier.
- Scripts are designed to be idempotent through `ON CONFLICT DO UPDATE`.

## Prediction Constraints

- Current primary model: `v3_elo_logreg_starters`
- `models/predict.py` falls back to v2 if needed.
- Prefer recent-form features when the minimum sample size is present.
- Convert `NaN` values to `None` before JSON serialization.
- `statsapi.schedule()` should be called without `hydrate`.

## When Using This Skill

- Start with the narrowest command that can verify the change.
- Prefer repo-local binaries from `.venv/bin/` when direct invocation is clearer.
- Keep reruns safe and incremental because the daily pipeline is idempotent.
- Do not rename database tables or identifiers to match schema filenames when the live system uses different names.
