# System Guide — MLB Betting Co-Pilot

A plain-English explanation of every model, metric, and formula used in this system. No prior statistics knowledge required.

---

## Table of Contents

1. [How the System Works (Overview)](#1-how-the-system-works-overview)
2. [ELO Ratings](#2-elo-ratings)
3. [The Prediction Model](#3-the-prediction-model)
4. [Edge — Finding Value in the Market](#4-edge--finding-value-in-the-market)
5. [Confidence Score](#5-confidence-score)
6. [The Rules Engine — Selecting Picks](#6-the-rules-engine--selecting-picks)
7. [Evaluation Metrics](#7-evaluation-metrics)
   - [Brier Score](#brier-score)
   - [Calibration](#calibration)
   - [Closing Line Value (CLV)](#closing-line-value-clv)
   - [ROI Backtest](#roi-backtest)
   - [Coverage](#coverage)
   - [Edge Distribution](#edge-distribution)
8. [What the System Does Not Do](#8-what-the-system-does-not-do)

---

## 1. How the System Works (Overview)

The system runs in three stages every morning:

```
Ingest → Predict → Recommend
```

**Ingest** pulls today's schedule, probable pitchers, live odds, and team stats from external APIs and stores them in the database.

**Predict** runs the model: it takes ELO ratings + season stats + pitcher matchup and outputs a win probability for each team in each game.

**Recommend** compares model probabilities against bookmaker odds, computes edge and confidence, applies the rules engine, and outputs up to 5 picks for the day.

---

## 2. ELO Ratings

### What is ELO?

ELO is a rating system originally developed for chess. Every team has a numeric rating — higher means stronger. After each game, the winner takes points from the loser. The key insight is that **the amount of points transferred depends on how surprising the result was**.

- A heavy favorite beating a weak opponent: small point transfer (expected result)
- A big underdog pulling an upset: large point transfer (surprising result)

### How it works here

All teams start at **1500 points** at the beginning of the 2023 season. Games are processed in chronological order — 2023 through the present — so the ratings are always based on everything that has happened up to that point.

**Win probability formula:**

The expected win probability for the home team is:

```
P(home win) = 1 / (1 + 10^(-(home_elo - away_elo + HFA) / divisor))
```

Where:
- `home_elo - away_elo` = the rating gap between the two teams
- `HFA` = **+35 points** added to the home team's rating to account for home field advantage
- `divisor` = **800**, which controls how steeply the curve rises. A wider divisor means ratings have to differ more to create a meaningful probability gap.

**After each game, ratings update:**

```
new_home_elo = old_home_elo + K × (actual - expected)
```

Where:
- `actual` = 1 if home team won, 0 if they lost
- `expected` = the pre-game win probability computed above
- `K` = **20** for the current season, **10** for prior seasons (current season games matter more)

**Season reset:** At the start of each new season, ratings regress 75% of the way back toward 1500. A team with a 1600 rating going into the new season starts at 1575. This prevents old dominance from persisting forever.

**Why ELO?** It captures form and strength in a single number that updates continuously. It doesn't care about margins — a 1-run win counts the same as a 10-run blowout — which actually makes it more robust in baseball where run differential is noisy.

---

## 3. The Prediction Model

ELO alone gives a reasonable win probability, but we can do better by layering in additional signals. The system uses a **logistic regression** trained on top of ELO.

### Features

The model takes the following inputs for each game:

| Feature | What it measures |
|---------|-----------------|
| `elo_diff` | Home ELO − Away ELO. The core strength signal. |
| `era_diff` | Away team ERA − Home team ERA. Positive = home pitching advantage. Season average. |
| `whip_diff` | Away WHIP − Home WHIP. WHIP = (Walks + Hits) / Innings Pitched. Season average. |
| `k9_diff` | Home K/9 − Away K/9. Strikeouts per 9 innings. Season average. |
| `ops_diff` | Home OPS − Away OPS. OPS = On-base % + Slugging %. Season batting. |
| `win_pct_diff` | Home win% − Away win%. Current season record. |
| `runs_diff` | Home runs/game − Away runs/game. Offensive output. |
| `starter_era_diff` | Away starter ERA − Home starter ERA. **Computed from last 5 starts.** |
| `starter_whip_diff` | Away starter WHIP − Home starter WHIP. From last 5 starts. |
| `has_starter_data` | 1.0 if both pitchers were matched, 0.0 if falling back to season stats. |

**`starter_era_diff` is the highest-weight feature** because it reflects the specific pitcher for that game — the most important single factor in how bookmakers price individual games.

Starter ERA is computed fresh at prediction time from `pitcher_game_logs` (up to the last 5 starts from the current season only, minimum 1 start). It is not stored at ingest time.

#### Why ERA uses Bayesian shrinkage

A pitcher's raw ERA after one start is nearly meaningless. If a starter is knocked out in the 2nd inning after giving up 4 runs, their ERA is 18.00 — but that single outing tells you very little about how they'll pitch in their next start. Using that 18.00 directly in the model would push `starter_era_diff` far outside the range of values the model was trained on, causing predictions to saturate near 0% or 100%.

**The fix: blend toward league average based on innings pitched.**

Instead of using raw ERA, the system applies a formula that asks: *how much should we trust this pitcher's observed stats given how little data we have?* With 3 innings pitched, we don't trust the data much and lean on the league average (4.50 ERA). With 40 innings pitched, the observed ERA almost entirely speaks for itself.

```
blended_era = (observed_era × IP + 4.50 × 15) / (IP + 15)
```

The number 15 is the **prior weight** — it means we treat every pitcher as if they've already thrown 15 innings at league-average ERA before their season begins. As their real innings accumulate, the prior fades.

**Practical effect:**

| Scenario | Raw ERA | Blended ERA |
|----------|---------|-------------|
| 1 bad inning (knocked out early) | 27.00 | 5.34 |
| 1 good start, 6 IP, 0 ER | 0.00 | 3.21 |
| 3 starts, 18 IP, solid | 3.00 | 3.45 |
| Mid-season, 60 IP, ace | 2.50 | 2.71 |

By June, blended ERA is within 0.3–0.5 runs of the raw ERA for any starter with regular work. In April, it prevents extreme single-game outliers from distorting predictions.

### Logistic Regression + Calibration

Logistic regression takes the feature vector and outputs a probability between 0 and 1. It learns weights for each feature from historical data (2023–2025 seasons).

The model is wrapped in **isotonic calibration** (`CalibratedClassifierCV` from scikit-learn), which ensures the output probabilities are honest: a prediction of 65% should win about 65% of the time historically. Without calibration, raw logistic regression can be overconfident.

### Run Differential Model

A second model predicts the expected **run margin** (home runs − away runs). This is used to compute cover probabilities for the run line (±1.5). It uses the same features plus the residual standard deviation of run margins to derive a probability of covering the spread.

---

## 4. Edge — Finding Value in the Market

**Edge** is the fundamental concept behind every pick. It measures how much better the model thinks a team's chances are versus what the bookmaker is implying.

### How it's computed

Bookmaker odds imply a probability. For example:
- A team priced at **-150** (bet $150 to win $100) implies: 150 / (100 + 150) = **60% win probability**
- A team priced at **+130** (bet $100 to win $130) implies: 100 / (100 + 130) = **43% win probability**

Edge = Model probability − Implied probability

```
edge = model_prob - implied_prob
```

Examples:
- Model says 58%, market implies 50% → **+8% edge** (take this bet)
- Model says 45%, market implies 52% → **-7% edge** (no bet — market has it right or we're wrong)

### Why edge matters

In betting, you don't need to be right most of the time — you need to be right *more often than the odds imply*. If a team is priced as a 40% chance but you believe they have a 50% chance, that's a profitable bet in the long run even if they only win half the time.

### Minimum edge thresholds (locked v1 values)

The rules engine requires a minimum edge before a pick qualifies:

| Market | Minimum Edge |
|--------|-------------|
| Moneyline | 3% |
| Run line | 4% |

Run line requires more edge because the -1.5 spread narrows the margin for error.

---

## 5. Confidence Score

Edge alone doesn't tell the full story. A 5% edge from a model that's been consistently accurate means more than a 5% edge from a model that's been all over the place. **Confidence** captures how much we trust this specific pick.

### Formula

Confidence is scored 1–10, built from four equally-weighted components (25% each):

**1. Edge magnitude (0–10)**
```
edge_score = min(10, abs(edge) / 0.01)
```
A 1% edge = 1 point. A 10%+ edge = 10 points (capped). Pure signal strength.

**2. Model conviction (0–10)**
```
conviction_score = min(10, abs(model_prob - 0.5) * 20)
```
How far is the model's prediction from 50/50? A 50% prediction = 0 conviction. A 75% prediction = 5 points. A 100% prediction = 10 points (capped).

**3. Historical calibration (1–10, with early-season penalty)**
```
bucket_score = 10 - calibration_error * 50
sample_penalty = 4.0 * max(0, 1 - games_played / 30)
calibration_score = max(1.0, bucket_score - sample_penalty)
```
How accurate has the model been historically at this probability range? Based on `eval/results/calibration_data.csv`. The early-season penalty reduces this score when teams have played fewer than 30 games — stats are noisy in April.

**4. Injury certainty (3, 6, or 10)**
```
injury_score = 10 if no injury, 6 if questionable, 3 if key player out
```

**Final composite:**
```
raw_composite = (edge_score + conviction_score + calibration_score + injury_score) / 4
confidence = raw_composite * sample_multiplier
```

Where `sample_multiplier = max(0.7, games_played / 30)` — a global early-season dampener that scales the entire confidence score down by up to 30% when there are fewer than 30 games played.

**Minimum confidence to qualify: 5.0/10**

---

## 6. The Rules Engine — Selecting Picks

After edge and confidence are computed for every game+market+side combination, the rules engine filters and ranks them.

### Qualification criteria (all must pass)

1. **Positive edge** — we must be on the right side of the bet
2. **Above minimum edge threshold** (3% moneyline / 4% run line)
3. **Not efficiently priced** — if |edge| < 1%, the market is too tight to exploit
4. **Minimum confidence ≥ 5.0**
5. **Injury block** — if a key player is out and confidence < 6.0, skip

### Bet sizing

All qualifying picks are currently sized as **"medium"**. The formula is:
```
composite = edge × confidence
if composite >= 0.06: decision = 'medium'
else: decision = 'small'
```
("Large" sizing is not used in v1.)

### Deduplication and final selection

After scoring, picks are sorted by confidence (descending), then **deduplicated to one pick per game** — the highest-confidence pick per game ID. This prevents recommending both sides of the same game or the same game on two markets.

The top 5 picks after deduplication are the day's slate.

---

## 7. Evaluation Metrics

### Brier Score

The Brier Score measures how well-calibrated the model's probabilities are. It is the mean squared error between predicted probability and actual outcome (1 = win, 0 = loss):

```
Brier Score = mean((predicted_prob - actual_outcome)²)
```

**Lower is better.** Key benchmarks:
- Random guessing (always predict 50%) = **0.25**
- Perfect model = **0.00**
- Current model (v3, in-sample) = **0.2333**
- Cross-validated (out-of-sample, 2024–2025) = **~0.240**

The cross-validated score is the honest number — it's what you'd get from predictions the model never trained on.

Run: `python eval/brier.py`

---

### Calibration

Calibration checks whether the model's stated probabilities match reality. It works by bucketing all predictions into bands (0–10%, 10–20%, etc.) and comparing the predicted average to the actual win rate in each bucket.

A well-calibrated model's calibration curve lies close to the diagonal:
- Predictions in the 60–70% bucket should win ~65% of the time
- Predictions in the 70–80% bucket should win ~75% of the time

**Mean Calibration Error (MCE):** Average absolute gap between predicted and actual per bucket. Lower is better. Under 0.05 is good; under 0.02 is excellent.

The calibration data is also used in the confidence formula — picks in probability ranges where the model has historically been less accurate get a lower calibration score.

Run: `python eval/calibration.py`

---

### Closing Line Value (CLV)

CLV measures whether the model finds value that the market later confirms. If the model likes a team at +100 odds in the morning and by game time the line has moved to -120, that's positive CLV — the market moved in the direction the model predicted.

**CLV = model probability − closing line implied probability**

Positive CLV is one of the strongest signals that a betting model has real edge, because the closing line is set by the sharpest money in the world. Consistently beating it means you're identifying something the market didn't fully price in at open.

Run: `python eval/clv.py`

---

### ROI Backtest

The most direct measure of real-world performance. Simulates placing a flat $100 bet on every qualifying pick and calculates total profit/loss.

**How wins are determined:**
- **Moneyline:** Did the picked team win the game?
- **Run line:** Did the picked team cover the spread? (Away team -1.5 means they must win by 2+; +1.5 means they can lose by 1 and still cover.)

**P&L per bet:**
```
Win with +130 odds  → profit $130
Win with -150 odds  → profit $66.67
Loss (any odds)     → -$100
```

**ROI = Total P&L / Total Staked**

A ROI of +5% over 200+ bets is considered good. Early season samples (< 100 bets) are not statistically significant — one or two big wins can make the numbers look much better than they are.

Run: `python eval/roi.py`

---

### Coverage

Measures how often the system generates picks relative to the total number of game+market+side opportunities evaluated.

Key questions coverage answers:
- What % of moneyline opportunities become picks?
- How many picks per day on average?
- How often does the system generate 0 picks?
- What reasons are most common for rejecting picks (low confidence, below edge threshold, etc.)?

A low pick rate (< 5%) suggests the thresholds are too tight. A high pick rate (> 30%) suggests the model may be over-fitting or thresholds are too loose.

Run: `python eval/coverage.py`

---

### Edge Distribution

Analyzes the spread of model edge across all evaluated picks. Key questions:

- Are high-edge picks actually winning more often? (They should be.)
- Is edge skewed toward away teams? (May indicate a home field calibration issue.)
- Are the edges too large early in the season? (Suggests early-season noise.)

A healthy system should show a monotonic relationship between edge quintile and win rate: the highest-edge picks should win most often.

Run: `python eval/edge_dist.py`

---

## 8. What the System Does Not Do

Understanding the limits is as important as understanding the capabilities.

**Does not account for:**
- Bullpen strength or usage patterns (only starting pitcher is modeled)
- Weather conditions
- Travel schedules or fatigue
- Lineup changes after the model runs
- Park factors (some parks dramatically favor hitters or pitchers)
- Platoon advantages (left/right pitcher-batter matchups)

**Statistical limitations:**
- Early season (fewer than 30 games): all stats are extremely noisy. The confidence penalty helps, but picks in April should be treated with extra caution.
- The training data covers 2023–2025 only. Three seasons is a small sample for a machine learning model.
- The model predicts game outcomes, not in-game events. It cannot handle mid-game situations.

**Not financial advice.** All output is for research and informational purposes. Sports betting involves real financial risk.
