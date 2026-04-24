# Requires: pip install mlb-statsapi
import statsapi
import unicodedata
import pandas as pd
from pybaseball import pitching_stats
from sqlalchemy import create_engine, text
from dotenv import load_dotenv
import os
import time

load_dotenv("/Users/sahilshah/betting-copilot/.env")
engine = create_engine(os.getenv("DATABASE_URL"))

SEASONS = [2023, 2024, 2025]

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


def normalize_name(name):
    """Strip accents and lowercase for fuzzy name matching."""
    if not name:
        return ''
    nfkd = unicodedata.normalize('NFKD', name)
    return ''.join(c for c in nfkd if not unicodedata.combining(c)).lower().strip()


def load_pitcher_stats(year):
    """
    Fetch season pitching stats for a year.
    Returns dict: {normalized_name: {era, whip, k9, gs}}
    """
    print(f"  Fetching pybaseball pitching stats for {year}...")
    try:
        df = pitching_stats(year, qual=1)
    except Exception as e:
        print(f"  WARNING: Could not fetch pitching stats for {year}: {e}")
        return {}

    stats = {}
    for _, row in df.iterrows():
        name = str(row.get('Name', ''))
        if not name:
            continue
        try:
            entry = {
                'era': float(row['ERA']) if pd.notna(row.get('ERA')) else None,
                'whip': float(row['WHIP']) if pd.notna(row.get('WHIP')) else None,
                'k9': float(row['K/9']) if pd.notna(row.get('K/9')) else None,
                'gs': int(row['GS']) if pd.notna(row.get('GS')) else None,
            }
        except (KeyError, ValueError):
            continue
        stats[normalize_name(name)] = entry
        stats[name.lower().strip()] = entry  # also store non-normalized for exact match
    return stats


def load_game_dates():
    """Load all unique (game_date, year) pairs for target seasons."""
    season_strs = [str(s) for s in SEASONS]
    with engine.connect() as conn:
        df = pd.read_sql(text("""
            SELECT DISTINCT game_date
            FROM games
            WHERE status = 'final'
            AND LEFT(game_date::text, 4) = ANY(:seasons)
            ORDER BY game_date ASC
        """), conn, params={"seasons": season_strs})
    return df['game_date'].tolist()


def fetch_starters_for_date(date_str):
    """
    Fetch probable pitchers from statsapi for a given date.
    Returns list of {home_team, away_team, home_starter, away_starter} dicts.
    """
    try:
        schedule = statsapi.schedule(date=str(date_str), sportId=1)
    except Exception as e:
        print(f"    WARNING: statsapi error for {date_str}: {e}")
        return []

    results = []
    for game in schedule:
        home_name = game.get('home_name', '')
        away_name = game.get('away_name', '')

        home_abbr = STATSAPI_TEAM_TO_ID.get(home_name)
        away_abbr = STATSAPI_TEAM_TO_ID.get(away_name)

        if not home_abbr or not away_abbr:
            continue

        home_pitcher = game.get('home_probable_pitcher', '')
        away_pitcher = game.get('away_probable_pitcher', '')

        results.append({
            'home_abbr': home_abbr,
            'away_abbr': away_abbr,
            'home_starter': home_pitcher if home_pitcher else None,
            'away_starter': away_pitcher if away_pitcher else None,
        })

    return results


def match_game_id(home_abbr, away_abbr, game_date, conn):
    """Find our game_id for a given home/away/date."""
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


def lookup_pitcher(name, pitcher_stats):
    """Look up a pitcher's stats by name. Returns stats dict or None."""
    if not name:
        return None
    # Exact match first
    if name.lower().strip() in pitcher_stats:
        return pitcher_stats[name.lower().strip()]
    # Normalized (accent-stripped) match
    norm = normalize_name(name)
    if norm in pitcher_stats:
        return pitcher_stats[norm]
    return None


def upsert_starter(conn, game_id, side, name, stats):
    """Upsert a starter record into game_starters."""
    conn.execute(text("""
        INSERT INTO game_starters (game_id, side, starter_name, starter_era, starter_whip, starter_k9, starter_gs)
        VALUES (:game_id, :side, :name, :era, :whip, :k9, :gs)
        ON CONFLICT (game_id, side) DO UPDATE SET
            starter_name = EXCLUDED.starter_name,
            starter_era  = EXCLUDED.starter_era,
            starter_whip = EXCLUDED.starter_whip,
            starter_k9   = EXCLUDED.starter_k9,
            starter_gs   = EXCLUDED.starter_gs,
            captured_at  = NOW()
    """), {
        "game_id": game_id,
        "side": side,
        "name": name,
        "era": stats.get('era') if stats else None,
        "whip": stats.get('whip') if stats else None,
        "k9": stats.get('k9') if stats else None,
        "gs": stats.get('gs') if stats else None,
    })


def main():
    dates = load_game_dates()
    print(f"Total game dates to process: {len(dates)}")

    # Pre-load pitcher stats for each season (avoid repeated API calls)
    pitcher_stats_by_year = {}
    for year in SEASONS:
        pitcher_stats_by_year[year] = load_pitcher_stats(year)
        time.sleep(2)

    total_games = 0
    total_starters_found = 0
    total_stats_matched = 0
    total_stats_missing = 0

    with engine.connect() as conn:
        for i, game_date in enumerate(dates):
            date_str = str(game_date)
            year = int(date_str[:4])
            pitcher_stats = pitcher_stats_by_year.get(year, {})

            game_entries = fetch_starters_for_date(date_str)

            for entry in game_entries:
                game_id = match_game_id(entry['home_abbr'], entry['away_abbr'], game_date, conn)
                if not game_id:
                    continue

                total_games += 1

                for side, starter_name in [('home', entry['home_starter']), ('away', entry['away_starter'])]:
                    if starter_name:
                        total_starters_found += 1
                        stats = lookup_pitcher(starter_name, pitcher_stats)
                        if stats:
                            total_stats_matched += 1
                        else:
                            total_stats_missing += 1
                        upsert_starter(conn, game_id, side, starter_name, stats)
                    else:
                        # No probable pitcher announced — insert name-only row
                        upsert_starter(conn, game_id, side, None, None)

            conn.commit()

            if (i + 1) % 50 == 0:
                print(f"  [{i+1}/{len(dates)}] {date_str} — {total_games} games, {total_starters_found} starters")

            time.sleep(0.5)

    print(f"\nDone.")
    print(f"  Game dates processed  : {len(dates)}")
    print(f"  Games matched in DB   : {total_games}")
    print(f"  Starters found        : {total_starters_found}")
    print(f"  Stats matched         : {total_stats_matched}")
    print(f"  Stats missing (no match): {total_stats_missing}")


if __name__ == "__main__":
    main()
