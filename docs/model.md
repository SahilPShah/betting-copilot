# Model Training — MLB Betting Co-Pilot

## Overview

The prediction model is a two-stage hybrid: an **ELO rating system** that tracks relative team strength over time, combined with a **calibrated logistic regression** that layers in game-level, rolling, and season-level features. The current production model is `v4_elo_logreg_l7`.

---

## Stage 1: ELO Ratings (`models/elo.py`)

ELO provides a continuously updated strength signal for each team that is immune to season-level averaging bias.

**Parameters (locked for v1):**

| Parameter | Value | Effect |
|-----------|-------|--------|
| K-factor (prior seasons) | 10 | Controls how fast ratings move. Lower = more stable. |
| K-factor (current season) | 20 | More reactive to current-season results. |
| Divisor | 800 | Scales the rating gap into a probability. |
| Home field advantage | +35 ELO points | Applied to home team before computing win probability. |
| Season reset factor | 0.75 | Regresses ratings 25% toward 1500 each new season. |
| Recency decay half-life | 365 days | Older games have slightly less influence. |
| Starting rating | 1500 | All teams begin equal at the start of the first season. |

**How it works:**
1. All completed games are loaded in chronological order (2023 → present).
2. For each game, the pre-game expected win probability is computed from the current ratings.
3. After the game, ratings are updated: the winner gains points, the loser loses the same amount. Magnitude depends on how surprising the result was.
4. Ratings carry across seasons with a partial reset (75% of deviation from 1500 is retained).

The ELO system produces two outputs used downstream:
- `elo_diff` — the pre-game rating gap (home ELO − away ELO), used as a feature
- Final ratings for all teams — used at inference time to reflect the most current team strength

---

## Stage 2: Feature Engineering

For each game, a feature vector is built by combining ELO history, season-level team stats, rolling L7 form stats, and game-level starter stats.

### Features

| Feature | Source | Direction | Time horizon |
|---------|--------|-----------|--------------|
| `elo_diff` | ELO system | Positive = home team stronger | Multi-season rolling |
| `era_diff` | `team_season_stats` | Away ERA − Home ERA. Positive = home pitching advantage | Full season |
| `whip_diff` | `team_season_stats` | Same direction as ERA | Full season |
| `k9_diff` | `team_season_stats` | Home K/9 − Away K/9. Positive = home strikeout advantage | Full season |
| `ops_diff` | `team_season_stats` | Home OPS − Away OPS. Positive = home batting advantage | Full season |
| `win_pct_diff` | `games` table (L7) | Home L7 win% − Away L7 win%. Falls back to season win% if <3 prior games | Last 7 games |
| `runs_diff` | `games` table (L7) | Home L7 runs/game − Away L7 runs/game. Falls back to season avg if <3 prior games | Last 7 games |
| `starter_era_diff` | `pitcher_game_logs` (rolling) | Away starter ERA − Home starter ERA. **Highest-weight feature.** | Last 5 starts (L3 preferred) |
| `starter_whip_diff` | `pitcher_game_logs` (rolling) | Same direction as starter ERA | Last 5 starts (L3 preferred) |
| `has_starter_data` | Boolean flag | 1.0 if both starters have ERA data, 0.0 if not | — |

**Why mix time horizons?** Each feature captures a different type of signal. Season-level ERA and WHIP measure stable pitching staff quality — these are slow-moving and not well-represented by 7-game samples. L7 win% and runs capture current form and hot/cold streaks, which season averages obscure. Game-level starter ERA reflects the specific matchup for today. These are complementary, not redundant, and the model learns appropriate weights for each.

**Training/inference consistency rule:** Each feature must use the same data source in training as it does at inference. `win_pct_diff` and `runs_diff` use L7 data in both training (computed from the `games` table via SQL window function) and inference (from `team_stats_mlb`). Season-level features use `team_season_stats` in both. This consistency is critical — a mismatch causes the scaler to receive out-of-distribution values at inference.

### L7 Form Stats: How They're Computed

**At training time** (`models/train.py` → `load_historical_l7_stats()`):
A SQL self-join on the `games` table computes each team's L7 win% and average runs scored for every training game, using only games completed strictly before that game's date within the same season.

```sql
-- Simplified: for each training game, for each team,
-- look at up to 7 most recent prior games in the same season
WHERE ag.game_date < tg.game_date  -- no data leakage
  AND ag.season = tg.season        -- season-scoped (mirrors inference)
  AND rn <= 7                      -- cap at last 7
```

**At inference time** (`models/predict.py`): Reads from `team_stats_mlb`, which is populated daily by `ingest/mlb_stats.py` using the same logic.

**Fallback rule:** If either team has fewer than 3 prior games in the season (opening week), season stats are used instead. This applies identically in training and inference.

### Starter ERA: Bayesian Shrinkage

Raw ERA from a small number of innings is extremely noisy. Starter ERA and WHIP are blended toward league average proportionally to innings pitched.

**Formula:**
```
blended_era = (raw_era × IP + 4.50 × 15) / (IP + 15)
```

| Constant | Value |
|----------|-------|
| `LEAGUE_AVG_ERA` | 4.50 |
| `LEAGUE_AVG_WHIP` | 1.30 |
| `PRIOR_IP` | 15.0 (at 15 IP, observed and prior are weighted equally) |

**ERA windows:** Full window = last 5 starts in the current season. L3 = last 3 starts, used when available as it captures more recent form. Requires ≥1 start; returns `None` otherwise.

**Why this matters:** `starter_era_diff` has the highest coefficient in the model. Without shrinkage, a single bad outing produces values far outside the model's training distribution, causing the logistic regression to saturate toward 0% or 100%. Shrinkage keeps all values in a realistic range.

---

## Stage 3: Model Training (`models/train.py`)

**Model:** `sklearn.linear_model.LogisticRegression` wrapped in `CalibratedClassifierCV`

**Training procedure:**
1. Load ELO history for 2023–2025 (chronological — no look-ahead bias in ELO itself).
2. Load season team stats from `team_season_stats`.
3. Compute historical L7 stats from `games` table via SQL window function.
4. Load starter ERA/WHIP from `game_starters` table (stored historical values).
5. Build feature matrix: one row per game, features as above, target = `home_won` (binary).
6. Fit `StandardScaler` to normalize features (required for logistic regression).
7. Fit `CalibratedClassifierCV(LogisticRegression(), cv=5, method='isotonic')` — 5-fold cross-validation with isotonic regression calibration ensures predicted probabilities match real-world frequencies.
8. Also train a `LinearRegression` run differential model using the same feature set and scaler.
9. Save scaler + both models + feature column list to `models/versions/{version}.pkl`.

**To retrain:**
```bash
source .venv/bin/activate
python models/train.py
```

---

## Stage 4: Inference (`models/predict.py`)

For each scheduled game on a given date:
1. Load current ELO ratings (same parameters as training).
2. Load season team stats, L7 rolling stats, and L7 OPS from batting logs.
3. Build feature vector using the same logic and fallbacks as training.
4. Scale features with the saved `StandardScaler`.
5. Win probability model outputs `home_win_prob` / `away_win_prob`.
6. Run differential model outputs `predicted_margin`.
7. Cover probabilities are derived from predicted margin + residual std using a normal distribution: `P(home covers -1.5) = P(margin > 1.5)`.

**To run:**
```bash
python models/predict.py --date 2026-04-22
```

---

## Learned Feature Weights (v4)

Mean coefficients over 5 CV folds. Positive = favors home team win.

```
starter_era_diff    +0.3683   ← away starter ERA much higher than home → home favored
ops_diff            +0.2504   ← home team has better batting
whip_diff           +0.1178   ← away team has higher WHIP
era_diff            +0.0622   ← away team has higher season ERA
has_starter_data    +0.0462   ← having starter data slightly improves home prediction
elo_diff            -0.0533   ← note: negative because elo_diff sign convention vs model
starter_whip_diff   -0.0247
win_pct_diff        -0.0148
k9_diff             -0.0101
runs_diff           +0.0090
(intercept)         +0.1219   ← small home field advantage baseline
```

**Scaler statistics** (training distribution — v4):

| Feature | Training mean | Training std |
|---------|--------------|-------------|
| `elo_diff` | −0.06 | 54.84 |
| `era_diff` | 0.00 | 0.73 |
| `whip_diff` | 0.00 | 0.13 |
| `k9_diff` | 0.00 | 0.77 |
| `ops_diff` | 0.00 | 0.05 |
| `win_pct_diff` | −0.01 | **0.309** |
| `runs_diff` | −0.02 | **1.840** |
| `starter_era_diff` | 0.01 | 1.86 |
| `starter_whip_diff` | 0.00 | 0.30 |
| `has_starter_data` | 0.97 | 0.16 |

`win_pct_diff` std is 0.309 (vs 0.119 in v3) — reflecting the wider variance of L7 form stats. This is intentional and correct: inference sends L7 values in this range, and now the scaler expects them.

---

## Evaluation (`eval/`)

### Brier Score (`eval/brier.py`)
Mean squared error between predicted probability and actual outcome. Lower is better. Random baseline = 0.25.

Time-series cross-validation splits (no look-ahead):
- Train on 2023 → Test on 2024
- Train on 2023+2024 → Test on 2025

### Calibration (`eval/calibration.py`)
Checks whether predicted probabilities match actual win rates by bucketing predictions into 10% bands and comparing predicted vs. actual.

### Closing Line Value (`eval/clv.py`)
Measures whether the model finds value the market later confirms. Requires v2+ (starter features) to be meaningful.

---

## Model Versioning

| Version | Key changes | Brier (in-sample) |
|---------|------------|-------------------|
| `v1_elo_logreg` | ELO + season team stats only | 0.244 |
| `v2_elo_logreg_starters` | Added starter ERA/WHIP features | 0.2333 |
| `v3_elo_logreg_starters` | Added L3 ERA, run differential model, cover probs | 0.2333 |
| `v4_elo_logreg_l7` | Fixed train/inference mismatch: `win_pct_diff` and `runs_diff` now use L7 rolling stats in training (computed from `games` table), matching inference behavior | 0.2340 |

**Why v4 Brier is nearly identical to v3:** The fix corrects a distribution mismatch that caused extreme predictions at inference time — it does not change in-sample accuracy because the training targets and most features are unchanged. The benefit shows up in prediction sanity (no more 6–93% win probabilities from L7 streak noise) rather than raw Brier improvement.

Model artifacts are stored in `models/versions/`. `models/predict.py` loads v4 by default and falls back to v3 if v4 is not present.
