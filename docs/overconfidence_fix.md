# Model Overconfidence — Known Flaw & Fix Plan

## The Problem

The v4 model is overconfident on extreme predictions. Backtest across 182 games (Mar 25 – Apr 15, 2026):

| Predicted probability | Actual win rate | Assessment |
|---|---|---|
| <40% | 45.7% | Underdogs win far more than predicted |
| 40–50% | ~58% | Predicted losers actually winning more |
| 50–60% | ~52% | Noisy, near-random |
| **60%+** | **70.4%** | Well-calibrated (model predicted 70.7%) |

The 60%+ bucket is accurate. The problem is in the tails — the model pushes too many games below 40% that don't belong there.

## Root Cause

Logistic regression has no ceiling on its logit. When multiple features simultaneously point the same direction, probabilities can saturate toward 0% or 1%. The two main drivers:

**1. `starter_era_diff` extremes.** The highest-weight feature (+0.37 coefficient). When one starter has a 2.0 ERA and the other has a 7.0 ERA after Bayesian shrinkage, the resulting `starter_era_diff` is large enough — combined with other agreeing features — to push predictions to 6–8% or 92–94%.

**2. Isotonic calibration trained on old distribution.** The calibration layer inside `CalibratedClassifierCV` was fit on 2023–2025 games. Early-season 2026 feature combinations (volatile L7 streaks + small-sample pitcher ERAs) are more extreme than anything in training, so the calibration can't correct for them.

The fix for the L7 distribution mismatch (v3 → v4) eliminated the worst cases. The remaining overconfidence is driven by starter ERA extremes in small samples.

---

## Fix Plan (to implement later as v5)

### Fix 1 — Clip `starter_era_diff` at feature build time (highest priority)

Cap `starter_era_diff` and `starter_whip_diff` to a physically meaningful range before the scaler. A 5-ERA gap and a 3-ERA gap should not produce meaningfully different model outputs — both represent "large advantage."

Apply in both `models/train.py` (`build_features()`) and `models/predict.py` (`build_feature_vector()`):

```python
starter_era_diff  = max(-3.0, min(3.0, starter_era_diff))
starter_whip_diff = max(-0.9, min(0.9, starter_whip_diff))
```

Retrain as v5 with these clips in place so the scaler learns the clipped distribution. The bounds (±3.0 ERA, ±0.9 WHIP) represent ~1.6σ of the training distribution — enough headroom for genuine large matchup advantages.

### Fix 2 — Tune logistic regression regularization

The current model uses default `C=1.0`. Lowering `C` shrinks all coefficients toward zero, producing less extreme predictions everywhere. Grid-search in the same training run:

```python
# In models/train.py train()
for C in [0.1, 0.3, 0.5, 1.0]:
    base_model = LogisticRegression(max_iter=1000, random_state=42, C=C)
    # compare Brier scores across C values
```

Try alongside Fix 1 — both go into the same retrain pass for v5.

### Fix 3 — Temperature scaling on 2026 out-of-sample data (do when ≥300 games available, ~mid-May)

A single scalar parameter `T` divides the model's logit before applying sigmoid. `T > 1` uniformly reduces confidence. Fit `T` by minimizing Brier score on 2026 labeled outcomes:

```python
from scipy.special import logit, expit
from scipy.optimize import minimize_scalar
import numpy as np

def brier_at_T(T):
    calibrated = expit(logit(raw_probs) / T)
    return np.mean((calibrated - actuals) ** 2)

result = minimize_scalar(brier_at_T, bounds=(0.5, 2.0), method='bounded')
T = result.x  # store in model artifact, apply at inference
```

Store `T` in the pkl artifact and apply at inference in `models/predict.py`:

```python
from scipy.special import logit, expit
T = artifact.get('temperature', 1.0)
home_prob = float(expit(logit(raw_prob) / T))
```

Requires ~300 labeled 2026 games for reliable fitting. Before that, the sample is too small to trust.

### Fix 4 — Market probability blend in edge calculation (optional, trades edge size for quality)

Blend model probability with market implied probability before computing edge. Pulls extreme model predictions toward market consensus without requiring a retrain.

In `recs/edge.py`, `compute_edges()`:

```python
MARKET_BLEND = 0.3  # weight on market implied prob
blended_prob = (1 - MARKET_BLEND) * model_prob + MARKET_BLEND * float(odds_row['implied_prob'])
edge = blended_prob - float(odds_row['implied_prob'])
```

Tradeoff: raw edge numbers shrink (you're moving toward the market), but surviving edges are higher quality. This is conservative — use only if Fixes 1–3 don't sufficiently tame the extremes.

---

## Implementation Order

| Step | Fix | When | Files |
|------|-----|------|-------|
| v5 retrain | Clip `starter_era_diff` ± 3.0, tune `C` | Now | `models/train.py`, `models/predict.py` |
| v5+ patch | Temperature scaling | Mid-May (≥300 2026 games) | `models/train.py`, `models/predict.py` |
| Optional | Market blend | Anytime | `recs/edge.py` |

## Success Criteria

After v5, re-run the backtest on all available 2026 final games. Target:

- `<40%` bucket actual win rate drops from 45.7% toward 38–42%
- No predictions below 15% or above 85% for standard matchups (non-Ohtani-level outliers)
- High-confidence (>60%) accuracy holds at 60%+
- Overall Brier score ≤ 0.234 (does not regress vs v4)
