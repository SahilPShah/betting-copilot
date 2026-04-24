import argparse
import json
import os
import sys
import textwrap
from datetime import date as date_cls
import pandas as pd

from sqlalchemy import create_engine, text
from dotenv import load_dotenv

load_dotenv()
engine = create_engine(os.getenv("DATABASE_URL"))

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

from recs.edge import load_current_odds, load_predictions, compute_edges
from recs.confidence import compute_confidence, load_injury_flags
from recs.rules import apply_rules, select_top_picks
from ingest.mlb_boxscores import compute_starter_eras
from llm.explain import generate_explanation


def load_team_stats(date_str):
    """Load season stats + L7 rolling stats + games played for all teams. Returns dict keyed by team_id."""
    year = int(date_str[:4])
    with engine.connect() as conn:
        season_stats = pd.read_sql(text("""
            SELECT DISTINCT ON (team_id)
                team_id, team_win_pct, team_ops, runs_scored_avg,
                team_pitching_era, team_pitching_whip
            FROM team_season_stats
            WHERE season <= :year
            ORDER BY team_id, season DESC
        """), conn, params={"year": year})

        l7_stats = pd.read_sql(text("""
            SELECT DISTINCT ON (team_id)
                team_id,
                l7_win_pct, l7_runs_scored_avg, l7_runs_allowed_avg,
                l7_run_diff_avg, l7_games
            FROM team_stats_mlb
            WHERE as_of_date <= :date
            ORDER BY team_id, as_of_date DESC
        """), conn, params={"date": date_str})

        gp = pd.read_sql(text("""
            WITH all_games AS (
                SELECT home_team_id as team_id FROM games
                WHERE status = 'final' AND LEFT(game_date::text, 4) = :year
                UNION ALL
                SELECT away_team_id FROM games
                WHERE status = 'final' AND LEFT(game_date::text, 4) = :year
            )
            SELECT team_id, COUNT(*) as games_played FROM all_games GROUP BY team_id
        """), conn, params={"year": str(year)})

    merged = season_stats.merge(l7_stats, on='team_id', how='left')
    merged = merged.merge(gp, on='team_id', how='left')
    merged['games_played'] = merged['games_played'].fillna(0).astype(int)
    return merged.set_index('team_id').to_dict('index')


def load_starters(date_str):
    """Load probable starters for all games on this date, with ERA from pitcher_game_logs."""
    with engine.connect() as conn:
        df = pd.read_sql(text("""
            SELECT gs.game_id, gs.side, gs.starter_name
            FROM game_starters gs
            JOIN games g ON g.game_id = gs.game_id
            WHERE g.game_date = :date
        """), conn, params={"date": date_str})

    if df.empty:
        return df

    all_names = df['starter_name'].dropna().tolist()
    era_map = compute_starter_eras(all_names, date_str)

    df['starter_era'] = df['starter_name'].map(lambda n: era_map.get(n, {}).get('era'))
    df['starter_whip'] = df['starter_name'].map(lambda n: era_map.get(n, {}).get('whip'))
    df['starter_l3_era'] = df['starter_name'].map(lambda n: era_map.get(n, {}).get('l3_era'))
    return df


def write_slate_run(conn, date_str, model_version, games_count, picks_count):
    result = conn.execute(text("""
        INSERT INTO slate_runs (run_date, model_version, games_count, picks_count, ran_at)
        VALUES (:run_date, :model_version, :games_count, :picks_count, NOW())
        ON CONFLICT (run_date) DO UPDATE SET
            games_count  = EXCLUDED.games_count,
            picks_count  = EXCLUDED.picks_count,
            model_version = EXCLUDED.model_version,
            ran_at       = NOW()
        RETURNING slate_run_id
    """), {
        "run_date": date_str,
        "model_version": model_version,
        "games_count": games_count,
        "picks_count": picks_count,
    })
    return result.fetchone()[0]


def build_reasoning(rec, game_starters, team_stats_map):
    """Generate concise, number-specific reasoning for a pick."""
    game_id = rec['game_id']
    side = rec['side']
    model_prob = rec['model_prob']
    implied_prob = rec['implied_prob']
    edge = rec['edge']
    american_odds = rec['american_odds']
    predicted_margin = rec.get('predicted_margin')

    # Parse home/away team IDs from game_id: YYYY-MM-DD-HOME-AWAY-N
    parts = game_id.split('-')
    home_id = parts[3]
    away_id = parts[4]
    pick_id = home_id if side == 'home' else away_id
    opp_id = away_id if side == 'home' else home_id

    home_s = game_starters.get('home', {})
    away_s = game_starters.get('away', {})
    h_name = home_s.get('name') or 'TBD'
    a_name = away_s.get('name') or 'TBD'
    # Prefer L3 ERA for recency signal, fall back to season ERA
    h_era = home_s.get('l3_era') or home_s.get('era')
    a_era = away_s.get('l3_era') or away_s.get('era')
    h_era_label = 'L3 ERA' if home_s.get('l3_era') is not None else 'ERA'
    a_era_label = 'L3 ERA' if away_s.get('l3_era') is not None else 'ERA'

    def era_str(e): return f"{e:.2f}" if e is not None and e == e else "N/A"
    def stat_str(v, fmt='.3f'): return format(v, fmt) if v is not None else "N/A"

    # Team stats
    pick_stats = team_stats_map.get(pick_id, {})
    opp_stats = team_stats_map.get(opp_id, {})
    pick_gp = pick_stats.get('games_played', 0)
    opp_gp = opp_stats.get('games_played', 0)
    min_gp = min(pick_gp, opp_gp)

    lines = []

    # Line 1: Edge summary with margin
    margin_str = f", proj. margin {predicted_margin:+.1f}" if predicted_margin is not None else ""
    lines.append(
        f"{pick_id} {side} {model_prob:.1%}{margin_str} vs mkt {implied_prob:.1%} "
        f"({american_odds:+d}) → {edge:.1%} edge."
    )

    # Line 2: Team stat comparison
    pick_wl  = stat_str(pick_stats.get('team_win_pct'), '.3f')
    opp_wl   = stat_str(opp_stats.get('team_win_pct'), '.3f')
    pick_ops = stat_str(pick_stats.get('team_ops'), '.3f')
    opp_ops  = stat_str(opp_stats.get('team_ops'), '.3f')
    pick_r   = stat_str(pick_stats.get('runs_scored_avg'), '.1f')
    opp_r    = stat_str(opp_stats.get('runs_scored_avg'), '.1f')
    pick_era = stat_str(pick_stats.get('team_pitching_era'), '.2f')
    opp_era  = stat_str(opp_stats.get('team_pitching_era'), '.2f')
    lines.append(
        f"{pick_id}: {pick_wl} W% / {pick_ops} OPS / {pick_r} R/G / {pick_era} ERA  "
        f"vs  {opp_id}: {opp_wl} W% / {opp_ops} OPS / {opp_r} R/G / {opp_era} ERA."
    )

    # Line: L7 recent form
    def l7_record(stats):
        games = stats.get('l7_games')
        win_pct = stats.get('l7_win_pct')
        if games is None or win_pct is None:
            return None
        wins = round(float(win_pct) * int(games))
        losses = int(games) - wins
        net = stats.get('l7_run_diff_avg')
        net_str = f", {float(net):+.1f} R/G net" if net is not None else ""
        return f"{wins}-{losses} L{int(games)}{net_str}"

    pick_l7 = l7_record(pick_stats)
    opp_l7 = l7_record(opp_stats)
    if pick_l7 or opp_l7:
        pick_l7_str = pick_l7 or "N/A"
        opp_l7_str = opp_l7 or "N/A"
        lines.append(f"Recent form: {pick_id} {pick_l7_str}  vs  {opp_id} {opp_l7_str}.")

    # Line 3: Starter matchup
    lines.append(f"Starters: {h_name} {h_era_label} {era_str(h_era)} (home) vs {a_name} {a_era_label} {era_str(a_era)} (away).")

    # Line 4: Small sample caveat
    if min_gp < 15:
        lines.append(f"⚠ Early season ({min_gp} games) — stats are noisy, ELO carries more weight.")

    return " ".join(lines)


def _clean_for_json(obj):
    """Recursively replace float NaN with None so json.dumps produces valid JSON."""
    if isinstance(obj, float) and (obj != obj):  # NaN check
        return None
    if isinstance(obj, dict):
        return {k: _clean_for_json(v) for k, v in obj.items()}
    return obj


def write_recommendation(conn, slate_run_id, rec, game_starters=None):
    context = _clean_for_json({
        "model_prob": rec['model_prob'],
        "implied_prob": rec['implied_prob'],
        "edge": rec['edge'],
        "bookmaker": rec['bookmaker'],
        "american_odds": rec['american_odds'],
        "home_starter": game_starters.get('home') if game_starters else None,
        "away_starter": game_starters.get('away') if game_starters else None,
    })
    conn.execute(text("""
        INSERT INTO recommendations (
            slate_run_id, prediction_id, odds_snapshot_id, game_id,
            market, side, edge, confidence, decision, no_bet_reason,
            context_snapshot, created_at
        )
        VALUES (
            :slate_run_id, :prediction_id, :snapshot_id, :game_id,
            :market, :side, :edge, :confidence, :decision, :no_bet_reason,
            :context_snapshot, NOW()
        )
    """), {
        "slate_run_id": str(slate_run_id),
        "prediction_id": str(rec['prediction_id']),
        "snapshot_id": str(rec['snapshot_id']),
        "game_id": rec['game_id'],
        "market": rec['market'],
        "side": rec['side'],
        "edge": round(float(rec['edge']), 4),
        "confidence": float(rec['confidence']),
        "decision": rec['decision'],
        "no_bet_reason": rec.get('no_bet_reason'),
        "context_snapshot": json.dumps(context),
    })


def build_pick_context(pick, starter_map, team_stats_map, date_str):
    """Assemble a structured dict for the LLM explanation prompt."""
    game_id = pick['game_id']
    side = pick['side']
    parts = game_id.split('-')
    home_id = parts[3]
    away_id = parts[4]
    pick_id = home_id if side == 'home' else away_id
    opp_id = away_id if side == 'home' else home_id

    game_starters = starter_map.get(game_id, {})
    ps = team_stats_map.get(pick_id, {})
    os_ = team_stats_map.get(opp_id, {})

    def team_stat_dict(stats):
        return {
            'win_pct': stats.get('team_win_pct'),
            'ops': stats.get('team_ops'),
            'runs_scored_avg': stats.get('runs_scored_avg'),
            'era': stats.get('team_pitching_era'),
            'l7_win_pct': stats.get('l7_win_pct'),
            'l7_games': stats.get('l7_games'),
            'l7_run_diff_avg': stats.get('l7_run_diff_avg'),
        }

    return {
        'game_id': game_id,
        'date': date_str,
        'side': side,
        'market': pick.get('market'),
        'home_team': home_id,
        'away_team': away_id,
        'model_prob': pick.get('model_prob'),
        'implied_prob': pick.get('implied_prob'),
        'edge': pick.get('edge'),
        'american_odds': pick.get('american_odds'),
        'predicted_margin': pick.get('predicted_margin'),
        'home_starter': game_starters.get('home'),
        'away_starter': game_starters.get('away'),
        'pick_team_stats': team_stat_dict(ps),
        'opp_team_stats': team_stat_dict(os_),
        'elo_diff': pick.get('elo_diff'),
        'games_played': min(
            team_stats_map.get(home_id, {}).get('games_played', 0),
            team_stats_map.get(away_id, {}).get('games_played', 0),
        ),
    }


def main(date_str=None):
    if date_str is None:
        date_str = date_cls.today().strftime('%Y-%m-%d')

    print(f"Running recommendations for {date_str}")

    print("  Loading predictions...")
    predictions = load_predictions(date_str, engine)
    if predictions.empty:
        print("  No predictions found. Run models/predict.py first.")
        return None

    print(f"  {len(predictions)} predictions loaded")

    print("  Loading current odds...")
    odds = load_current_odds(date_str, engine)
    print(f"  {len(odds)} odds rows loaded")

    if odds.empty:
        print("  No odds found. Run ingest/odds_api.py first.")
        return None

    print("  Loading injury flags...")
    injuries = load_injury_flags(date_str, engine)
    print(f"  {len(injuries)} injury rows loaded")

    print("  Loading team stats...")
    team_stats_map = load_team_stats(date_str)

    print("  Loading probable starters...")
    starters = load_starters(date_str)
    # Build lookup: game_id -> {home_name, home_era, away_name, away_era}
    starter_map = {}
    for _, row in starters.iterrows():
        gid = row['game_id']
        if gid not in starter_map:
            starter_map[gid] = {}
        starter_map[gid][row['side']] = {
            'name': row['starter_name'],
            'era': float(row['starter_era']) if row['starter_era'] is not None else None,
            'whip': float(row['starter_whip']) if row['starter_whip'] is not None else None,
            'l3_era': float(row['starter_l3_era']) if row['starter_l3_era'] is not None else None,
        }

    print("  Computing edges...")
    edges_df = compute_edges(predictions, odds)

    if edges_df.empty:
        print("  No edges computed (no matching game+odds pairs).")
        return None

    print(f"  {len(edges_df)} edge rows computed")

    # Attach min games_played (home vs away) so confidence can apply early-season penalty
    def _min_gp(game_id):
        parts = game_id.split('-')
        home_id, away_id = parts[3], parts[4]
        home_gp = team_stats_map.get(home_id, {}).get('games_played', 0)
        away_gp = team_stats_map.get(away_id, {}).get('games_played', 0)
        return min(home_gp, away_gp)

    edges_df = edges_df.copy()
    edges_df['min_gp'] = edges_df['game_id'].map(_min_gp)

    print("  Applying rules...")
    results = apply_rules(edges_df, compute_confidence, injuries)
    top_picks, results = select_top_picks(results, n=5)

    qualifying_count = len(top_picks)
    model_version = predictions['model_version'].iloc[0]

    print(f"  Top picks: {qualifying_count} qualifying (showing up to 5)")

    print("  Writing to DB...")
    with engine.connect() as conn:
        slate_run_id = write_slate_run(
            conn, date_str, model_version,
            games_count=len(predictions),
            picks_count=qualifying_count,
        )

        for rec in results:
            game_starters = starter_map.get(rec['game_id'], {})
            rec['_starters'] = game_starters
            write_recommendation(conn, slate_run_id, rec, game_starters)

        conn.commit()

    print(f"  Slate run ID: {slate_run_id}")

    if top_picks:
        print("  Generating LLM explanations...")
        with engine.connect() as conn:
            for pick in top_picks:
                try:
                    ctx = build_pick_context(pick, starter_map, team_stats_map, date_str)
                    explanation = generate_explanation(ctx)
                    pick['_llm_explanation'] = explanation
                    conn.execute(text("""
                        UPDATE recommendations
                        SET llm_explanation = :explanation
                        WHERE game_id = :game_id
                          AND slate_run_id = :slate_run_id
                          AND market = :market
                          AND side = :side
                    """), {
                        "explanation": explanation,
                        "game_id": pick['game_id'],
                        "slate_run_id": str(slate_run_id),
                        "market": pick['market'],
                        "side": pick['side'],
                    })
                except Exception as e:
                    print(f"  Warning: LLM explanation failed for {pick['game_id']}: {e}")
                    pick['_llm_explanation'] = None
            conn.commit()

    def fmt_starter(s):
        if not s or not s.get('name'):
            return 'TBD'
        l3_era = s.get('l3_era')
        era = s.get('era')
        if l3_era is not None and l3_era == l3_era:
            era_str = f" (L3 ERA {l3_era:.2f})"
        elif era is not None and era == era:
            era_str = f" (ERA {era:.2f})"
        else:
            era_str = ''
        return f"{s['name']}{era_str}"

    if top_picks:
        for rank, pick in enumerate(top_picks, start=1):
            label = f"PICK #{rank}" if rank > 1 else "TOP PICK"
            game_starters = starter_map.get(pick['game_id'], {})
            home_s = game_starters.get('home', {})
            away_s = game_starters.get('away', {})

            llm_explanation = pick.get('_llm_explanation')
            display_text = llm_explanation if llm_explanation else build_reasoning(pick, game_starters, team_stats_map)

            print(f"\n  ══════════════════════════════════════")
            print(f"  {label} — {date_str}")
            print(f"  ══════════════════════════════════════")
            print(f"    Game       : {pick['game_id']}")
            print(f"    Home SP    : {fmt_starter(home_s)}")
            print(f"    Away SP    : {fmt_starter(away_s)}")
            print(f"    Market     : {pick['market']}")
            print(f"    Side       : {pick['side']}")
            print(f"    Odds       : {pick['american_odds']:+d}")
            print(f"    Model prob : {pick['model_prob']:.1%}")
            print(f"    Implied    : {pick['implied_prob']:.1%}")
            print(f"    Edge       : {pick['edge']:.1%}")
            print(f"    Confidence : {pick['confidence']}/10")
            print(f"    Decision   : {pick['decision']}")
            print(f"  ──────────────────────────────────────")
            for line in textwrap.wrap(display_text, width=58):
                print(f"    {line}")
            print(f"  ══════════════════════════════════════")
    else:
        print("\n  NO BET — no game cleared edge + confidence thresholds today")

    return top_picks[0] if top_picks else None


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--date', default=date_cls.today().strftime('%Y-%m-%d'))
    args = parser.parse_args()
    main(args.date)
