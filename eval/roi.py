"""
ROI Backtest — eval/roi.py

Evaluates P&L on all settled qualifying picks (decision != 'no_bet').
Assumes $100 flat bet per pick.

Usage:
    python eval/roi.py
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

STAKE = 100.0


def load_settled_picks():
    """
    Load qualifying picks joined with final game outcomes.
    Deduplicates to one row per (slate_run_id, game_id, market, side) —
    latest odds snapshot wins.
    """
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
                g.home_score,
                g.away_score,
                g.status,
                (r.context_snapshot->>'american_odds')::int AS american_odds,
                o.run_line_point
            FROM recommendations r
            JOIN slate_runs s ON s.slate_run_id = r.slate_run_id
            JOIN games g ON g.game_id = r.game_id
            LEFT JOIN odds_snapshots o ON o.snapshot_id = r.odds_snapshot_id
            WHERE r.decision <> 'no_bet'
              AND g.status = 'final'
              AND g.home_score IS NOT NULL
            ORDER BY s.run_date, r.game_id, r.market, r.side, r.created_at DESC
        """), conn)
    return df


def determine_winner(row):
    """Returns True if the pick won, False if lost, None if undecidable."""
    home_score = row['home_score']
    away_score = row['away_score']
    side = row['side']
    market = row['market']

    if home_score is None or away_score is None:
        return None

    home_score = float(home_score)
    away_score = float(away_score)
    margin = home_score - away_score  # positive = home won

    if market == 'moneyline':
        if side == 'home':
            return margin > 0
        else:
            return margin < 0

    elif market == 'run_line':
        run_line_point = row.get('run_line_point')
        # Fall back to context_snapshot if run_line_point missing
        if run_line_point is None:
            ctx = row.get('context_snapshot') or {}
            run_line_point = ctx.get('run_line_point')

        if run_line_point is None:
            # Can't determine cover without run_line_point
            return None

        run_line_point = float(run_line_point)

        if side == 'home':
            return margin + run_line_point > 0
        else:
            return (-margin) + run_line_point > 0

    return None


def compute_pnl(row, won):
    """Compute P&L for a $STAKE flat bet given american odds."""
    odds = row.get('american_odds')
    if odds is None:
        return None

    odds = int(odds)
    if won:
        if odds > 0:
            return STAKE * (odds / 100.0)
        else:
            return STAKE * (100.0 / abs(odds))
    else:
        return -STAKE


def print_breakdown(label, df):
    wins = df['won'].sum()
    total = len(df)
    pnl = df['pnl'].sum()
    staked = total * STAKE
    roi = pnl / staked if staked > 0 else 0
    win_rate = wins / total if total > 0 else 0
    print(f"  {label:<25} {total:>5} picks  {win_rate:>6.1%} win  {pnl:>+9.2f} P&L  {roi:>+7.1%} ROI")


def main():
    print("Loading settled picks...")
    df = load_settled_picks()
    print(f"  {len(df)} rows loaded\n")

    if df.empty:
        print("No settled picks found.")
        return

    # Determine outcomes
    df['won'] = df.apply(determine_winner, axis=1)
    df['pnl'] = df.apply(lambda r: compute_pnl(r, r['won']) if r['won'] is not None else None, axis=1)

    # Drop undecidable rows (missing run_line_point etc.)
    undecidable = df['won'].isna().sum()
    if undecidable:
        print(f"  Dropped {undecidable} picks with undecidable outcome (missing run_line_point).\n")
    df = df.dropna(subset=['won', 'pnl'])
    df['won'] = df['won'].astype(bool)

    total = len(df)
    wins = df['won'].sum()
    losses = total - wins
    total_staked = total * STAKE
    total_pnl = df['pnl'].sum()
    roi = total_pnl / total_staked if total_staked > 0 else 0
    win_rate = wins / total if total > 0 else 0

    date_range = f"{df['run_date'].min()} → {df['run_date'].max()}"

    print(f"{'='*60}")
    print(f"  ROI BACKTEST — ${STAKE:.0f} flat bet per pick")
    print(f"  {date_range}")
    print(f"{'='*60}")
    print(f"\n  Total picks  : {total}")
    print(f"  Wins / Losses: {wins} / {losses}")
    print(f"  Win rate     : {win_rate:.1%}")
    print(f"  Total staked : ${total_staked:,.0f}")
    print(f"  Total P&L    : ${total_pnl:+,.2f}")
    print(f"  ROI          : {roi:+.1%}")

    print(f"\n{'─'*60}")
    print("  BY MARKET")
    print(f"{'─'*60}")
    for market in sorted(df['market'].unique()):
        print_breakdown(market, df[df['market'] == market])

    print(f"\n{'─'*60}")
    print("  BY SIDE")
    print(f"{'─'*60}")
    for side in ['home', 'away']:
        print_breakdown(side, df[df['side'] == side])

    print(f"\n{'─'*60}")
    print("  BY DECISION TIER")
    print(f"{'─'*60}")
    for tier in sorted(df['decision'].unique()):
        print_breakdown(tier, df[df['decision'] == tier])

    print(f"\n{'─'*60}")
    print("  BY MONTH")
    print(f"{'─'*60}")
    df['month'] = df['run_date'].astype(str).str[:7]
    for month in sorted(df['month'].unique()):
        print_breakdown(month, df[df['month'] == month])

    print(f"\n{'─'*60}")
    print("  EDGE QUINTILE BREAKDOWN")
    print(f"{'─'*60}")
    df['edge_quintile'] = pd.qcut(df['edge'], q=5, labels=['Q1 (low)', 'Q2', 'Q3', 'Q4', 'Q5 (high)'])
    for q in df['edge_quintile'].cat.categories:
        subset = df[df['edge_quintile'] == q]
        mean_edge = subset['edge'].mean()
        print_breakdown(f"{q}  (edge ~{mean_edge:.1%})", subset)

    # Save
    from datetime import date as date_cls
    today = date_cls.today().strftime('%Y-%m-%d')
    os.makedirs("eval/backtest_result", exist_ok=True)
    out = df[['run_date', 'game_id', 'market', 'side', 'edge', 'confidence',
              'decision', 'american_odds', 'won', 'pnl']].copy()
    out_path = f"eval/backtest_result/roi_{today}.csv"
    out.to_csv(out_path, index=False)
    print(f"\n  Results saved to {out_path}")


if __name__ == "__main__":
    main()
