import requests
import argparse
from datetime import date as date_cls, datetime, timezone
from sqlalchemy import create_engine, text
from dotenv import load_dotenv
import os

load_dotenv("/Users/sahilshah/betting-copilot/.env")
engine = create_engine(os.getenv("DATABASE_URL"))
API_KEY = os.getenv("ODDS_API_KEY")

LIVE_URL = "https://api.the-odds-api.com/v4/sports/baseball_mlb/odds"

# Priority order for bookmaker selection
BOOKMAKER_PRIORITY = ['draftkings', 'fanduel', 'betmgm']

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

SKIP_TEAMS = {'American League', 'National League'}


def implied_prob_from_american(odds):
    if odds > 0:
        return 100 / (odds + 100)
    else:
        return abs(odds) / (abs(odds) + 100)


def remove_vig(home_prob, away_prob):
    total = home_prob + away_prob
    return home_prob / total, away_prob / total


def fetch_live_odds():
    params = {
        "apiKey": API_KEY,
        "regions": "us",
        "markets": "h2h,spreads",
        "oddsFormat": "american",
        "bookmakers": ",".join(BOOKMAKER_PRIORITY),
    }
    response = requests.get(LIVE_URL, params=params)
    if response.status_code != 200:
        print(f"  ERROR: Odds API returned {response.status_code}")
        return [], None
    remaining = response.headers.get('X-Requests-Remaining', '?')
    return response.json(), remaining


def match_game_id(home_abbr, away_abbr, date_str, conn):
    result = conn.execute(text("""
        SELECT game_id FROM games
        WHERE home_team_id = :home
        AND away_team_id = :away
        AND game_date = :date
        LIMIT 1
    """), {"home": home_abbr, "away": away_abbr, "date": date_str})
    row = result.fetchone()
    return row[0] if row else None


def select_bookmaker(bookmakers):
    """Return the highest-priority bookmaker available."""
    bk_by_key = {bk['key']: bk for bk in bookmakers}
    for priority_key in BOOKMAKER_PRIORITY:
        if priority_key in bk_by_key:
            return bk_by_key[priority_key]
    return bookmakers[0] if bookmakers else None


def parse_and_insert(api_games, date_str, conn):
    inserted = 0
    skipped = 0
    captured_at = datetime.now(timezone.utc).isoformat()

    for game in api_games:
        home_name = game['home_team']
        away_name = game['away_team']

        if home_name in SKIP_TEAMS or away_name in SKIP_TEAMS:
            continue

        home_abbr = TEAM_NAME_TO_ID.get(home_name)
        away_abbr = TEAM_NAME_TO_ID.get(away_name)

        if not home_abbr or not away_abbr:
            print(f"  WARNING: Unknown team: {home_name} or {away_name}")
            continue

        game_id = match_game_id(home_abbr, away_abbr, date_str, conn)
        if not game_id:
            skipped += 1
            continue

        bookmakers = game.get('bookmakers', [])
        if not bookmakers:
            continue

        bk = select_bookmaker(bookmakers)
        if not bk:
            continue

        bookmaker_key = bk['key']
        bk_captured_at = bk.get('last_update', captured_at)

        for market in bk['markets']:
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
                counterpart = next((o for o in outcomes if o['name'] != team_name), None)
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
                """), {
                    "game_id": game_id,
                    "bookmaker": bookmaker_key,
                    "market": market_type,
                    "side": side,
                    "american_odds": american_odds,
                    "run_line_point": run_line_point,
                    "implied_prob": round(fair_prob, 4),
                    "captured_at": bk_captured_at,
                    "is_closing": False,
                })
                inserted += 1

    return inserted, skipped


def main(date_str=None):
    if date_str is None:
        date_str = date_cls.today().strftime('%Y-%m-%d')

    print(f"  Fetching live odds for {date_str}...")
    api_games, remaining = fetch_live_odds()

    if not api_games:
        print("  No odds data returned.")
        return

    print(f"  {len(api_games)} games from Odds API (requests remaining: {remaining})")

    with engine.connect() as conn:
        inserted, skipped = parse_and_insert(api_games, date_str, conn)
        conn.commit()

    print(f"  {inserted} odds rows inserted, {skipped} games not matched in DB")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--date', default=date_cls.today().strftime('%Y-%m-%d'))
    args = parser.parse_args()
    main(args.date)
