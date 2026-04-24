-- Migration v5: Split team_stats_mlb into season stats and L7 rolling stats
--
-- IMPORTANT: Run ingest/mlb_stats.py seed_season_stats() BEFORE applying this migration.
-- That seeds team_season_stats from the current team_stats_mlb data before those columns are dropped.

-- 1. Create team_season_stats: one row per team per season (season-level baseline)
CREATE TABLE team_season_stats (
    stat_id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    team_id             VARCHAR(10) NOT NULL REFERENCES teams(team_id),
    season              INTEGER NOT NULL,
    team_pitching_era   NUMERIC(5,2),
    team_pitching_whip  NUMERIC(5,3),
    team_pitching_k9    NUMERIC(5,2),
    team_ops            NUMERIC(6,3),
    team_win_pct        NUMERIC(5,3),
    runs_scored_avg     NUMERIC(5,2),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(team_id, season)
);

CREATE INDEX idx_season_stats_team ON team_season_stats(team_id, season);

-- 2. Restructure team_stats_mlb: drop season columns, add L7 rolling columns
ALTER TABLE team_stats_mlb
    DROP COLUMN IF EXISTS team_pitching_era,
    DROP COLUMN IF EXISTS team_pitching_whip,
    DROP COLUMN IF EXISTS team_pitching_k9,
    DROP COLUMN IF EXISTS team_ops_l14,
    DROP COLUMN IF EXISTS team_era_l14,
    DROP COLUMN IF EXISTS team_win_pct,
    DROP COLUMN IF EXISTS runs_scored_avg,
    ADD COLUMN l7_win_pct           NUMERIC(5,3),
    ADD COLUMN l7_runs_scored_avg   NUMERIC(5,2),
    ADD COLUMN l7_runs_allowed_avg  NUMERIC(5,2),
    ADD COLUMN l7_run_diff_avg      NUMERIC(5,2),
    ADD COLUMN l7_games             INTEGER;

-- 3. Clear all existing season-snapshot rows (backfill done by ingest/mlb_stats.py)
TRUNCATE TABLE team_stats_mlb;
