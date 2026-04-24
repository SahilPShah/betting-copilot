import argparse
import pickle
import os
import sys
import pandas as pd
import numpy as np
from datetime import date as date_cls
from scipy.stats import norm
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

load_dotenv()
engine = create_engine(os.getenv("DATABASE_URL"))

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from models.elo import run_elo
from ingest.mlb_boxscores import compute_starter_eras, compute_l7_ops

MODEL_PATH = os.path.join(os.path.dirname(__file__), 'versions', 'v4_elo_logreg_l7.pkl')
FALLBACK_MODEL_PATH = os.path.join(os.path.dirname(__file__), 'versions', 'v3_elo_logreg_starters.pkl')


def load_model():
    path = MODEL_PATH if os.path.exists(MODEL_PATH) else FALLBACK_MODEL_PATH
    with open(path, 'rb') as f:
        artifact = pickle.load(f)
    print(f"  Model loaded: {artifact['version']} from {os.path.basename(path)}")
    return artifact


def load_todays_games(date_str):
    with engine.connect() as conn:
        df = pd.read_sql(text("""
            SELECT game_id, game_date, home_team_id, away_team_id
            FROM games
            WHERE game_date = :date
            AND status = 'scheduled'
        """), conn, params={"date": date_str})
    return df


def load_team_stats(date_str):
    """Load most recent season stats for each team, preferring current year but falling back to prior year."""
    year = date_str[:4]
    with engine.connect() as conn:
        df = pd.read_sql(text("""
            SELECT DISTINCT ON (team_id)
                team_id, season,
                team_pitching_era, team_pitching_whip, team_pitching_k9,
                team_ops, team_win_pct, runs_scored_avg
            FROM team_season_stats
            WHERE season <= :year
            ORDER BY team_id, season DESC
        """), conn, params={"year": int(year)})
    return df


def load_starter_names(date_str):
    """Load probable starter names for today's games."""
    with engine.connect() as conn:
        df = pd.read_sql(text("""
            SELECT gs.game_id, gs.side, gs.starter_name
            FROM game_starters gs
            JOIN games g ON g.game_id = gs.game_id
            WHERE g.game_date = :date
        """), conn, params={"date": date_str})
    return df


def load_l7_stats(date_str):
    """Load most recent L7 rolling stats per team as of date_str. Returns dict keyed by team_id."""
    with engine.connect() as conn:
        df = pd.read_sql(text("""
            SELECT DISTINCT ON (team_id)
                team_id, l7_win_pct, l7_runs_scored_avg, l7_run_diff_avg, l7_games
            FROM team_stats_mlb
            WHERE as_of_date <= :date
            ORDER BY team_id, as_of_date DESC
        """), conn, params={"date": date_str})
    if df.empty:
        return {}
    return df.set_index('team_id').to_dict('index')


def build_starter_wide(starter_df, date_str):
    """
    Build game_id-indexed DataFrame with home/away starter ERA, WHIP, L3 ERA, and L3 WHIP,
    computed from pitcher_game_logs (not stored in game_starters).
    """
    if starter_df.empty:
        return pd.DataFrame()

    all_names = starter_df['starter_name'].dropna().tolist()
    era_map = compute_starter_eras(all_names, date_str)

    rows = {}
    for _, row in starter_df.iterrows():
        gid = row['game_id']
        side = row['side']
        name = row['starter_name']
        if not name:
            continue
        stats = era_map.get(name, {})
        if gid not in rows:
            rows[gid] = {}
        rows[gid][f'{side}_starter_era'] = stats.get('era')
        rows[gid][f'{side}_starter_whip'] = stats.get('whip')
        rows[gid][f'{side}_starter_l3_era'] = stats.get('l3_era')
        rows[gid][f'{side}_starter_l3_whip'] = stats.get('l3_whip')

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame.from_dict(rows, orient='index')
    df.index.name = 'game_id'
    for col in ['home_starter_era', 'home_starter_whip', 'away_starter_era', 'away_starter_whip',
                'home_starter_l3_era', 'home_starter_l3_whip', 'away_starter_l3_era', 'away_starter_l3_whip']:
        if col not in df.columns:
            df[col] = None
    return df


def build_feature_vector(game, elo_ratings, team_stats, starter_wide, feature_cols,
                         l7_stats=None, l7_ops=None):
    home = game['home_team_id']
    away = game['away_team_id']
    year = int(str(game['game_date'])[:4])

    home_elo = elo_ratings.get(home, 1500)
    away_elo = elo_ratings.get(away, 1500)

    home_stats = team_stats[team_stats['team_id'] == home]
    away_stats = team_stats[team_stats['team_id'] == away]

    if home_stats.empty or away_stats.empty:
        return None

    h = home_stats.iloc[0]
    a = away_stats.iloc[0]

    def safe_float(val, default=0.0):
        try:
            f = float(val)
            return default if np.isnan(f) else f
        except (TypeError, ValueError):
            return default

    # Starter features — prefer L3 ERA if available, fall back to full-window ERA
    gid = game['game_id']
    starter_era_diff = 0.0
    starter_whip_diff = 0.0
    has_starter_data = 0.0

    if not starter_wide.empty and gid in starter_wide.index:
        row_s = starter_wide.loc[gid]
        # Try L3 first, then fall back to season window
        h_era = row_s.get('home_starter_l3_era') or row_s.get('home_starter_era')
        a_era = row_s.get('away_starter_l3_era') or row_s.get('away_starter_era')
        h_whip = row_s.get('home_starter_l3_whip') or row_s.get('home_starter_whip')
        a_whip = row_s.get('away_starter_l3_whip') or row_s.get('away_starter_whip')
        if pd.notna(h_era) and pd.notna(a_era) and pd.notna(h_whip) and pd.notna(a_whip):
            starter_era_diff = float(a_era) - float(h_era)
            starter_whip_diff = float(a_whip) - float(h_whip)
            has_starter_data = 1.0

    # OPS: prefer L7 batting logs if available (≥3 games), fall back to season OPS
    l7_ops = l7_ops or {}
    h_l7 = l7_ops.get(home, {})
    a_l7 = l7_ops.get(away, {})
    if h_l7.get('ops') is not None and h_l7.get('games', 0) >= 3 \
            and a_l7.get('ops') is not None and a_l7.get('games', 0) >= 3:
        ops_diff = safe_float(h_l7['ops']) - safe_float(a_l7['ops'])
    else:
        ops_diff = safe_float(h['team_ops']) - safe_float(a['team_ops'])

    # Win% and runs: prefer L7 rolling stats if available (≥3 games), fall back to season
    l7_stats = l7_stats or {}
    h_l7s = l7_stats.get(home, {})
    a_l7s = l7_stats.get(away, {})
    h_l7_ok = h_l7s.get('l7_games', 0) is not None and (h_l7s.get('l7_games') or 0) >= 3
    a_l7_ok = a_l7s.get('l7_games', 0) is not None and (a_l7s.get('l7_games') or 0) >= 3

    if h_l7_ok and a_l7_ok:
        win_pct_diff = safe_float(h_l7s.get('l7_win_pct')) - safe_float(a_l7s.get('l7_win_pct'))
        runs_diff = safe_float(h_l7s.get('l7_runs_scored_avg')) - safe_float(a_l7s.get('l7_runs_scored_avg'))
    else:
        win_pct_diff = safe_float(h['team_win_pct']) - safe_float(a['team_win_pct'])
        runs_diff = safe_float(h['runs_scored_avg']) - safe_float(a['runs_scored_avg'])

    all_features = {
        'elo_diff': home_elo - away_elo,
        'era_diff': safe_float(a['team_pitching_era']) - safe_float(h['team_pitching_era']),
        'whip_diff': safe_float(a['team_pitching_whip']) - safe_float(h['team_pitching_whip']),
        'k9_diff': safe_float(h['team_pitching_k9']) - safe_float(a['team_pitching_k9']),
        'ops_diff': ops_diff,
        'win_pct_diff': win_pct_diff,
        'runs_diff': runs_diff,
        'starter_era_diff': starter_era_diff,
        'starter_whip_diff': starter_whip_diff,
        'has_starter_data': has_starter_data,
    }

    # Return only the features the model was trained on, in order
    try:
        return [all_features[col] for col in feature_cols]
    except KeyError as e:
        print(f"  WARNING: Missing feature {e} for {gid}, skipping")
        return None


RUN_LINE = 1.5  # standard MLB run line spread


def compute_cover_probs(predicted_margin, residual_std):
    """
    Convert predicted run margin to cover probabilities using a normal distribution.
    P(home covers -1.5) = P(run_diff > 1.5)
    P(away covers +1.5) = P(run_diff < 1.5)
    """
    home_cover = float(1 - norm.cdf(RUN_LINE, loc=predicted_margin, scale=residual_std))
    away_cover = float(norm.cdf(RUN_LINE, loc=predicted_margin, scale=residual_std))
    return home_cover, away_cover


def write_prediction(conn, game_id, home_prob, away_prob, predicted_margin,
                     home_cover_prob, away_cover_prob, model_version, elo_diff=None):
    conn.execute(text("""
        INSERT INTO predictions (
            game_id, model_version, home_win_prob, away_win_prob,
            predicted_margin, home_cover_prob, away_cover_prob, elo_diff, created_at
        )
        VALUES (
            :game_id, :model_version, :home_win_prob, :away_win_prob,
            :predicted_margin, :home_cover_prob, :away_cover_prob, :elo_diff, NOW()
        )
        ON CONFLICT (game_id) DO UPDATE SET
            home_win_prob   = EXCLUDED.home_win_prob,
            away_win_prob   = EXCLUDED.away_win_prob,
            predicted_margin = EXCLUDED.predicted_margin,
            home_cover_prob = EXCLUDED.home_cover_prob,
            away_cover_prob = EXCLUDED.away_cover_prob,
            elo_diff        = EXCLUDED.elo_diff,
            model_version   = EXCLUDED.model_version,
            created_at      = NOW()
    """), {
        "game_id": game_id,
        "model_version": model_version,
        "home_win_prob": round(float(home_prob), 4),
        "away_win_prob": round(float(away_prob), 4),
        "predicted_margin": round(float(predicted_margin), 2) if predicted_margin is not None else None,
        "home_cover_prob": round(float(home_cover_prob), 4) if home_cover_prob is not None else None,
        "away_cover_prob": round(float(away_cover_prob), 4) if away_cover_prob is not None else None,
        "elo_diff": elo_diff,
    })


def main(date_str=None):
    if date_str is None:
        date_str = date_cls.today().strftime('%Y-%m-%d')

    print(f"Running predictions for {date_str}")

    print("  Loading model...")
    artifact = load_model()
    model = artifact['model']
    run_diff_model = artifact.get('run_diff_model')
    run_diff_residual_std = artifact.get('run_diff_residual_std', 2.8)
    scaler = artifact['scaler']
    feature_cols = artifact['feature_cols']
    model_version = artifact['version']
    k = artifact['k']
    k_current = artifact.get('k_current', k)
    divisor = artifact['divisor']
    season_regress = artifact.get('season_regress', 1.0)
    decay_half_life = artifact.get('decay_half_life', 9999)

    print("  Running ELO to get current team ratings...")
    year = date_str[:4]
    seasons = list(dict.fromkeys(['2023', '2024', '2025', year]))
    elo_ratings, _ = run_elo(
        seasons, k=k, k_current=k_current, divisor=divisor,
        season_regress=season_regress, decay_half_life=decay_half_life,
    )

    print(f"  Loading today's games for {date_str}...")
    games = load_todays_games(date_str)
    print(f"  {len(games)} scheduled games found")

    if games.empty:
        print("  No scheduled games. Exiting.")
        return []

    print("  Loading team stats...")
    team_stats = load_team_stats(date_str)

    print("  Loading starter stats...")
    starter_df = load_starter_names(date_str)
    starter_wide = build_starter_wide(starter_df, date_str)

    print("  Loading L7 rolling stats...")
    l7_stats = load_l7_stats(date_str)
    all_team_ids = list(games['home_team_id']) + list(games['away_team_id'])
    l7_ops = compute_l7_ops(all_team_ids, date_str)
    print(f"  L7 stats: {len(l7_stats)} teams with rolling data, "
          f"{sum(1 for v in l7_ops.values() if v['ops'] is not None)} teams with L7 OPS")

    predictions = []
    skipped = 0

    for _, game in games.iterrows():
        features = build_feature_vector(
            game, elo_ratings, team_stats, starter_wide, feature_cols,
            l7_stats=l7_stats, l7_ops=l7_ops,
        )
        if features is None:
            print(f"  Skipping {game['game_id']} — missing team stats")
            skipped += 1
            continue

        X = scaler.transform([features])

        # Win probability model
        probs = model.predict_proba(X)[0]
        home_prob = float(probs[1])
        away_prob = float(probs[0])

        # Run differential model → cover probabilities
        if run_diff_model is not None:
            predicted_margin = float(run_diff_model.predict(X)[0])
            home_cover, away_cover = compute_cover_probs(predicted_margin, run_diff_residual_std)
        else:
            predicted_margin = None
            home_cover = away_cover = None

        home_elo = elo_ratings.get(game['home_team_id'], 1500)
        away_elo = elo_ratings.get(game['away_team_id'], 1500)

        predictions.append({
            'game_id': game['game_id'],
            'home_team_id': game['home_team_id'],
            'away_team_id': game['away_team_id'],
            'home_win_prob': home_prob,
            'away_win_prob': away_prob,
            'predicted_margin': predicted_margin,
            'home_cover_prob': home_cover,
            'away_cover_prob': away_cover,
            'elo_diff': round(home_elo - away_elo, 1),
        })

    print(f"  Writing {len(predictions)} predictions to DB ({skipped} skipped)...")
    with engine.connect() as conn:
        for pred in predictions:
            write_prediction(
                conn, pred['game_id'],
                pred['home_win_prob'], pred['away_win_prob'],
                pred['predicted_margin'], pred['home_cover_prob'], pred['away_cover_prob'],
                model_version, elo_diff=pred.get('elo_diff'),
            )
        conn.commit()

    print(f"  Done.")
    for pred in predictions:
        margin = pred['predicted_margin']
        margin_str = f"  margin={margin:+.1f}" if margin is not None else ""
        print(f"    {pred['game_id']}: home={pred['home_win_prob']:.3f} away={pred['away_win_prob']:.3f}{margin_str}")

    return predictions


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--date', default=date_cls.today().strftime('%Y-%m-%d'))
    args = parser.parse_args()
    main(args.date)
