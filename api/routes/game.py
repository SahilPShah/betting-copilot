from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.engine import Connection

from api.deps import get_conn
from api.schemas import (
    GameDetailResponse, GamePrediction, GameOddsRow,
    GameInjuryRow, GameRecommendation, StarterSnapshot,
)
from api.services.game import (
    fetch_game, fetch_prediction, fetch_latest_odds,
    fetch_starters, fetch_injuries, fetch_latest_recommendation,
    build_starters,
)

router = APIRouter()


@router.get("/game/{game_id}", response_model=GameDetailResponse)
def get_game(game_id: str, conn: Connection = Depends(get_conn)):
    game = fetch_game(conn, game_id)
    if not game:
        return JSONResponse(status_code=404,
                            content={"detail": f"Game not found: {game_id}"})

    pred_row = fetch_prediction(conn, game_id)
    prediction = None
    if pred_row:
        prediction = GamePrediction(
            model_version=pred_row["model_version"],
            home_win_prob=float(pred_row["home_win_prob"]),
            away_win_prob=float(pred_row["away_win_prob"]),
            predicted_margin=float(pred_row["predicted_margin"]) if pred_row.get("predicted_margin") is not None else None,
            home_cover_prob=float(pred_row["home_cover_prob"]) if pred_row.get("home_cover_prob") is not None else None,
            away_cover_prob=float(pred_row["away_cover_prob"]) if pred_row.get("away_cover_prob") is not None else None,
            elo_diff=float(pred_row["elo_diff"]) if pred_row.get("elo_diff") is not None else None,
        )

    odds_rows = fetch_latest_odds(conn, game_id)
    odds = [
        GameOddsRow(
            market=o["market"],
            side=o["side"],
            american_odds=o["american_odds"],
            implied_prob=float(o["implied_prob"]),
            bookmaker=o["bookmaker"],
            captured_at=o["captured_at"].isoformat() if o["captured_at"] else "",
            run_line_point=float(o["run_line_point"]) if o.get("run_line_point") is not None else None,
        )
        for o in odds_rows
    ]

    starter_names = fetch_starters(conn, game_id)
    injury_rows = fetch_injuries(conn, game_id)
    injuries = [GameInjuryRow(**i) for i in injury_rows]

    rec = fetch_latest_recommendation(conn, game_id)
    recommendation = None
    if rec and rec.get("decision") != "no_bet":
        # Get run_date from the slate_run
        from sqlalchemy import text
        sr = conn.execute(
            text("SELECT run_date FROM slate_runs WHERE slate_run_id = :sid"),
            {"sid": str(rec["slate_run_id"])},
        ).mappings().fetchone()
        recommendation = GameRecommendation(
            run_date=str(sr["run_date"]) if sr else "",
            market=rec["market"],
            side=rec["side"],
            decision=rec["decision"],
            confidence=float(rec["confidence"]),
            edge=float(rec["edge"]),
            llm_explanation=rec.get("llm_explanation"),
        )

    starters_dict = build_starters(rec, starter_names)
    starters = {
        side: StarterSnapshot(**data) if data else None
        for side, data in starters_dict.items()
    }

    return GameDetailResponse(
        game_id=game["game_id"],
        game_date=str(game["game_date"]),
        status=game["status"],
        home_team=game["home_team_id"],
        away_team=game["away_team_id"],
        scores={"home": game["home_score"], "away": game["away_score"]},
        prediction=prediction,
        odds=odds,
        starters=starters,
        injuries=injuries,
        recommendation=recommendation,
    )
