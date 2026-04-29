#!/usr/bin/env bash
# deploy/setup.sh
#
# Usage:
#   Bootstrap (one-time on a new Droplet):  ./deploy/setup.sh bootstrap <droplet-ip>
#   Deploy a new image:                     ./deploy/setup.sh deploy <droplet-ip> <registry> <image-tag>
#
# Examples:
#   ./deploy/setup.sh bootstrap 164.90.xxx.xxx
#   ./deploy/setup.sh deploy 164.90.xxx.xxx registry.digitalocean.com/betting-copilot abc1234
#
set -euo pipefail

COMMAND=${1:-}
DROPLET_IP=${2:-}

if [[ -z "$COMMAND" || -z "$DROPLET_IP" ]]; then
    echo "Usage:"
    echo "  $0 bootstrap <droplet-ip>"
    echo "  $0 deploy <droplet-ip> <registry> <image-tag>"
    exit 1
fi

###############################################################################
# BOOTSTRAP — run once on a fresh Droplet
###############################################################################
bootstrap() {
    echo "==> Bootstrapping Droplet at $DROPLET_IP"

    ssh root@"$DROPLET_IP" bash << 'REMOTE'
set -euo pipefail

echo "--- Installing Docker ---"
curl -fsSL https://get.docker.com | sh
usermod -aG docker root

echo "--- Creating app directory ---"
mkdir -p /opt/betting-copilot/models/versions

echo "--- Creating .env placeholder (edit this with real values) ---"
cat > /opt/betting-copilot/.env << 'ENV'
DATABASE_URL=postgresql://doadmin:REPLACE_ME@REPLACE_ME.db.ondigitalocean.com:25060/betting_copilot?sslmode=require
ANTHROPIC_API_KEY=sk-ant-REPLACE_ME
ODDS_API_KEY=REPLACE_ME
ENV
chmod 600 /opt/betting-copilot/.env

echo ""
echo "==> Bootstrap complete."
echo "    Next steps:"
echo "    1. Edit /opt/betting-copilot/.env with real credentials"
echo "    2. Run: doctl registry login  (to authenticate to DO Container Registry)"
echo "    3. Set up cron: crontab -e, then add:"
echo "       TZ=America/New_York"
echo "       0 9 * * * docker exec betting-copilot-api python ingest/capture_odds.py >> /var/log/capture_odds.log 2>&1"
REMOTE
}

###############################################################################
# DEPLOY — build, push, and restart
###############################################################################
deploy() {
    REGISTRY=${3:-}
    IMAGE_TAG=${4:-$(git rev-parse --short HEAD)}

    if [[ -z "$REGISTRY" ]]; then
        echo "Error: registry required for deploy"
        echo "  $0 deploy <droplet-ip> <registry> [image-tag]"
        exit 1
    fi

    IMAGE="${REGISTRY}/betting-copilot:${IMAGE_TAG}"
    IMAGE_LATEST="${REGISTRY}/betting-copilot:latest"

    echo "==> Building image for linux/amd64 (required for DO Droplets from Apple Silicon)"
    docker buildx build \
        --platform linux/amd64 \
        --tag "$IMAGE" \
        --tag "$IMAGE_LATEST" \
        --push \
        .

    echo "==> Copying docker-compose.yml to Droplet"
    scp docker-compose.yml root@"$DROPLET_IP":/opt/betting-copilot/

    echo "==> Pulling image and restarting API on Droplet"
    ssh root@"$DROPLET_IP" bash << REMOTE
set -euo pipefail
cd /opt/betting-copilot
export REGISTRY="${REGISTRY}"
export IMAGE_TAG="${IMAGE_TAG}"
docker compose pull
docker compose up -d --no-deps api
docker image prune -f
echo "==> Deploy complete. Verifying health..."
sleep 5
curl -sf http://localhost:8000/health | python3 -m json.tool || echo "WARNING: health check failed"
REMOTE

    echo ""
    echo "==> Deployed ${IMAGE}"
    echo "    API: http://${DROPLET_IP}:8000"
    echo "    Docs: http://${DROPLET_IP}:8000/docs"
}

case "$COMMAND" in
    bootstrap) bootstrap ;;
    deploy) deploy "$@" ;;
    *)
        echo "Unknown command: $COMMAND"
        echo "Use 'bootstrap' or 'deploy'"
        exit 1
        ;;
esac
