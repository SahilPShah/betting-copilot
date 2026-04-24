# Betting Copilot — Progress Report (March 30, 2026)

## What Was Changed

### Pitcher ERA Architecture Overhaul

The most significant change this session was replacing the pybaseball-based pitcher ERA lookup with a fully dynamic, database-driven approach.

**Before:**
- `mlb_games.py` imported pybaseball at every ingest run and scraped FanGraphs for full-season pitcher stats
- ERA was resolved at ingest time and stored in `game_starters.starter_era`
- Two pybaseball calls per run (current year + prior year) — full pitcher roster each time
- Past game rows in `game_starters` were retroactively overwritten whenever ERA changed
- 2025 (prior year) ERA was re-fetched daily despite never changing

**After:**
- `mlb_games.py` stores only the pitcher's **name** and the game's external integer `game_pk` — no pybaseball dependency at all
- New `ingest/mlb_boxscores.py` fetches box scores for final games and stores **starter-only** lines (IP, ER, H, BB, K) in `pitcher_game_logs`
- ERA is computed at **prediction time** from the last 5 starts, regardless of season boundary
- Relief pitcher data is explicitly excluded — only the starting pitcher per side is persisted
- Historical rows in `game_starters` are no longer retroactively overwritten

### New Table: `pitcher_game_logs`
- One row per starter per game
- Stores: `innings_pitched`, `earned_runs`, `hits`, `walks`, `strikeouts`, `is_starter` (always TRUE)
- Indexed on `(pitcher_name, season)` and `game_date`
- `games` table gains a `game_pk` integer column used to look up box scores

### ERA Computation: Last 5 Starts
`compute_starter_eras()` in `mlb_boxscores.py`:
- Fetches each pitcher's last 5 starts from `pitcher_game_logs` ordered by `game_date DESC`
- Sums IP, ER, H, BB across those 5 games and computes ERA and WHIP
- Requires minimum 2 starts — returns None below that threshold
- Crosses the season boundary naturally: if a pitcher has 1 start in 2026, their last 4 from 2025 fill the window

### Pipeline Updates
- `ingest/capture_odds.py` (cron): now fetches box scores for last 5 days of final games daily
- `ingest/run_ingest.py` (on-demand): now includes box score step for the full date range
- `models/predict.py`: imports and calls `compute_starter_eras()` instead of reading from `game_starters`
- `recs/run_recs.py`: same — ERA in reasoning output now comes from `pitcher_game_logs`

### Backfill Completed
- 2025 season (July 1 – September 28): 1,164 games, 10,055 pitcher log rows ingested, then 7,990 relief rows deleted
- 2026 season (March 27–30): 35 games, 333 starter rows
- Final state: **2,398 starter rows** across both seasons

### Naming Convention Applied
All column and index names describe the data they store, not the source API:
- `statsapi_game_pk` → `game_pk`
- `statsapi_player_id` → `player_id`
- `idx_games_statsapi_pk` → `idx_games_game_pk`

---

## Current Architecture

```
Daily cron (9am)                    On-demand (run_daily.sh)
─────────────────                   ────────────────────────
mlb_games.py        ──→ games, game_starters (name only)
mlb_boxscores.py    ──→ pitcher_game_logs (last 5 starts used at predict time)
odds_api.py         ──→ odds_snapshots
mlb_stats.py        ──→ team_stats_mlb
                        mlb_injuries.py ──→ injury_statuses
                        predict.py ──→ predictions
                        run_recs.py ──→ recommendations, slate_runs
```

ERA flows: `box score API → pitcher_game_logs → compute_starter_eras() → predict.py / run_recs.py`

---

## Architectural Limitations

### 1. Team Stats Are Season-Level, Not Rolling
`team_stats_mlb` stores cumulative season ERA, OPS, win%, and runs/game from pybaseball. Hot and cold streaks are invisible to the model. A team that's 8-2 in their last 10 games looks the same as a team that's 2-8 over that same stretch, as long as their overall records are similar.

**Impact:** Moderate. Early season this is most pronounced — a 3-game hot start dominates the season average. By June the signal stabilizes.

### 2. ERA Window Is Time-Blind
Last 5 starts works well but treats a start from 3 weeks ago the same as one from yesterday. A pitcher who has been dominant recently but had a rough outing 5 starts ago carries that outing at full weight.

**Impact:** Low-moderate. With only 5 starts in the window this is unlikely to be significant, but recency weighting would be more theoretically correct.

### 3. No Bullpen Signal
The model has no visibility into bullpen quality. For run line bets especially, the quality of the bullpen in the 6th–9th innings matters significantly. A team with an elite starter but a poor bullpen looks the same as a team with both.

**Impact:** Moderate for run line markets. Less so for moneyline.

### 4. Only Back Half of 2025 Backfilled
`pitcher_game_logs` contains starters from July 1 – September 28, 2025. Pitchers who only appeared in the first half of 2025 (injured in August, called up in September, etc.) will have fewer than 5 starts in the window and may return None for ERA.

**Impact:** Low in practice. Most rotation starters pitched in the second half. Truly missing pitchers fall back to `has_starter_data = 0` in the model gracefully.

### 5. No Point-in-Time Integrity for `game_starters`
`game_starters` is re-upserted on every cron run for the last 5 days. If a probable pitcher changes between ingest runs, the old name is overwritten. This is fine for predictions (you want the current probable), but means historical `game_starters` rows don't necessarily reflect who was listed as the probable at the time the prediction was made.

**Impact:** Low currently. Would matter for backtesting prediction accuracy against pre-game probables.

---

## Possible Improvements

| Priority | Improvement | What It Needs |
|----------|------------|---------------|
| High | **Rolling team stats (L7/L14)** | Compute wins, runs scored/allowed from `games` table — already have the data. OPS/ERA rolling would require box score batting data (not stored). |
| High | **One pick per game rule** | Filter top 5 to prevent same game appearing in both moneyline and run line slots |
| Medium | **Recency weighting for ERA** | Apply exponential decay to starter ERA computation (more recent starts weighted higher) |
| Medium | **Bullpen ERA signal** | Store relief pitcher appearances in `pitcher_game_logs` (currently excluded). Compute team bullpen ERA as a feature. |
| Medium | **ROI backtest (`eval/roi.py`)** | Validate whether historical picks at locked thresholds would have been profitable |
| Medium | **CLV analysis (`eval/clv.py`)** | Requires closing odds — currently only morning odds are captured |
| Low | **Full 2025 backfill** | Run `pull_boxscores.py` from 2025-03-27 to give all starters a complete season window |
| Low | **Pitcher injury awareness** | If a starter is on the IL, ERA lookup returns a stale result from their last healthy start |
| Low | **LLM explanations (`llm/`)** | Replace template reasoning with Claude API for richer natural-language output |
| Low | **API layer (`api/`)** | FastAPI endpoints to expose picks to a frontend or mobile app |

---

## Next Steps

### Immediate
1. **Run `./run_daily.sh`** on the first game day after this session to verify the new ERA pipeline works end-to-end with live data
2. **Monitor starter ERA values** in reasoning output — confirm they look realistic (3.00–5.00 range) rather than inflated opening-week numbers

### Short Term
3. **One pick per game deduplication** — add a rule in `recs/rules.py` to prevent both moneyline and run line for the same game appearing in the top 5
4. **Rolling L7 team stats** — computable from the existing `games` table; would replace the most noise-prone signal in early season
5. **Edge normalization check** — by mid-April, edges should compress to the 3–10% range; if still seeing 20%+ edges, the model needs recalibration

### Medium Term
6. **Retrain model (v4)** — once ~3–4 weeks of 2026 data is in `pitcher_game_logs`, retrain with ERA computed from the new pipeline rather than pybaseball season totals
7. **ROI backtest** — validate historical edge threshold against actual bet outcomes
8. **Bullpen ERA feature** — store relief appearances, compute team bullpen ERA as an additional model signal

---

## Frontend & LLM Roadmap

This section covers what's needed to turn the current CLI tool into a fully usable product.

### LLM Pick Explanations (`llm/`)

The current reasoning output is template-driven — it fills in stats and numbers but reads mechanically. Replacing this with a Claude API call would produce richer, context-aware explanations.

**What to build:**
- `llm/explain.py` — takes a pick dict (edge, confidence, starters, team stats, model prob) and calls the Claude API to generate a 2–3 sentence natural-language explanation
- Store the result in `recommendations.llm_explanation` (column already exists in the schema)
- Call at the end of `recs/run_recs.py` after picks are written to DB

**What it enables:**
- Explanations that sound like an analyst wrote them, not a template
- Can incorporate context the model doesn't see (e.g. "this is a revenge game", "pitcher is coming off an IL stint") if manually surfaced
- Foundation for a conversational `/chat` endpoint later

**API:** Use `claude-sonnet-4-6`. Prompt should include the full pick context as structured data and ask for a confident, specific, jargon-appropriate explanation. Keep temperature low (0.3) for consistency.

**Estimated scope:** Small — 1 new file, minor update to `run_recs.py`. The schema column already exists.

---

### API Layer (`api/`)

To expose picks to any frontend or mobile app, a lightweight FastAPI service is needed. The DB already has everything — this is purely a read layer.

**Endpoints to build:**

| Endpoint | What it returns |
|----------|----------------|
| `GET /slate?date=YYYY-MM-DD` | Top picks for a given date with edge, confidence, starters, reasoning |
| `GET /game/{game_id}` | Full prediction + odds + starters + recommendation for one game |
| `GET /history?days=7` | Recent slate runs with pick counts and model version |
| `POST /chat` | Conversational endpoint — takes a natural-language question, returns Claude API response with pick context injected |

**Stack:**
- FastAPI + uvicorn
- SQLAlchemy (already used throughout) for DB reads
- Pydantic models for response serialization
- `api/main.py` as the entry point

**Estimated scope:** Medium — 3–4 endpoint files, Pydantic schemas, one shared DB session. No new data infrastructure needed.

---

### Frontend (`frontend/`)

A simple web UI to view daily picks without running CLI commands.

**Pages:**

| Page | What it shows |
|------|--------------|
| **Today's Slate** | Top 5 picks with game, starters, odds, edge, confidence, LLM explanation |
| **Game Detail** | Full model breakdown for one game — win prob, cover prob, team stats, pitching matchup |
| **History** | Past slates, pick outcomes (once results are tracked), running ROI |
| **Settings** | Edge/confidence threshold overrides for personal use |

**Recommended stack:**
- Next.js (React) — simple to deploy, good for mostly-static data with occasional refreshes
- Tailwind CSS for styling
- Fetches from the FastAPI layer above
- No auth needed for personal use; add if sharing with others

**Estimated scope:** Medium-large. Today's Slate page is the MVP and could be built in isolation first. History and Settings can follow.

---

### Recommended Build Order

1. **LLM explanations** — small lift, immediately improves pick output quality even in CLI
2. **FastAPI `/slate` endpoint** — single endpoint, unblocks everything else
3. **Today's Slate page** — one page frontend consuming the API; usable as a daily dashboard
4. **`/game/{id}` endpoint + Game Detail page** — drill-down for individual picks
5. **`/chat` endpoint** — conversational layer on top of existing data; can be added without changing anything else
6. **History page + ROI tracking** — requires storing actual game outcomes against picks (partially available via `games.status = final` + `recommendations`)

---

## Idea: Incorporating Industry Feel via Confidence Flags

### Background

Baseball has a strong tradition of qualitative knowledge — pitching matchup narratives, bullpen usage patterns, lineup dynamics — that isn't captured in season-level stats or ELO ratings. The goal is to incorporate "what the industry is saying" without compromising the model's statistical integrity.

### Why Not a Model Feature

Sentiment scores fed directly into the logistic regression have two hard problems:
1. **No historical backfill** — the model was trained on 2023–2025 games; you can't retroactively produce sentiment scores for those games without enormous effort
2. **LLM scores aren't stable** — logistic regression needs consistent, calibrated features; LLM sentiment outputs vary with prompting and context

### The Right Integration Point: Confidence Layer

Confidence is currently a 4-component score averaged equally:

| Component | Current value |
|-----------|--------------|
| Edge magnitude | Dynamic — varies per game |
| Model conviction | Dynamic — varies per game |
| Historical calibration | **Static 7.0 constant — adds no information** |
| Injury certainty | Dynamic — out/questionable/none |

The calibration component is a placeholder. This is the natural home for qualitative signals — replacing or supplementing the static 7.0 with flags that actually vary by game.

### Proposed Flags (Structured, Not Sentiment Scores)

Rather than asking an LLM "how positive is the sentiment for this team?" (vague, inconsistent), the LLM acts as a **structured information extractor** that outputs specific binary or categorical flags:

```json
{
  "game_id": "2026-04-15-PHI-NYM-1",
  "flags": {
    "pitcher_short_rest": false,
    "key_lineup_scratch": true,
    "bullpen_fatigue_home": true,
    "bullpen_fatigue_away": false,
    "weather_concern": false,
    "narrative_note": "Wheeler has dominated this lineup in last 3 matchups"
  }
}
```

Each flag adjusts confidence up or down before the bet/no-bet decision. The model probability is never touched.

### Signal Sources and What's Already Available

| Signal | Source | LLM needed? | Already have data? |
|--------|--------|-------------|-------------------|
| Bullpen fatigue | `pitcher_game_logs` (relief IP last 3 days) | No | **Yes — build this now** |
| Pitcher on short rest | `games` table (days since last start) | No | **Yes — build this now** |
| Weather (wind, dome) | Weather API | No | No |
| Key lineup scratch | Beat reporters, Rotoworld | Yes — extract from text | No |
| Pitching matchup history | Already priced into odds | Yes — nice to have | No |
| Public betting % / line movement | Odds movement API | No | No |

### Recommended Approach

1. **Bullpen fatigue and short rest** — compute directly from existing DB data, no LLM required. These are the most actionable signals and cost nothing to add.
2. **Weather** — structured API call, not LLM. Relevant primarily for run line and totals.
3. **Lineup news** — LLM reads a sports news feed (e.g. Rotoworld RSS or beat reporter tweets) and extracts key scratches as structured flags. Requires a content source.
4. **Pitching narrative** — lowest priority; mostly already priced into the line by game time.

### Important Limitation

Most qualitative information is already reflected in the odds line. Sharps read beat reporters and move the number before any automated system can react. The edge from this approach comes from **synthesis** (combining signals the model doesn't see) not from **speed**. Treat these as confidence modifiers, not edge generators.
