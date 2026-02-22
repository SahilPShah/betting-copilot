import pandas as pd
import numpy as np
from sqlalchemy import create_engine, text
from dotenv import load_dotenv
import os
import sys
from sklearn.model_selection import cross_val_score
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import Pipeline
import sys

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from models.elo import run_elo
from models.train import build_features, load_team_stats

load_dotenv("/Users/sahilshah/betting-copilot/.env")
engine = create_engine(os.getenv("DATABASE_URL"))

SEASONS = ['2023', '2024', '2025']


def brier_score(history_df):
    """Lower is better. Perfect = 0, Random = 0.25."""
    return np.mean((history_df['home_expected'] - history_df['home_actual']) ** 2)


def accuracy(history_df):
    """% of games where the model correctly predicted the winner."""
    correct = (
        ((history_df['home_expected'] > 0.5) & (history_df['home_actual'] == 1)) |
        ((history_df['home_expected'] < 0.5) & (history_df['home_actual'] == 0))
    )
    return correct.mean()


def evaluate_season(season, history_df):
    """Evaluate model performance for a single season."""
    season_history = history_df[
        history_df['game_date'].astype(str).str[:4] == str(season)
    ].copy()

    if season_history.empty:
        print(f"  No data for {season}")
        return None

    bs = brier_score(season_history)
    acc = accuracy(season_history)
    games = len(season_history)

    print(f"\n  Season {season}:")
    print(f"    Games evaluated : {games}")
    print(f"    Brier Score     : {bs:.4f}  (target < 0.25, random = 0.25)")
    print(f"    Accuracy        : {acc:.1%}  (% correct winner predicted)")

    return {"season": season, "games": games, "brier_score": bs, "accuracy": acc}


def run_season_backtest(k=20, divisor=400):
    """Run full backtest across all seasons with given parameters."""
    print(f"\nRunning ELO backtest — K={k}, divisor={divisor}...")
    ratings, history = run_elo(SEASONS, k=k, divisor=divisor)

    results = []
    for season in [2023, 2024, 2025]:
        result = evaluate_season(season, history)
        if result:
            results.append(result)

    overall_bs = brier_score(history)
    overall_acc = accuracy(history)

    print(f"\n  Overall Brier Score : {overall_bs:.4f}")
    print(f"  Overall Accuracy    : {overall_acc:.1%}")

    return pd.DataFrame(results), overall_bs, overall_acc


def grid_search(
    k_values=[10, 15, 20, 25, 32],
    divisors=[200, 300, 400, 500, 600]
):
    """
    Test every combination of K-factor and divisor.
    Finds the combination that minimizes overall Brier score.
    """
    print("\n--- Full Grid Search (K-factor × Divisor) ---")
    print(f"{'K':<8} {'Divisor':<12} {'Brier Score':<15} {'Accuracy'}")
    print("-" * 50)

    results = []

    for k in k_values:
        for d in divisors:
            _, history = run_elo(SEASONS, k=k, divisor=d)
            bs = brier_score(history)
            acc = accuracy(history)
            print(f"{k:<8} {d:<12} {bs:<15.4f} {acc:.1%}")
            results.append({
                "k_factor": k,
                "divisor": d,
                "brier_score": bs,
                "accuracy": acc
            })

    df = pd.DataFrame(results)
    best = df.loc[df['brier_score'].idxmin()]

    print("\n--- Best Combination ---")
    print(f"  K-factor    : {int(best['k_factor'])}")
    print(f"  Divisor     : {int(best['divisor'])}")
    print(f"  Brier Score : {best['brier_score']:.4f}")
    print(f"  Accuracy    : {best['accuracy']:.1%}")

    return df, best

def cross_validate_model():
    """
    Proper out-of-sample evaluation using time-series cross validation.
    Train on earlier seasons, test on later seasons.
    """
    print("\n--- Cross-Validated Evaluation ---")

    _, history = run_elo(SEASONS, k=10, divisor=800)
    stats = load_team_stats()
    features = build_features(history, stats)

    feature_cols = [
        'elo_diff', 'era_diff', 'whip_diff',
        'k9_diff', 'ops_diff', 'win_pct_diff', 'runs_diff'
    ]

    results = []

    # Time-series splits — train on past, test on future
    splits = [
        {"train_seasons": ["2023"],            "test_season": "2024"},
        {"train_seasons": ["2023", "2024"],     "test_season": "2025"},
    ]

    for split in splits:
        train_seasons = split["train_seasons"]
        test_season = split["test_season"]

        train = features[
            features['game_date'].astype(str).str[:4].isin(train_seasons)
        ]
        test = features[
            features['game_date'].astype(str).str[:4] == test_season
        ]

        X_train = train[feature_cols].values
        y_train = train['home_won'].values
        X_test = test[feature_cols].values
        y_test = test['home_won'].values

        # Scale using only training data
        scaler = StandardScaler()
        X_train_scaled = scaler.fit_transform(X_train)
        X_test_scaled = scaler.transform(X_test)

        model = LogisticRegression(max_iter=1000, random_state=42)
        model.fit(X_train_scaled, y_train)

        probs = model.predict_proba(X_test_scaled)[:, 1]
        bs = np.mean((probs - y_test) ** 2)
        acc = np.mean(
            ((probs > 0.5) & (y_test == 1)) |
            ((probs < 0.5) & (y_test == 0))
        )

        train_label = "+".join(train_seasons)
        print(f"\n  Train: {train_label} → Test: {test_season}")
        print(f"    Brier Score : {bs:.4f}")
        print(f"    Accuracy    : {acc:.1%}")
        print(f"    Games tested: {len(test)}")

        results.append({
            "train": train_label,
            "test": test_season,
            "brier_score": bs,
            "accuracy": acc,
            "games": len(test)
        })

    df = pd.DataFrame(results)
    avg_brier = df['brier_score'].mean()
    print(f"\n  Average cross-validated Brier Score: {avg_brier:.4f}")
    print(f"  Pure ELO baseline:                  0.2443")
    print(f"  Improvement:                        {0.2443 - avg_brier:.4f}")

    return df


if __name__ == "__main__":
    # Baseline
    season_results, overall_bs, overall_acc = run_season_backtest(k=10, divisor=800)

    # Cross-validated evaluation
    cv_results = cross_validate_model()

    # Save
    os.makedirs("eval/results", exist_ok=True)
    season_results.to_csv("eval/results/elo_baseline.csv", index=False)
    cv_results.to_csv("eval/results/cross_validated_results.csv", index=False)
    print("\nResults saved to eval/results/")