# Plan: FastAPI Layer + Telegram Bot + DigitalOcean Deployment

*Created: 2026-04-22*

## Context

The betting copilot runs as a local CLI pipeline (`run_daily.sh`) on a Mac with a LaunchAgent that silently skips runs when the machine sleeps (see `docs/launchd_gap.md` — 15 days missed Apr 8–21). Moving to a cloud VM with a reliable cron eliminates this entirely. The user also wants a REST API to serve predictions/picks (currently only terminal output) and a Telegram bot for daily pick delivery.

---

## Part 1: FastAPI API Layer

### Files to Create

| File | Purpose |
|------|---------|
| `api/main.py` | FastAPI app, CORS, router registration |
| `api/deps.py` | DB dependency injection (reuses `db/session.engine`) |
| `api/schemas.py` | All Pydantic response models |
| `api/routes/__init__.py` | Empty (file missing, needed for imports) |
| `api/routes/health.py` | `GET /health` |
| `api/routes/slate.py` | `GET /slate`, `GET /slate/{date}` |
| `api/routes/game.py` | `GET /game/{game_id}` |
| `api/routes/history.py` | `GET /history` |
| `api/routes/pipeline.py` | `POST /run-pipeline/{date}` |

### Files to Modify

| File | Change |
|------|--------|
| `db/session.py` | Remove hardcoded path `load_dotenv("/Users/sahilshah/betting-copilot/.env")` → use relative `load_dotenv()` (finds `.env` from cwd) |
| `recs/run_recs.py` | Same hardcoded `.env` path fix (line 13) |

### Endpoints

**`GET /health`** — DB connectivity check, latest slate date, latest game date.

**`GET /slate/{date?}`** — Primary endpoint. Returns today's slate (default) or a specific date.
- Query: `slate_runs` joined with `recommendations` + `games` (2 queries total)
- Rich data comes from `context_snapshot` JSONB — no N+1 queries needed
- Response: `{run_date, model_version, games_count, picks_count, ran_at, picks: [...], no_bets: [...]}`
- Each pick includes: game_id, teams, market, side, edge, confidence, decision, odds, model_prob, implied_prob, starters (name + ERA from context_snapshot), llm_explanation

**`GET /game/{game_id}`** — Single game detail combining all data sources.
- 5 queries: games, predictions, odds (DISTINCT ON market+side, latest), game_starters, injury_statuses, recommendations
- Starter ERA comes from `context_snapshot` when recommendation exists, name-only otherwise (ERA is computed dynamically at predict time, not stored in `game_starters`)

**`GET /history`** — Past picks with outcomes.
- Query params: `start_date`, `end_date` (default last 30 days), `decision` filter, `page`/`per_page` pagination
- Joins recommendations + games to compute win/loss/push from final scores
- Standard run line = 1.5 (matches `models/predict.py`)
- Response includes summary: `{wins, losses, pushes, pending, win_rate}`

**`POST /run-pipeline/{date}`** — Triggers daily pipeline via FastAPI `BackgroundTasks`.
- Calls the 3 pipeline steps as subprocess (ingest → predict → recommend)
- Returns immediately with `{status: "started", date, message}`
- In Docker: calls `python ingest/run_ingest.py && python models/predict.py && python recs/run_recs.py` directly (no shell script needed)

### Design Decisions
- **Sync, not async** — existing `db/session.py` uses sync SQLAlchemy engine. FastAPI runs sync endpoints in a threadpool automatically. Fine for single-user system.
- **Raw Connection, not ORM Session** — matches existing codebase pattern (`engine.connect()` + `text()` everywhere)
- **Pydantic response models** — type safety, auto-generated OpenAPI docs at `/docs`
- **No auth** — single-user system, can add later behind a reverse proxy

### Implementation Order
1. `db/session.py` + `recs/run_recs.py` — fix hardcoded `.env` paths
2. `api/deps.py` — DB dependency
3. `api/schemas.py` — all Pydantic models
4. `api/routes/__init__.py` — empty file
5. `api/routes/health.py` — validates DB works
6. `api/routes/slate.py` — most important endpoint
7. `api/routes/game.py` — game detail
8. `api/routes/history.py` — historical results
9. `api/routes/pipeline.py` — pipeline trigger
10. `api/main.py` — wire everything, CORS

---

## Part 2: Telegram Bot

### Files to Create

| File | Purpose |
|------|---------|
| `telegram/bot.py` | Bot setup, `/picks` command, daily auto-send |
| `telegram/formatter.py` | Format pick data into Telegram-friendly messages |

### How It Works
- Uses `python-telegram-bot` library (add to requirements)
- **Polling mode** (not webhook) — simpler, no HTTPS/Elastic IP needed, works behind NAT
- Runs as a long-lived process alongside the API
- Two triggers for sending picks:
  1. **User command**: `/picks` or `/picks 2026-04-22` — queries the API's `/slate` endpoint and formats the response
  2. **Auto-send after pipeline**: the pipeline trigger endpoint (`POST /run-pipeline`) calls the bot's send function after completion

### New Env Vars
```
TELEGRAM_BOT_TOKEN=...     # from @BotFather
TELEGRAM_CHAT_ID=...       # your chat/group ID
```

### Message Format
```
MLB Picks — 2026-04-22

#1 NYY (home) ML -115
   Model: 58.2% vs Market: 51.6%
   Edge: 6.7% | Confidence: 7.3/10
   SP: Severino L3 ERA 2.89 vs Bibee ERA 3.45
   NYY edge on starting pitching and home field...

#2 LAD (away) RL +1.5 -140
   ...

3 picks today | Season: 45-32 (58.4%)
```

---

## Part 3: DigitalOcean Deployment

### Infrastructure

| Resource | Spec | Cost |
|----------|------|------|
| Droplet | Basic, 2 GB RAM / 1 vCPU / 50 GB SSD (Regular Intel, `s-1vcpu-2gb`) | $12/month |
| Managed PostgreSQL | Basic, 1 GB RAM / 10 GB storage (`db-s-1vcpu-1gb`) | $15/month |
| Container Registry | Starter (free tier, 500 MB, 1 repo) | $0 |
| **Total** | | **~$27/month** |

DigitalOcean Droplets are x86 (AMD/Intel) — no ARM cross-compilation issues.

### Files to Create

| File | Purpose |
|------|---------|
| `Dockerfile` | Single image for API + pipeline + Telegram bot |
| `docker-compose.yml` | 3 services: api, telegram-bot, pipeline (cron) |
| `.dockerignore` | Exclude .venv, .git, notebooks, __pycache__ |
| `requirements.txt` | Generated from current venv (production deps only) |
| `deploy/setup.sh` | One-time Droplet setup script |

### Dockerfile (single image, multiple entry points)
- Base: `python:3.12-slim`
- Install system deps: `libpq-dev gcc` (for psycopg2)
- Copy code + model pickles (21KB total, trivial)
- `pip install -r requirements.txt`
- Default CMD: `uvicorn api.main:app --host 0.0.0.0 --port 8000`

### docker-compose.yml — 3 services

```yaml
services:
  api:
    build: .
    command: uvicorn api.main:app --host 0.0.0.0 --port 8000
    ports: ["8000:8000"]
    env_file: .env
    restart: unless-stopped

  telegram-bot:
    build: .
    command: python telegram/bot.py
    env_file: .env
    restart: unless-stopped

  pipeline:
    build: .
    command: >
      sh -c "
        echo '0 14 * * * cd /app && python ingest/capture_odds.py >> /var/log/cron.log 2>&1' > /etc/crontabs/root &&
        echo '0 17 * * * cd /app && python ingest/run_ingest.py && python models/predict.py && python recs/run_recs.py >> /var/log/cron.log 2>&1' >> /etc/crontabs/root &&
        crond -f
      "
    env_file: .env
    restart: unless-stopped
```

**Schedule** (UTC — adjust to your timezone):
- `0 14 * * *` (10am ET) — odds capture
- `0 17 * * *` (1pm ET) — full pipeline (after lineups are set)

**Why not APScheduler**: Host cron (via the `pipeline` container) is simpler, more transparent, and the job is already idempotent. APScheduler adds a dependency and in-process scheduling complexity for a once-daily job.

### Database: Managed PostgreSQL (not in compose)
- DigitalOcean Managed DB handles backups, failover, updates
- Connection string provided by DO: `postgresql://user:pass@host:25060/betting_copilot?sslmode=require`
- Update `DATABASE_URL` in `.env` on the Droplet
- **Do NOT run Postgres in compose** — the data (historical games, odds, pitcher logs) is the most valuable asset. Managed DB protects it.

### DB Migration Steps
1. Local: `pg_dump -Fc betting_copilot > betting_copilot.dump`
2. Create DO Managed PostgreSQL via console/doctl
3. `pg_restore -h <do-host> -p 25060 -U doadmin -d betting_copilot betting_copilot.dump`
4. Run any pending migrations (check `v7_predictions_elo_diff.sql` if not yet applied)
5. Verify: connect via `psql` and spot-check row counts

### Deployment Steps
1. **Local**: Generate `requirements.txt` from venv (exclude jupyter/dev packages)
2. **Local**: Build and test Docker image locally (`docker compose up`)
3. **DO Console**: Create Managed PostgreSQL database
4. **Local**: `pg_dump` → `pg_restore` to DO database
5. **DO Console**: Create Droplet (s-1vcpu-2gb), add SSH key
6. **Droplet**: Install Docker + docker-compose (`deploy/setup.sh`)
7. **DO Console**: Create Container Registry
8. **Local**: Push image to DOCR (`doctl registry login`, `docker push`)
9. **Droplet**: Pull image, create `.env` with DO database URL + API keys
10. **Droplet**: `docker compose up -d`
11. **Verify**: `curl http://<droplet-ip>:8000/health`

### Secret Management
- `.env` file on the Droplet (not in the image, not in git)
- Contains: `DATABASE_URL`, `ANTHROPIC_API_KEY`, `ODDS_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
- For rotation: edit `.env`, `docker compose restart`

### LaunchAgent Gap Fix
Moving to a cloud Droplet **completely solves** the `docs/launchd_gap.md` problem:
- The Droplet is always on (99.99% SLA)
- Cron in the pipeline container fires reliably
- `run_ingest.py` already has backfill logic for any gaps
- No more missed runs from laptop sleep

### Monitoring
- `docker compose logs -f` for live output
- `/health` endpoint for uptime monitoring (can add UptimeRobot free tier)
- Pipeline container logs capture all ingest/predict/recommend output
- `restart: unless-stopped` auto-recovers from crashes

---

## Verification Plan

### FastAPI (local, before deployment)
```bash
source .venv/bin/activate
uvicorn api.main:app --reload --port 8000
# Test each endpoint:
curl http://localhost:8000/health
curl http://localhost:8000/slate
curl http://localhost:8000/slate/2026-04-22
curl http://localhost:8000/game/2026-04-22-NYY-BOS-1
curl "http://localhost:8000/history?start_date=2026-03-27&end_date=2026-04-22"
curl -X POST http://localhost:8000/run-pipeline/2026-04-22
# Check auto-docs: http://localhost:8000/docs
```

### Docker (local)
```bash
docker compose build
docker compose up
# Same curl tests against localhost:8000
```

### Telegram Bot
1. Create bot via @BotFather, get token
2. Send `/start` to your bot, get chat ID
3. Run `python telegram/bot.py` locally
4. Send `/picks` command in Telegram — should return today's slate

### Production (after deploy)
```bash
curl http://<droplet-ip>:8000/health
curl http://<droplet-ip>:8000/slate
# Verify cron: check pipeline container logs after scheduled time
# Verify Telegram: wait for auto-send or use /picks command
```
