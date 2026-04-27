from sqlalchemy import text


def settle_row(row: dict) -> str:
    """Determine win/loss/push/pending for a recommendation row."""
    if row["status"] != "final" or row["home_score"] is None or row["away_score"] is None:
        return "pending"

    home_score = row["home_score"]
    away_score = row["away_score"]
    market = row["market"]
    side = row["side"]

    if market == "moneyline":
        if side == "home":
            return "win" if home_score > away_score else "loss"
        else:
            return "win" if away_score > home_score else "loss"

    # run_line
    run_line_point = float(row["run_line_point"]) if row.get("run_line_point") is not None else -1.5
    if side == "home":
        adjusted = home_score + run_line_point
        if adjusted > away_score:
            return "win"
        elif adjusted == away_score:
            return "push"
        else:
            return "loss"
    else:
        adjusted = away_score + run_line_point
        if adjusted > home_score:
            return "win"
        elif adjusted == home_score:
            return "push"
        else:
            return "loss"


def fetch_history(conn, *, start_date: str, end_date: str,
                  decision: str | None, market: str | None,
                  include_no_bets: bool) -> list[dict]:
    """Fetch all deduped recommendations in range with scores and odds."""
    filters = ["s.run_date BETWEEN :start_date AND :end_date"]
    params: dict = {"start_date": start_date, "end_date": end_date}

    if not include_no_bets:
        filters.append("r.decision != 'no_bet'")
    if decision:
        filters.append("r.decision = :decision")
        params["decision"] = decision
    if market:
        filters.append("r.market = :market")
        params["market"] = market

    where = " AND ".join(filters)

    rows = conn.execute(
        text(f"""
            WITH deduped AS (
              SELECT *
              FROM (
                SELECT
                  r.*,
                  s.run_date,
                  ROW_NUMBER() OVER (
                    PARTITION BY r.slate_run_id, r.game_id, r.market, r.side
                    ORDER BY r.created_at DESC, r.rec_id DESC
                  ) AS rn
                FROM recommendations r
                JOIN slate_runs s ON s.slate_run_id = r.slate_run_id
                WHERE {where}
              ) x
              WHERE x.rn = 1
            )
            SELECT
              d.run_date, d.game_id, d.market, d.side,
              d.decision, d.edge, d.confidence,
              g.home_score, g.away_score, g.status,
              o.american_odds, o.run_line_point
            FROM deduped d
            JOIN games g ON g.game_id = d.game_id
            LEFT JOIN odds_snapshots o ON o.snapshot_id = d.odds_snapshot_id
            ORDER BY d.run_date DESC, d.confidence DESC
        """),
        params,
    ).mappings().all()
    return [dict(r) for r in rows]


def build_summary(items: list[dict]) -> dict:
    wins = sum(1 for i in items if i["result"] == "win")
    losses = sum(1 for i in items if i["result"] == "loss")
    pushes = sum(1 for i in items if i["result"] == "push")
    pending = sum(1 for i in items if i["result"] == "pending")
    decided = wins + losses
    return {
        "wins": wins,
        "losses": losses,
        "pushes": pushes,
        "pending": pending,
        "win_rate": round(wins / decided, 3) if decided > 0 else None,
    }
