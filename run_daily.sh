#!/usr/bin/env bash
# Usage: ./run_daily.sh [YYYY-MM-DD]   (defaults to today)
# chmod +x run_daily.sh
set -euo pipefail

DATE=${1:-$(date +%Y-%m-%d)}
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

cd "$SCRIPT_DIR"
source .venv/bin/activate

echo "========================================"
echo "  Betting Copilot — Daily Run: $DATE"
echo "========================================"

echo ""
echo "[Step 1/3] Ingest"
python ingest/run_ingest.py --date "$DATE"

echo ""
echo "[Step 2/3] Predict"
python models/predict.py --date "$DATE"

echo ""
echo "[Step 3/3] Recommend"
python recs/run_recs.py --date "$DATE"

echo ""
echo "========================================"
echo "  Daily run complete for $DATE"
echo "========================================"
