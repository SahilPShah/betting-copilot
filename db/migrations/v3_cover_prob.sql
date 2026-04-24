-- v3: Add cover probability columns to predictions table
-- These are populated by the run differential regression model

ALTER TABLE predictions ADD COLUMN IF NOT EXISTS home_cover_prob NUMERIC(6,4);
ALTER TABLE predictions ADD COLUMN IF NOT EXISTS away_cover_prob NUMERIC(6,4);
ALTER TABLE predictions ALTER COLUMN model_version TYPE VARCHAR(50);
