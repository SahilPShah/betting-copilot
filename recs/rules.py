import pandas as pd

# Thresholds locked for v1 (planning doc Section 1.4)
MIN_EDGE_MONEYLINE = 0.03   # 3%
MIN_EDGE_RUN_LINE  = 0.04   # 4%
MIN_CONFIDENCE     = 5.0
EFFICIENCY_BLOCK   = 0.01   # |edge| < 1% = efficiently priced


def check_edge(edge, market):
    """Returns (passes: bool, no_bet_reason: str|None)."""
    if abs(edge) < EFFICIENCY_BLOCK:
        return False, 'efficiently_priced'
    min_edge = MIN_EDGE_MONEYLINE if market == 'moneyline' else MIN_EDGE_RUN_LINE
    if edge < min_edge:
        return False, 'below_edge_threshold'
    return True, None


def size_bet(edge, confidence):
    """
    Bet sizing based on edge * confidence composite.
    Capped at 'medium' for v1 per planning doc.
    """
    composite = edge * confidence
    if composite >= 0.06:
        return 'medium'
    return 'small'


def apply_rules(edges_df, confidence_fn, injuries_df):
    """
    Apply all pick qualification rules from planning doc Section 1.4.
    Returns list of result dicts — one per (game, market, side) evaluated.
    """
    if edges_df is None or edges_df.empty:
        return []

    results = []

    for _, row in edges_df.iterrows():
        game_id = row['game_id']
        market = row['market']
        side = row['side']
        edge = row['edge']
        model_prob = row['model_prob']

        base = row.to_dict()

        # Only consider positive edge — we need to be on the right side
        if edge <= 0:
            results.append({**base, 'decision': 'no_bet', 'confidence': 0.0, 'no_bet_reason': 'negative_edge'})
            continue

        # Edge threshold + efficiency block
        passes_edge, no_bet_reason = check_edge(edge, market)
        if not passes_edge:
            results.append({**base, 'decision': 'no_bet', 'confidence': 0.0, 'no_bet_reason': no_bet_reason})
            continue

        # Injury context — check the team we're betting on
        has_injury = False
        injury_severity = None
        if injuries_df is not None and not injuries_df.empty:
            game_injuries = injuries_df[injuries_df['game_id'] == game_id]
            if not game_injuries.empty:
                has_injury = True
                if (game_injuries['status'] == 'out').any():
                    injury_severity = 'out'
                else:
                    injury_severity = 'questionable'

        games_played = int(row.get('min_gp', 0))
        confidence = confidence_fn(edge, model_prob, 0, has_injury, injury_severity, games_played)

        # Injury block: injury present AND confidence < 6
        if has_injury and confidence < 6.0:
            results.append({**base, 'decision': 'no_bet', 'confidence': confidence, 'no_bet_reason': 'injury_uncertainty'})
            continue

        # Minimum confidence
        if confidence < MIN_CONFIDENCE:
            results.append({**base, 'decision': 'no_bet', 'confidence': confidence, 'no_bet_reason': 'low_confidence'})
            continue

        decision = size_bet(edge, confidence)
        results.append({**base, 'decision': decision, 'confidence': confidence, 'no_bet_reason': None})

    return results


def select_top_picks(results, n=5):
    """
    Select the top-N highest-edge qualifying picks, sorted by edge descending.
    Picks beyond rank n are downgraded to no_bet with reason 'not_top_pick'.
    Returns (top_picks: list, all_results: list).
    """
    qualifying = sorted(
        [r for r in results if r['decision'] != 'no_bet'],
        key=lambda r: r['confidence'],
        reverse=True,
    )

    # One pick per game — keep highest-confidence pick per game_id
    seen_games = set()
    deduped = []
    for r in qualifying:
        if r['game_id'] not in seen_games:
            seen_games.add(r['game_id'])
            deduped.append(r)

    top_picks = deduped[:n]
    top_game_ids = {r['game_id'] for r in top_picks}

    for r in results:
        if r['decision'] != 'no_bet' and r['game_id'] not in top_game_ids:
            r['decision'] = 'no_bet'
            r['no_bet_reason'] = 'not_top_pick'

    return top_picks, results
