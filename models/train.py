import pandas as pd
import numpy as np
from sqlalchemy import create_engine, text
from dotenv import load_dotenv
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler
from sklearn.calibration import CalibratedClassifierCV
import pickle
import os
import sys

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from models.elo import run_elo

load_dotenv("/Users/sahilshah/betting-copilot/.env")
engine = create_engine(os.getenv("DATABASE_URL"))

SEASONS = ['2023', '2024', '2025']
MODEL_VERSION = "v1_elo_logreg"


def load_team_stats():
    """Load season-level team stats from DB."""
    with engine.connect() as conn:
        df = pd.read_sql(text("""
            SELECT
                team_id,
                LEFT(as_of_date::text, 4)::integer as season,
                team_pitching_era,
                team_pitching_whip,
                team_pitching_k9,
                team_ops_l14,
                team_era_l14,
                team_win_pct,
                runs_scored_avg
            FROM team_stats_snapshots
        """), conn)
    return df


def build_features(history_df, stats_df):
    """
    Join ELO history with team stats to build feature matrix.
    Each row = one game, with features for both home and away teams.
    """
    rows = []

    for _, game in history_df.iterrows():
        season = int(str(game['game_date'])[:4])
        home = game['home_team_id']
        away = game['away_team_id']

        # Get season stats for each team
        home_stats = stats_df[
            (stats_df['team_id'] == home) &
            (stats_df['season'] == season)
        ]
        away_stats = stats_df[
            (stats_df['team_id'] == away) &
            (stats_df['season'] == season)
        ]

        # Skip if stats missing for either team
        if home_stats.empty or away_stats.empty:
            continue

        h = home_stats.iloc[0]
        a = away_stats.iloc[0]

        rows.append({
            "game_id": game['game_id'],
            "game_date": game['game_date'],
            "home_team_id": home,
            "away_team_id": away,

            # ELO features
            "elo_diff": game['home_elo_pre'] - game['away_elo_pre'],
            "home_elo": game['home_elo_pre'],
            "away_elo": game['away_elo_pre'],

            # Pitching features (higher away ERA = better for home)
            "era_diff": float(a['team_pitching_era']) - float(h['team_pitching_era']),
            "whip_diff": float(a['team_pitching_whip']) - float(h['team_pitching_whip']),
            "k9_diff": float(h['team_pitching_k9']) - float(a['team_pitching_k9']),

            # Batting features
            "ops_diff": float(h['team_ops_l14']) - float(a['team_ops_l14']),

            # Overall quality
            "win_pct_diff": float(h['team_win_pct']) - float(a['team_win_pct']),
            "runs_diff": float(h['runs_scored_avg']) - float(a['runs_scored_avg']),

            # Target
            "home_won": game['home_actual'],
        })

    return pd.DataFrame(rows)


def train(features_df):
    """Train logistic regression on feature matrix."""
    feature_cols = [
        'elo_diff', 'era_diff', 'whip_diff',
        'k9_diff', 'ops_diff', 'win_pct_diff', 'runs_diff'
    ]

    X = features_df[feature_cols].values
    y = features_df['home_won'].values

    # Scale features
    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)

    # Logistic regression with calibration for better probability estimates
    base_model = LogisticRegression(max_iter=1000, random_state=42)
    model = CalibratedClassifierCV(base_model, cv=5, method='isotonic')
    model.fit(X_scaled, y)

    return model, scaler, feature_cols


def save_model(model, scaler, feature_cols, version=MODEL_VERSION):
    """Save model artifacts to disk."""
    os.makedirs("models/versions", exist_ok=True)
    with open(f"models/versions/{version}.pkl", "wb") as f:
        pickle.dump({
            "model": model,
            "scaler": scaler,
            "feature_cols": feature_cols,
            "version": version,
            "k": 10,
            "divisor": 800
        }, f)
    print(f"  Model saved to models/versions/{version}.pkl")


def main():
    print("Loading ELO history...")
    _, history = run_elo(SEASONS, k=10, divisor=800)
    print(f"  {len(history)} games in ELO history")

    print("\nLoading team stats...")
    stats = load_team_stats()
    print(f"  {len(stats)} team-season stat rows")

    print("\nBuilding feature matrix...")
    features = build_features(history, stats)
    print(f"  {len(features)} games with complete features")
    print(f"  Skipped: {len(history) - len(features)} games (missing stats)")

    print("\nFeature summary:")
    print(features[[
        'elo_diff', 'era_diff', 'whip_diff',
        'ops_diff', 'win_pct_diff'
    ]].describe().round(4))

    print("\nTraining logistic regression...")
    model, scaler, feature_cols = train(features)
    print("  Training complete")

    # Quick in-sample evaluation
    X = scaler.transform(features[feature_cols].values)
    probs = model.predict_proba(X)[:, 1]
    features['predicted_prob'] = probs

    brier = np.mean((probs - features['home_won'].values) ** 2)
    accuracy = np.mean(
        ((probs > 0.5) & (features['home_won'] == 1)) |
        ((probs < 0.5) & (features['home_won'] == 0))
    )

    print(f"\n  In-sample Brier Score : {brier:.4f}")
    print(f"  In-sample Accuracy    : {accuracy:.1%}")
    print(f"  (Note: in-sample scores are optimistic — "
          f"cross-validated scores will be lower)")

    save_model(model, scaler, feature_cols)
    print(f"\nDone. Model version: {MODEL_VERSION}")


if __name__ == "__main__":
    main()