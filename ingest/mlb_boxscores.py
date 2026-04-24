"""
Ingest starting pitcher game logs and team batting logs from MLB Stats API box scores.

For each final game with a known game_pk, fetches the box score and stores:
  - Starting pitcher's line (IP, ER, H, BB, K) in pitcher_game_logs
  - Team batting stats (AB, H, 2B, 3B, HR, BB, K, R) in team_batting_logs

Only the starter (first pitcher listed per side) is persisted.

Also exposes compute_starter_eras() for use by models/predict.py and
recs/run_recs.py to resolve ERA from the last 5 starts, with L3 (last 3 starts)
breakdown alongside full-window stats.
"""
import statsapi
import argparse
import os
import sys
import time
import pandas as pd
from datetime import date as date_cls
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

load_dotenv("/Users/sahilshah/betting-copilot/.env")
engine = create_engine(os.getenv("DATABASE_URL"))

MIN_STARTS_FOR_ERA = 1  # require at least 1 start before trusting ERA
STARTS_WINDOW = 5       # use last N starts for ERA computation
L3_WINDOW = 3           # recent form window

# Bayesian shrinkage constants — blend observed ERA/WHIP toward league average
# when a pitcher has few innings. PRIOR_IP controls shrinkage strength:
# at PRIOR_IP innings the observed and prior are weighted equally.
# By ~40-50 IP the blended value is ~75% observed and converges by mid-season.
LEAGUE_AVG_ERA  = 4.50
LEAGUE_AVG_WHIP = 1.30
PRIOR_IP        = 15.0


def parse_ip(ip_str):
    """
    Convert statsapi innings pitched string to decimal.
    '6.1' → 6.333 (6 innings, 1 out), '6.2' → 6.667
    """
    try:
        parts = str(ip_str).split('.')
        whole = int(parts[0])
        outs = int(parts[1]) if len(parts) > 1 else 0
        return round(whole + outs / 3.0, 3)
    except (ValueError, TypeError):
        return 0.0


def fetch_box_score(game_pk):
    """
    Fetch starting pitcher lines and team batting stats from a box score.

    Returns dict with keys 'home' and 'away', each containing:
      - 'starter': {name, player_id, ip, er, h, bb, k} or None
      - 'batting': {ab, hits, doubles, triples, home_runs, walks, strikeouts, runs}

    Returns {} on failure.
    """
    try:
        data = statsapi.boxscore_data(int(game_pk))
    except Exception as e:
        print(f"    WARNING: boxscore_data({game_pk}) failed: {e}")
        return {}

    result = {}
    for side in ['home', 'away']:
        side_data = data.get(side, {})
        pitcher_ids = side_data.get('pitchers', [])
        players = side_data.get('players', {})
        team_stats = side_data.get('teamStats', {})

        # --- Starter ---
        starter = None
        if pitcher_ids:
            starter_id = pitcher_ids[0]
            player = players.get(f'ID{starter_id}', {})
            name = player.get('person', {}).get('fullName', '')
            if name:
                pitching = player.get('stats', {}).get('pitching', {})
                starter = {
                    'name': name,
                    'player_id': player.get('person', {}).get('id'),
                    'ip': parse_ip(pitching.get('inningsPitched', '0.0')),
                    'er': int(pitching.get('earnedRuns', 0) or 0),
                    'h': int(pitching.get('hits', 0) or 0),
                    'bb': int(pitching.get('baseOnBalls', 0) or 0),
                    'k': int(pitching.get('strikeOuts', 0) or 0),
                }

        # --- Team batting ---
        batting = team_stats.get('batting', {})
        result[side] = {
            'starter': starter,
            'batting': {
                'ab': int(batting.get('atBats', 0) or 0),
                'hits': int(batting.get('hits', 0) or 0),
                'doubles': int(batting.get('doubles', 0) or 0),
                'triples': int(batting.get('triples', 0) or 0),
                'home_runs': int(batting.get('homeRuns', 0) or 0),
                'walks': int(batting.get('baseOnBalls', 0) or 0),
                'strikeouts': int(batting.get('strikeOuts', 0) or 0),
                'runs': int(batting.get('runs', 0) or 0),
            },
        }

    return result


def fetch_starters(game_pk):
    """
    Backwards-compatible wrapper — returns {'home': dict, 'away': dict} with starter info only.
    """
    box = fetch_box_score(game_pk)
    result = {}
    for side, data in box.items():
        if data.get('starter'):
            result[side] = data['starter']
    return result


def upsert_starter_log(conn, game_id, game_date, team_id, starter):
    conn.execute(text("""
        INSERT INTO pitcher_game_logs (
            game_id, game_date, season, team_id,
            pitcher_name, player_id,
            innings_pitched, earned_runs, hits, walks, strikeouts, is_starter
        )
        VALUES (
            :game_id, :game_date, :season, :team_id,
            :name, :player_id,
            :ip, :er, :h, :bb, :k, TRUE
        )
        ON CONFLICT (game_id, pitcher_name) DO UPDATE SET
            innings_pitched = EXCLUDED.innings_pitched,
            earned_runs     = EXCLUDED.earned_runs,
            hits            = EXCLUDED.hits,
            walks           = EXCLUDED.walks,
            strikeouts      = EXCLUDED.strikeouts,
            captured_at     = NOW()
    """), {
        'game_id': game_id,
        'game_date': str(game_date),
        'season': int(str(game_date)[:4]),
        'team_id': team_id,
        'name': starter['name'],
        'player_id': starter.get('player_id'),
        'ip': starter['ip'],
        'er': starter['er'],
        'h': starter['h'],
        'bb': starter['bb'],
        'k': starter['k'],
    })


def upsert_batting_log(conn, game_id, game_date, team_id, batting):
    conn.execute(text("""
        INSERT INTO team_batting_logs (
            game_id, game_date, season, team_id,
            at_bats, hits, doubles, triples, home_runs,
            walks, strikeouts, runs_scored
        )
        VALUES (
            :game_id, :game_date, :season, :team_id,
            :ab, :hits, :doubles, :triples, :home_runs,
            :walks, :strikeouts, :runs
        )
        ON CONFLICT (game_id, team_id) DO UPDATE SET
            at_bats    = EXCLUDED.at_bats,
            hits       = EXCLUDED.hits,
            doubles    = EXCLUDED.doubles,
            triples    = EXCLUDED.triples,
            home_runs  = EXCLUDED.home_runs,
            walks      = EXCLUDED.walks,
            strikeouts = EXCLUDED.strikeouts,
            runs_scored = EXCLUDED.runs_scored,
            captured_at = NOW()
    """), {
        'game_id': game_id,
        'game_date': str(game_date),
        'season': int(str(game_date)[:4]),
        'team_id': team_id,
        'ab': batting['ab'],
        'hits': batting['hits'],
        'doubles': batting['doubles'],
        'triples': batting['triples'],
        'home_runs': batting['home_runs'],
        'walks': batting['walks'],
        'strikeouts': batting['strikeouts'],
        'runs': batting['runs'],
    })


def _ops_from_row(ab, hits, doubles, triples, home_runs, walks):
    """Compute OPS from raw batting totals. Returns None if ab == 0."""
    if ab == 0:
        return None
    singles = hits - doubles - triples - home_runs
    # On-base percentage: (H + BB) / (AB + BB)
    obp_denom = ab + walks
    obp = (hits + walks) / obp_denom if obp_denom > 0 else 0.0
    # Slugging: total_bases / AB
    total_bases = singles + 2 * doubles + 3 * triples + 4 * home_runs
    slg = total_bases / ab
    return round(obp + slg, 3)


def compute_l7_ops(team_ids, date_str, eng=None):
    """
    Compute L7 OPS for each team from team_batting_logs (last 7 games before date_str).
    Returns dict keyed by team_id: {'ops': float|None, 'games': int}
    """
    if not team_ids:
        return {}

    eng = eng or engine
    ids = list(set(t for t in team_ids if t))

    with eng.connect() as conn:
        df = pd.read_sql(text("""
            SELECT team_id,
                   SUM(at_bats)    AS ab,
                   SUM(hits)       AS hits,
                   SUM(doubles)    AS doubles,
                   SUM(triples)    AS triples,
                   SUM(home_runs)  AS home_runs,
                   SUM(walks)      AS walks,
                   COUNT(*)        AS games
            FROM (
                SELECT team_id, at_bats, hits, doubles, triples, home_runs, walks,
                       ROW_NUMBER() OVER (
                           PARTITION BY team_id ORDER BY game_date DESC
                       ) AS rn
                FROM team_batting_logs
                WHERE team_id = ANY(:ids)
                  AND game_date < :date
            ) ranked
            WHERE rn <= 7
            GROUP BY team_id
        """), conn, params={'ids': ids, 'date': date_str})

    result = {}
    for _, row in df.iterrows():
        ops = _ops_from_row(
            int(row['ab']), int(row['hits']), int(row['doubles']),
            int(row['triples']), int(row['home_runs']), int(row['walks'])
        )
        result[row['team_id']] = {'ops': ops, 'games': int(row['games'])}

    for tid in ids:
        if tid not in result:
            result[tid] = {'ops': None, 'games': 0}

    return result


def compute_starter_eras(pitcher_names, date_str, eng=None):
    """
    Compute ERA and WHIP for each pitcher at two windows:
      - Full window (last STARTS_WINDOW starts) — used as primary season-level signal
      - L3 window (last 3 starts) — used as recent form signal

    Requires at least MIN_STARTS_FOR_ERA starts in a window before returning values.

    Returns dict keyed by pitcher name:
      {name: {'era': float|None, 'whip': float|None,
              'l3_era': float|None, 'l3_whip': float|None,
              'season_era': float|None, 'season_whip': float|None}}

    'era' and 'whip' are the primary values (full window, same as before).
    'season_era'/'season_whip' are aliases for 'era'/'whip' (for clarity in callers).
    'l3_era'/'l3_whip' are the recent 3-start values.
    """
    if not pitcher_names:
        return {}

    eng = eng or engine
    names = list(set(n for n in pitcher_names if n))

    season = int(date_str[:4])
    with eng.connect() as conn:
        df = pd.read_sql(text("""
            SELECT pitcher_name, rn,
                   innings_pitched, earned_runs, hits, walks
            FROM (
                SELECT pitcher_name, innings_pitched, earned_runs, hits, walks,
                       ROW_NUMBER() OVER (
                           PARTITION BY pitcher_name
                           ORDER BY game_date DESC
                       ) AS rn
                FROM pitcher_game_logs
                WHERE pitcher_name = ANY(:names)
                  AND game_date < :date
                  AND season = :season
            ) ranked
            WHERE rn <= :window
        """), conn, params={'names': names, 'window': STARTS_WINDOW, 'date': date_str, 'season': season})

    result = {}
    for name in names:
        rows = df[df['pitcher_name'] == name]

        def _calc(subset):
            if len(subset) < MIN_STARTS_FOR_ERA:
                return None, None
            ip = float(subset['innings_pitched'].sum() or 0)
            if ip == 0:
                return None, None
            raw_era  = float(subset['earned_runs'].sum()) / ip * 9
            raw_whip = (float(subset['hits'].sum()) + float(subset['walks'].sum())) / ip
            # Bayesian shrinkage: blend toward league average weighted by IP.
            # Low IP → pulled toward LEAGUE_AVG; high IP → converges to raw value.
            weight = ip + PRIOR_IP
            era  = round((raw_era  * ip + LEAGUE_AVG_ERA  * PRIOR_IP) / weight, 2)
            whip = round((raw_whip * ip + LEAGUE_AVG_WHIP * PRIOR_IP) / weight, 3)
            return era, whip

        full_era, full_whip = _calc(rows)
        l3_era, l3_whip = _calc(rows[rows['rn'] <= L3_WINDOW])

        result[name] = {
            'era': full_era,
            'whip': full_whip,
            'season_era': full_era,
            'season_whip': full_whip,
            'l3_era': l3_era,
            'l3_whip': l3_whip,
        }

    for name in names:
        if name not in result:
            result[name] = {
                'era': None, 'whip': None,
                'season_era': None, 'season_whip': None,
                'l3_era': None, 'l3_whip': None,
            }

    return result


def main(start_date_str=None, end_date_str=None):
    if start_date_str is None:
        start_date_str = date_cls.today().strftime('%Y-%m-%d')
    if end_date_str is None:
        end_date_str = start_date_str

    date_label = (f"{start_date_str} → {end_date_str}"
                  if start_date_str != end_date_str else start_date_str)
    print(f"  Fetching box scores for {date_label}...")

    with engine.connect() as conn:
        games = pd.read_sql(text("""
            SELECT game_id, game_date, home_team_id, away_team_id, game_pk
            FROM games
            WHERE game_date BETWEEN :start AND :end
              AND status = 'final'
              AND game_pk IS NOT NULL
        """), conn, params={'start': start_date_str, 'end': end_date_str})

    if games.empty:
        print("  No final games with game_pk found — skipping.")
        return

    print(f"  {len(games)} final games found")
    pitcher_rows = 0
    batting_rows = 0

    with engine.connect() as conn:
        for _, game in games.iterrows():
            box = fetch_box_score(game['game_pk'])
            if not box:
                continue
            side_to_team = {'home': game['home_team_id'], 'away': game['away_team_id']}
            for side, data in box.items():
                team_id = side_to_team[side]
                if data.get('starter'):
                    upsert_starter_log(conn, game['game_id'], game['game_date'],
                                       team_id, data['starter'])
                    pitcher_rows += 1
                if data.get('batting'):
                    upsert_batting_log(conn, game['game_id'], game['game_date'],
                                       team_id, data['batting'])
                    batting_rows += 1
            time.sleep(0.05)
        conn.commit()

    print(f"  {pitcher_rows} pitcher log rows upserted")
    print(f"  {batting_rows} batting log rows upserted")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--date', default=date_cls.today().strftime('%Y-%m-%d'))
    parser.add_argument('--start-date', default=None)
    parser.add_argument('--end-date', default=None)
    args = parser.parse_args()
    start = args.start_date or args.date
    end = args.end_date or start
    main(start, end)
