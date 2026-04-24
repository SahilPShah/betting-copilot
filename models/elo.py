import math
import pandas as pd
from datetime import date as date_cls
from sqlalchemy import create_engine, text
from dotenv import load_dotenv
import os

load_dotenv("/Users/sahilshah/betting-copilot/.env")
engine = create_engine(os.getenv("DATABASE_URL"))

# ELO configuration
K_PRIOR = 10            # K-factor for prior season games
K_CURRENT = 20          # K-factor for current season games (more responsive to recent results)
DEFAULT_RATING = 1500
DEFAULT_DIVISOR = 800
HOME_FIELD_ADVANTAGE = 35
SEASON_REGRESS = 0.75   # Fraction of prior rating retained at season reset; rest pulls toward 1500
DECAY_HALF_LIFE = 365   # Days; a game this old contributes half the K-weight of today's game


def expected_result(rating_a, rating_b, divisor=DEFAULT_DIVISOR):
    """Expected win probability for team A against team B."""
    return 1 / (1 + 10 ** ((rating_b - rating_a) / divisor))


def update_rating(rating, expected, actual, k):
    """Update ELO rating after a game result."""
    return rating + k * (actual - expected)


def compute_k(game_date, current_season, reference_date,
              k_prior=K_PRIOR, k_current=K_CURRENT, decay_half_life=DECAY_HALF_LIFE):
    """
    Effective K = base_k × recency_decay.

    Fix 2 — base_k: K_CURRENT for current season games, K_PRIOR for prior seasons.
    Fix 3 — recency_decay: exponential decay so older games have diminishing impact.
      decay = exp(-days_ago × ln(2) / half_life)
      → game from today:      decay = 1.00  (full weight)
      → game from 1 year ago: decay = 0.50  (half weight)
      → game from 2 years ago: decay = 0.25 (quarter weight)
    """
    game_year = int(str(game_date)[:4])
    base_k = k_current if game_year == current_season else k_prior

    gdate = game_date.date() if hasattr(game_date, 'date') else game_date
    days_ago = max(0, (reference_date - gdate).days)
    decay = math.exp(-days_ago * math.log(2) / decay_half_life)

    return base_k * decay


def load_games(seasons):
    """Load all completed games ordered chronologically."""
    with engine.connect() as conn:
        df = pd.read_sql(text("""
            SELECT game_id, game_date, home_team_id, away_team_id,
                   home_score, away_score
            FROM games
            WHERE status = 'final'
            AND LEFT(game_date::text, 4) = ANY(:seasons)
            ORDER BY game_date ASC
        """), conn, params={"seasons": seasons})
    return df


def run_elo(seasons, k=K_PRIOR, k_current=K_CURRENT, divisor=DEFAULT_DIVISOR,
            season_regress=SEASON_REGRESS, decay_half_life=DECAY_HALF_LIFE,
            reference_date=None):
    """
    Run ELO simulation with three enhancements over a naive equal-weight replay:

    Fix 1 — Season reset: at each season boundary, ratings regress toward the mean:
        new_rating = season_regress × old_rating + (1 - season_regress) × 1500
      This accounts for offseason roster changes that ELO can't see.

    Fix 2 — Current season K: games in the current season use k_current (default 20)
      instead of k (default 10), making the model more responsive to recent results.

    Fix 3 — Recency decay: effective K decays exponentially with game age so that
      old games have diminishing influence on today's ratings.

    Args:
        seasons       : list of season year strings e.g. ['2023', '2024', '2025']
        k             : base K-factor for prior season games (default 10)
        k_current     : K-factor for current season games (default 20)
        divisor       : ELO divisor controlling rating gap sensitivity (default 800)
        season_regress: fraction of prior rating retained on season reset (default 0.75)
        decay_half_life: days until a game's K contribution halves (default 365)
        reference_date: date to measure recency from (defaults to today)

    Returns:
        ratings  — final ELO rating per team {team_id: float}
        history  — DataFrame with one row per game, pre-game ratings and outcomes
    """
    if reference_date is None:
        reference_date = date_cls.today()

    current_season = max(int(s) for s in seasons)
    games = load_games(seasons)
    ratings = {}
    history = []
    prev_season = None

    for _, game in games.iterrows():
        home = game['home_team_id']
        away = game['away_team_id']
        game_season = int(str(game['game_date'])[:4])

        # Fix 1: Apply season reset at each season boundary
        if prev_season is not None and game_season != prev_season:
            for team in ratings:
                ratings[team] = (
                    season_regress * ratings[team]
                    + (1 - season_regress) * DEFAULT_RATING
                )

        prev_season = game_season

        if home not in ratings:
            ratings[home] = DEFAULT_RATING
        if away not in ratings:
            ratings[away] = DEFAULT_RATING

        home_rating = ratings[home]
        away_rating = ratings[away]

        home_expected = expected_result(
            home_rating + HOME_FIELD_ADVANTAGE, away_rating, divisor
        )
        away_expected = 1 - home_expected

        home_won = 1 if game['home_score'] > game['away_score'] else 0
        away_won = 1 - home_won

        history.append({
            "game_id": game['game_id'],
            "game_date": game['game_date'],
            "home_team_id": home,
            "away_team_id": away,
            "home_elo_pre": home_rating,
            "away_elo_pre": away_rating,
            "home_expected": round(home_expected, 4),
            "away_expected": round(away_expected, 4),
            "home_actual": home_won,
            "away_actual": away_won,
            "home_score": game['home_score'],
            "away_score": game['away_score'],
        })

        # Fix 2 + 3: Effective K with recency decay and current-season boost
        effective_k = compute_k(
            game['game_date'], current_season, reference_date,
            k_prior=k, k_current=k_current, decay_half_life=decay_half_life,
        )

        ratings[home] = update_rating(home_rating, home_expected, home_won, effective_k)
        ratings[away] = update_rating(away_rating, away_expected, away_won, effective_k)

    return ratings, pd.DataFrame(history)


if __name__ == "__main__":
    print(f"Running ELO with K_PRIOR={K_PRIOR}, K_CURRENT={K_CURRENT}, "
          f"SEASON_REGRESS={SEASON_REGRESS}, DECAY_HALF_LIFE={DECAY_HALF_LIFE}d...")
    seasons = ['2023', '2024', '2025']
    ratings, history = run_elo(seasons)

    print(f"\nGames processed: {len(history)}")
    print(f"Teams rated: {len(ratings)}")

    print("\nTop 10 teams by final ELO rating:")
    sorted_ratings = sorted(ratings.items(), key=lambda x: x[1], reverse=True)
    for team, rating in sorted_ratings[:10]:
        print(f"  {team}: {rating:.1f}")

    print("\nBottom 5 teams:")
    for team, rating in sorted_ratings[-5:]:
        print(f"  {team}: {rating:.1f}")
