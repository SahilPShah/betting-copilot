-- v7: store elo_diff in predictions so downstream consumers (LLM explanations, API) can use it
ALTER TABLE predictions ADD COLUMN IF NOT EXISTS elo_diff FLOAT;
