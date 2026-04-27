from datetime import date, timedelta
from fastapi import APIRouter, Depends, Query
from sqlalchemy.engine import Connection

from api.deps import get_conn
from api.schemas import HistoryResponse, HistoryItem, HistorySummary
from api.services.history import fetch_history, settle_row, build_summary

router = APIRouter()


@router.get("/history", response_model=HistoryResponse)
def get_history(
    start_date: str | None = Query(None),
    end_date: str | None = Query(None),
    decision: str | None = Query(None),
    market: str | None = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    include_no_bets: bool = Query(False),
    conn: Connection = Depends(get_conn),
):
    today = date.today()
    start = start_date or str(today - timedelta(days=30))
    end = end_date or str(today)

    all_rows = fetch_history(
        conn,
        start_date=start,
        end_date=end,
        decision=decision,
        market=market,
        include_no_bets=include_no_bets,
    )

    # Settle all rows
    for row in all_rows:
        row["result"] = settle_row(row)

    # Summary computed from the full set, not just the page
    summary_data = build_summary(all_rows)

    # Paginate
    total = len(all_rows)
    offset = (page - 1) * per_page
    page_rows = all_rows[offset : offset + per_page]

    items = [
        HistoryItem(
            run_date=str(r["run_date"]),
            game_id=r["game_id"],
            market=r["market"],
            side=r["side"],
            decision=r["decision"],
            edge=float(r["edge"]),
            confidence=float(r["confidence"]),
            american_odds=r["american_odds"],
            run_line_point=float(r["run_line_point"]) if r.get("run_line_point") is not None else None,
            home_score=r["home_score"],
            away_score=r["away_score"],
            result=r["result"],
        )
        for r in page_rows
    ]

    return HistoryResponse(
        start_date=start,
        end_date=end,
        page=page,
        per_page=per_page,
        total=total,
        summary=HistorySummary(**summary_data),
        items=items,
    )
