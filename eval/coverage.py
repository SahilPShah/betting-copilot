"""
Coverage Analysis — eval/coverage.py

Measures how often the system generates picks, pick rate by market,
slate size distribution, and no-bet reason breakdown.

Usage:
    python eval/coverage.py
"""
import os
import sys
import pandas as pd
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

load_dotenv("/Users/sahilshah/betting-copilot/.env")
engine = create_engine(os.getenv("DATABASE_URL"))


def load_all_recommendations():
    with engine.connect() as conn:
        df = pd.read_sql(text("""
            SELECT DISTINCT ON (s.run_date, r.game_id, r.market, r.side)
                s.run_date,
                r.game_id,
                r.market,
                r.side,
                r.decision,
                r.no_bet_reason,
                r.edge,
                r.confidence
            FROM recommendations r
            JOIN slate_runs s ON s.slate_run_id = r.slate_run_id
            ORDER BY s.run_date, r.game_id, r.market, r.side, r.created_at DESC
        """), conn)
    return df


def load_slate_runs():
    with engine.connect() as conn:
        df = pd.read_sql(text("""
            SELECT run_date, games_count, picks_count, model_version, ran_at
            FROM slate_runs
            ORDER BY run_date
        """), conn)
    return df


def main():
    print("Loading recommendations...")
    recs = load_all_recommendations()
    slates = load_slate_runs()
    print(f"  {len(recs)} recommendation rows across {len(slates)} slates\n")

    if recs.empty:
        print("No data found.")
        return

    qualifying = recs[recs['decision'] != 'no_bet']
    no_bet = recs[recs['decision'] == 'no_bet']

    total_opportunities = len(recs)
    total_picks = len(qualifying)
    pick_rate = total_picks / total_opportunities if total_opportunities > 0 else 0

    print(f"{'='*55}")
    print(f"  COVERAGE ANALYSIS")
    print(f"{'='*55}")
    print(f"\n  Date range      : {recs['run_date'].min()} → {recs['run_date'].max()}")
    print(f"  Total slates    : {len(slates)}")
    print(f"  Total evaluated : {total_opportunities} game+market+side combos")
    print(f"  Qualifying picks: {total_picks}")
    print(f"  Overall pick rate: {pick_rate:.1%}")

    # Slate size distribution
    print(f"\n{'─'*55}")
    print("  SLATE SIZE DISTRIBUTION (picks per day)")
    print(f"{'─'*55}")
    slate_sizes = qualifying.groupby('run_date').size().value_counts().sort_index()
    for size, count in slate_sizes.items():
        bar = '█' * count
        print(f"  {size} picks/day : {count:>3} slates  {bar}")
    zero_pick_days = len(slates) - qualifying['run_date'].nunique()
    if zero_pick_days:
        print(f"  0 picks/day : {zero_pick_days:>3} slates")

    avg_picks = slates['picks_count'].mean()
    print(f"\n  Average picks per slate: {avg_picks:.1f}")

    # Pick rate by market
    print(f"\n{'─'*55}")
    print("  PICK RATE BY MARKET")
    print(f"{'─'*55}")
    for market in sorted(recs['market'].unique()):
        market_recs = recs[recs['market'] == market]
        market_picks = market_recs[market_recs['decision'] != 'no_bet']
        rate = len(market_picks) / len(market_recs) if len(market_recs) > 0 else 0
        print(f"  {market:<12} : {len(market_picks):>4} picks / {len(market_recs):>4} evaluated  ({rate:.1%})")

    # Pick rate by side
    print(f"\n{'─'*55}")
    print("  PICK RATE BY SIDE")
    print(f"{'─'*55}")
    for side in ['home', 'away']:
        side_recs = recs[recs['side'] == side]
        side_picks = side_recs[side_recs['decision'] != 'no_bet']
        rate = len(side_picks) / len(side_recs) if len(side_recs) > 0 else 0
        print(f"  {side:<8} : {len(side_picks):>4} picks / {len(side_recs):>4} evaluated  ({rate:.1%})")

    # No-bet reason breakdown
    print(f"\n{'─'*55}")
    print("  WHY PICKS WERE REJECTED (no_bet reasons)")
    print(f"{'─'*55}")
    reason_counts = no_bet['no_bet_reason'].value_counts()
    for reason, count in reason_counts.items():
        pct = count / len(no_bet)
        print(f"  {reason:<30} : {count:>5}  ({pct:.1%})")

    # Confidence distribution on qualifying picks
    print(f"\n{'─'*55}")
    print("  CONFIDENCE DISTRIBUTION (qualifying picks)")
    print(f"{'─'*55}")
    bins = [0, 5, 6, 7, 8, 9, 10]
    labels = ['<5', '5–6', '6–7', '7–8', '8–9', '9–10']
    qualifying = qualifying.copy()
    qualifying['conf_bucket'] = pd.cut(qualifying['confidence'], bins=bins, labels=labels, right=True)
    conf_dist = qualifying['conf_bucket'].value_counts().sort_index()
    for bucket, count in conf_dist.items():
        pct = count / len(qualifying)
        bar = '█' * int(pct * 40)
        print(f"  {bucket:<8} : {count:>4}  ({pct:.1%})  {bar}")

    # Monthly coverage trend
    print(f"\n{'─'*55}")
    print("  MONTHLY COVERAGE TREND")
    print(f"{'─'*55}")
    recs['month'] = recs['run_date'].astype(str).str[:7]
    qualifying['month'] = qualifying['run_date'].astype(str).str[:7]
    for month in sorted(recs['month'].unique()):
        month_total = len(recs[recs['month'] == month])
        month_picks = len(qualifying[qualifying['month'] == month])
        rate = month_picks / month_total if month_total > 0 else 0
        print(f"  {month} : {month_picks:>3} picks / {month_total:>4} evaluated  ({rate:.1%})")

    # Save
    os.makedirs("eval/results", exist_ok=True)
    summary = recs.groupby(['run_date', 'market', 'side']).apply(
        lambda g: pd.Series({
            'total': len(g),
            'qualifying': (g['decision'] != 'no_bet').sum(),
        })
    ).reset_index()
    summary.to_csv("eval/results/coverage.csv", index=False)
    print(f"\n  Results saved to eval/results/coverage.csv")


if __name__ == "__main__":
    main()
