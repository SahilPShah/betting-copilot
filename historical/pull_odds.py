import requests
import pandas as pd
from sqlalchemy import create_engine, text
from dotenv import load_dotenv
import os
import time
from datetime import datetime, timedelta

load_dotenv("/Users/sahilshah/betting-copilot/.env")
engine = create_engine(os.getenv("DATABASE_URL"))
API_KEY = os.getenv("ODDS_API_KEY")

BASE_URL = "https://api.the-odds-api.com/v4/historical/sports/baseball_mlb/odds"

TEAM_NAME_TO_ID = {
    'Arizona Diamondbacks': 'ARI',
    'Atlanta Braves': 'ATL',
    'Baltimore Orioles': 'BAL',
    'Boston Red Sox': 'BOS',
    'Chicago Cubs': 'CHC',
    'Chicago White Sox': 'CHW',
    'Cincinnati Reds': 'CIN',
    'Cleveland Guardians': 'CLE',
    'Colorado Rockies': 'COL',
    'Detroit Tigers': 'DET',
    'Houston Astros': 'HOU',
    'Kansas City Royals': 'KCR',
    'Los Angeles Angels': 'LAA',
    'Los Angeles Dodgers': 'LAD',
    'Miami Marlins': 'MIA',
    'Milwaukee Brewers': 'MIL',
    'Minnesota Twins': 'MIN',
    'New York Mets': 'NYM',
    'New York Yankees': 'NYY',
    'Oakland Athletics': 'OAK',
    'Athletics': 'ATH',
    'Philadelphia Phillies': 'PHI',
    'Pittsburgh Pirates': 'PIT',
    'San Diego Padres': 'SDP',
    'San Francisco Giants': 'SFG',
    'Seattle Mariners': 'SEA',
    'St. Louis Cardinals': 'STL',
    'Tampa Bay Rays': 'TBR',
    'Texas Rangers': 'TEX',
    'Toronto Blue Jays': 'TOR',
    'Washington Nationals': 'WSN',
}

# All-star / exhibition teams to skip
SKIP_TEAMS = {'American League', 'National League'}


def implied_prob_from_american(odds):
    """Convert American odds to vig-removed implied probability."""
    if odds > 0:
        return 100 / (odds + 100)
    else:
        return abs(odds) / (abs(odds) + 100)


def remove_vig(home_prob, away_prob):
    """Remove vig to get fair implied probabilities."""
    total = home_prob + away_prob
    return home_prob / total, away_prob / total


def load_game_dates():
    """Load all unique game dates from DB."""
    with engine.connect() as conn:
        df = pd.read_sql(text("""
            SELECT DISTINCT game_date
            FROM games
            WHERE status = 'final'
            AND LEFT(game_date::text, 4) IN ('2023', '2024', '2025')
            ORDER BY game_date ASC
        """), conn)
    return df['game_date'].tolist()


def fetch_odds_for_date(game_date):
    """
    Fetch closing odds snapshot for a given date.
    Query at 23:55 UTC to get pre-game closing lines.
    """
    date_str = f"{game_date}T23:55:00Z"

    params = {
        "apiKey": API_KEY,
        "regions": "us",
        "markets": "h2h,spreads",
        "oddsFormat": "american",
        "date": date_str,
        "bookmakers": "draftkings"
    }

    response = requests.get(BASE_URL, params=params)

    if response.status_code != 200:
        print(f"  ❌ API error {response.status_code} for {game_date}")
        return []

    remaining = response.headers.get('X-Requests-Remaining', '?')
    data = response.json()
    games = data.get('data', [])

    return games, remaining


def match_game_id(home_abbr, away_abbr, game_date, conn):
    """Find matching game_id in DB for this home/away/date combination."""
    result = conn.execute(text("""
        SELECT game_id FROM games
        WHERE home_team_id = :home
        AND away_team_id = :away
        AND game_date = :date
        AND status = 'final'
        LIMIT 1
    """), {"home": home_abbr, "away": away_abbr, "date": str(game_date)})
    row = result.fetchone()
    return row[0] if row else None


def parse_and_insert(games, game_date, conn):
    """Parse odds response and insert into odds_snapshots."""
    inserted = 0
    skipped = 0

    for game in games:
        home_name = game['home_team']
        away_name = game['away_team']

        # Skip All-Star / exhibition games
        if home_name in SKIP_TEAMS or away_name in SKIP_TEAMS:
            continue

        home_abbr = TEAM_NAME_TO_ID.get(home_name)
        away_abbr = TEAM_NAME_TO_ID.get(away_name)

        if not home_abbr or not away_abbr:
            print(f"  ⚠️  Unknown team: {home_name} or {away_name}")
            continue

        game_id = match_game_id(home_abbr, away_abbr, game_date, conn)
        if not game_id:
            skipped += 1
            continue

        bookmakers = game.get('bookmakers', [])
        if not bookmakers:
            continue

        dk = bookmakers[0]
        captured_at = dk['last_update']

        for market in dk['markets']:
            market_key = market['key']
            market_type = 'moneyline' if market_key == 'h2h' else 'run_line'
            outcomes = market['outcomes']

            for outcome in outcomes:
                team_name = outcome['name']
                team_abbr = TEAM_NAME_TO_ID.get(team_name)
                if not team_abbr:
                    continue

                side = 'home' if team_abbr == home_abbr else 'away'
                american_odds = outcome['price']
                run_line_point = outcome.get('point')

                raw_prob = implied_prob_from_american(american_odds)

                # Get counterpart odds for vig removal
                counterpart = next(
                    (o for o in outcomes if o['name'] != team_name), None
                )
                if counterpart:
                    counter_prob = implied_prob_from_american(counterpart['price'])
                    fair_prob, _ = remove_vig(raw_prob, counter_prob)
                else:
                    fair_prob = raw_prob

                conn.execute(text("""
                    INSERT INTO odds_snapshots (
                        game_id, bookmaker, market, side,
                        american_odds, run_line_point,
                        implied_prob, captured_at, is_closing
                    )
                    VALUES (
                        :game_id, :bookmaker, :market, :side,
                        :american_odds, :run_line_point,
                        :implied_prob, :captured_at, :is_closing
                    )
                    ON CONFLICT DO NOTHING
                """), {
                    "game_id": game_id,
                    "bookmaker": "draftkings",
                    "market": market_type,
                    "side": side,
                    "american_odds": american_odds,
                    "run_line_point": run_line_point,
                    "implied_prob": round(fair_prob, 4),
                    "captured_at": captured_at,
                    "is_closing": True
                })
                inserted += 1

    return inserted, skipped


def main():
    dates = load_game_dates()
    print(f"Total dates to process: {len(dates)}")

    total_inserted = 0
    total_skipped = 0
    requests_used = 0

    with engine.connect() as conn:
        for i, game_date in enumerate(dates):
            print(f"  [{i+1}/{len(dates)}] {game_date}...", end=" ")

            result = fetch_odds_for_date(game_date)
            if not result:
                print("skipped")
                continue

            games, remaining = result
            inserted, skipped = parse_and_insert(games, game_date, conn)
            conn.commit()

            requests_used += 1
            total_inserted += inserted
            total_skipped += skipped

            print(f"{len(games)} games, {inserted} odds rows inserted "
                  f"(requests remaining: {remaining})")

            time.sleep(1)  # stay polite to the API

    print(f"\nDone.")
    print(f"  Total requests used  : {requests_used}")
    print(f"  Total odds inserted  : {total_inserted}")
    print(f"  Games not matched    : {total_skipped}")


if __name__ == "__main__":
    main()