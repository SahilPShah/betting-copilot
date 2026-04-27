from datetime import datetime
from fastapi import APIRouter, BackgroundTasks
from fastapi.responses import JSONResponse

from api.schemas import PipelineTriggerResponse
from api.services.pipeline import is_running, run_pipeline

router = APIRouter()


@router.post("/run-pipeline/{date_str}", response_model=PipelineTriggerResponse,
             status_code=202)
def trigger_pipeline(date_str: str, background_tasks: BackgroundTasks):
    # Validate date format
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return JSONResponse(status_code=422,
                            content={"detail": f"Invalid date format: {date_str}. Use YYYY-MM-DD."})

    if is_running(date_str):
        return JSONResponse(status_code=409,
                            content={"detail": f"Pipeline already running for {date_str}"})

    background_tasks.add_task(run_pipeline, date_str)

    return PipelineTriggerResponse(status="started", date=date_str)
