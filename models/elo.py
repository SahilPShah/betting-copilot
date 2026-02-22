import pandas as pd
from sqlalchemy import create_engine, text
from dotenv import load_dotenv
import os

load_dotenv("/Users/sahilshah/betting-copilot/.env")
engine = create_engine(os.getenv("DATABASE_URL"))

#ELO configuration
K = 10
DEFAULT_RATING = 1500
DEFAULT_DIVISOR = 800
HOME_FIELD_ADVANTAGE = 35


def expected_result(rating_a, rating_b, divisor=DEFAULT_DIVISOR):
    """Expected win probability for team A against team B."""
    return 1 / (1 + 10 ** ((rating_b - rating_a) / divisor))


def update_rating(rating, expected, actual, k=K):
    """Update ELO rating after a game result."""
    return rating + k * (actual - expected)


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


def run_elo(seasons, k=K, divisor=DEFAULT_DIVISOR):
    """
    Run ELO simulation across all games in given seasons.
    Returns:
        ratings  — final ELO rating per team
        history  — per-game record with pre-game ratings and predictions
    """
    games = load_games(seasons)
    ratings = {}
    history = []

    for _, game in games.iterrows():
        home = game['home_team_id']
        away = game['away_team_id']

        if home not in ratings:
            ratings[home] = DEFAULT_RATING
        if away not in ratings:
            ratings[away] = DEFAULT_RATING

        home_rating = ratings[home]
        away_rating = ratings[away]

        # Apply home field advantage
        home_expected = expected_result(
            home_rating + HOME_FIELD_ADVANTAGE, away_rating, divisor
        )
        away_expected = 1 - home_expected

        # Actual result
        home_won = 1 if game['home_score'] > game['away_score'] else 0
        away_won = 1 - home_won

        # Record pre-game state for evaluation
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

        # Update ratings
        ratings[home] = update_rating(home_rating, home_expected, home_won, k)
        ratings[away] = update_rating(away_rating, away_expected, away_won, k)

    return ratings, pd.DataFrame(history)


if __name__ == "__main__":
    print(f"Running ELO with K={K}, divisor={DEFAULT_DIVISOR}...")
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

    print("\nSample predictions (first 5 games):")
    print(history[['game_date', 'home_team_id', 'away_team_id',
                   'home_expected', 'home_actual']].head())