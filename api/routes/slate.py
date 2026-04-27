from datetime import date
from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy.engine import Connection

from api.deps import get_conn
from api.schemas import SlateResponse, SlatePick
from api.services.slate import fetch_slate_run, fetch_slate_picks, shape_pick

router = APIRouter()


def _build_slate(conn: Connection, run_date: str, include_no_bets: bool):
    slate = fetch_slate_run(conn, run_date)
    if not slate:
        return JSONResponse(status_code=404,
                            content={"detail": f"Slate not found for {run_date}"})

    rows = fetch_slate_picks(conn, slate["slate_run_id"])
    shaped = [shape_pick(r) for r in rows]

    picks = [SlatePick(**p) for p in shaped if p["decision"] != "no_bet"]
    no_bets = [SlatePick(**p) for p in shaped if p["decision"] == "no_bet"] if include_no_bets else None

    return SlateResponse(
        run_date=str(slate["run_date"]),
        model_version=slate["model_version"],
        games_count=slate["games_count"],
        picks_count=slate["picks_count"],
        ran_at=slate["ran_at"].isoformat() if slate["ran_at"] else "",
        picks=picks,
        no_bets=no_bets,
    )


@router.get("/slate", response_model=SlateResponse)
def get_slate(
    date_param: str | None = Query(None, alias="date"),
    include_no_bets: bool = Query(False),
    conn: Connection = Depends(get_conn),
):
    run_date = date_param or str(date.today())
    return _build_slate(conn, run_date, include_no_bets)


@router.get("/slate/{slate_date}", response_model=SlateResponse)
def get_slate_by_date(
    slate_date: str,
    include_no_bets: bool = Query(False),
    conn: Connection = Depends(get_conn),
):
    return _build_slate(conn, slate_date, include_no_bets)
