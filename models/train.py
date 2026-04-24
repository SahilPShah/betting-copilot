import pandas as pd
import numpy as np
from sqlalchemy import create_engine, text
from dotenv import load_dotenv
from sklearn.linear_model import LogisticRegression, LinearRegression
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
MODEL_VERSION = "v4_elo_logreg_l7"


def load_team_stats():
    with engine.connect() as conn:
        df = pd.read_sql(text("""
            SELECT
                team_id,
                season,
                team_pitching_era,
                team_pitching_whip,
                team_pitching_k9,
                team_ops,
                team_win_pct,
                runs_scored_avg
            FROM team_season_stats
        """), conn)
    return df

def load_starter_stats():
    with engine.connect() as conn:
        df = pd.read_sql(text("""
            SELECT game_id, side, starter_era, starter_whip, starter_k9
            FROM game_starters
        """), conn)
    return df


def load_historical_l7_stats():
    """
    Compute L7 win% and avg runs scored for each team for each training game,
    from completed games strictly prior to the game date, scoped to the same season.

    Uses a self-join on the games table — no new data sources required.
    Returns a DataFrame indexed by (game_id, team_id) with columns:
        l7_games, l7_win_pct, l7_runs_scored_avg

    Only rows with l7_games >= 1 are returned. build_features() applies
    the same >=3 game threshold as inference before using L7 values.
    """
    with engine.connect() as conn:
        df = pd.read_sql(text("""
            WITH all_games AS (
                SELECT game_id, game_date,
                       EXTRACT(YEAR FROM game_date)::int AS season,
                       home_team_id AS team_id,
                       home_score AS scored, away_score AS allowed
                FROM games WHERE status = 'final'
                UNION ALL
                SELECT game_id, game_date,
                       EXTRACT(YEAR FROM game_date)::int AS season,
                       away_team_id AS team_id,
                       away_score AS scored, home_score AS allowed
                FROM games WHERE status = 'final'
            ),
            ranked AS (
                SELECT
                    tg.game_id AS target_game_id,
                    tg.team_id,
                    ag.scored,
                    CASE WHEN ag.scored > ag.allowed THEN 1 ELSE 0 END AS won,
                    ROW_NUMBER() OVER (
                        PARTITION BY tg.game_id, tg.team_id
                        ORDER BY ag.game_date DESC
                    ) AS rn
                FROM all_games tg
                JOIN all_games ag
                    ON  ag.team_id  = tg.team_id
                    AND ag.season   = tg.season
                    AND ag.game_date < tg.game_date
            )
            SELECT
                target_game_id                   AS game_id,
                team_id,
                COUNT(*)::int                    AS l7_games,
                ROUND(AVG(won)::numeric,    3)   AS l7_win_pct,
                ROUND(AVG(scored)::numeric, 2)   AS l7_runs_scored_avg
            FROM ranked
            WHERE rn <= 7
            GROUP BY target_game_id, team_id
        """), conn)
    return df.set_index(['game_id', 'team_id'])


def build_features(history_df, stats_df, starters_df=None, l7_df=None):
    """
    Join ELO history with team stats to build feature matrix.
    Each row = one game, with features for both home and away teams.
    Optionally joins starter pitcher stats (starter_era_diff, starter_whip_diff).

    When l7_df is provided, win_pct_diff and runs_diff use L7 rolling stats
    (if both teams have >=3 prior games in that season), else fall back to
    season stats. This mirrors the substitution logic in models/predict.py.
    """
    # Pivot starters to wide format: one row per game with home_ and away_ columns
    starter_wide = pd.DataFrame()
    if starters_df is not None and not starters_df.empty:
        home_s = (
            starters_df[starters_df['side'] == 'home']
            .set_index('game_id')[['starter_era', 'starter_whip']]
            .rename(columns={'starter_era': 'home_starter_era', 'starter_whip': 'home_starter_whip'})
        )
        away_s = (
            starters_df[starters_df['side'] == 'away']
            .set_index('game_id')[['starter_era', 'starter_whip']]
            .rename(columns={'starter_era': 'away_starter_era', 'starter_whip': 'away_starter_whip'})
        )
        starter_wide = home_s.join(away_s, how='outer')

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

        # Starter features — fall back to 0.0 if not available
        gid = game['game_id']
        has_starter_data = 0.0
        starter_era_diff = 0.0
        starter_whip_diff = 0.0

        if not starter_wide.empty and gid in starter_wide.index:
            row_s = starter_wide.loc[gid]
            h_era = row_s.get('home_starter_era')
            a_era = row_s.get('away_starter_era')
            h_whip = row_s.get('home_starter_whip')
            a_whip = row_s.get('away_starter_whip')
            if pd.notna(h_era) and pd.notna(a_era) and pd.notna(h_whip) and pd.notna(a_whip):
                starter_era_diff = float(a_era) - float(h_era)
                starter_whip_diff = float(a_whip) - float(h_whip)
                has_starter_data = 1.0

        # win_pct_diff and runs_diff: prefer L7 if both teams have >=3 prior
        # games in that season, else fall back to season stats.
        use_l7 = False
        if l7_df is not None and not l7_df.empty:
            h_key, a_key = (gid, home), (gid, away)
            h_l7_ok = h_key in l7_df.index and int(l7_df.loc[h_key, 'l7_games']) >= 3
            a_l7_ok = a_key in l7_df.index and int(l7_df.loc[a_key, 'l7_games']) >= 3
            use_l7 = h_l7_ok and a_l7_ok

        if use_l7:
            win_pct_diff = (float(l7_df.loc[(gid, home), 'l7_win_pct'])
                            - float(l7_df.loc[(gid, away), 'l7_win_pct']))
            runs_diff    = (float(l7_df.loc[(gid, home), 'l7_runs_scored_avg'])
                            - float(l7_df.loc[(gid, away), 'l7_runs_scored_avg']))
        else:
            win_pct_diff = float(h['team_win_pct']) - float(a['team_win_pct'])
            runs_diff    = float(h['runs_scored_avg']) - float(a['runs_scored_avg'])

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
            "ops_diff": float(h['team_ops']) - float(a['team_ops']),

            # Overall quality (L7 when available, season fallback)
            "win_pct_diff": win_pct_diff,
            "runs_diff": runs_diff,

            # Starter pitcher features (game-level signal)
            "starter_era_diff": starter_era_diff,
            "starter_whip_diff": starter_whip_diff,
            "has_starter_data": has_starter_data,

            # Targets
            "home_won": game['home_actual'],
            "run_differential": int(game['home_score']) - int(game['away_score']),

            # Diagnostic only — not a model feature
            "used_l7": use_l7,
        })

    return pd.DataFrame(rows)


def train(features_df):
    """
    Train two models on a shared scaler and feature set:
      1. Win probability model  — calibrated logistic regression on home_won (0/1)
      2. Run diff model         — linear regression on run_differential (continuous)

    Returns (win_model, run_diff_model, scaler, feature_cols, run_diff_residual_std)
    """
    feature_cols = [
        'elo_diff', 'era_diff', 'whip_diff',
        'k9_diff', 'ops_diff', 'win_pct_diff', 'runs_diff',
        'starter_era_diff', 'starter_whip_diff', 'has_starter_data',
    ]

    X = features_df[feature_cols].values
    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)

    # Model 1: Win probability (calibrated logistic regression)
    y_win = features_df['home_won'].values
    base_model = LogisticRegression(max_iter=1000, random_state=42)
    win_model = CalibratedClassifierCV(base_model, cv=5, method='isotonic')
    win_model.fit(X_scaled, y_win)

    # Model 2: Run differential (linear regression)
    y_diff = features_df['run_differential'].values
    run_diff_model = LinearRegression()
    run_diff_model.fit(X_scaled, y_diff)
    residuals = y_diff - run_diff_model.predict(X_scaled)
    run_diff_residual_std = float(residuals.std())

    return win_model, run_diff_model, scaler, feature_cols, run_diff_residual_std


def save_model(win_model, run_diff_model, scaler, feature_cols,
               run_diff_residual_std, version=MODEL_VERSION):
    """Save both model artifacts to a single pkl."""
    from models.elo import K_PRIOR, K_CURRENT, DEFAULT_DIVISOR, SEASON_REGRESS, DECAY_HALF_LIFE
    os.makedirs("models/versions", exist_ok=True)
    with open(f"models/versions/{version}.pkl", "wb") as f:
        pickle.dump({
            "model": win_model,
            "run_diff_model": run_diff_model,
            "scaler": scaler,
            "feature_cols": feature_cols,
            "run_diff_residual_std": run_diff_residual_std,
            "version": version,
            "k": K_PRIOR,
            "k_current": K_CURRENT,
            "divisor": DEFAULT_DIVISOR,
            "season_regress": SEASON_REGRESS,
            "decay_half_life": DECAY_HALF_LIFE,
        }, f)
    print(f"  Model saved to models/versions/{version}.pkl")


def main():
    print("Loading ELO history...")
    _, history = run_elo(SEASONS)
    print(f"  {len(history)} games in ELO history")

    print("\nLoading team stats...")
    stats = load_team_stats()
    print(f"  {len(stats)} team-season stat rows")

    print("\nLoading starter stats...")
    starters = load_starter_stats()
    print(f"  {len(starters)} starter rows ({starters['game_id'].nunique()} games)")

    print("\nLoading historical L7 stats...")
    l7_stats = load_historical_l7_stats()
    print(f"  {len(l7_stats)} (game_id, team_id) L7 rows computed")

    print("\nBuilding feature matrix...")
    features = build_features(history, stats, starters, l7_df=l7_stats)
    print(f"  {len(features)} games with complete features")
    print(f"  Skipped: {len(history) - len(features)} games (missing stats)")

    starter_coverage = features['has_starter_data'].mean()
    l7_coverage = features['used_l7'].mean()
    print(f"  Starter data coverage: {starter_coverage:.1%} of games")
    print(f"  L7 used for win_pct/runs: {l7_coverage:.1%} | Season fallback: {1-l7_coverage:.1%}")

    print("\nFeature summary:")
    print(features[[
        'elo_diff', 'era_diff', 'whip_diff',
        'ops_diff', 'win_pct_diff', 'starter_era_diff'
    ]].describe().round(4))

    print("\nTraining models...")
    win_model, run_diff_model, scaler, feature_cols, run_diff_residual_std = train(features)
    print("  Training complete")

    X = scaler.transform(features[feature_cols].values)

    # Win probability model stats
    coefs = np.array([cc.estimator.coef_[0] for cc in win_model.calibrated_classifiers_])
    intercepts = np.array([cc.estimator.intercept_[0] for cc in win_model.calibrated_classifiers_])
    mean_coef = coefs.mean(axis=0)
    mean_intercept = intercepts.mean()
    print("\n  [Win model] Feature weights (mean over 5 CV folds):")
    for name, c in sorted(zip(feature_cols, mean_coef), key=lambda x: -abs(x[1])):
        print(f"    {name:20} {c:+.4f}")
    print(f"    {'(intercept)':20} {mean_intercept:+.4f}")

    win_probs = win_model.predict_proba(X)[:, 1]
    brier = np.mean((win_probs - features['home_won'].values) ** 2)
    accuracy = np.mean(
        ((win_probs > 0.5) & (features['home_won'] == 1)) |
        ((win_probs < 0.5) & (features['home_won'] == 0))
    )
    print(f"\n  [Win model] In-sample Brier Score : {brier:.4f}")
    print(f"  [Win model] In-sample Accuracy    : {accuracy:.1%}")

    # Run differential model stats
    run_diff_coefs = run_diff_model.coef_
    print(f"\n  [Run diff model] Feature weights:")
    for name, c in sorted(zip(feature_cols, run_diff_coefs), key=lambda x: -abs(x[1])):
        print(f"    {name:20} {c:+.4f}")
    print(f"  [Run diff model] Residual std     : {run_diff_residual_std:.4f} runs")

    pred_margins = run_diff_model.predict(X)
    margin_mae = np.mean(np.abs(pred_margins - features['run_differential'].values))
    print(f"  [Run diff model] In-sample MAE    : {margin_mae:.4f} runs")
    print(f"\n  (Note: in-sample scores are optimistic — cross-validated scores will be lower)")

    save_model(win_model, run_diff_model, scaler, feature_cols, run_diff_residual_std)
    print(f"\nDone. Model version: {MODEL_VERSION}")


if __name__ == "__main__":
    main()