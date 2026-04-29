# System Architecture & Deployment Guide

*North-star reference document. Last updated: 2026-04-28.*

---

## Overview

The betting copilot is being refactored from a single local pipeline script into a cloud-hosted, multi-service system:

```
┌─────────────────────────────────────────────────────┐
│  Python Scheduled Job  (cron · 9am + 1pm ET)        │
│                                                     │
│  1. Resolve yesterday's final scores & boxscores    │
│  2. Ingest today's odds + probable starters         │
│  3. Run predictions  (ELO + logistic regression)    │
│  4. Run recommendations  (rules engine)             │
│  5. Write → slate_runs, recommendations, predictions│
└──────────────────────┬──────────────────────────────┘
                       │ writes
                       ▼
         ┌─────────────────────────┐
         │  PostgreSQL             │
         │  (DigitalOcean Managed) │
         └──────────┬──────────────┘
                    │ reads (read-only)
                    ▼
┌──────────────────────────────────────────────────────┐
│  Java Spring Boot API  (:8080)                       │
│                                                      │
│  GET /health                                         │
│  GET /slate              → today's picks             │
│  GET /slate/{date}       → historical picks          │
│  GET /game/{id}          → single game detail        │
│  GET /history            → past performance          │
└──────────────────────┬───────────────────────────────┘
                       │ HTTP
                       ▼
┌──────────────────────────────────────────────────────┐
│  Telegram Bot  (Python · python-telegram-bot)        │
│                                                      │
│  User message → LLM parses intent                    │
│  → API call → formatted response                     │
└──────────────────────────────────────────────────────┘
```

**Core principle:** The Java API is entirely read-only. All scoring and recommendations are pre-computed by the Python job and stored in PostgreSQL. The API has zero ML dependency at request time.

---

## Component 1: Python Scheduled Job

### What It Does

Replaces the current `run_daily.sh` + `refresh_stats.py` split. A single entry point:

```
scheduled_job.py  [--date YYYY-MM-DD]
  Step 1: resolve_yesterday()   → fetch final scores, boxscores, update pitcher/batting logs
  Step 2: ingest_today()        → odds snapshot + probable starters
  Step 3: predict()             → ELO + logistic regression → writes to predictions
  Step 4: recommend()           → rules engine → writes to slate_runs + recommendations
```

All steps are idempotent (safe to re-run). Steps use existing logic from:
- `ingest/run_ingest.py` — odds, starters, game schedule
- `ingest/mlb_boxscores.py` — final scores + starter/batting logs
- `models/predict.py` — predictions
- `recs/run_recs.py` — recommendations

### Run Schedule

| Time (ET) | Job | Purpose |
|-----------|-----|---------|
| 9:00 AM | `scheduled_job.py --ingest-only` | Morning lines + starters |
| 1:00 PM | `scheduled_job.py` | Full pipeline after lineups confirmed |

### Deployment (Docker container on Droplet)

```cron
TZ=America/New_York
0 9  * * * docker exec betting-copilot-jobs python scheduled_job.py --ingest-only >> /var/log/jobs.log 2>&1
0 13 * * * docker exec betting-copilot-jobs python scheduled_job.py >> /var/log/jobs.log 2>&1
```

### Docker Image

Built from the existing `Dockerfile` (Python 3.12-slim). Pushed to Docker Hub as:
```
docker.io/<username>/betting-copilot-jobs:latest
```

---

## Component 2: Java Spring Boot API

### Stack

| Library | Version | Purpose |
|---------|---------|---------|
| Spring Boot | 3.x | Application framework |
| Spring Data JPA | (included) | ORM + repository abstraction |
| Hibernate | (included) | JPA implementation |
| PostgreSQL driver | `org.postgresql:postgresql` | DB connectivity |
| Lombok | latest | Reduce entity/DTO boilerplate |
| Jackson | (included) | JSON serialization, JSONB handling |

### Project Structure

```
betting-copilot-api/
  src/main/java/com/bettingcopilot/
    entity/
      Recommendation.java        ← recommendations table
      SlateRun.java              ← slate_runs table
      Game.java                  ← games table
      Prediction.java            ← predictions table
    repository/
      RecommendationRepository.java
      SlateRunRepository.java
      GameRepository.java
    service/
      SlateService.java          ← business logic + JSONB parsing
      HistoryService.java
    controller/
      SlateController.java       ← REST endpoints
      HealthController.java
    dto/
      SlateResponse.java
      PickDto.java
      GameDetailResponse.java
      HistoryResponse.java
  src/main/resources/
    application.properties
```

### JPA Configuration

The existing schema uses snake_case column names. Add to `application.properties`:

```properties
spring.datasource.url=${DATABASE_URL}
spring.jpa.hibernate.ddl-auto=none
spring.jpa.hibernate.naming.physical-strategy=org.hibernate.boot.model.naming.PhysicalNamingStrategyStandardImpl
spring.jpa.properties.hibernate.dialect=org.hibernate.dialect.PostgreSQLDialect
```

`ddl-auto=none` is critical — Hibernate must never touch the existing schema.

### JPA Entity Pattern

```java
@Entity
@Table(name = "recommendations")
@Getter @Setter         // Lombok
public class Recommendation {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    @ManyToOne(fetch = FetchType.LAZY)
    @JoinColumn(name = "run_id")
    private SlateRun slateRun;

    @Column(name = "game_id")
    private String gameId;

    @Column(name = "market")
    private String market;                // "moneyline" | "run_line"

    @Column(name = "side")
    private String side;                  // "home" | "away"

    @Column(name = "edge")
    private Double edge;

    @Column(name = "confidence")
    private Double confidence;

    @Column(name = "decision")
    private String decision;             // "BET" | "PASS"

    @Column(name = "odds")
    private Integer odds;                // American odds e.g. -115

    @Column(name = "context_snapshot", columnDefinition = "jsonb")
    private String contextSnapshot;      // deserialize with Jackson in service layer

    @Column(name = "llm_explanation")
    private String llmExplanation;

    @Column(name = "created_at")
    private OffsetDateTime createdAt;
}
```

### JSONB Handling

`context_snapshot` is a JSONB column containing rich prediction context. Map as `String` in the entity; deserialize in the service layer:

```java
@Service
public class SlateService {

    private final ObjectMapper objectMapper = new ObjectMapper();

    public PickDto toPickDto(Recommendation rec) {
        PickDto dto = new PickDto();
        dto.setEdge(rec.getEdge());
        // ... other fields

        // Parse JSONB
        try {
            JsonNode ctx = objectMapper.readTree(rec.getContextSnapshot());
            dto.setModelProb(ctx.path("model_prob").asDouble());
            dto.setHomeStarter(ctx.path("home_starter").path("name").asText());
            dto.setHomeStarterEra(ctx.path("home_starter").path("l3_era").asDouble());
            // etc.
        } catch (JsonProcessingException e) {
            // log and continue — context_snapshot may be null for older records
        }

        return dto;
    }
}
```

### Repository Pattern

Spring Data JPA provides free CRUD. Only write custom queries for non-trivial lookups:

```java
public interface RecommendationRepository extends JpaRepository<Recommendation, Long> {

    // Deduplicate: if job ran twice, take the latest rec per (game, market, side)
    @Query(value = """
        SELECT DISTINCT ON (r.game_id, r.market, r.side) r.*
        FROM recommendations r
        JOIN slate_runs s ON r.run_id = s.id
        WHERE s.run_date = :runDate
          AND r.decision = 'BET'
        ORDER BY r.game_id, r.market, r.side, r.created_at DESC
        """, nativeQuery = true)
    List<Recommendation> findBetsByDate(@Param("runDate") LocalDate runDate);
}
```

### API Endpoints

**`GET /health`**
```json
{ "status": "ok", "latestGameDate": "2026-04-28", "latestSlateDate": "2026-04-28" }
```

**`GET /slate`** and **`GET /slate/{date}`**
```json
{
  "runDate": "2026-04-28",
  "modelVersion": "v3_elo_logreg_starters",
  "gamesCount": 14,
  "picksCount": 3,
  "picks": [
    {
      "gameId": "2026-04-28-NYY-BOS-1",
      "homeTeam": "NYY", "awayTeam": "BOS",
      "market": "moneyline", "side": "home",
      "odds": -115, "edge": 0.067, "confidence": 7.3,
      "modelProb": 0.582, "impliedProb": 0.535,
      "homeStarter": "Severino", "homeStarterL3Era": 2.89,
      "awayStarter": "Bibee", "awayStarterL3Era": 3.45,
      "llmExplanation": "NYY holds a significant edge on starting pitching..."
    }
  ]
}
```

**`GET /game/{game_id}`** — Full game detail from games + predictions + odds + starters.

**`GET /history`** — Past picks with win/loss outcomes. Query params: `start_date`, `end_date`, `page`, `per_page`.

### Docker Image

```
docker.io/<username>/betting-copilot-api:latest
```

Runs on port `8080`. No model files needed (read-only DB queries only).

---

## Component 3: Telegram Bot

Kept in Python to reuse the existing `llm/` module (Anthropic SDK).

```
bot/
  telegram_bot.py     ← message handler + LLM intent routing
  api_client.py       ← HTTP client calling Spring Boot API
  formatter.py        ← JSON → Telegram message formatting
```

### Flow

```
User: "what are today's picks?"
  → LLM: parse intent → { action: "slate", date: "today" }
  → API: GET http://api:8080/slate
  → Format: structured Telegram message
  → Reply to user
```

### Message Format

```
MLB Picks — 2026-04-28

#1 NYY (home) ML -115
   Model: 58.2% vs Market: 53.5%
   Edge: 6.7% | Confidence: 7.3/10
   SP: Severino L3 ERA 2.89 vs Bibee ERA 3.45
   NYY holds a significant edge on starting pitching...

3 picks today | Season: 45-32 (58.4%)
```

---

## Infrastructure & Deployment

### Docker Hub Setup

Docker Hub replaces DigitalOcean Container Registry — simpler, no `doctl` dependency for image management.

**One-time setup:**

1. Create account at [hub.docker.com](https://hub.docker.com)
2. Create two repositories:
   - `<username>/betting-copilot-jobs` — Python scheduled job
   - `<username>/betting-copilot-api` — Java Spring Boot API
3. Authenticate locally:
   ```bash
   docker login
   # Enter Docker Hub username + password (or access token)
   ```

**Build and push (Apple Silicon Mac — must cross-compile for x86):**

```bash
# Python jobs image
docker buildx build \
  --platform linux/amd64 \
  -t <username>/betting-copilot-jobs:latest \
  --push \
  .

# Java API image (from betting-copilot-api/ directory)
docker buildx build \
  --platform linux/amd64 \
  -t <username>/betting-copilot-api:latest \
  --push \
  ./betting-copilot-api
```

**Pull on Droplet:**

```bash
docker login   # once, using Docker Hub credentials
docker pull <username>/betting-copilot-jobs:latest
docker pull <username>/betting-copilot-api:latest
```

**Free tier:** 1 private repo. Use public repos (acceptable for non-sensitive code) or upgrade to Docker Pro ($5/mo) for unlimited private repos.

---

### DigitalOcean Infrastructure

| Resource | Spec | Cost |
|----------|------|------|
| Droplet | `s-1vcpu-2gb`, Ubuntu 24.04 | $12/mo |
| Managed PostgreSQL | `db-s-1vcpu-1gb`, 10 GB | $15/mo |
| **Total** | | **~$27/mo** |

**Alternative — Railway (~$20-25/mo):** First-class cron job support, web services, and managed Postgres as primitives. No SSH/Docker management. Good choice if you prefer less DevOps overhead.

---

### Step-by-Step: DigitalOcean Deployment

#### Step 1: Create Managed PostgreSQL

DO Console → **Databases** → **Create Database**

| Setting | Value |
|---------|-------|
| Engine | PostgreSQL 16 |
| Plan | Basic — `db-s-1vcpu-1gb` |
| Region | NYC3 (or closest to you) |
| Name | `betting-copilot-db` |

After creation → **Connection Details** → copy the full connection string:
```
postgresql://doadmin:PASS@host.db.ondigitalocean.com:25060/defaultdb?sslmode=require
```

#### Step 2: Migrate the Database

```bash
# 1. Dump local DB
pg_dump -Fc postgresql://admin:password@localhost:5432/betting_copilot \
  > betting_copilot_$(date +%Y%m%d).dump

# 2. Create the database on DO (if still named defaultdb)
psql "postgresql://doadmin:PASS@do-host:25060/defaultdb?sslmode=require" \
  -c "CREATE DATABASE betting_copilot;"

# 3. Restore
pg_restore \
  -h do-host -p 25060 -U doadmin \
  -d betting_copilot --no-owner --no-privileges \
  betting_copilot_YYYYMMDD.dump

# 4. Verify
psql "postgresql://doadmin:PASS@do-host:25060/betting_copilot?sslmode=require" \
  -c "SELECT COUNT(*) FROM games; SELECT COUNT(*) FROM recommendations;"
```

#### Step 3: Create Droplet

DO Console → **Droplets** → **Create Droplet**

| Setting | Value |
|---------|-------|
| Image | Ubuntu 24.04 LTS |
| Plan | `s-1vcpu-2gb` |
| Region | **Same region as DB** |
| Authentication | SSH Key |
| Hostname | `betting-copilot` |

After creation: Databases → `betting-copilot-db` → **Settings** → **Trusted Sources** → add the Droplet.

#### Step 4: Bootstrap the Droplet

```bash
# Install Docker and set up directory structure
./deploy/setup.sh bootstrap <droplet-ip>
```

SSH in and configure:

```bash
ssh root@<droplet-ip>
nano /opt/betting-copilot/.env
```

```env
DATABASE_URL=postgresql://doadmin:PASS@do-host:25060/betting_copilot?sslmode=require
ANTHROPIC_API_KEY=sk-ant-...
ODDS_API_KEY=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

```bash
chmod 600 /opt/betting-copilot/.env
docker login   # authenticate to Docker Hub
```

#### Step 5: Create docker-compose.yml on Droplet

```yaml
services:
  api:
    image: <username>/betting-copilot-api:latest
    container_name: betting-copilot-api
    restart: unless-stopped
    ports: ["8080:8080"]
    env_file: .env

  jobs:
    image: <username>/betting-copilot-jobs:latest
    container_name: betting-copilot-jobs
    restart: unless-stopped
    env_file: .env
    volumes:
      - ./models/versions:/app/models/versions:ro
    command: tail -f /dev/null   # keep alive; cron drives execution

  telegram:
    image: <username>/betting-copilot-jobs:latest
    container_name: betting-copilot-telegram
    restart: unless-stopped
    env_file: .env
    command: python bot/telegram_bot.py
```

#### Step 6: Set Up Cron

```bash
ssh root@<droplet-ip>
crontab -e
```

```cron
TZ=America/New_York
0 9  * * * docker exec betting-copilot-jobs python scheduled_job.py --ingest-only >> /var/log/jobs.log 2>&1
0 13 * * * docker exec betting-copilot-jobs python scheduled_job.py >> /var/log/jobs.log 2>&1
```

#### Step 7: Launch

```bash
ssh root@<droplet-ip>
cd /opt/betting-copilot
docker compose pull
docker compose up -d
docker compose ps   # verify all 3 services are running
```

#### Step 8: Verify

```bash
curl http://<droplet-ip>:8080/health
curl http://<droplet-ip>:8080/slate
```

---

### Ongoing Operations

**Deploy a code change:**

```bash
# Rebuild and push new image
docker buildx build --platform linux/amd64 \
  -t <username>/betting-copilot-api:latest --push ./betting-copilot-api

# Pull and restart on Droplet
ssh root@<droplet-ip> "cd /opt/betting-copilot && docker compose pull api && docker compose restart api"
```

**Update ML model pickle (no rebuild needed):**

```bash
scp models/versions/v3_elo_logreg_starters.pkl \
  root@<droplet-ip>:/opt/betting-copilot/models/versions/
```

**View logs:**

```bash
ssh root@<droplet-ip> docker logs -f betting-copilot-api
ssh root@<droplet-ip> tail -f /var/log/jobs.log
```

**Rotate an API key:**

```bash
ssh root@<droplet-ip>
nano /opt/betting-copilot/.env
docker compose restart
```

---

## Execution Phases

| Phase | Task | Language | Status |
|-------|------|----------|--------|
| 1 | Merge `run_daily.sh` + `refresh_stats.py` → `scheduled_job.py` | Python | TODO |
| 2 | Create Spring Boot project + JPA entities for existing schema | Java | TODO |
| 3 | Implement repositories + services + REST controllers | Java | TODO |
| 4 | Migrate PostgreSQL to DO Managed DB | Infra | TODO |
| 5 | Docker Hub setup + deploy both images to Droplet | Infra | TODO |
| 6 | Build Telegram bot calling Spring Boot API | Python | TODO |

---

## Environment Variables Reference

| Variable | Used By | Description |
|----------|---------|-------------|
| `DATABASE_URL` | Python job, Java API | PostgreSQL connection string |
| `ANTHROPIC_API_KEY` | Python job, Telegram bot | Claude API for LLM explanations |
| `ODDS_API_KEY` | Python job | The Odds API for moneyline data |
| `TELEGRAM_BOT_TOKEN` | Telegram bot | Token from @BotFather |
| `TELEGRAM_CHAT_ID` | Telegram bot | Your personal chat/group ID |

---

## Key Design Decisions

**Why Java/Spring Boot for the API?** Sahil is a Java expert. The API is purely read-only DB queries — no ML at request time — which is a natural fit for Spring Data JPA. The Python model and scoring logic stay in the scheduled job.

**Why pre-compute, not score on-demand?** All prediction data is already in `predictions` + `recommendations` after the job runs. Scoring on-demand per API request would require loading the pickle model and running pandas/sklearn in Java — a major complexity with no benefit.

**Why Docker Hub over DO Container Registry?** Simpler setup — no `doctl` required, works from any machine, free for public images. The DO registry saves $5/mo and removes a vendor-specific dependency.

**Why managed PostgreSQL?** Historical game data, predictions, and pitcher logs are the most valuable asset. DO Managed DB handles backups, failover, and security patches automatically. Never run the DB in a container.
