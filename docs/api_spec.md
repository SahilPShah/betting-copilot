# API Spec

*Created: 2026-04-24*

## Purpose

Define the actual HTTP API for this repository as it exists today.

This spec is grounded in:

- the live schema in `db/migrations/`
- the current pipeline outputs in `models/predict.py` and `recs/run_recs.py`
- the current recommendation/rules logic in `recs/`

This spec intentionally ignores deployment concerns.

## Scope

Primary goals:

- expose the latest daily slate
- expose a specific date's slate
- expose a single game's current detail
- expose historical pick results
- expose health/status for local operation

Non-goals for v1:

- authentication
- websocket updates
- write APIs beyond a local pipeline trigger
- chat endpoints

## Source Of Truth

Use code and schema as the source of truth, not older docs.

Important current facts:

- `models/predict.py` loads `v4_elo_logreg_l7` by default and falls back to `v3`
- `recommendations.context_snapshot` stores only:
  - `model_prob`
  - `implied_prob`
  - `edge`
  - `bookmaker`
  - `american_odds`
  - `home_starter`
  - `away_starter`
- `predicted_margin`, `elo_diff`, and `run_line_point` are not stored in `context_snapshot`
- `recommendations` can accumulate duplicate logical rows across reruns for the same `slate_run_id`
- `team_stats_mlb` now stores L7 rolling stats, not season stats
- season stats live in `team_season_stats`

## API Style

- Framework: FastAPI
- DB access: sync SQLAlchemy engine with raw SQL via `text()`
- Response format: JSON
- Date format: `YYYY-MM-DD`
- IDs:
  - `game_id` format: `{YYYY-MM-DD}-{HOME_ABBR}-{AWAY_ABBR}-{GAME_NUM}`
  - UUIDs remain internal unless useful for debugging

## Implementation Constraints

### Environment

Before any API implementation work, remove hardcoded `.env` paths across the execution path used by the API. It is not enough to fix only `db/session.py`.

At minimum, the following modules must stop loading `/Users/sahilshah/betting-copilot/.env` directly:

- `db/session.py`
- `ingest/run_ingest.py`
- `models/predict.py`
- `recs/run_recs.py`

Prefer `load_dotenv()` plus repo-root working-directory discipline, or centralize engine/config setup.

### Recommendation Deduplication

The API must not assume one logical recommendation row per `(run_date, game_id, market, side)`.

Current write behavior:

- `slate_runs` upserts by `run_date`
- `recommendations` always inserts
- rerunning the same date can create duplicate logical recommendations under the same `slate_run_id`

Therefore every read path that returns recommendations must dedupe.

Recommended dedupe rule:

- partition by `(slate_run_id, game_id, market, side)`
- keep the newest row by `created_at DESC, rec_id DESC`

This should be implemented in SQL with a window function or `DISTINCT ON`.

### Team Data Model

To build rich responses:

- use `team_season_stats` for season ERA/WHIP/K9/OPS/win%/runs
- use `team_stats_mlb` for L7 win%, L7 runs, L7 run diff, L7 games
- do not read season stats from `team_stats_mlb`

### Run Line Settlement

For history endpoints, settle run-line bets using the stored `run_line_point` from the joined odds snapshot or latest selected odds row.

Do not assume all run lines are `±1.5`, even though prediction logic currently uses a standard `1.5` for cover probabilities.

## Routes

### `GET /health`

Basic process and database health.

#### Response

```json
{
  "status": "ok",
  "database": "ok",
  "today": "2026-04-24",
  "latest_game_date": "2026-04-24",
  "latest_slate_date": "2026-04-24",
  "latest_model_version": "v4_elo_logreg_l7"
}
```

#### Implementation

Run lightweight queries only:

- `SELECT 1`
- `SELECT MAX(game_date) FROM games`
- `SELECT run_date, model_version FROM slate_runs ORDER BY run_date DESC LIMIT 1`

#### Failure Behavior

- DB unavailable: return `503`
- DB reachable but tables empty: return `200` with nullable dates

### `GET /slate`

Return the latest slate for today by default.

#### Query Params

- `date` optional, `YYYY-MM-DD`
- `include_no_bets` optional, default `false`

If `date` is omitted, use server local date.

### `GET /slate/{date}`

Alias for `GET /slate?date={date}`.

#### Response Shape

```json
{
  "run_date": "2026-04-24",
  "model_version": "v4_elo_logreg_l7",
  "games_count": 15,
  "picks_count": 3,
  "ran_at": "2026-04-24T13:02:14Z",
  "picks": [
    {
      "game_id": "2026-04-24-NYY-BOS-1",
      "game_date": "2026-04-24",
      "home_team": "NYY",
      "away_team": "BOS",
      "market": "moneyline",
      "side": "home",
      "decision": "medium",
      "confidence": 7.3,
      "edge": 0.067,
      "model_prob": 0.582,
      "implied_prob": 0.516,
      "american_odds": -115,
      "bookmaker": "draftkings",
      "predicted_margin": 1.2,
      "elo_diff": 54.0,
      "home_score": null,
      "away_score": null,
      "status": "scheduled",
      "starters": {
        "home": {
          "name": "Luis Gil",
          "era": 3.41,
          "whip": 1.18,
          "l3_era": 2.89
        },
        "away": {
          "name": "Tanner Houck",
          "era": 3.75,
          "whip": 1.24,
          "l3_era": 3.45
        }
      },
      "llm_explanation": "..."
    }
  ],
  "no_bets": []
}
```

#### Inclusion Rules

- `picks` contains rows where `decision != 'no_bet'`
- `no_bets` is omitted unless `include_no_bets=true`

#### Data Sources

Use:

- `slate_runs`
- deduped `recommendations`
- `games`
- `predictions`

Primary data source for recommendation-specific fields:

- `recommendations`
- `context_snapshot`

Primary data source for prediction-specific fields missing from `context_snapshot`:

- `predictions.predicted_margin`
- `predictions.elo_diff`

Primary data source for teams/date/status:

- `games`

#### Query Strategy

Use two queries.

Query 1:

- fetch the `slate_run_id`, `run_date`, `model_version`, `games_count`, `picks_count`, `ran_at` for the requested date

Query 2:

- fetch all deduped recommendation rows for that `slate_run_id`
- join `games`
- left join `predictions`

Recommended SQL shape:

```sql
WITH deduped AS (
  SELECT *
  FROM (
    SELECT
      r.*,
      ROW_NUMBER() OVER (
        PARTITION BY r.slate_run_id, r.game_id, r.market, r.side
        ORDER BY r.created_at DESC, r.rec_id DESC
      ) AS rn
    FROM recommendations r
    WHERE r.slate_run_id = :slate_run_id
  ) x
  WHERE x.rn = 1
)
SELECT
  d.*,
  g.game_date,
  g.home_team_id,
  g.away_team_id,
  g.status,
  g.home_score,
  g.away_score,
  p.predicted_margin,
  p.elo_diff
FROM deduped d
JOIN games g ON g.game_id = d.game_id
LEFT JOIN predictions p ON p.game_id = d.game_id
ORDER BY d.decision DESC, d.confidence DESC, d.edge DESC;
```

#### Notes

- Parse starter data from `context_snapshot`
- Do not recompute starter ERA in the request path for `/slate`
- If no `slate_runs` row exists for the date, return `404`

### `GET /game/{game_id}`

Return current detail for a single game.

This route is game-centric, not recommendation-centric.

#### Response Shape

```json
{
  "game_id": "2026-04-24-NYY-BOS-1",
  "game_date": "2026-04-24",
  "status": "scheduled",
  "home_team": "NYY",
  "away_team": "BOS",
  "scores": {
    "home": null,
    "away": null
  },
  "prediction": {
    "model_version": "v4_elo_logreg_l7",
    "home_win_prob": 0.582,
    "away_win_prob": 0.418,
    "predicted_margin": 1.2,
    "home_cover_prob": 0.54,
    "away_cover_prob": 0.46,
    "elo_diff": 54.0
  },
  "odds": [
    {
      "market": "moneyline",
      "side": "home",
      "american_odds": -115,
      "implied_prob": 0.516,
      "bookmaker": "draftkings",
      "captured_at": "2026-04-24T12:58:11Z",
      "run_line_point": null
    }
  ],
  "starters": {
    "home": {
      "name": "Luis Gil",
      "era": 3.41,
      "whip": 1.18,
      "l3_era": 2.89
    },
    "away": {
      "name": "Tanner Houck",
      "era": 3.75,
      "whip": 1.24,
      "l3_era": 3.45
    }
  },
  "injuries": [
    {
      "team_id": "NYY",
      "player_name": "Player Name",
      "status": "questionable"
    }
  ],
  "recommendation": {
    "run_date": "2026-04-24",
    "market": "moneyline",
    "side": "home",
    "decision": "medium",
    "confidence": 7.3,
    "edge": 0.067,
    "llm_explanation": "..."
  }
}
```

#### Behavior

- Always return game metadata if the game exists
- `prediction`, `odds`, `injuries`, `recommendation`, and starter metrics are nullable/optional sections

#### Data Sources

- `games`
- `predictions`
- latest `odds_snapshots` by `(game_id, market, side)`
- `game_starters`
- `injury_statuses`
- latest deduped recommendation for the most recent slate that includes this game

#### Starter Handling

Use this precedence:

1. if a latest recommendation exists and `context_snapshot` contains starter blobs, use those values
2. otherwise return names from `game_starters`
3. if the implementation wants richer non-recommendation starter metrics later, add an explicit service layer that calls `compute_starter_eras()`

Do not silently claim starter ERA is available when the route only has `game_starters`.

#### Query Strategy

Use up to five focused queries:

1. game row
2. prediction row
3. latest odds rows via `DISTINCT ON (market, side)`
4. starter names + injury rows
5. latest recommendation for that game from the most recent relevant `slate_run_id`

Recommended recommendation query:

```sql
WITH latest_run AS (
  SELECT s.slate_run_id, s.run_date
  FROM slate_runs s
  JOIN recommendations r ON r.slate_run_id = s.slate_run_id
  WHERE r.game_id = :game_id
  ORDER BY s.run_date DESC, s.ran_at DESC
  LIMIT 1
),
deduped AS (
  SELECT *
  FROM (
    SELECT
      r.*,
      ROW_NUMBER() OVER (
        PARTITION BY r.slate_run_id, r.game_id, r.market, r.side
        ORDER BY r.created_at DESC, r.rec_id DESC
      ) AS rn
    FROM recommendations r
    JOIN latest_run lr ON lr.slate_run_id = r.slate_run_id
    WHERE r.game_id = :game_id
  ) x
  WHERE x.rn = 1
)
SELECT * FROM deduped
ORDER BY confidence DESC, edge DESC
LIMIT 1;
```

#### Error Behavior

- unknown `game_id`: `404`

### `GET /history`

Return settled and unsettled recommendation history.

This is recommendation history, not model-prediction history.

#### Query Params

- `start_date` optional, default `today - 30 days`
- `end_date` optional, default `today`
- `decision` optional: `small|medium|no_bet`
- `market` optional: `moneyline|run_line`
- `page` optional, default `1`
- `per_page` optional, default `50`, max `200`
- `include_no_bets` optional, default `false`

#### Response Shape

```json
{
  "start_date": "2026-03-25",
  "end_date": "2026-04-24",
  "page": 1,
  "per_page": 50,
  "total": 42,
  "summary": {
    "wins": 18,
    "losses": 14,
    "pushes": 1,
    "pending": 9,
    "win_rate": 0.545
  },
  "items": [
    {
      "run_date": "2026-04-22",
      "game_id": "2026-04-22-NYY-BOS-1",
      "market": "moneyline",
      "side": "home",
      "decision": "medium",
      "edge": 0.067,
      "confidence": 7.3,
      "american_odds": -115,
      "run_line_point": null,
      "home_score": 5,
      "away_score": 3,
      "result": "win"
    }
  ]
}
```

#### Inclusion Rules

Default behavior:

- include only rows where `decision != 'no_bet'`

If `include_no_bets=true`:

- include all deduped recommendation rows

#### Settlement Rules

Moneyline:

- side `home` wins if `home_score > away_score`
- side `away` wins if `away_score > home_score`

Run line:

- use the stored `run_line_point`
- if the side is `home`, settle `home_score + run_line_point` vs `away_score`
- if the side is `away`, settle `away_score + run_line_point` vs `home_score`
- equal adjusted scores = `push`

Pending:

- any game where `games.status != 'final'`
- any row missing scores

#### Query Strategy

Use one base CTE for deduped recommendations joined to `slate_runs`, `games`, and `odds_snapshots`.

Important:

- use `recommendations.odds_snapshot_id` to recover the exact booked line and odds for that recommendation
- do not infer the line from latest odds

Recommended SQL shape:

```sql
WITH deduped AS (
  SELECT *
  FROM (
    SELECT
      r.*,
      s.run_date,
      ROW_NUMBER() OVER (
        PARTITION BY r.slate_run_id, r.game_id, r.market, r.side
        ORDER BY r.created_at DESC, r.rec_id DESC
      ) AS rn
    FROM recommendations r
    JOIN slate_runs s ON s.slate_run_id = r.slate_run_id
    WHERE s.run_date BETWEEN :start_date AND :end_date
  ) x
  WHERE x.rn = 1
)
SELECT
  d.*,
  g.home_score,
  g.away_score,
  g.status,
  o.american_odds,
  o.run_line_point
FROM deduped d
JOIN games g ON g.game_id = d.game_id
LEFT JOIN odds_snapshots o ON o.snapshot_id = d.odds_snapshot_id;
```

Settlement can be done in Python after fetching rows.

#### Notes

- `win_rate` should exclude `push` and `pending`
- if pagination is applied, compute summary from the full filtered result set, not just the current page

### `POST /run-pipeline/{date}`

Trigger the local daily pipeline for one date.

This route is operational and should be considered local-only.

#### Request

No body required.

#### Response

```json
{
  "status": "started",
  "date": "2026-04-24"
}
```

#### Execution Model

Use `BackgroundTasks` or an internal task runner.

Recommended execution sequence:

1. `ingest/run_ingest.py --date {date}`
2. `models/predict.py --date {date}`
3. `recs/run_recs.py --date {date}`

#### Implementation Details

- call the current Python interpreter with `sys.executable`
- set `cwd` to repo root
- capture stdout/stderr to logs
- fail the task if any step returns non-zero

Recommended subprocess form:

```python
subprocess.run(
    [sys.executable, "ingest/run_ingest.py", "--date", date_str],
    cwd=repo_root,
    check=True,
)
```

Do not depend on `source .venv/bin/activate` inside the API process. If the API itself is running from the project virtualenv, `sys.executable` is enough.

#### Concurrency Guard

The route must reject concurrent runs for the same date.

Minimum guard:

- in-process lock keyed by `date`

Better guard:

- DB-backed run lock table or advisory lock

#### Response Codes

- invalid date: `422`
- accepted: `202`
- already running: `409`

## Internal Modules

Recommended module layout:

```text
api/
├── main.py
├── deps.py
├── schemas.py
├── services/
│   ├── slate.py
│   ├── game.py
│   ├── history.py
│   └── pipeline.py
└── routes/
    ├── health.py
    ├── slate.py
    ├── game.py
    ├── history.py
    └── pipeline.py
```

Use service modules for SQL and row-shaping so route handlers stay thin.

## Schemas

Recommended Pydantic models:

- `HealthResponse`
- `StarterSnapshot`
- `SlatePick`
- `SlateResponse`
- `GamePrediction`
- `GameOddsRow`
- `GameInjuryRow`
- `GameRecommendation`
- `GameDetailResponse`
- `HistoryItem`
- `HistorySummary`
- `HistoryResponse`
- `PipelineTriggerResponse`

## Validation Rules

- dates must parse as ISO `YYYY-MM-DD`
- `game_id` is opaque at the HTTP boundary
- `per_page` max `200`
- unknown enum values should return `422`

## Error Model

Use a simple error shape:

```json
{
  "detail": "Slate not found for 2026-04-24"
}
```

Suggested status codes:

- `404` missing game/slate
- `409` duplicate pipeline run in progress
- `422` invalid params
- `503` DB unavailable

## Performance Notes

- `/slate` and `/history` should do SQL-side deduplication
- index usage already exists for `games`, `recommendations`, and `odds_snapshots`
- if `/history` becomes slow, add an index on `slate_runs(run_date)` and possibly a composite index for recommendation dedupe patterns

## Implementation Order

1. remove hardcoded `.env` loading in the API execution path
2. add API app/bootstrap
3. implement shared dedupe SQL helpers
4. implement `GET /health`
5. implement `GET /slate` and `GET /slate/{date}`
6. implement `GET /game/{game_id}`
7. implement `GET /history`
8. implement `POST /run-pipeline/{date}`

## Testing Checklist

- `/health` with DB up and down
- `/slate` on a date with picks
- `/slate` on a date with only no-bets
- `/slate` on a missing date
- `/slate` after rerunning the same date twice and confirming dedupe
- `/game/{game_id}` for a scheduled game with prediction but no recommendation
- `/game/{game_id}` for a game with recommendation and `llm_explanation`
- `/history` settlement for moneyline win/loss
- `/history` settlement for run-line win/loss/push using stored `run_line_point`
- `/run-pipeline/{date}` accepted, invalid date, and already-running behavior

