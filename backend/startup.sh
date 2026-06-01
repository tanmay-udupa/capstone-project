#!/usr/bin/env bash
# startup.sh — install dependencies and start the API server.
# Designed for Azure App Service (Linux) or any bash environment.

set -euo pipefail

echo "==> Installing Python dependencies..."
pip install --no-cache-dir -r requirements.txt

echo "==> Copying model artefact if not present..."
if [ ! -f models/xgb_best_model.pkl ]; then
  echo "ERROR: models/xgb_best_model.pkl not found."
  echo "Copy xgb_best_model.pkl from the capstone repo root into backend/models/"
  exit 1
fi

echo "==> Starting API server..."
uvicorn main:app \
  --host 0.0.0.0 \
  --port "${PORT:-8000}" \
  --workers "${WORKERS:-1}" \
  --log-level info
