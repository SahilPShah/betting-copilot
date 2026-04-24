-- Migration v6: team batting logs for L7 OPS computation
-- Per-game team batting stats extracted from box scores.
-- One row per (game_id, team_id). Used to compute rolling L7 OPS.

CREATE TABLE IF NOT EXISTS team_batting_logs (
    log_id          SERIAL PRIMARY KEY,
    game_id         VARCHAR(40) NOT NULL REFERENCES games(game_id),
    game_date       DATE NOT NULL,
    season          INTEGER NOT NULL,
    team_id         VARCHAR(10) NOT NULL REFERENCES teams(team_id),
    at_bats         INTEGER NOT NULL DEFAULT 0,
    hits            INTEGER NOT NULL DEFAULT 0,
    doubles         INTEGER NOT NULL DEFAULT 0,
    triples         INTEGER NOT NULL DEFAULT 0,
    home_runs       INTEGER NOT NULL DEFAULT 0,
    walks           INTEGER NOT NULL DEFAULT 0,
    strikeouts      INTEGER NOT NULL DEFAULT 0,
    runs_scored     INTEGER NOT NULL DEFAULT 0,
    captured_at     TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (game_id, team_id)
);

CREATE INDEX IF NOT EXISTS idx_batting_logs_team_date ON team_batting_logs(team_id, game_date);
CREATE INDEX IF NOT EXISTS idx_batting_logs_date ON team_batting_logs(game_date);
