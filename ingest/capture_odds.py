"""
Daily odds capture job — runs at 9am.
1. Ensures today's games are in the DB (backfills any missing days too)
2. Captures morning odds snapshot for today's games
3. Updates season-to-date team stats
4. Fetches box scores for recent final games → pitcher_game_logs
"""
import os
import sys
from datetime import date as date_cls, timedelta

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

from ingest.run_ingest import missing_dates
from ingest import mlb_games, odds_api, mlb_stats, mlb_boxscores


def main():
    from datetime import datetime
    print(f"\n{'='*40}")
    print(f"Odds capture started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*40}")

    today = date_cls.today()
    today_str = today.strftime('%Y-%m-%d')

    # Backfill any missing dates + always re-fetch last 5 days to pick up final scores
    start_date, end_date = missing_dates(today)
    refresh_start = (today - timedelta(days=5)).strftime('%Y-%m-%d')
    fetch_start = min(start_date, refresh_start)
    print(f"Refreshing games in DB ({fetch_start} → {end_date})...")
    mlb_games.main(fetch_start, end_date)

    # Fetch box scores for recent final games → pitcher_game_logs
    print(f"\nFetching box scores ({refresh_start} → {today_str})...")
    mlb_boxscores.main(refresh_start, today_str)

    # Capture morning odds
    print(f"\nCapturing morning odds for {today_str}...")
    odds_api.main(today_str)

    # Update season-to-date team stats
    print(f"\nUpdating team stats for {today_str}...")
    mlb_stats.main(today_str)

    print(f"\nOdds capture complete: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


if __name__ == "__main__":
    main()
