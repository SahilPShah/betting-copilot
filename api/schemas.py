from __future__ import annotations
from pydantic import BaseModel, Field
from typing import Optional


# ---------- Shared ----------

class StarterSnapshot(BaseModel):
    name: str
    era: Optional[float] = None
    whip: Optional[float] = None
    l3_era: Optional[float] = None


# ---------- /health ----------

class HealthResponse(BaseModel):
    status: str
    database: str
    today: str
    latest_game_date: Optional[str] = None
    latest_slate_date: Optional[str] = None
    latest_model_version: Optional[str] = None


# ---------- /slate ----------

class SlatePick(BaseModel):
    game_id: str
    game_date: str
    home_team: str
    away_team: str
    market: str
    side: str
    decision: str
    confidence: float
    edge: float
    model_prob: float
    implied_prob: float
    american_odds: Optional[int] = None
    bookmaker: Optional[str] = None
    predicted_margin: Optional[float] = None
    elo_diff: Optional[float] = None
    home_score: Optional[int] = None
    away_score: Optional[int] = None
    status: str
    starters: dict[str, Optional[StarterSnapshot]] = {}
    llm_explanation: Optional[str] = None


class SlateResponse(BaseModel):
    run_date: str
    model_version: str
    games_count: Optional[int] = None
    picks_count: Optional[int] = None
    ran_at: str
    picks: list[SlatePick]
    no_bets: Optional[list[SlatePick]] = None


# ---------- /game/{game_id} ----------

class GamePrediction(BaseModel):
    model_version: str
    home_win_prob: float
    away_win_prob: float
    predicted_margin: Optional[float] = None
    home_cover_prob: Optional[float] = None
    away_cover_prob: Optional[float] = None
    elo_diff: Optional[float] = None


class GameOddsRow(BaseModel):
    market: str
    side: str
    american_odds: int
    implied_prob: float
    bookmaker: str
    captured_at: str
    run_line_point: Optional[float] = None


class GameInjuryRow(BaseModel):
    team_id: str
    player_name: str
    status: str


class GameRecommendation(BaseModel):
    run_date: str
    market: str
    side: str
    decision: str
    confidence: float
    edge: float
    llm_explanation: Optional[str] = None


class GameDetailResponse(BaseModel):
    game_id: str
    game_date: str
    status: str
    home_team: str
    away_team: str
    scores: dict[str, Optional[int]]
    prediction: Optional[GamePrediction] = None
    odds: list[GameOddsRow] = []
    starters: dict[str, Optional[StarterSnapshot]] = {}
    injuries: list[GameInjuryRow] = []
    recommendation: Optional[GameRecommendation] = None


# ---------- /history ----------

class HistoryItem(BaseModel):
    run_date: str
    game_id: str
    market: str
    side: str
    decision: str
    edge: float
    confidence: float
    american_odds: Optional[int] = None
    run_line_point: Optional[float] = None
    home_score: Optional[int] = None
    away_score: Optional[int] = None
    result: Optional[str] = None  # win | loss | push | pending


class HistorySummary(BaseModel):
    wins: int
    losses: int
    pushes: int
    pending: int
    win_rate: Optional[float] = None


class HistoryResponse(BaseModel):
    start_date: str
    end_date: str
    page: int
    per_page: int
    total: int
    summary: HistorySummary
    items: list[HistoryItem]


# ---------- /run-pipeline ----------

class PipelineTriggerResponse(BaseModel):
    status: str
    date: str
