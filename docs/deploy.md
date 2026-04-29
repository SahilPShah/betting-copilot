# DigitalOcean Deployment Guide

*Covers everything from zero DO resources to a running production API.*

---

## Prerequisites (local machine)

Before starting, make sure you have:

- [ ] **Docker Desktop** installed and running (with `buildx` support — included by default)
- [ ] **doctl CLI** installed: `brew install doctl`
- [ ] **doctl authenticated**: `doctl auth init` (requires a DO Personal Access Token)
- [ ] **An SSH key** added to your DO account (DO Console → Settings → Security → SSH Keys)
- [ ] Local API working: `source .venv/bin/activate && python -m api.main` → `curl localhost:8000/health` returns 200

---

## Step 1: Create DO Container Registry

The Container Registry stores your Docker image. The free Starter tier gives you 500 MB and 1 repository — more than enough.

```bash
# Create the registry (only run once)
doctl registry create betting-copilot --subscription-tier starter

# Authenticate Docker to the registry
doctl registry login
```

Your registry URL will be: `registry.digitalocean.com/betting-copilot`

---

## Step 2: Create DO Managed PostgreSQL

DO Console → **Databases** → **Create Database**

| Setting | Value |
|---------|-------|
| Engine | PostgreSQL 16 |
| Plan | Basic — 1 GB RAM / 1 vCPU / 10 GB (`db-s-1vcpu-1gb`) |
| Region | **Pick one close to you** — use the same region for your Droplet |
| Name | `betting-copilot-db` |

After creation:
1. Go to the database → **Connection Details** → select **Connection String** format
2. Copy the full string — it looks like:
   ```
   postgresql://doadmin:YOURPASS@db-betting-copilot-do-user-xxx.db.ondigitalocean.com:25060/defaultdb?sslmode=require
   ```
3. Note the host, port (25060), username, and password. You'll need these in Step 5.

> **Note:** The database is named `defaultdb` by default. You can rename it to `betting_copilot` or just update the name in the connection string.

---

## Step 3: Migrate Your Local Database to DO PostgreSQL

This copies all your historical game data, predictions, and recommendations to the managed DB.

```bash
# 1. Dump local DB
pg_dump -Fc postgresql://admin:password@localhost:5432/betting_copilot \
  > betting_copilot_$(date +%Y%m%d).dump

# 2. Create the betting_copilot database on DO (if it's named defaultdb)
psql "postgresql://doadmin:YOURPASS@do-host:25060/defaultdb?sslmode=require" \
  -c "CREATE DATABASE betting_copilot;"

# 3. Restore to DO
pg_restore \
  -h do-host \
  -p 25060 \
  -U doadmin \
  -d betting_copilot \
  --no-owner \
  --no-privileges \
  betting_copilot_YYYYMMDD.dump

# 4. Verify — spot-check row counts
psql "postgresql://doadmin:YOURPASS@do-host:25060/betting_copilot?sslmode=require" \
  -c "SELECT COUNT(*) AS games FROM games; SELECT COUNT(*) AS predictions FROM predictions; SELECT COUNT(*) AS recs FROM recommendations;"
```

---

## Step 4: Create DO Droplet

DO Console → **Droplets** → **Create Droplet**

| Setting | Value |
|---------|-------|
| Image | Ubuntu 24.04 LTS |
| Plan | Basic — 2 GB RAM / 1 vCPU / 50 GB SSD (`s-1vcpu-2gb`) |
| Region | **Same region as your database** |
| Authentication | SSH Key (select yours) |
| Hostname | `betting-copilot` |

After creation, note the Droplet's public IP address.

---

## Step 5: Allow the Droplet to Access the Database

DO Console → **Databases** → `betting-copilot-db` → **Settings** → **Trusted Sources**

Add your Droplet. This opens the firewall between the two resources so the API can connect to Postgres.

---

## Step 6: Bootstrap the Droplet

Run this once from your local machine. It installs Docker and sets up the directory structure.

```bash
./deploy/setup.sh bootstrap <droplet-ip>
```

Then SSH in to set the real credentials:

```bash
ssh root@<droplet-ip>
nano /opt/betting-copilot/.env
```

Edit the `.env` file with your actual values:

```bash
DATABASE_URL=postgresql://doadmin:YOURPASS@do-host:25060/betting_copilot?sslmode=require
ANTHROPIC_API_KEY=sk-ant-...
ODDS_API_KEY=...
```

Save and set permissions:

```bash
chmod 600 /opt/betting-copilot/.env
```

---

## Step 7: Copy Model Files to Droplet

The model pickle files are mounted as a volume (not baked into the Docker image) so you can update them without rebuilding.

```bash
# From your local machine
scp models/versions/*.pkl root@<droplet-ip>:/opt/betting-copilot/models/versions/
```

---

## Step 8: Build and Push the Docker Image

```bash
# Set your registry URL
REGISTRY="registry.digitalocean.com/betting-copilot"
IMAGE_TAG=$(git rev-parse --short HEAD)

# The --platform linux/amd64 flag is REQUIRED on Apple Silicon Macs.
# DO Droplets are x86 (Intel/AMD) — an ARM image will fail to run.
docker buildx build \
  --platform linux/amd64 \
  --tag "${REGISTRY}/betting-copilot:${IMAGE_TAG}" \
  --tag "${REGISTRY}/betting-copilot:latest" \
  --push \
  .
```

The first build takes ~5 minutes (downloading base image, compiling C extensions). Subsequent builds that only change Python code take ~30 seconds — the `pip install` layer is cached.

---

## Step 9: Deploy

```bash
./deploy/setup.sh deploy <droplet-ip> registry.digitalocean.com/betting-copilot
```

This script:
1. Copies `docker-compose.yml` to the Droplet
2. Pulls the new image
3. Restarts the API container
4. Prunes old images
5. Runs a health check

Manually verify after it completes:

```bash
curl http://<droplet-ip>:8000/health
```

You should see something like:

```json
{
  "status": "ok",
  "latest_game_date": "2026-04-27",
  "latest_slate_date": "2026-04-27"
}
```

---

## Step 10: Set Up the Daily Cron Job

The `capture_odds.py` script needs to run every morning at 9am ET to fetch the day's odds, update scores, and refresh stats.

SSH into the Droplet:

```bash
ssh root@<droplet-ip>
crontab -e
```

Add these two lines at the top:

```cron
TZ=America/New_York
0 9 * * * docker exec betting-copilot-api python ingest/capture_odds.py >> /var/log/capture_odds.log 2>&1
```

Save and exit. Verify it was saved:

```bash
crontab -l
```

The cron job runs inside the existing API container, so it has access to the same Python environment and database connection. Logs go to `/var/log/capture_odds.log` on the Droplet.

---

## Step 11: Configure Firewall (Recommended)

By default the Droplet has all ports open. Lock it down to only SSH and your API:

```bash
doctl compute firewall create \
  --name betting-copilot-fw \
  --inbound-rules "protocol:tcp,ports:22,address:0.0.0.0/0 protocol:tcp,ports:8000,address:0.0.0.0/0" \
  --outbound-rules "protocol:tcp,ports:all,address:0.0.0.0/0 protocol:udp,ports:all,address:0.0.0.0/0"

# Attach to the Droplet (get the Droplet ID first)
doctl compute droplet list
doctl compute firewall add-droplets <firewall-id> --droplet-ids <droplet-id>
```

---

## Step 12: End-to-End Verification

```bash
# Health check
curl http://<droplet-ip>:8000/health

# Trigger today's pipeline
curl -X POST http://<droplet-ip>:8000/run-pipeline/$(date +%Y-%m-%d)

# Wait ~2 minutes, then check the slate
curl http://<droplet-ip>:8000/slate

# Interactive API docs
open http://<droplet-ip>:8000/docs
```

---

## Ongoing Operations

### Deploy a code change

```bash
IMAGE_TAG=$(git rev-parse --short HEAD)
./deploy/setup.sh deploy <droplet-ip> registry.digitalocean.com/betting-copilot $IMAGE_TAG
```

### Update a model (no code change)

```bash
# Copy the new .pkl to the Droplet — the volume mount picks it up on the next pipeline run
scp models/versions/new_model.pkl root@<droplet-ip>:/opt/betting-copilot/models/versions/
```

### View logs

```bash
# API logs (live)
ssh root@<droplet-ip> docker logs -f betting-copilot-api

# Daily cron logs
ssh root@<droplet-ip> tail -f /var/log/capture_odds.log
```

### Restart the API

```bash
ssh root@<droplet-ip> "cd /opt/betting-copilot && docker compose restart api"
```

### Rotate an API key

```bash
ssh root@<droplet-ip>
nano /opt/betting-copilot/.env   # update the key
docker compose -f /opt/betting-copilot/docker-compose.yml restart api
```

---

## Infrastructure Summary

| Resource | Spec | Cost |
|----------|------|------|
| Droplet | `s-1vcpu-2gb`, Ubuntu 24.04, Regular Intel | $12/mo |
| Managed PostgreSQL | `db-s-1vcpu-1gb`, 10 GB storage | $15/mo |
| Container Registry | Starter (500 MB free tier) | $0 |
| **Total** | | **~$27/mo** |
