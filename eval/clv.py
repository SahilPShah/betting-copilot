import pandas as pd
import numpy as np
from sqlalchemy import create_engine, text
from dotenv import load_dotenv
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler
import os
import sys
import pickle

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from models.elo import run_elo
from models.train import build_features, load_team_stats

load_dotenv("/Users/sahilshah/betting-copilot/.env")
engine = create_engine(os.getenv("DATABASE_URL"))

SEASONS = ['2023', '2024', '2025']
EDGE_THRESHOLD = 0.03
CONFIDENCE_THRESHOLD = 5.0

FEATURE_COLS = [
    'elo_diff', 'era_diff', 'whip_diff',
    'k9_diff', 'ops_diff', 'win_pct_diff', 'runs_diff'
]

# Time-series splits — train on past, predict on future only
CV_SPLITS = [
    {"train_seasons": ["2023"],             "test_season": "2024"},
    {"train_seasons": ["2023", "2024"],     "test_season": "2025"},
]


def load_closing_odds():
    """Load the most recent closing moneyline odds per game for home side only."""
    with engine.connect() as conn:
        df = pd.read_sql(text("""
            SELECT DISTINCT ON (game_id)
                game_id,
                implied_prob as closing_implied_prob,
                american_odds as closing_odds
            FROM odds_snapshots
            WHERE market = 'moneyline'
            AND side = 'home'
            AND is_closing = true
            AND bookmaker = 'draftkings'
            AND implied_prob BETWEEN 0.15 AND 0.85
            AND american_odds BETWEEN -600 AND 500
            ORDER BY game_id, captured_at DESC
        """), conn)
    return df


def compute_confidence(edge, model_prob, elo_diff):
    """Confidence score 1-10 based on edge, probability conviction, and ELO differential."""
    edge_score = min(edge / 0.10 * 4, 4)
    prob_score = min(abs(model_prob - 0.5) / 0.20 * 3, 3)
    elo_score = min(abs(elo_diff) / 200 * 3, 3)
    return round(edge_score + prob_score + elo_score, 1)


def train_model(train_df):
    """Train logistic regression on training seasons only."""
    X = train_df[FEATURE_COLS].values
    y = train_df['home_won'].values
    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)
    model = LogisticRegression(max_iter=1000, random_state=42)
    model.fit(X_scaled, y)
    return model, scaler


def run_clv_backtest():
    """
    Valid CLV backtest using time-series cross-validation.
    Train on past seasons only, predict on future seasons only.
    No look-ahead bias.
    """
    print("Loading ELO history and team stats...")
    _, history = run_elo(SEASONS, k=10, divisor=800)
    stats = load_team_stats()
    all_features = build_features(history, stats)
    print(f"  Total games with features: {len(all_features)}")

    print("Loading closing odds...")
    odds = load_closing_odds()
    print(f"  Closing odds rows: {len(odds)}")

    all_results = []

    for split in CV_SPLITS:
        train_seasons = split["train_seasons"]
        test_season = split["test_season"]

        print(f"\n  Split: train={train_seasons} → test={test_season}")

        # Split features by season
        train_df = all_features[
            all_features['game_date'].astype(str).str[:4].isin(train_seasons)
        ].copy()

        test_df = all_features[
            all_features['game_date'].astype(str).str[:4] == test_season
        ].copy()

        # Train on past only
        model, scaler = train_model(train_df)

        # Predict on future only
        X_test = scaler.transform(test_df[FEATURE_COLS].values)
        test_df = test_df.copy()
        test_df['model_prob'] = model.predict_proba(X_test)[:, 1]

        # Merge with closing odds
        merged = test_df.merge(odds, on='game_id', how='inner')
        # Add this debug block right after the merge in run_clv_backtest()
        print("\nDebug sample — first 10 merged rows:")
        print(merged[['game_id', 'home_team_id', 'away_team_id', 
                    'model_prob', 'closing_implied_prob', 
                    'closing_odds', 'home_won']].head(10).to_string())

        print("\nModel prob distribution:")
        print(merged['model_prob'].describe())

        print("\nClosing prob distribution:")
        print(merged['closing_implied_prob'].describe())
        print(f"    Games with closing odds: {len(merged)}")

        for _, row in merged.iterrows():
            model_prob = float(row['model_prob'])
            closing_prob = float(row['closing_implied_prob'])
            edge = model_prob - closing_prob
            elo_diff = float(row['elo_diff'])
            confidence = compute_confidence(edge, model_prob, elo_diff)

            all_results.append({
                "game_id": row['game_id'],
                "game_date": row['game_date'],
                "home_team_id": row['home_team_id'],
                "away_team_id": row['away_team_id'],
                "train_seasons": "+".join(train_seasons),
                "test_season": test_season,
                "model_prob": round(model_prob, 4),
                "closing_prob": round(closing_prob, 4),
                "edge": round(edge, 4),
                "clv": round(model_prob - closing_prob, 4),
                "confidence": confidence,
                "elo_diff": round(elo_diff, 1),
                "home_actual": int(row['home_won']),
                "qualifies": edge >= EDGE_THRESHOLD and confidence >= CONFIDENCE_THRESHOLD
            })

    results_df = pd.DataFrame(all_results)
    qualifying_df = results_df[results_df['qualifies']].copy()

    return results_df, qualifying_df


def print_clv_summary(results_df, qualifying_df):
    """Print CLV analysis."""
    print(f"\n{'='*55}")
    print(f"  VALID CLV BACKTEST — TIME-SERIES CROSS-VALIDATION")
    print(f"{'='*55}")

    print(f"\n--- Overall Summary ---")
    print(f"  Total games evaluated : {len(results_df)}")
    print(f"  Qualifying picks      : {len(qualifying_df)}")
    print(f"  Pick rate             : {len(qualifying_df)/len(results_df):.1%}")

    if qualifying_df.empty:
        print("  No qualifying picks found")
        return

    print(f"\n--- CLV on Qualifying Picks ---")
    mean_clv = qualifying_df['clv'].mean()
    median_clv = qualifying_df['clv'].median()
    pct_positive = (qualifying_df['clv'] > 0).mean()

    print(f"  Mean CLV     : {mean_clv:+.4f} ({mean_clv*100:+.2f}%)")
    print(f"  Median CLV   : {median_clv:+.4f} ({median_clv*100:+.2f}%)")
    print(f"  % Positive   : {pct_positive:.1%}")
    print(f"  Std Dev      : {qualifying_df['clv'].std():.4f}")

    print(f"\n--- Per Season Breakdown ---")
    for season in ["2024", "2025"]:
        season_picks = qualifying_df[
            qualifying_df['test_season'] == season
        ]
        if season_picks.empty:
            continue
        s_clv = season_picks['clv'].mean()
        s_pct = (season_picks['clv'] > 0).mean()
        s_acc = season_picks['home_actual'].mean()
        print(f"\n  {season} (out-of-sample):")
        print(f"    Picks          : {len(season_picks)}")
        print(f"    Mean CLV       : {s_clv:+.4f} ({s_clv*100:+.2f}%)")
        print(f"    % Positive CLV : {s_pct:.1%}")
        print(f"    Actual win rate: {s_acc:.1%}")

    print(f"\n--- Edge Distribution on Qualifying Picks ---")
    print(f"  Mean edge : {qualifying_df['edge'].mean():+.4f}")
    print(f"  Max edge  : {qualifying_df['edge'].max():+.4f}")
    print(f"  Min edge  : {qualifying_df['edge'].min():+.4f}")

    print(f"\n--- Interpretation ---")
    if mean_clv > 0.02:
        print("  ✅ Strong positive CLV — model consistently finds value")
    elif mean_clv > 0:
        print("  ✅ Positive CLV — model has edge over closing line")
    elif mean_clv > -0.01:
        print("  ⚠️  Near-zero CLV — model finding minimal edge")
    else:
        print("  ❌ Negative CLV — model not beating closing line")

    print(f"\n--- Note ---")
    print(f"  This evaluation uses strict time-series splits.")
    print(f"  Model never trained on data from the season it predicts.")
    print(f"  These are the honest out-of-sample CLV numbers.")


if __name__ == "__main__":
    results_df, qualifying_df = run_clv_backtest()
    print_clv_summary(results_df, qualifying_df)

    os.makedirs("eval/results", exist_ok=True)
    results_df.to_csv("eval/results/clv_valid_backtest.csv", index=False)
    qualifying_df.to_csv("eval/results/clv_qualifying_picks.csv", index=False)
    print(f"\nResults saved to eval/results/")