from datetime import date
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.engine import Connection

from api.deps import get_conn
from api.schemas import HealthResponse

router = APIRouter()


@router.get("/health", response_model=HealthResponse)
def health(conn: Connection = Depends(get_conn)):
    try:
        conn.execute(text("SELECT 1"))
    except Exception:
        return JSONResponse(
            status_code=503,
            content={"status": "error", "database": "unavailable",
                     "today": str(date.today())},
        )

    latest_game = conn.execute(
        text("SELECT MAX(game_date) FROM games")
    ).scalar()

    slate_row = conn.execute(
        text("SELECT run_date, model_version FROM slate_runs ORDER BY run_date DESC LIMIT 1")
    ).mappings().fetchone()

    return HealthResponse(
        status="ok",
        database="ok",
        today=str(date.today()),
        latest_game_date=str(latest_game) if latest_game else None,
        latest_slate_date=str(slate_row["run_date"]) if slate_row else None,
        latest_model_version=slate_row["model_version"] if slate_row else None,
    )
