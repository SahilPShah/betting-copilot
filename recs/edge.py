import pandas as pd
from sqlalchemy import text


def load_current_odds(date_str, engine):
    """Load the most recent odds snapshot per game+market+side for today's games."""
    with engine.connect() as conn:
        df = pd.read_sql(text("""
            SELECT DISTINCT ON (o.game_id, o.market, o.side)
                o.snapshot_id, o.game_id, o.market, o.side,
                o.american_odds, o.run_line_point, o.implied_prob, o.bookmaker
            FROM odds_snapshots o
            JOIN games g ON g.game_id = o.game_id
            WHERE g.game_date = :date
            ORDER BY o.game_id, o.market, o.side, o.captured_at DESC
        """), conn, params={"date": date_str})
    return df


def load_predictions(date_str, engine):
    """Load predictions for today's games, including cover probabilities."""
    with engine.connect() as conn:
        df = pd.read_sql(text("""
            SELECT p.prediction_id, p.game_id, p.model_version,
                   p.home_win_prob, p.away_win_prob,
                   p.predicted_margin, p.home_cover_prob, p.away_cover_prob,
                   p.elo_diff
            FROM predictions p
            JOIN games g ON g.game_id = p.game_id
            WHERE g.game_date = :date
        """), conn, params={"date": date_str})
    return df


def compute_edges(predictions_df, odds_df):
    """
    Compute edge = model_prob - implied_prob for each game+market+side.

    Moneyline: model_prob = win probability (from logistic regression)
    Run line:  model_prob = cover probability (from run differential model)
               Falls back to win probability if cover probs not available.

    Returns DataFrame with one row per (game, market, side).
    """
    rows = []

    for _, pred in predictions_df.iterrows():
        game_odds = odds_df[odds_df['game_id'] == pred['game_id']]
        has_cover_prob = (
            pred.get('home_cover_prob') is not None
            and pred.get('away_cover_prob') is not None
            and pd.notna(pred.get('home_cover_prob'))
            and pd.notna(pred.get('away_cover_prob'))
        )

        for _, odds_row in game_odds.iterrows():
            market = odds_row['market']
            side = odds_row['side']

            if market == 'run_line' and has_cover_prob:
                # Use dedicated cover probability model
                model_prob = pred['home_cover_prob'] if side == 'home' else pred['away_cover_prob']
            else:
                # Moneyline (or fallback): use win probability model
                model_prob = pred['home_win_prob'] if side == 'home' else pred['away_win_prob']

            edge = float(model_prob) - float(odds_row['implied_prob'])

            rows.append({
                'game_id': pred['game_id'],
                'prediction_id': pred['prediction_id'],
                'snapshot_id': odds_row['snapshot_id'],
                'model_version': pred['model_version'],
                'market': market,
                'side': side,
                'model_prob': float(model_prob),
                'implied_prob': float(odds_row['implied_prob']),
                'edge': edge,
                'american_odds': int(odds_row['american_odds']),
                'run_line_point': odds_row['run_line_point'],
                'bookmaker': odds_row['bookmaker'],
                'predicted_margin': float(pred['predicted_margin']) if pd.notna(pred.get('predicted_margin')) else None,
            })

    return pd.DataFrame(rows) if rows else pd.DataFrame()
