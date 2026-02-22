import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import os
import sys
import pickle

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from models.elo import run_elo
from models.train import build_features, load_team_stats

from dotenv import load_dotenv
load_dotenv("/Users/sahilshah/betting-copilot/.env")

SEASONS = ['2023', '2024', '2025']
MODEL_VERSION = "v1_elo_logreg"


def load_model():
    with open(f"models/versions/{MODEL_VERSION}.pkl", "rb") as f:
        artifacts = pickle.load(f)
    return artifacts


def compute_calibration(probs, actuals, n_bins=10):
    """
    Bucket predictions into probability bands.
    Compare predicted probability to actual win rate per bucket.
    """
    bins = np.linspace(0, 1, n_bins + 1)
    bucket_data = []

    for i in range(n_bins):
        low, high = bins[i], bins[i + 1]
        mask = (probs >= low) & (probs < high)
        count = mask.sum()

        if count == 0:
            continue

        mean_predicted = probs[mask].mean()
        actual_win_rate = actuals[mask].mean()
        bucket_data.append({
            "bucket": f"{low:.1f}-{high:.1f}",
            "mean_predicted": mean_predicted,
            "actual_win_rate": actual_win_rate,
            "count": count
        })

    return pd.DataFrame(bucket_data)


def plot_calibration(calibration_df, title, filename):
    """Plot predicted vs actual win rate."""
    fig, axes = plt.subplots(1, 2, figsize=(14, 6))

    # Calibration curve
    ax1 = axes[0]
    ax1.plot([0, 1], [0, 1], 'k--', label='Perfect calibration', alpha=0.5)
    ax1.scatter(
        calibration_df['mean_predicted'],
        calibration_df['actual_win_rate'],
        s=calibration_df['count'] / 10,
        alpha=0.8,
        color='steelblue',
        zorder=5
    )
    ax1.plot(
        calibration_df['mean_predicted'],
        calibration_df['actual_win_rate'],
        'o-', color='steelblue', alpha=0.6
    )
    for _, row in calibration_df.iterrows():
        ax1.annotate(
            f"n={int(row['count'])}",
            (row['mean_predicted'], row['actual_win_rate']),
            textcoords="offset points", xytext=(5, 5), fontsize=8
        )
    ax1.set_xlabel('Mean Predicted Probability')
    ax1.set_ylabel('Actual Win Rate')
    ax1.set_title(f'Calibration Curve — {title}')
    ax1.legend()
    ax1.set_xlim(0, 1)
    ax1.set_ylim(0, 1)
    ax1.grid(True, alpha=0.3)

    # Prediction distribution
    ax2 = axes[1]
    ax2.bar(
        calibration_df['bucket'],
        calibration_df['count'],
        color='steelblue',
        alpha=0.7
    )
    ax2.set_xlabel('Predicted Probability Bucket')
    ax2.set_ylabel('Number of Games')
    ax2.set_title(f'Prediction Distribution — {title}')
    ax2.tick_params(axis='x', rotation=45)
    ax2.grid(True, alpha=0.3, axis='y')

    plt.tight_layout()
    os.makedirs("eval/results", exist_ok=True)
    plt.savefig(f"eval/results/{filename}.png", dpi=150, bbox_inches='tight')
    print(f"  Saved to eval/results/{filename}.png")
    plt.close()


def main():
    print("Loading model...")
    artifacts = load_model()
    model = artifacts['model']
    scaler = artifacts['scaler']
    feature_cols = artifacts['feature_cols']

    print("Loading ELO history and features...")
    _, history = run_elo(SEASONS, k=10, divisor=800)
    stats = load_team_stats()
    features = build_features(history, stats)

    print(f"  {len(features)} games with features")

    # Generate predictions
    X = scaler.transform(features[feature_cols].values)
    probs = model.predict_proba(X)[:, 1]
    actuals = features['home_won'].values

    # Overall calibration
    print("\n--- Overall Calibration ---")
    calibration = compute_calibration(probs, actuals)
    print(calibration[['bucket', 'mean_predicted', 'actual_win_rate', 'count']].to_string(index=False))
    plot_calibration(calibration, "All Seasons", "calibration_all_seasons")

    # Per-season calibration
    features['predicted_prob'] = probs
    for season in [2023, 2024, 2025]:
        season_mask = features['game_date'].astype(str).str[:4] == str(season)
        season_features = features[season_mask]
        season_probs = season_features['predicted_prob'].values
        season_actuals = season_features['home_won'].values

        print(f"\n--- Season {season} Calibration ---")
        cal = compute_calibration(season_probs, season_actuals)
        print(cal[['bucket', 'mean_predicted', 'actual_win_rate', 'count']].to_string(index=False))
        plot_calibration(cal, str(season), f"calibration_{season}")

    # Calibration error summary
    calibration['error'] = abs(
        calibration['mean_predicted'] - calibration['actual_win_rate']
    )
    mean_cal_error = calibration['error'].mean()
    print(f"\nMean Calibration Error: {mean_cal_error:.4f}")
    print("(Lower = better. < 0.05 is good, < 0.02 is excellent)")

    calibration.to_csv("eval/results/calibration_data.csv", index=False)
    print("\nDone. Calibration plots saved to eval/results/")


if __name__ == "__main__":
    main()