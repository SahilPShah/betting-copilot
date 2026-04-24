-- Migration v2: per-game starting pitcher data
-- Run after v1_init.sql

CREATE TABLE game_starters (
  starter_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  game_id      VARCHAR(20) REFERENCES games(game_id),
  side         VARCHAR(10) NOT NULL,   -- 'home' | 'away'
  starter_name VARCHAR(100),
  starter_era  NUMERIC(5,2),
  starter_whip NUMERIC(5,3),
  starter_k9   NUMERIC(5,2),
  starter_gs   INTEGER,               -- games started that season (data quality signal)
  captured_at  TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE(game_id, side)
);

CREATE INDEX idx_starters_game_id ON game_starters(game_id);
