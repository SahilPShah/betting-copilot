import os
import pandas as pd
from sqlalchemy import text

_CAL_TABLE = None  # module-level cache


def _load_calibration_table():
    """
    Load calibration error data from eval/results/calibration_data.csv.
    Returns list of (low, high, error) tuples, or None if file not found.
    """
    cal_path = os.path.join(os.path.dirname(__file__), '..', 'eval', 'results', 'calibration_data.csv')
    cal_path = os.path.normpath(cal_path)
    if not os.path.exists(cal_path):
        return None
    df = pd.read_csv(cal_path)
    table = []
    for _, row in df.iterrows():
        low, high = (float(x) for x in row['bucket'].split('-'))
        table.append((low, high, float(row['error'])))
    return table


def _get_calibration_table():
    global _CAL_TABLE
    if _CAL_TABLE is None:
        _CAL_TABLE = _load_calibration_table()
    return _CAL_TABLE


def _lookup_bucket_score(model_prob, cal_table):
    """
    Map model_prob to calibration score (1–10) based on measured error in that bucket.
    Formula: 10 - error * 50  (error 0.0 → 10, error 0.10 → 5, error 0.20 → 0)
    Falls back to 7.0 if cal_table is None or prob doesn't match any bucket.
    """
    if cal_table is None:
        return 7.0
    for low, high, error in cal_table:
        if low <= model_prob <= high:
            return max(1.0, min(10.0, 10.0 - error * 50.0))
    return 7.0


def compute_confidence(edge, model_prob, elo_diff, has_injury, injury_severity, games_played=0,
                       signal_results=None):
    """
    Composite confidence score 1-10, equal-weighted across 4 components (25% each):

    1. Edge magnitude:       min(10, abs(edge) / 0.01)  — 1% edge = 1pt, 10%+ = 10
    2. Model conviction:     min(10, abs(model_prob - 0.5) * 20) — 50% = 0, 75% = 5, 100% = 10
    3. Historical calibration: bucket score from calibration_data.csv (how accurate the model
       is at this probability level), minus a sample-size penalty for early season:
         bucket_score  = 10 - calibration_error * 50  (from eval/results/calibration_data.csv)
         sample_penalty = 4.0 * max(0, 1 - games_played / 30)
                          → 4.0 at 0 games, 3.5 at 4 games, 2.0 at 15 games, 0 at 30+
         calibration_score = max(1.0, bucket_score - sample_penalty)
    4. Injury certainty:     10 if no injury, 6 if questionable, 3 if key player out

    Optional signal_results: list of SignalResult objects from LLM-based signals
    (e.g. injury severity override, weather). When provided, their scores are averaged
    and added as a fifth equal-weight component. See llm/signals.py.

    Final composite is scaled by an early-season multiplier applied to the whole score:
        sample_multiplier = max(0.7, games_played / 30)
        → 0.70 at 0 games, 0.73 at 4 games, 0.83 at 15 games, 1.0 at 30+
    """
    edge_score = min(10.0, abs(edge) / 0.01)
    conviction_score = min(10.0, abs(model_prob - 0.5) * 20.0)

    cal_table = _get_calibration_table()
    bucket_score = _lookup_bucket_score(model_prob, cal_table)
    sample_penalty = 4.0 * max(0.0, 1.0 - games_played / 30.0)
    calibration_score = max(1.0, bucket_score - sample_penalty)

    if not has_injury:
        injury_score = 10.0
    elif injury_severity == 'questionable':
        injury_score = 6.0
    else:
        injury_score = 3.0

    components = [edge_score, conviction_score, calibration_score, injury_score]

    if signal_results:
        signal_avg = sum(s.score for s in signal_results) / len(signal_results)
        components.append(signal_avg)

    composite = sum(components) / len(components)

    # Early-season multiplier: scales composite down linearly from 0.7 at 0 games to 1.0 at 30+
    sample_multiplier = max(0.7, games_played / 30.0)
    composite = composite * sample_multiplier

    return round(max(1.0, min(10.0, composite)), 2)


def load_injury_flags(date_str, engine):
    """Load active injury flags for today's games."""
    with engine.connect() as conn:
        df = pd.read_sql(text("""
            SELECT i.game_id, i.team_id, i.player_name, i.status
            FROM injury_statuses i
            JOIN games g ON g.game_id = i.game_id
            WHERE g.game_date = :date
            AND i.status IN ('out', 'questionable')
        """), conn, params={"date": date_str})
    return df


def get_injury_context(game_id, team_id, injuries_df):
    """
    Returns (has_injury: bool, severity: str|None) for a team in a game.
    Worst case takes precedence: 'out' > 'questionable'.
    """
    if injuries_df is None or injuries_df.empty:
        return False, None

    game_injuries = injuries_df[
        (injuries_df['game_id'] == game_id) &
        (injuries_df['team_id'] == team_id)
    ]

    if game_injuries.empty:
        return False, None

    if (game_injuries['status'] == 'out').any():
        return True, 'out'

    return True, 'questionable'
