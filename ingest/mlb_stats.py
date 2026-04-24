import argparse
import pandas as pd
from bs4 import BeautifulSoup, Comment
from pybaseball.datasources.bref import BRefSession
from sqlalchemy import create_engine, text
from dotenv import load_dotenv
from datetime import date as date_cls, timedelta
import os
import time

load_dotenv("/Users/sahilshah/betting-copilot/.env")
engine = create_engine(os.getenv("DATABASE_URL"))

_bref_session = BRefSession()

# B-Ref abbreviation → our DB team_id (only entries that differ)
_BREF_TO_ABBR = {
    'CWS': 'CHW',
}


def _bref_league_stats(year):
    """
    Fetch B-Ref league standard batting and pitching pages for *year*.
    Returns (batting_df, pitching_df) with columns matching the old FanGraphs format,
    or raises on HTTP/parse error.
    """
    def _scrape(path, table_id, col_map):
        url = f"https://www.baseball-reference.com/leagues/majors/{year}{path}"
        resp = _bref_session.get(url)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.content, 'html.parser')

        # B-Ref sometimes wraps tables in HTML comments
        table = soup.find('table', {'id': table_id})
        if table is None:
            for comment in soup.find_all(string=lambda t: isinstance(t, Comment)):
                if table_id in comment:
                    table = BeautifulSoup(comment, 'html.parser').find('table', {'id': table_id})
                    if table:
                        break
        if table is None:
            raise ValueError(f"Table '{table_id}' not found on {url}")

        rows = []
        for tr in table.find('tbody').find_all('tr'):
            if 'thead' in (tr.get('class') or []):
                continue
            a = tr.find('a', href=lambda h: h and '/teams/' in h)
            if not a:
                continue
            abbr = a['href'].split('/')[2]
            abbr = _BREF_TO_ABBR.get(abbr, abbr)
            row = {'Team': abbr}
            for stat, col in col_map.items():
                td = tr.find('td', {'data-stat': stat})
                try:
                    row[col] = float(td.text.strip())
                except (AttributeError, ValueError):
                    row[col] = None
            rows.append(row)

        return pd.DataFrame(rows)

    batting_df = _scrape(
        '-standard-batting.shtml',
        'teams_standard_batting',
        {'onbase_plus_slugging': 'OPS', 'batting_avg': 'AVG',
         'onbase_perc': 'OBP', 'slugging_perc': 'SLG'},
    )
    time.sleep(3)
    pitching_df = _scrape(
        '-standard-pitching.shtml',
        'teams_standard_pitching',
        {'earned_run_avg': 'ERA', 'whip': 'WHIP', 'strikeouts_per_nine': 'K/9'},
    )
    return batting_df, pitching_df


def fetch_batting(year):
    try:
        batting_df, _ = _bref_league_stats(year)
        df = batting_df[['Team', 'OPS', 'AVG', 'OBP', 'SLG']].dropna(subset=['OPS']).copy()
        df['year'] = year
        print(f"    Batting {year} — {len(df)} teams")
        return df
    except Exception as e:
        print(f"    WARNING: Batting {year}: {e}")
        return None


def fetch_pitching(year):
    try:
        _, pitching_df = _bref_league_stats(year)
        df = pitching_df[['Team', 'ERA', 'WHIP', 'K/9']].dropna(subset=['ERA']).copy()
        df['year'] = year
        print(f"    Pitching {year} — {len(df)} teams")
        return df
    except Exception as e:
        print(f"    WARNING: Pitching {year}: {e}")
        return None


def seed_season_stats():
    """
    One-time seed: copy latest row per team/season from team_stats_mlb into team_season_stats.
    Must be called BEFORE applying the v5 migration that drops the season columns.
    Safe to call multiple times (ON CONFLICT DO NOTHING).
    """
    with engine.connect() as conn:
        result = conn.execute(text("""
            INSERT INTO team_season_stats (
                team_id, season,
                team_pitching_era, team_pitching_whip, team_pitching_k9,
                team_ops, team_win_pct, runs_scored_avg
            )
            SELECT DISTINCT ON (team_id, season)
                team_id,
                LEFT(as_of_date::text, 4)::int AS season,
                team_pitching_era,
                team_pitching_whip,
                team_pitching_k9,
                team_ops_l14 AS team_ops,
                team_win_pct,
                runs_scored_avg
            FROM team_stats_mlb
            ORDER BY team_id, season, as_of_date DESC
            ON CONFLICT (team_id, season) DO NOTHING
        """))
        conn.commit()
        print(f"  Seeded {result.rowcount} rows into team_season_stats")


def upsert_season_stats(date_str):
    """
    Fetch current-season stats from Baseball Reference and upsert into team_season_stats.
    One row per team per season — updated daily with latest cumulative totals.
    """
    year = int(date_str[:4])
    print(f"  Fetching team stats for {year} from Baseball Reference (as of {date_str})...")

    try:
        batting, pitching = _bref_league_stats(year)
        batting = batting[['Team', 'OPS', 'AVG', 'OBP', 'SLG']].dropna(subset=['OPS']).copy()
        pitching = pitching[['Team', 'ERA', 'WHIP', 'K/9']].dropna(subset=['ERA']).copy()
        print(f"    Batting {year} — {len(batting)} teams")
        print(f"    Pitching {year} — {len(pitching)} teams")
    except Exception as e:
        print(f"  WARNING: Could not fetch stats for {year} from Baseball Reference: {e}")
        print(f"  Skipping season stats update.")
        return

    if batting.empty or pitching.empty:
        print(f"  WARNING: Empty stats for {year}, skipping season stats.")
        return

    merged = pd.merge(batting, pitching, on='Team')

    with engine.connect() as conn:
        win_stats = pd.read_sql(text("""
            WITH all_games AS (
                SELECT home_team_id AS team_id,
                       home_score AS runs_scored,
                       CASE WHEN home_score > away_score THEN 1 ELSE 0 END AS won
                FROM games
                WHERE LEFT(game_date::text, 4) = :year AND status = 'final'
                UNION ALL
                SELECT away_team_id AS team_id,
                       away_score AS runs_scored,
                       CASE WHEN away_score > home_score THEN 1 ELSE 0 END AS won
                FROM games
                WHERE LEFT(game_date::text, 4) = :year AND status = 'final'
            )
            SELECT team_id,
                   ROUND(AVG(runs_scored), 2) AS runs_scored_avg,
                   ROUND(SUM(won)::numeric / COUNT(*), 3) AS win_pct
            FROM all_games
            GROUP BY team_id
        """), conn, params={"year": str(year)})

        inserted = 0
        for _, row in merged.iterrows():
            team_id = row['Team']
            win_row = win_stats[win_stats['team_id'] == team_id]
            win_pct = float(win_row['win_pct'].values[0]) if not win_row.empty else None
            runs_avg = float(win_row['runs_scored_avg'].values[0]) if not win_row.empty else None

            conn.execute(text("""
                INSERT INTO team_season_stats (
                    team_id, season,
                    team_pitching_era, team_pitching_whip, team_pitching_k9,
                    team_ops, team_win_pct, runs_scored_avg,
                    updated_at
                )
                VALUES (
                    :team_id, :season,
                    :team_pitching_era, :team_pitching_whip, :team_pitching_k9,
                    :team_ops, :team_win_pct, :runs_scored_avg,
                    NOW()
                )
                ON CONFLICT (team_id, season) DO UPDATE SET
                    team_pitching_era  = EXCLUDED.team_pitching_era,
                    team_pitching_whip = EXCLUDED.team_pitching_whip,
                    team_pitching_k9   = EXCLUDED.team_pitching_k9,
                    team_ops           = EXCLUDED.team_ops,
                    team_win_pct       = EXCLUDED.team_win_pct,
                    runs_scored_avg    = EXCLUDED.runs_scored_avg,
                    updated_at         = NOW()
            """), {
                "team_id": team_id,
                "season": year,
                "team_pitching_era": float(row['ERA']),
                "team_pitching_whip": float(row['WHIP']),
                "team_pitching_k9": float(row['K/9']),
                "team_ops": float(row['OPS']),
                "team_win_pct": win_pct,
                "runs_scored_avg": runs_avg,
            })
            inserted += 1

        conn.commit()

    print(f"  {inserted} season stat rows upserted for {year}")


def _compute_l7_for_date(conn, date_str):
    """
    Compute L7 rolling stats for all teams as of date_str.
    Uses all available final games before that date (up to 7 per team).
    Returns list of dicts ready to insert.
    """
    rows = pd.read_sql(text("""
        WITH recent AS (
            SELECT
                team_id,
                game_date,
                CASE WHEN side = 'home' THEN home_score ELSE away_score END AS scored,
                CASE WHEN side = 'home' THEN away_score ELSE home_score END AS allowed,
                CASE
                    WHEN side = 'home' AND home_score > away_score THEN 1
                    WHEN side = 'away' AND away_score > home_score THEN 1
                    ELSE 0
                END AS won,
                ROW_NUMBER() OVER (
                    PARTITION BY team_id ORDER BY game_date DESC
                ) AS rn
            FROM (
                SELECT home_team_id AS team_id, 'home' AS side,
                       game_date, home_score, away_score
                FROM games
                WHERE status = 'final' AND game_date < :date
                UNION ALL
                SELECT away_team_id AS team_id, 'away' AS side,
                       game_date, home_score, away_score
                FROM games
                WHERE status = 'final' AND game_date < :date
            ) g
        )
        SELECT
            team_id,
            ROUND(AVG(won)::numeric, 3)               AS l7_win_pct,
            ROUND(AVG(scored)::numeric, 2)             AS l7_runs_scored_avg,
            ROUND(AVG(allowed)::numeric, 2)            AS l7_runs_allowed_avg,
            ROUND(AVG(scored - allowed)::numeric, 2)   AS l7_run_diff_avg,
            COUNT(*)::int                              AS l7_games
        FROM recent
        WHERE rn <= 7
        GROUP BY team_id
    """), conn, params={"date": date_str})
    return rows


def upsert_l7_stats(date_str):
    """
    Compute and upsert L7 rolling stats into team_stats_mlb for the given date.
    Safe to re-run (ON CONFLICT DO UPDATE).
    """
    with engine.connect() as conn:
        rows = _compute_l7_for_date(conn, date_str)

        if rows.empty:
            print(f"  No L7 data available for {date_str} (no final games before this date)")
            return

        for _, row in rows.iterrows():
            conn.execute(text("""
                INSERT INTO team_stats_mlb (
                    team_id, as_of_date,
                    l7_win_pct, l7_runs_scored_avg, l7_runs_allowed_avg,
                    l7_run_diff_avg, l7_games,
                    captured_at
                )
                VALUES (
                    :team_id, :as_of_date,
                    :l7_win_pct, :l7_runs_scored_avg, :l7_runs_allowed_avg,
                    :l7_run_diff_avg, :l7_games,
                    NOW()
                )
                ON CONFLICT (team_id, as_of_date) DO UPDATE SET
                    l7_win_pct          = EXCLUDED.l7_win_pct,
                    l7_runs_scored_avg  = EXCLUDED.l7_runs_scored_avg,
                    l7_runs_allowed_avg = EXCLUDED.l7_runs_allowed_avg,
                    l7_run_diff_avg     = EXCLUDED.l7_run_diff_avg,
                    l7_games            = EXCLUDED.l7_games,
                    captured_at         = NOW()
            """), {
                "team_id": row['team_id'],
                "as_of_date": date_str,
                "l7_win_pct": float(row['l7_win_pct']),
                "l7_runs_scored_avg": float(row['l7_runs_scored_avg']),
                "l7_runs_allowed_avg": float(row['l7_runs_allowed_avg']),
                "l7_run_diff_avg": float(row['l7_run_diff_avg']),
                "l7_games": int(row['l7_games']),
            })

        conn.commit()
        print(f"  {len(rows)} L7 stat rows upserted for {date_str}")


def backfill_l7_stats(start_date_str, end_date_str):
    """
    Backfill L7 stats for every date from start_date to end_date (inclusive).
    Used to seed team_stats_mlb after the v5 migration.
    """
    start = date_cls.fromisoformat(start_date_str)
    end = date_cls.fromisoformat(end_date_str)
    current = start

    print(f"  Backfilling L7 stats from {start_date_str} → {end_date_str}...")
    while current <= end:
        upsert_l7_stats(current.strftime('%Y-%m-%d'))
        current += timedelta(days=1)

    print(f"  Backfill complete.")


def main(date_str=None):
    if date_str is None:
        date_str = date_cls.today().strftime('%Y-%m-%d')

    upsert_season_stats(date_str)
    upsert_l7_stats(date_str)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--date', default=date_cls.today().strftime('%Y-%m-%d'))
    parser.add_argument('--seed', action='store_true',
                        help='Seed team_season_stats from current team_stats_mlb (run before v5 migration)')
    parser.add_argument('--backfill', action='store_true',
                        help='Backfill L7 stats (run after v5 migration)')
    parser.add_argument('--backfill-start', default='2026-03-25')
    args = parser.parse_args()

    if args.seed:
        seed_season_stats()
    elif args.backfill:
        backfill_l7_stats(args.backfill_start, args.date)
    else:
        main(args.date)
