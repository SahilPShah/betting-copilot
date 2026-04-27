import json
from sqlalchemy import text


def fetch_game(conn, game_id: str) -> dict | None:
    row = conn.execute(
        text("""
            SELECT game_id, game_date, status,
                   home_team_id, away_team_id,
                   home_score, away_score
            FROM games WHERE game_id = :game_id
        """),
        {"game_id": game_id},
    ).mappings().fetchone()
    return dict(row) if row else None


def fetch_prediction(conn, game_id: str) -> dict | None:
    row = conn.execute(
        text("""
            SELECT model_version, home_win_prob, away_win_prob,
                   predicted_margin, home_cover_prob, away_cover_prob, elo_diff
            FROM predictions WHERE game_id = :game_id
        """),
        {"game_id": game_id},
    ).mappings().fetchone()
    return dict(row) if row else None


def fetch_latest_odds(conn, game_id: str) -> list[dict]:
    rows = conn.execute(
        text("""
            SELECT DISTINCT ON (market, side)
                   market, side, american_odds, implied_prob,
                   bookmaker, captured_at, run_line_point
            FROM odds_snapshots
            WHERE game_id = :game_id
            ORDER BY market, side, captured_at DESC
        """),
        {"game_id": game_id},
    ).mappings().all()
    return [dict(r) for r in rows]


def fetch_starters(conn, game_id: str) -> dict:
    rows = conn.execute(
        text("""
            SELECT side, starter_name
            FROM game_starters
            WHERE game_id = :game_id
        """),
        {"game_id": game_id},
    ).mappings().all()
    return {r["side"]: r["starter_name"] for r in rows}


def fetch_injuries(conn, game_id: str) -> list[dict]:
    rows = conn.execute(
        text("""
            SELECT team_id, player_name, status
            FROM injury_statuses
            WHERE game_id = :game_id
        """),
        {"game_id": game_id},
    ).mappings().all()
    return [dict(r) for r in rows]


def fetch_latest_recommendation(conn, game_id: str) -> dict | None:
    row = conn.execute(
        text("""
            WITH latest_run AS (
              SELECT s.slate_run_id, s.run_date
              FROM slate_runs s
              JOIN recommendations r ON r.slate_run_id = s.slate_run_id
              WHERE r.game_id = :game_id
              ORDER BY s.run_date DESC, s.ran_at DESC
              LIMIT 1
            ),
            deduped AS (
              SELECT *
              FROM (
                SELECT
                  r.*,
                  ROW_NUMBER() OVER (
                    PARTITION BY r.slate_run_id, r.game_id, r.market, r.side
                    ORDER BY r.created_at DESC, r.rec_id DESC
                  ) AS rn
                FROM recommendations r
                JOIN latest_run lr ON lr.slate_run_id = r.slate_run_id
                WHERE r.game_id = :game_id
              ) x
              WHERE x.rn = 1
            )
            SELECT * FROM deduped
            ORDER BY confidence DESC, edge DESC
            LIMIT 1
        """),
        {"game_id": game_id},
    ).mappings().fetchone()
    return dict(row) if row else None


def build_starters(rec: dict | None, starter_names: dict) -> dict:
    """Build starters dict: prefer context_snapshot blobs, fall back to names only."""
    starters: dict = {}

    if rec:
        ctx = rec.get("context_snapshot") or {}
        if isinstance(ctx, str):
            ctx = json.loads(ctx)
        for side in ("home", "away"):
            blob = ctx.get(f"{side}_starter")
            if blob and isinstance(blob, dict):
                starters[side] = {
                    "name": blob.get("name"),
                    "era": blob.get("era"),
                    "whip": blob.get("whip"),
                    "l3_era": blob.get("l3_era"),
                }

    # Fill in any sides not covered by context_snapshot
    for side in ("home", "away"):
        if side not in starters and side in starter_names:
            starters[side] = {"name": starter_names[side], "era": None, "whip": None, "l3_era": None}

    return starters
