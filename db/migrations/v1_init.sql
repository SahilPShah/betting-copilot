CREATE TABLE teams (
  team_id VARCHAR(10) PRIMARY KEY, -- e.g. 'CWS', 'CHC'
  full_name VARCHAR(100) NOT NULL,
  division VARCHAR(20), -- 'American' | 'National'
  created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE games (
  game_id VARCHAR(20) PRIMARY KEY, -- MLB Stats API game_pk
  game_date DATE NOT NULL,
  first_pitch_utc TIMESTAMPTZ,
  home_team_id VARCHAR(10) REFERENCES teams(team_id),
  away_team_id VARCHAR(10) REFERENCES teams(team_id),
  status VARCHAR(20) DEFAULT 'scheduled', -- scheduled|final
  home_score INTEGER,
  away_score INTEGER,
  created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_games_date ON games(game_date);


CREATE TABLE odds_snapshots (
  snapshot_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  game_id VARCHAR(20) REFERENCES games(game_id),
  bookmaker VARCHAR(50) NOT NULL, -- 'draftkings'
  market VARCHAR(20) NOT NULL, -- 'moneyline' | 'run_line'
  side VARCHAR(10) NOT NULL, -- 'home' | 'away'
  american_odds INTEGER NOT NULL,
  run_line_point NUMERIC(3,1), -- -1.5 or +1.5, null for moneyline
  implied_prob NUMERIC(6,4) NOT NULL, -- vig-removed
  captured_at TIMESTAMPTZ NOT NULL,
  is_closing BOOLEAN DEFAULT FALSE
);

CREATE INDEX idx_odds_game_id ON odds_snapshots(game_id);

CREATE INDEX idx_odds_captured ON odds_snapshots(captured_at);


CREATE TABLE injury_statuses (
  injury_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  game_id VARCHAR(20) REFERENCES games(game_id),
  player_name VARCHAR(100) NOT NULL,
  team_id VARCHAR(10) REFERENCES teams(team_id),
  status VARCHAR(30) NOT NULL, -- 'out' | 'questionable' | 'available'
  reason VARCHAR(200),
  impact_score NUMERIC(4,2), -- computed: minutes share of player
  captured_at TIMESTAMPTZ NOT NULL,
  source VARCHAR(50) DEFAULT 'mlb_statsapi'
);
CREATE INDEX idx_injury_game_id ON injury_statuses(game_id);


CREATE TABLE team_stats_snapshots (
  stat_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  team_id VARCHAR(10) REFERENCES teams(team_id),
  as_of_date DATE NOT NULL,
  starter_name VARCHAR(100),
  starter_era NUMERIC(5,2),
  starter_whip NUMERIC(5,3),
  starter_k9 NUMERIC(5,2),
  bullpen_era_l7 NUMERIC(5,2), -- bullpen ERA last 7 days
  team_ops_l14 NUMERIC(6,3), -- team OPS last 14 games
  team_era_l14 NUMERIC(5,2), -- team ERA last 14 games
  captured_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX idx_stats_team_date ON team_stats_snapshots(team_id, as_of_date);


CREATE TABLE predictions (
  prediction_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  game_id VARCHAR(20) UNIQUE REFERENCES games(game_id),
  model_version VARCHAR(20) NOT NULL,
  home_win_prob NUMERIC(6,4) NOT NULL,
  away_win_prob NUMERIC(6,4) NOT NULL,
  predicted_margin NUMERIC(6,2), -- positive = home wins
  predicted_total NUMERIC(6,2),
  created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE slate_runs (
  slate_run_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  run_date DATE NOT NULL UNIQUE,
  model_version VARCHAR(20) NOT NULL,
  games_count INTEGER,
  picks_count INTEGER,
  ran_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE recommendations (
  rec_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  slate_run_id UUID REFERENCES slate_runs(slate_run_id),
  prediction_id UUID REFERENCES predictions(prediction_id),
  odds_snapshot_id UUID REFERENCES odds_snapshots(snapshot_id),
  game_id VARCHAR(20) REFERENCES games(game_id),
  market VARCHAR(20) NOT NULL,
  side VARCHAR(10) NOT NULL,
  edge NUMERIC(6,4) NOT NULL,
  confidence NUMERIC(4,2) NOT NULL,
  decision VARCHAR(20) NOT NULL, -- 'no_bet'|'small'|'medium'|'large'
  no_bet_reason VARCHAR(50),
  context_snapshot JSONB, -- denormalized context at time of rec
  llm_explanation TEXT, -- formatted output from LLM
  created_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX idx_rec_game_id ON recommendations(game_id);
CREATE INDEX idx_rec_slate ON recommendations(slate_run_id);

ALTER TABLE games ADD COLUMN data_source VARCHAR(20) DEFAULT 'live';

