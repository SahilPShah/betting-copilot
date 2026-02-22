from pybaseball import team_batting, team_pitching
import pandas as pd
from sqlalchemy import create_engine, text
from dotenv import load_dotenv
import os
import time

load_dotenv("/Users/sahilshah/betting-copilot/.env")
engine = create_engine(os.getenv("DATABASE_URL"))

SEASONS = [2023, 2024, 2025]

# Last day of each season — stats apply to all games in that season
SEASON_END_DATES = {
    2023: '2023-10-01',
    2024: '2024-09-29',
    2025: '2025-09-28',
}


def fetch_batting(year):
    try:
        df = team_batting(year)
        df = df[['Team', 'OPS', 'AVG', 'OBP', 'SLG', 'wRC+', 'BB%', 'K%', 'wOBA']].copy()
        df['year'] = year
        print(f"  ✅ Batting {year} — {len(df)} teams")
        return df
    except Exception as e:
        print(f"  ❌ Batting {year}: {e}")
        return None


def fetch_pitching(year):
    try:
        df = team_pitching(year)
        df = df[['Team', 'ERA', 'WHIP', 'K/9', 'BB/9', 'xFIP', 'FIP']].copy()
        df['year'] = year
        print(f"  ✅ Pitching {year} — {len(df)} teams")
        return df
    except Exception as e:
        print(f"  ❌ Pitching {year}: {e}")
        return None


def upsert_stats(conn, row):
    conn.execute(text("""
        INSERT INTO team_stats_snapshots (
            team_id,
            as_of_date,
            team_pitching_era,
            team_pitching_whip,
            team_pitching_k9,
            team_ops_l14,
            team_era_l14,
            team_win_pct,
            runs_scored_avg,
            captured_at
        )
        VALUES (
            :team_id,
            :as_of_date,
            :team_pitching_era,
            :team_pitching_whip,
            :team_pitching_k9,
            :team_ops_l14,
            :team_era_l14,
            :team_win_pct,
            :runs_scored_avg,
            NOW()
        )
        ON CONFLICT (team_id, as_of_date) DO UPDATE SET
            team_pitching_era    = EXCLUDED.team_pitching_era,
            team_pitching_whip   = EXCLUDED.team_pitching_whip,
            team_pitching_k9     = EXCLUDED.team_pitching_k9,
            team_ops_l14   = EXCLUDED.team_ops_l14,
            team_era_l14   = EXCLUDED.team_era_l14,
            team_win_pct   = EXCLUDED.team_win_pct,
            runs_scored_avg = EXCLUDED.runs_scored_avg
    """), row)


def main():
    total = 0

    with engine.connect() as conn:
        for year in SEASONS:
            print(f"\n--- Season {year} ---")

            batting = fetch_batting(year)
            time.sleep(3)
            pitching = fetch_pitching(year)
            time.sleep(3)

            if batting is None or pitching is None:
                print(f"  Skipping {year} — missing data")
                continue

            # Merge on Team
            merged = pd.merge(batting, pitching, on=['Team', 'year'], suffixes=('_bat', '_pit'))

            # Compute season win pct and runs scored avg from games table
            win_stats = pd.read_sql(text("""
                SELECT
                    home_team_id as team_id,
                    ROUND(AVG(home_score), 2) as runs_scored_avg,
                    ROUND(SUM(CASE WHEN home_score > away_score THEN 1 ELSE 0 END)::numeric / COUNT(*), 3) as win_pct
                FROM games
                WHERE LEFT(game_date::text, 4) = :year
                AND status = 'final'
                GROUP BY home_team_id
            """), conn, params={"year": str(year)})

            as_of_date = SEASON_END_DATES[year]

            for _, row in merged.iterrows():
                team_id = row['Team']
                win_row = win_stats[win_stats['team_id'] == team_id]
                win_pct = float(win_row['win_pct'].values[0]) if not win_row.empty else None
                runs_avg = float(win_row['runs_scored_avg'].values[0]) if not win_row.empty else None

                upsert_stats(conn, {
                    "team_id": team_id,
                    "as_of_date": as_of_date,
                    "team_pitching_era": float(row['ERA']),
                    "team_pitching_whip": float(row['WHIP']),
                    "team_pitching_k9": float(row['K/9']),
                    "team_ops_l14": float(row['OPS']),
                    "team_era_l14": float(row['ERA']),
                    "team_win_pct": win_pct,
                    "runs_scored_avg": runs_avg,
                })
                total += 1

            conn.commit()
            print(f"  Season {year} complete — {len(merged)} teams inserted")

    print(f"\nDone. Total rows inserted/updated: {total}")


if __name__ == "__main__":
    main()