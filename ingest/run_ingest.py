import argparse
import os
import sys
from datetime import date as date_cls, timedelta

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()
engine = create_engine(os.getenv("DATABASE_URL"))

SEASON_START = "2026-03-27"  # first day of 2026 regular season


def latest_ingested_date():
    """
    Return the most recent game_date in the games table, or SEASON_START - 1 day
    if the table is empty for this season.
    """
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT MAX(game_date) FROM games
            WHERE game_date >= :season_start
        """), {"season_start": SEASON_START}).fetchone()
    latest = row[0]
    if latest is None:
        return None
    return latest if isinstance(latest, date_cls) else date_cls.fromisoformat(str(latest))


def missing_dates(today):
    """
    Return (start_date, end_date) covering all dates not yet in the DB,
    from SEASON_START through today. Returns (today, today) if fully up to date.
    """
    latest = latest_ingested_date()
    if latest is None:
        return SEASON_START, today.strftime('%Y-%m-%d')
    next_needed = latest + timedelta(days=1)
    if next_needed > today:
        return today.strftime('%Y-%m-%d'), today.strftime('%Y-%m-%d')
    return next_needed.strftime('%Y-%m-%d'), today.strftime('%Y-%m-%d')


def main():
    parser = argparse.ArgumentParser(description="Run all ingest steps, auto-backfilling any missing days.")
    parser.add_argument('--date', default=None,
                        help="Override: ingest only this specific date (YYYY-MM-DD).")
    args = parser.parse_args()

    today = date_cls.today()

    if args.date:
        # Manual override: single date
        start_date = args.date
        end_date = args.date
        backfill = False
    else:
        start_date, end_date = missing_dates(today)
        backfill = start_date != end_date

    from ingest import mlb_games, odds_api, mlb_stats, mlb_injuries, mlb_boxscores

    if backfill:
        print(f"Running ingest for {end_date} (backfilling {start_date} → {end_date})")
    else:
        print(f"Running ingest for {end_date}")

    # Games: backfill the full range in one API call
    print("\n[1/5] Games...")
    mlb_games.main(start_date, end_date)

    # Box scores: fetch pitcher logs for the same range (final games only)
    print("\n[2/5] Box scores...")
    mlb_boxscores.main(start_date, end_date)

    # Odds, stats, injuries: today only (historical data unrecoverable)
    print("\n[3/5] Odds...")
    if backfill:
        print("  NOTE: Odds only available for today — past odds cannot be backfilled.")
    odds_api.main(end_date)

    print("\n[4/5] Team stats...")
    if backfill:
        mlb_stats.upsert_season_stats(end_date)
        mlb_stats.backfill_l7_stats(start_date, end_date)
    else:
        mlb_stats.main(end_date)

    print("\n[5/5] Injuries...")
    mlb_injuries.main(end_date)

    if backfill:
        print(f"\nIngest complete. Backfilled games + L7 stats {start_date} → {end_date}. "
              f"Season stats/odds/injuries reflect {end_date} only.")
    else:
        print(f"\nIngest complete for {end_date}")


if __name__ == "__main__":
    main()
