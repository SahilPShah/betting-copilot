import re
import pybaseball
from pybaseball import schedule_and_record
import pandas as pd
from sqlalchemy import create_engine, text
from dotenv import load_dotenv
import os
import time

load_dotenv("/Users/sahilshah/betting-copilot/.env")
engine = create_engine(os.getenv("DATABASE_URL"))

MLB_TEAMS = [
    'ARI', 'ATH', 'ATL', 'BAL', 'BOS', 'CHC', 'CHW', 'CIN', 'CLE', 'COL', 'DET',
    'HOU', 'KCR', 'LAA', 'LAD', 'MIA', 'MIL', 'MIN', 'NYM', 'NYY', 'OAK',
    'PHI', 'PIT', 'SDP', 'SFG', 'SEA', 'STL', 'TBR', 'TEX', 'TOR', 'WSN'
]

MLB_TEAM_DATA = [
    ('ARI', 'Arizona Diamondbacks', 'NL West'),
    ('ATL', 'Atlanta Braves', 'NL East'),
    ('BAL', 'Baltimore Orioles', 'AL East'),
    ('BOS', 'Boston Red Sox', 'AL East'),
    ('CHC', 'Chicago Cubs', 'NL Central'),
    ('CHW', 'Chicago White Sox', 'AL Central'),
    ('CIN', 'Cincinnati Reds', 'NL Central'),
    ('CLE', 'Cleveland Guardians', 'AL Central'),
    ('COL', 'Colorado Rockies', 'NL West'),
    ('DET', 'Detroit Tigers', 'AL Central'),
    ('HOU', 'Houston Astros', 'AL West'),
    ('KCR', 'Kansas City Royals', 'AL Central'),
    ('LAA', 'Los Angeles Angels', 'AL West'),
    ('LAD', 'Los Angeles Dodgers', 'NL West'),
    ('MIA', 'Miami Marlins', 'NL East'),
    ('MIL', 'Milwaukee Brewers', 'NL Central'),
    ('MIN', 'Minnesota Twins', 'AL Central'),
    ('NYM', 'New York Mets', 'NL East'),
    ('NYY', 'New York Yankees', 'AL East'),
    ('OAK', 'Oakland Athletics', 'AL West'),
    ('PHI', 'Philadelphia Phillies', 'NL East'),
    ('PIT', 'Pittsburgh Pirates', 'NL Central'),
    ('SDP', 'San Diego Padres', 'NL West'),
    ('SFG', 'San Francisco Giants', 'NL West'),
    ('SEA', 'Seattle Mariners', 'AL West'),
    ('STL', 'St. Louis Cardinals', 'NL Central'),
    ('TBR', 'Tampa Bay Rays', 'AL East'),
    ('TEX', 'Texas Rangers', 'AL West'),
    ('TOR', 'Toronto Blue Jays', 'AL East'),
    ('WSN', 'Washington Nationals', 'NL East'),
    ('ATH', 'Athletics', 'AL West'),
]

SEASONS = [2023,2024, 2025]


def build_game_id(date_str, home_team, away_team, game_num=1):
    return f"{date_str}-{home_team}-{away_team}-{game_num}"


def parse_game_num(date_raw):
    match = re.search(r'\((\d)\)', str(date_raw))
    return int(match.group(1)) if match else 1


def parse_date(date_raw, year):
    clean = re.sub(r'\(\d\)', '', str(date_raw)).strip()
    # Skip month-only header rows inserted by baseball-reference
    if clean in ('April', 'May', 'June', 'July', 'August', 'September', 'October'):
        return None
    return pd.to_datetime(f"{clean}, {year}", format="%A, %b %d, %Y").strftime("%Y-%m-%d")


def safe_int(value):
    try:
        return int(float(value))
    except (ValueError, TypeError):
        return None


def parse_season(team, year):
    url = f"https://www.baseball-reference.com/teams/{team}/{year}-schedule-scores.shtml"
    try:
        tables = pd.read_html(url)
        df = tables[0]
    except Exception as e:
        print(f"FAILED {team} {year}: {type(e).__name__}: {e}")
        return []

    # Rename columns to match expected format
    df = df[df['Date'] != 'Date'].copy()  # drop repeated header rows
    df = df.rename(columns={'Unnamed: 4': 'Home_Away'})

    # Filter home games only
    home_games = df[df['Home_Away'] != '@'].copy()

    if home_games.empty:
        print(f"  No home games found for {team} {year}")
        return []

    games = []
    for _, row in home_games.iterrows():
        if pd.isna(row.get('R')) or pd.isna(row.get('RA')):
            continue

        try:
            date_str = parse_date(row['Date'], year)
            game_num = parse_game_num(row['Date'])
        except Exception as e:
            print(f"  Could not parse date '{row['Date']}' for {team} {year}: {e}")
            continue

        if date_str is None:
            continue

        home_score = safe_int(row.get('R'))
        away_score = safe_int(row.get('RA'))

        if home_score is None or away_score is None:
            continue

        away_team = row.get('Opp')
        if pd.isna(away_team):
            continue

        game_id = build_game_id(date_str, team, away_team, game_num)

        games.append({
            "game_id": game_id,
            "game_date": date_str,
            "home_team_id": team,
            "away_team_id": away_team,
            "home_score": home_score,
            "away_score": away_score,
            "status": "final",
            "data_source": "historical"
        })

    print(f"{team} {year} — {len(games)} games parsed")
    return games


def seed_teams(conn):
    print("Seeding teams table...")
    for team_id, full_name, division in MLB_TEAM_DATA:
        conn.execute(text("""
            INSERT INTO teams (team_id, full_name, division)
            VALUES (:team_id, :full_name, :division)
            ON CONFLICT (team_id) DO NOTHING
        """), {"team_id": team_id, "full_name": full_name, "division": division})
    print(f"  {len(MLB_TEAM_DATA)} teams seeded.")


def upsert_game(conn, game):
    conn.execute(text("""
        INSERT INTO games (
            game_id, game_date, home_team_id, away_team_id,
            home_score, away_score, status, data_source
        )
        VALUES (
            :game_id, :game_date, :home_team_id, :away_team_id,
            :home_score, :away_score, :status, :data_source
        )
        ON CONFLICT (game_id) DO UPDATE SET
            home_score = EXCLUDED.home_score,
            away_score = EXCLUDED.away_score,
            status     = EXCLUDED.status
    """), game)


def main():
    total_inserted = 0

    with engine.connect() as conn:
        seed_teams(conn)
        conn.commit()

        for year in SEASONS:
            print(f"\n--- Season {year} ---")
            for team in MLB_TEAMS:
                print(f"  Pulling {team} {year}...")
                games = parse_season(team, year)
                for game in games:
                    upsert_game(conn, game)
                    total_inserted += 1
                time.sleep(3)  # avoid rate limiting from baseball-reference
            conn.commit()
            print(f"  Season {year} complete.")

    print(f"\nDone. Total games inserted/updated: {total_inserted}")


if __name__ == "__main__":
    main()