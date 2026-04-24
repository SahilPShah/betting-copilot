"""
One-time backfill: populate pitcher_game_logs for all final games in a date range.

Also updates games.game_pk for any games missing it.

Usage:
    python historical/pull_boxscores.py --start-date 2025-03-27 --end-date 2025-09-30
    python historical/pull_boxscores.py --start-date 2026-03-27 --end-date 2026-03-30
"""
import statsapi
import argparse
import os
import sys
import time
import pandas as pd
from datetime import date as date_cls, timedelta
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

load_dotenv("/Users/sahilshah/betting-copilot/.env")
engine = create_engine(os.getenv("DATABASE_URL"))

from ingest.mlb_boxscores import fetch_boxscore_pitchers, upsert_pitcher_log

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


def daterange(start_str, end_str):
    start = date_cls.fromisoformat(start_str)
    end = date_cls.fromisoformat(end_str)
    while start <= end:
        yield start
        start += timedelta(days=1)


def fetch_statsapi_pks_for_date(date_str):
    """
    Returns a dict mapping (home_abbr, away_abbr, game_num) → game_pk
    for all games on this date.
    """
    try:
        schedule = statsapi.schedule(date=date_str, sportId=1)
    except Exception as e:
        print(f"  WARNING: schedule({date_str}) failed: {e}")
        return {}

    pks = {}
    for item in schedule:
        home_name = item.get('home_name', '')
        away_name = item.get('away_name', '')
        home_abbr = STATSAPI_TEAM_TO_ID.get(home_name)
        away_abbr = STATSAPI_TEAM_TO_ID.get(away_name)
        if not home_abbr or not away_abbr:
            continue
        game_num = item.get('game_num', 1)
        pk = item.get('game_id')
        if pk:
            pks[(home_abbr, away_abbr, game_num)] = int(pk)
    return pks


def main(start_date_str, end_date_str):
    print(f"Backfilling pitcher game logs: {start_date_str} → {end_date_str}")

    # Load all final games in range from DB
    with engine.connect() as conn:
        games = pd.read_sql(text("""
            SELECT game_id, game_date, home_team_id, away_team_id,
                   game_pk,
                   CAST(game_date AS TEXT) as date_str
            FROM games
            WHERE game_date BETWEEN :start AND :end
              AND status = 'final'
            ORDER BY game_date
        """), conn, params={'start': start_date_str, 'end': end_date_str})

    if games.empty:
        print("No final games found in DB for this range.")
        return

    print(f"{len(games)} final games to process")

    # Group by date so we fetch the schedule once per date
    games['date_str'] = games['game_date'].astype(str).str[:10]
    dates = games['date_str'].unique()

    # Build pk lookup for dates where some games are missing game_pk
    pk_lookup = {}  # date_str → {(home, away, num): pk}
    for date_str in dates:
        date_games = games[games['date_str'] == date_str]
        if date_games['game_pk'].isna().any():
            pk_lookup[date_str] = fetch_statsapi_pks_for_date(date_str)
            time.sleep(0.2)

    total_logged = 0
    total_pk_updated = 0

    with engine.connect() as conn:
        for _, game in games.iterrows():
            game_id = game['game_id']
            date_str = game['date_str']
            home = game['home_team_id']
            away = game['away_team_id']

            # Resolve game_pk if missing
            pk = game['game_pk']
            if pd.isna(pk) or pk is None:
                parts = game_id.split('-')
                game_num = int(parts[5]) if len(parts) > 5 else 1
                pk = pk_lookup.get(date_str, {}).get((home, away, game_num))
                if pk:
                    conn.execute(text("""
                        UPDATE games SET game_pk = :pk WHERE game_id = :gid
                    """), {'pk': pk, 'gid': game_id})
                    total_pk_updated += 1

            if not pk:
                print(f"  SKIP {game_id} — could not resolve game_pk")
                continue

            pitchers_by_side = fetch_boxscore_pitchers(pk)
            if not pitchers_by_side:
                continue

            side_to_team = {'home': home, 'away': away}
            for side, pitchers in pitchers_by_side.items():
                team_id = side_to_team[side]
                for p in pitchers:
                    upsert_pitcher_log(conn, game_id, game['game_date'], team_id, p)
                    total_logged += 1

            time.sleep(0.1)

        conn.commit()

    print(f"Done. {total_logged} pitcher log rows upserted, "
          f"{total_pk_updated} games updated with game_pk.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--start-date', required=True)
    parser.add_argument('--end-date', required=True)
    args = parser.parse_args()
    main(args.start_date, args.end_date)
