import json
from sqlalchemy import text


def fetch_slate_run(conn, run_date: str) -> dict | None:
    row = conn.execute(
        text("""
            SELECT slate_run_id, run_date, model_version,
                   games_count, picks_count, ran_at
            FROM slate_runs
            WHERE run_date = :run_date
        """),
        {"run_date": run_date},
    ).mappings().fetchone()
    return dict(row) if row else None


def fetch_slate_picks(conn, slate_run_id: str) -> list[dict]:
    rows = conn.execute(
        text("""
            WITH deduped AS (
              SELECT *
              FROM (
                SELECT
                  r.*,
                  ROW_NUMBER() OVER (
                    PARTITION BY r.slate_run_id, r.game_id, r.market, r.side
                    ORDER BY r.created_at DESC, r.rec_id DESC
                  ) AS rn
                FROM recommendations r
                WHERE r.slate_run_id = :slate_run_id
              ) x
              WHERE x.rn = 1
            )
            SELECT
              d.game_id, d.market, d.side, d.decision, d.confidence, d.edge,
              d.context_snapshot, d.llm_explanation,
              g.game_date, g.home_team_id, g.away_team_id,
              g.status, g.home_score, g.away_score,
              p.predicted_margin, p.elo_diff
            FROM deduped d
            JOIN games g ON g.game_id = d.game_id
            LEFT JOIN predictions p ON p.game_id = d.game_id
            ORDER BY
              CASE d.decision WHEN 'no_bet' THEN 0 ELSE 1 END DESC,
              d.confidence DESC, d.edge DESC
        """),
        {"slate_run_id": str(slate_run_id)},
    ).mappings().all()
    return [dict(r) for r in rows]


def shape_pick(row: dict) -> dict:
    ctx = row.get("context_snapshot") or {}
    if isinstance(ctx, str):
        ctx = json.loads(ctx)

    home_starter = ctx.get("home_starter")
    away_starter = ctx.get("away_starter")
    starters = {}
    if home_starter:
        starters["home"] = {
            "name": home_starter.get("name"),
            "era": home_starter.get("era"),
            "whip": home_starter.get("whip"),
            "l3_era": home_starter.get("l3_era"),
        }
    if away_starter:
        starters["away"] = {
            "name": away_starter.get("name"),
            "era": away_starter.get("era"),
            "whip": away_starter.get("whip"),
            "l3_era": away_starter.get("l3_era"),
        }

    return {
        "game_id": row["game_id"],
        "game_date": str(row["game_date"]),
        "home_team": row["home_team_id"],
        "away_team": row["away_team_id"],
        "market": row["market"],
        "side": row["side"],
        "decision": row["decision"],
        "confidence": float(row["confidence"]),
        "edge": float(row["edge"]),
        "model_prob": float(ctx.get("model_prob", 0)),
        "implied_prob": float(ctx.get("implied_prob", 0)),
        "american_odds": ctx.get("american_odds"),
        "bookmaker": ctx.get("bookmaker"),
        "predicted_margin": float(row["predicted_margin"]) if row.get("predicted_margin") is not None else None,
        "elo_diff": float(row["elo_diff"]) if row.get("elo_diff") is not None else None,
        "home_score": row["home_score"],
        "away_score": row["away_score"],
        "status": row["status"],
        "starters": starters,
        "llm_explanation": row.get("llm_explanation"),
    }
