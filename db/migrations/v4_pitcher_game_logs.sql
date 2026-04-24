-- Migration v4: pitcher game logs for dynamic ERA computation
-- Run after v1_init.sql, v2_game_starters.sql, v3_cover_prob.sql

-- Store the statsapi integer game PK so we can look up box scores
ALTER TABLE games ADD COLUMN IF NOT EXISTS game_pk INTEGER;
CREATE INDEX IF NOT EXISTS idx_games_game_pk ON games(game_pk);

-- Per-appearance pitcher lines from box scores
CREATE TABLE IF NOT EXISTS pitcher_game_logs (
    log_id          SERIAL PRIMARY KEY,
    game_id         VARCHAR(40) NOT NULL REFERENCES games(game_id),
    game_date       DATE NOT NULL,
    season          INTEGER NOT NULL,
    team_id         VARCHAR(10) NOT NULL,
    pitcher_name    VARCHAR(100) NOT NULL,
    player_id INTEGER,
    innings_pitched NUMERIC(6,3) NOT NULL DEFAULT 0,
    earned_runs     INTEGER NOT NULL DEFAULT 0,
    hits            INTEGER,
    walks           INTEGER,
    strikeouts      INTEGER,
    is_starter      BOOLEAN DEFAULT FALSE,
    captured_at     TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (game_id, pitcher_name)
);

CREATE INDEX IF NOT EXISTS idx_pitcher_logs_name_season ON pitcher_game_logs(pitcher_name, season);
CREATE INDEX IF NOT EXISTS idx_pitcher_logs_date ON pitcher_game_logs(game_date);
CREATE INDEX IF NOT EXISTS idx_pitcher_logs_season ON pitcher_game_logs(season);
