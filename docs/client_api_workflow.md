# Client API Workflow

*Created: 2026-04-24*

## Purpose

This document explains how a client app should call the API to achieve the same outcomes as the current local workflow.

Current workflow today:

- `./run_daily.sh`
  - generate predictions and recommendations for the current slate
- `refresh_stats.sh`
  - run on a daily schedule to refresh finalized game results and keep historical data usable for backtesting

This document maps those jobs to the proposed API surface in [api_spec.md](/Users/sahilshah/betting-copilot/docs/api_spec.md).

## Current Workflow Mapping

### Current Local Command: `./run_daily.sh`

Current behavior:

1. ingest inputs for a date
2. generate predictions
3. generate recommendations
4. persist slate output

API equivalent:

1. `POST /pipelines/slate/{date}`
2. `GET /slate/{date}`

### Current Scheduled Command: `refresh_stats.sh`

Current behavior:

1. refresh recent game rows and statuses
2. fetch box scores for final games
3. refresh derived stats used later for backtesting and historical analysis

API equivalents:

- broad scheduled maintenance:
  - `POST /results/refresh`
- targeted repair for one game:
  - `POST /games/{game_id}/refresh-result`

## Recommended API Set

Command endpoints:

- `POST /pipelines/slate/{date}`
- `POST /predictions/{date}` optional modular endpoint
- `POST /recommendations/{date}` optional modular endpoint
- `POST /results/refresh`
- `POST /games/{game_id}/refresh-result`

Read endpoints:

- `GET /health`
- `GET /slate`
- `GET /slate/{date}`
- `GET /game/{game_id}`
- `GET /history`

## Workflow 1: Replace `run_daily.sh`

This is the main forward-looking client workflow.

### Minimal Sequence

1. Trigger the slate pipeline.
2. Read the resulting slate.

Example:

```bash
curl -X POST http://localhost:8000/pipelines/slate/2026-04-24
curl http://localhost:8000/slate/2026-04-24
```

### Expected Behavior

`POST /pipelines/slate/{date}` should internally:

1. refresh/ingest date-scoped inputs needed for that slate
2. run prediction generation for the date
3. run recommendation generation for the date

The client app should treat it as an asynchronous or accepted job trigger, even if the first implementation runs it inline.

After completion:

- `GET /slate/{date}` becomes the source of truth for display

### Client UX Pattern

Recommended client pattern:

1. call `POST /pipelines/slate/{date}`
2. if response is `202`, show “generating slate”
3. poll `GET /slate/{date}`
4. once available, render picks and no-bets

If the server later exposes job status explicitly, the client can switch to polling a job endpoint. That is optional for v1.

## Workflow 2: More Modular Slate Generation

Only use this if the client needs finer operational control.

### Sequence

1. `POST /predictions/{date}`
2. `POST /recommendations/{date}`
3. `GET /slate/{date}`

Example:

```bash
curl -X POST http://localhost:8000/predictions/2026-04-24
curl -X POST http://localhost:8000/recommendations/2026-04-24
curl http://localhost:8000/slate/2026-04-24
```

### When To Use This

Use the modular sequence only when the client needs one of these:

- rerun recommendations without rerunning prediction
- rerun prediction after data repair
- operational troubleshooting

For normal app usage, prefer `POST /pipelines/slate/{date}`.

## Workflow 3: Replace Scheduled `refresh_stats`

This is the backward-looking historical maintenance workflow.

The goal is not to generate a current slate. The goal is to keep historical game results and downstream stats correct so later backtests and history views are trustworthy.

### Recommended Scheduled Flow

1. call `POST /results/refresh`
2. optionally inspect `GET /health`
3. later use `GET /history` for backtesting/reporting

Example:

```bash
curl -X POST http://localhost:8000/results/refresh
curl http://localhost:8000/health
curl "http://localhost:8000/history?start_date=2026-03-27&end_date=2026-04-24"
```

### Expected Behavior

`POST /results/refresh` should internally do the same category of work as the current scheduled script:

1. refresh recent game schedule/status rows
2. ingest box scores for final games
3. recompute downstream rolling stats as needed

This endpoint is maintenance-oriented and is the API equivalent of your scheduled refresh job.

## Workflow 4: Repair One Game

Use this when one game looks stale or wrong.

Example:

```bash
curl -X POST http://localhost:8000/games/2026-04-10-NYY-BOS-1/refresh-result
curl http://localhost:8000/game/2026-04-10-NYY-BOS-1
```

### Expected Behavior

`POST /games/{game_id}/refresh-result` should:

1. resolve the game and its date
2. refresh that date's schedule data so the game row is corrected
3. ingest final box score data for that date
4. recompute affected rolling stats from that game date forward

The client should think of this as a repair command, not a manual result update.

## Workflow 5: Read Slate Data In The App

Once a slate exists, the client app should primarily read:

- `GET /slate`
- `GET /slate/{date}`

Recommended usage:

- home screen for today: `GET /slate`
- date picker / archive view: `GET /slate/{date}`

### Suggested UI Mapping

Use `GET /slate/{date}` to populate:

- slate header
  - run date
  - model version
  - games count
  - picks count
  - ran at
- picks list
- optional no-bets list

## Workflow 6: Read Single Game Detail

Use:

- `GET /game/{game_id}`

Recommended usage:

- pick detail screen
- debugging stale game state
- history drill-down

This route should be the app’s one-stop read for:

- game metadata
- scores/status
- prediction
- latest odds
- starters
- injuries
- latest recommendation if any

## Workflow 7: Read Historical Results

Use:

- `GET /history`

Recommended usage:

- performance screen
- backtesting-style summaries
- filtering by date range or market

Example:

```bash
curl "http://localhost:8000/history?start_date=2026-03-27&end_date=2026-04-24&market=moneyline"
```

The client should treat this endpoint as the historical reporting view built on top of:

- deduped recommendations
- exact stored odds snapshot used at recommendation time
- finalized game scores

## Recommended Client Modes

There are three client modes that mirror your current process.

### 1. Daily Slate Mode

Use when the user wants today’s recommendations.

Sequence:

1. `POST /pipelines/slate/{today}`
2. `GET /slate/{today}`

### 2. Scheduled Maintenance Mode

Use when the system is keeping history fresh in the background.

Sequence:

1. `POST /results/refresh`
2. optional health check

### 3. Repair Mode

Use when one historical game is missing or stale.

Sequence:

1. `POST /games/{game_id}/refresh-result`
2. `GET /game/{game_id}`
3. optionally inspect `GET /history`

## Why Not Expose Every Internal Script

The client app should not directly orchestrate low-level ingest modules.

Avoid exposing endpoints that correspond directly to:

- `mlb_games.py`
- `mlb_boxscores.py`
- `mlb_stats.py`
- `odds_api.py`
- `mlb_injuries.py`

Reason:

- they are implementation details
- they force the client to understand pipeline internals
- they make the app more brittle if the backend workflow changes

Instead, the client should call higher-level business operations:

- generate slate
- refresh results
- repair one game
- read slate
- read game
- read history

## Suggested Error Handling

### For Command Endpoints

Endpoints:

- `POST /pipelines/slate/{date}`
- `POST /results/refresh`
- `POST /games/{game_id}/refresh-result`
- `POST /predictions/{date}`
- `POST /recommendations/{date}`

Client behavior:

- `202`: request accepted, show pending state
- `409`: operation already running, show non-fatal “already in progress”
- `404`: invalid game/date context where applicable
- `422`: invalid input
- `503`: backend unavailable

### For Read Endpoints

Endpoints:

- `GET /slate/{date}`
- `GET /game/{game_id}`
- `GET /history`

Client behavior:

- `404` on missing slate/game should render empty state, not generic crash
- `503` should render temporary system-unavailable state

## Suggested Client Polling Behavior

For `POST /pipelines/slate/{date}`:

- poll `GET /slate/{date}` every few seconds until present
- stop after a reasonable timeout

For `POST /games/{game_id}/refresh-result`:

- poll `GET /game/{game_id}` until the status/scores update
- optionally requery history if the client is on a performance screen

For `POST /results/refresh`:

- no immediate polling required unless the client displays maintenance state

## Example End-To-End Usage

### Same Result As Current `run_daily.sh`

```bash
curl -X POST http://localhost:8000/pipelines/slate/2026-04-24
curl http://localhost:8000/slate/2026-04-24
```

### Same Result As Current Scheduled `refresh_stats`

```bash
curl -X POST http://localhost:8000/results/refresh
curl "http://localhost:8000/history?start_date=2026-03-27&end_date=2026-04-24"
```

### Repair One Stale Historical Game

```bash
curl -X POST http://localhost:8000/games/2026-04-10-NYY-BOS-1/refresh-result
curl http://localhost:8000/game/2026-04-10-NYY-BOS-1
```

## Recommended First Client Integration

If the client app only needs parity with your current workflow, start with this subset:

- `POST /pipelines/slate/{date}`
- `GET /slate/{date}`
- `POST /results/refresh`
- `GET /history`
- `GET /game/{game_id}`
- `GET /health`

Add these only if needed later:

- `POST /predictions/{date}`
- `POST /recommendations/{date}`
- `POST /games/{game_id}/refresh-result`

## Summary

Your current workflow becomes:

- `run_daily.sh`
  - `POST /pipelines/slate/{date}` then `GET /slate/{date}`
- scheduled `refresh_stats`
  - `POST /results/refresh`
- surgical historical fix
  - `POST /games/{game_id}/refresh-result`

That keeps the client app aligned with the way you already operate, while still allowing more modular APIs where they add real value.

