# Requires: pip install mlb-statsapi
import statsapi
import argparse
from datetime import date as date_cls, datetime, timezone
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

# Map statsapi injury status strings to our schema values
STATUS_MAP = {
    'Out': 'out',
    '10-Day IL': 'out',
    '15-Day IL': 'out',
    '60-Day IL': 'out',
    'Day-To-Day': 'questionable',
    'Questionable': 'questionable',
}


def upsert_injury(conn, injury):
    conn.execute(text("""
        INSERT INTO injury_statuses (
            game_id, player_name, team_id, status, reason, captured_at, source
        )
        VALUES (
            :game_id, :player_name, :team_id, :status, :reason, :captured_at, :source
        )
        ON CONFLICT DO NOTHING
    """), injury)


def main(date_str=None):
    if date_str is None:
        date_str = date_cls.today().strftime('%Y-%m-%d')

    print(f"  Fetching injury reports for {date_str}...")
    captured_at = datetime.now(timezone.utc).isoformat()

    try:
        schedule = statsapi.schedule(date=date_str, sportId=1)
    except Exception as e:
        print(f"  WARNING: statsapi injury fetch failed: {e}")
        return

    inserted = 0

    with engine.connect() as conn:
        for game in schedule:
            home_name = game.get('home_name', '')
            away_name = game.get('away_name', '')
            home_abbr = STATSAPI_TEAM_TO_ID.get(home_name)
            away_abbr = STATSAPI_TEAM_TO_ID.get(away_name)

            # Look up our game_id
            if not home_abbr or not away_abbr:
                continue

            game_num = game.get('game_num', 1)
            game_id = f"{date_str}-{home_abbr}-{away_abbr}-{game_num}"

            # Check game exists in DB
            exists = conn.execute(text(
                "SELECT 1 FROM games WHERE game_id = :gid LIMIT 1"
            ), {"gid": game_id}).fetchone()
            if not exists:
                continue

            # Parse injuries for each team
            for side, team_name, team_abbr in [
                ('home', home_name, home_abbr),
                ('away', away_name, away_abbr),
            ]:
                team_injuries = (
                    game.get('teams', {})
                    .get(side, {})
                    .get('injuries', [])
                )
                if not team_injuries:
                    continue

                for inj in team_injuries:
                    player_name = inj.get('person', {}).get('fullName', '')
                    raw_status = inj.get('status', {}).get('description', '')
                    our_status = STATUS_MAP.get(raw_status)

                    if not our_status or not player_name:
                        continue

                    upsert_injury(conn, {
                        "game_id": game_id,
                        "player_name": player_name,
                        "team_id": team_abbr,
                        "status": our_status,
                        "reason": inj.get('notes', ''),
                        "captured_at": captured_at,
                        "source": "mlb_statsapi",
                    })
                    inserted += 1

        conn.commit()

    print(f"  {inserted} injury rows inserted")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--date', default=date_cls.today().strftime('%Y-%m-%d'))
    args = parser.parse_args()
    main(args.date)
