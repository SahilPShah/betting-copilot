"""
Edge Distribution Analysis — eval/edge_dist.py

Analyzes the distribution of model edge across all evaluated picks.
Shows whether higher edge correlates with better outcomes.

Usage:
    python eval/edge_dist.py
"""
import os
import sys
import pandas as pd
import numpy as np
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

load_dotenv("/Users/sahilshah/betting-copilot/.env")
engine = create_engine(os.getenv("DATABASE_URL"))


def load_picks_with_outcomes():
    """Load all evaluated picks (any decision) joined with final outcomes where available."""
    with engine.connect() as conn:
        df = pd.read_sql(text("""
            SELECT DISTINCT ON (s.run_date, r.game_id, r.market, r.side)
                s.run_date,
                r.game_id,
                r.market,
                r.side,
                r.edge,
                r.confidence,
                r.decision,
                r.no_bet_reason,
                g.home_score,
                g.away_score,
                g.status,
                o.american_odds,
                o.run_line_point
            FROM recommendations r
            JOIN slate_runs s ON s.slate_run_id = r.slate_run_id
            JOIN games g ON g.game_id = r.game_id
            LEFT JOIN odds_snapshots o ON o.snapshot_id = (
                r.context_snapshot->>'snapshot_id'
            )::uuid
            ORDER BY s.run_date, r.game_id, r.market, r.side, r.created_at DESC
        """), conn)
    return df


def determine_winner(row):
    """Returns True if the pick won, False if lost, None if unsettled or undecidable."""
    if row['status'] != 'final' or row['home_score'] is None:
        return None

    home_score = float(row['home_score'])
    away_score = float(row['away_score'])
    margin = home_score - away_score
    side = row['side']
    market = row['market']

    if market == 'moneyline':
        return margin > 0 if side == 'home' else margin < 0

    elif market == 'run_line':
        run_line_point = row.get('run_line_point')
        if run_line_point is None:
            return None
        run_line_point = float(run_line_point)
        if side == 'home':
            return margin + run_line_point > 0
        else:
            return (-margin) + run_line_point > 0

    return None


def print_edge_bucket_table(df, label="All picks"):
    """Print edge bucket analysis: edge range → win rate, avg P&L proxy."""
    settled = df.dropna(subset=['won'])
    if settled.empty:
        print(f"  {label}: no settled picks")
        return

    # Create edge buckets
    max_edge = settled['edge'].max()
    min_edge = settled['edge'].min()

    edges = sorted(settled['edge'].unique())
    bucket_edges = np.linspace(min_edge, max_edge, 8)
    settled = settled.copy()
    settled['edge_bucket'] = pd.cut(settled['edge'], bins=bucket_edges, include_lowest=True)

    print(f"\n  {label} ({len(settled)} settled):")
    print(f"  {'Edge Range':<20} {'N':>4}  {'Win%':>6}  {'Avg Odds':>9}")
    print(f"  {'─'*45}")
    for bucket in settled['edge_bucket'].cat.categories:
        subset = settled[settled['edge_bucket'] == bucket]
        if len(subset) == 0:
            continue
        win_rate = subset['won'].mean()
        avg_odds = subset['american_odds'].mean() if 'american_odds' in subset and subset['american_odds'].notna().any() else float('nan')
        odds_str = f"{avg_odds:+.0f}" if not np.isnan(avg_odds) else "N/A"
        print(f"  {str(bucket):<20} {len(subset):>4}  {win_rate:>6.1%}  {odds_str:>9}")


def main():
    print("Loading all picks with outcomes...")
    df = load_picks_with_outcomes()
    print(f"  {len(df)} rows loaded\n")

    if df.empty:
        print("No data found.")
        return

    df['won'] = df.apply(determine_winner, axis=1)
    settled = df.dropna(subset=['won'])
    qualifying = df[df['decision'] != 'no_bet']
    qualifying_settled = qualifying.dropna(subset=['won'])

    print(f"{'='*55}")
    print(f"  EDGE DISTRIBUTION ANALYSIS")
    print(f"{'='*55}")

    # Overall edge summary
    print(f"\n--- Edge Summary (all evaluated picks) ---")
    print(f"  Count        : {len(df)}")
    print(f"  Mean edge    : {df['edge'].mean():+.3f}  ({df['edge'].mean()*100:+.1f}%)")
    print(f"  Median edge  : {df['edge'].median():+.3f}  ({df['edge'].median()*100:+.1f}%)")
    print(f"  Std dev      : {df['edge'].std():.3f}")
    print(f"  Max edge     : {df['edge'].max():+.3f}  ({df['edge'].max()*100:+.1f}%)")
    print(f"  Min edge     : {df['edge'].min():+.3f}  ({df['edge'].min()*100:+.1f}%)")
    print(f"  % positive   : {(df['edge'] > 0).mean():.1%}")

    print(f"\n--- Edge Summary (qualifying picks only) ---")
    if not qualifying.empty:
        print(f"  Count        : {len(qualifying)}")
        print(f"  Mean edge    : {qualifying['edge'].mean():+.3f}  ({qualifying['edge'].mean()*100:+.1f}%)")
        print(f"  Median edge  : {qualifying['edge'].median():+.3f}  ({qualifying['edge'].median()*100:+.1f}%)")

    # Edge histogram
    print(f"\n{'─'*55}")
    print("  EDGE HISTOGRAM (all positive-edge picks)")
    print(f"{'─'*55}")
    pos_edge = df[df['edge'] > 0]['edge']
    bins = [0, 0.03, 0.06, 0.10, 0.15, 0.20, 0.30, 1.0]
    labels = ['0–3%', '3–6%', '6–10%', '10–15%', '15–20%', '20–30%', '30%+']
    bucketed = pd.cut(pos_edge, bins=bins, labels=labels, right=True)
    counts = bucketed.value_counts().sort_index()
    for label_str, count in counts.items():
        bar = '█' * min(count, 50)
        print(f"  {label_str:<10}: {count:>4}  {bar}")

    # Edge vs outcome (does higher edge actually win more?)
    print(f"\n{'─'*55}")
    print("  EDGE VS OUTCOME (settled qualifying picks)")
    print(f"{'─'*55}")
    if not qualifying_settled.empty:
        q_settled = qualifying_settled.copy()
        q_settled['edge_decile'] = pd.qcut(q_settled['edge'], q=5,
                                            labels=['Q1 (lowest)', 'Q2', 'Q3', 'Q4', 'Q5 (highest)'],
                                            duplicates='drop')
        print(f"  {'Quintile':<18} {'N':>4}  {'Win%':>6}  {'Mean Edge':>10}")
        print(f"  {'─'*45}")
        for q in q_settled['edge_decile'].cat.categories:
            subset = q_settled[q_settled['edge_decile'] == q]
            win_rate = subset['won'].mean()
            mean_edge = subset['edge'].mean()
            print(f"  {str(q):<18} {len(subset):>4}  {win_rate:>6.1%}  {mean_edge:>+10.1%}")

    # By market
    print(f"\n{'─'*55}")
    print("  EDGE DISTRIBUTION BY MARKET")
    print(f"{'─'*55}")
    for market in sorted(df['market'].unique()):
        market_df = qualifying[qualifying['market'] == market]
        if market_df.empty:
            continue
        print(f"\n  {market.upper()}:")
        print(f"    Qualifying picks : {len(market_df)}")
        print(f"    Mean edge        : {market_df['edge'].mean():+.1%}")
        print(f"    Median edge      : {market_df['edge'].median():+.1%}")
        settled_m = market_df.dropna(subset=['won'])
        if not settled_m.empty:
            print(f"    Win rate         : {settled_m['won'].mean():.1%}  ({len(settled_m)} settled)")

    # By side
    print(f"\n{'─'*55}")
    print("  EDGE DISTRIBUTION BY SIDE")
    print(f"{'─'*55}")
    for side in ['home', 'away']:
        side_df = qualifying[qualifying['side'] == side]
        if side_df.empty:
            continue
        print(f"\n  {side.upper()}:")
        print(f"    Qualifying picks : {len(side_df)}")
        print(f"    Mean edge        : {side_df['edge'].mean():+.1%}")
        print(f"    Median edge      : {side_df['edge'].median():+.1%}")
        settled_s = side_df.dropna(subset=['won'])
        if not settled_s.empty:
            print(f"    Win rate         : {settled_s['won'].mean():.1%}  ({len(settled_s)} settled)")

    # Save
    os.makedirs("eval/results", exist_ok=True)
    out = df[['run_date', 'game_id', 'market', 'side', 'edge', 'confidence', 'decision', 'won']].copy()
    out.to_csv("eval/results/edge_distribution.csv", index=False)
    print(f"\n  Results saved to eval/results/edge_distribution.csv")


if __name__ == "__main__":
    main()
