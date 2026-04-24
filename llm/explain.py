"""
LLM-powered pick explanation generator.

Calls the active LLM provider (see llm/client.py) to produce a 2–3 sentence
analyst-quality explanation for a pick. The result is stored in
recommendations.llm_explanation.
"""

from llm.client import get_client

_SYSTEM = (
    "You are a sharp sports betting analyst. Write a 2–3 sentence explanation "
    "for a specific MLB pick. Use the exact numbers provided. Sound like an analyst, "
    "not a template. No hedging phrases like 'it\\'s worth noting' or 'keep in mind'. "
    "Do not restate the market or odds at the start — lead with the analytical reason."
)


def _fmt(val, fmt=".3f", fallback="N/A"):
    if val is None:
        return fallback
    try:
        return format(float(val), fmt)
    except (TypeError, ValueError):
        return fallback


def _build_prompt(ctx: dict) -> str:
    game_id = ctx.get("game_id", "")
    date = ctx.get("date", "")
    side = ctx.get("side", "")
    market = ctx.get("market", "")

    home = ctx.get("home_team", "")
    away = ctx.get("away_team", "")
    pick_team = home if side == "home" else away
    opp_team = away if side == "home" else home

    model_prob = ctx.get("model_prob")
    implied_prob = ctx.get("implied_prob")
    edge = ctx.get("edge")
    odds = ctx.get("american_odds")
    margin = ctx.get("predicted_margin")

    hs = ctx.get("home_starter") or {}
    as_ = ctx.get("away_starter") or {}
    pick_starter = hs if side == "home" else as_
    opp_starter = as_ if side == "home" else hs

    def era_str(s):
        l3 = s.get("l3_era")
        era = s.get("era")
        if l3 is not None:
            return f"L3 ERA {_fmt(l3, '.2f')}"
        elif era is not None:
            return f"ERA {_fmt(era, '.2f')}"
        return "ERA N/A"

    ps = ctx.get("pick_team_stats") or {}
    os_ = ctx.get("opp_team_stats") or {}

    def l7(stats):
        games = stats.get("l7_games")
        win_pct = stats.get("l7_win_pct")
        net = stats.get("l7_run_diff_avg")
        if games is None or win_pct is None:
            return "N/A"
        wins = round(float(win_pct) * int(games))
        losses = int(games) - wins
        net_str = f", {float(net):+.1f} R/G net" if net is not None else ""
        return f"{wins}-{losses} L{int(games)}{net_str}"

    elo_diff = ctx.get("elo_diff")

    lines = [
        f"Pick: {pick_team} ({side}, {market})",
        f"Date: {date} | Game: {game_id}",
        f"Model: {_fmt(model_prob, '.1%')} win prob | Market implied: {_fmt(implied_prob, '.1%')} | Edge: {_fmt(edge, '.1%')} | Odds: {odds:+d}" if odds is not None else f"Model: {_fmt(model_prob, '.1%')} | Market: {_fmt(implied_prob, '.1%')} | Edge: {_fmt(edge, '.1%')}",
    ]

    if elo_diff is not None:
        lines.append(
            f"ELO diff (home minus away): {float(elo_diff):+.0f} points. "
            f"ELO is a running skill rating updated after every game — teams start at 1500, "
            f"ratings shift based on result vs expectation, and reset 75% toward 1500 each new season. "
            f"Home field is worth +35 ELO points. "
            f"A diff of ±50 is a modest edge; ±150 is substantial; ±300+ is a heavy favorite."
        )

    if margin is not None:
        lines.append(f"Projected margin: {float(margin):+.1f} runs (home perspective)")

    lines.append(
        f"Starting pitchers (ERA/WHIP are as-a-starter only, not overall pitcher stats — "
        f"computed from last 5 starts with Bayesian shrinkage toward 4.50 at low sample sizes): "
        f"{pick_team} — {pick_starter.get('name', 'TBD')} ({era_str(pick_starter)}, WHIP {_fmt(pick_starter.get('whip'), '.2f')}) | "
        f"{opp_team} — {opp_starter.get('name', 'TBD')} ({era_str(opp_starter)}, WHIP {_fmt(opp_starter.get('whip'), '.2f')})"
    )

    games_played = ctx.get("games_played", 0)

    lines.append(
        f"{pick_team} season ({games_played} G): {_fmt(ps.get('win_pct'), '.3f')} W% / {_fmt(ps.get('ops'), '.3f')} OPS / {_fmt(ps.get('runs_scored_avg'), '.1f')} R/G / {_fmt(ps.get('era'), '.2f')} ERA | Recent L7: {l7(ps)}"
    )
    lines.append(
        f"{opp_team} season ({games_played} G): {_fmt(os_.get('win_pct'), '.3f')} W% / {_fmt(os_.get('ops'), '.3f')} OPS / {_fmt(os_.get('runs_scored_avg'), '.1f')} R/G / {_fmt(os_.get('era'), '.2f')} ERA | Recent L7: {l7(os_)}"
    )
    lines.append(f"Starter stats (L3/L5 starts) and L7 team form are not affected by games played.")

    lines.append("\nWrite a 2–3 sentence explanation for why this is a good bet.")
    return "\n".join(lines)


def generate_explanation(pick_context: dict) -> str:
    """
    Generate a natural-language pick explanation using the active LLM provider.

    Args:
        pick_context: dict assembled by build_pick_context() in run_recs.py

    Returns:
        2–3 sentence explanation string, or empty string if the call fails.
    """
    client = get_client()
    prompt = _build_prompt(pick_context)
    return client.call(prompt, _SYSTEM, temperature=0.3)
