# LaunchAgent Gap — Known Flaw & Fix Plan

## The Problem

The daily pipeline (`capture_odds.sh` → `capture_odds.py`) runs via a macOS LaunchAgent at 9am. LaunchAgents do **not** catch up on missed runs — if the machine is asleep or off at the scheduled time, the job silently skips.

Confirmed gap: the job last ran April 7, 2026. Fifteen days of ingest (Apr 8–21) were missed, resulting in:

- Games on Apr 8 and Apr 10 stuck as `status='scheduled'` (never updated to 'final')
- `pitcher_game_logs` and `team_batting_logs` missing for those games
- `team_stats_mlb` (L7 rolling stats) missing for Apr 11–14 and Apr 16–21
- Predictions and recommendations for those dates not run

There is also a secondary L7 bug: `capture_odds.py` calls `mlb_stats.main(today_str)` which only writes L7 stats for the current date. If the job runs daily this is fine. But during any multi-day gap, intermediate dates never get L7 rows — even if the script is later run manually to catch up.

## Root Cause

macOS LaunchAgents are tied to the local machine's uptime. A `StartCalendarInterval` job that fires at 9am will not re-fire if the machine was asleep at that time. There is no built-in catch-up mechanism.

## Fix Plan (post-containerization)

Deploy as a containerized service (Docker + cloud scheduler or Kubernetes CronJob). A cloud-based scheduler fires regardless of machine state and can be configured to retry on failure.

When containerized:
- Replace the LaunchAgent with a scheduled job (e.g. `0 10 * * *` cron in the container)
- The existing `run_ingest.py` backfill logic already handles multi-day gaps — it just needs to run reliably
- Fix the L7 gap: during backfill, call `mlb_stats.backfill_l7_stats(start_date, end_date)` instead of `mlb_stats.main(end_date)` (this fix is already applied to `run_ingest.py`)

## Current Workaround

Run the daily script manually when gaps are detected:

```bash
./run_daily.sh              # catches up from last ingested date through today
```

Or directly trigger a backfill:

```bash
source .venv/bin/activate
python ingest/run_ingest.py   # auto-detects and backfills missing dates
```

Note: `run_ingest.py` detects gaps by `MAX(game_date)` in the games table. It will not detect games stuck as 'scheduled' from past dates — those require a manual re-fetch:

```bash
python -c "
from ingest import mlb_games, mlb_boxscores, mlb_stats
mlb_games.main('2026-04-08', '2026-04-22')      # refresh statuses
mlb_boxscores.main('2026-04-08', '2026-04-22')  # fetch missing box scores
mlb_stats.backfill_l7_stats('2026-04-08', '2026-04-22')  # fill L7 gaps
"
```
