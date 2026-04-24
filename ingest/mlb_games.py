# Requires: pip install mlb-statsapi
import statsapi
import argparse
from datetime import date as date_cls
from sqlalchemy import create_engine, text
from dotenv import load_dotenv
import os

load_dotenv("/Users/sahilshah/betting-copilot/.env")
engine = create_engine(os.getenv("DATABASE_URL"))

STATSAPI_TEAM_TO_ID = {
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

STATUS_MAP = {
    'Final': 'final',
    'Game Over': 'final',
    'Completed Early': 'final',
    'Scheduled': 'scheduled',
    'Pre-Game': 'scheduled',
    'Warmup': 'scheduled',
    'In Progress': 'scheduled',
    'Delayed': 'scheduled',
    'Suspended': 'scheduled',
    'Postponed': 'scheduled',
}


def build_game_id(date_str, home_abbr, away_abbr, game_num=1):
    return f"{date_str}-{home_abbr}-{away_abbr}-{game_num}"


def upsert_game(conn, game):
    conn.execute(text("""
        INSERT INTO games (
            game_id, game_date, home_team_id, away_team_id,
            home_score, away_score, status, data_source, game_pk
        )
        VALUES (
            :game_id, :game_date, :home_team_id, :away_team_id,
            :home_score, :away_score, :status, :data_source, :game_pk
        )
        ON CONFLICT (game_id) DO UPDATE SET
            home_score       = EXCLUDED.home_score,
            away_score       = EXCLUDED.away_score,
            status           = EXCLUDED.status,
            game_pk = COALESCE(games.game_pk, EXCLUDED.game_pk)
    """), game)


def upsert_starter(conn, game_id, side, name):
    """Upsert probable starter name into game_starters. ERA resolved at prediction time."""
    if not name:
        return
    conn.execute(text("""
        INSERT INTO game_starters (game_id, side, starter_name)
        VALUES (:game_id, :side, :name)
        ON CONFLICT (game_id, side) DO UPDATE SET
            starter_name = EXCLUDED.starter_name,
            captured_at  = NOW()
    """), {"game_id": game_id, "side": side, "name": name})


def fetch_schedule(start_date_str, end_date_str=None):
    if end_date_str is None or end_date_str == start_date_str:
        return statsapi.schedule(date=start_date_str, sportId=1)
    return statsapi.schedule(start_date=start_date_str, end_date=end_date_str, sportId=1)


def main(start_date_str=None, end_date_str=None):
    if start_date_str is None:
        start_date_str = date_cls.today().strftime('%Y-%m-%d')
    if end_date_str is None:
        end_date_str = start_date_str

    is_range = start_date_str != end_date_str
    date_label = f"{start_date_str} → {end_date_str}" if is_range else start_date_str

    print(f"  Fetching schedule for {date_label}...")
    try:
        schedule = fetch_schedule(start_date_str, end_date_str)
    except Exception as e:
        print(f"  ERROR: statsapi.schedule failed: {e}")
        return

    games_to_insert = []
    skipped = 0

    for item in schedule:
        home_name = item.get('home_name', '')
        away_name = item.get('away_name', '')

        home_abbr = STATSAPI_TEAM_TO_ID.get(home_name)
        away_abbr = STATSAPI_TEAM_TO_ID.get(away_name)

        if not home_abbr or not away_abbr:
            skipped += 1
            continue

        game_date = item.get('game_date', start_date_str)
        game_num = item.get('game_num', 1)
        game_id = build_game_id(game_date, home_abbr, away_abbr, game_num)
        game_pk = item.get('game_id')  # integer game PK from statsapi

        raw_status = item.get('status', 'Scheduled')
        status = STATUS_MAP.get(raw_status, 'scheduled')

        home_score = item.get('home_score') if status == 'final' else None
        away_score = item.get('away_score') if status == 'final' else None

        games_to_insert.append({
            "game_id": game_id,
            "game_date": game_date,
            "home_team_id": home_abbr,
            "away_team_id": away_abbr,
            "home_score": home_score,
            "away_score": away_score,
            "status": status,
            "data_source": "live",
            "game_pk": int(game_pk) if game_pk else None,
            "home_probable_pitcher": item.get('home_probable_pitcher', ''),
            "away_probable_pitcher": item.get('away_probable_pitcher', ''),
        })

    with engine.connect() as conn:
        for game in games_to_insert:
            upsert_game(conn, game)

            if game['home_probable_pitcher']:
                upsert_starter(conn, game['game_id'], 'home', game['home_probable_pitcher'])
            if game['away_probable_pitcher']:
                upsert_starter(conn, game['game_id'], 'away', game['away_probable_pitcher'])

        conn.commit()

    starters_found = sum(
        1 for g in games_to_insert
        if g['home_probable_pitcher'] or g['away_probable_pitcher']
    )
    print(f"  {len(games_to_insert)} games upserted ({skipped} skipped), "
          f"{starters_found} with probable pitchers")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--date', default=date_cls.today().strftime('%Y-%m-%d'))
    parser.add_argument('--start-date', default=None)
    parser.add_argument('--end-date', default=None)
    args = parser.parse_args()
    start = args.start_date or args.date
    end = args.end_date or start
    main(start, end)
