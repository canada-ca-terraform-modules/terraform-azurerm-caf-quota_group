#!/bin/bash
# Start the Quota Transfer web app on localhost:8000
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Create venv if it doesn't exist
if [ ! -d ".venv" ]; then
  echo "Creating virtual environment..."
  uv venv
  uv pip install -e .
fi

echo "Starting Quota Transfer app at http://localhost:8000"
echo "Press Ctrl+C to stop."
echo ""
.venv/bin/uvicorn quota_transfer_app.main:app --host 127.0.0.1 --port 8000 --reload
