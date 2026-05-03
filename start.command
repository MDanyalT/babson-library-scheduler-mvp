#!/bin/bash
# Babson Library Scheduler — Mac launcher
# Double-click this file to start the server.

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$SCRIPT_DIR/babson-scheduler"

echo ""
echo "========================================="
echo "  Babson Library Scheduler"
echo "========================================="
echo ""

# Move into the app directory
cd "$APP_DIR"

# Check that .venv exists
if [ ! -d ".venv" ]; then
  echo "ERROR: Virtual environment not found."
  echo ""
  echo "Run this once to set it up:"
  echo "  cd $APP_DIR"
  echo "  python3.11 -m venv .venv"
  echo "  source .venv/bin/activate"
  echo "  python -m pip install --upgrade pip"
  echo "  python -m pip install -r requirements.txt"
  echo ""
  read -p "Press Enter to close..."
  exit 1
fi

# Activate virtual environment
source .venv/bin/activate

echo "Starting server..."
echo ""
echo "  Admin UI  ->  http://localhost:8000/api/v1/admin/ui"
echo "  API Docs  ->  http://localhost:8000/docs"
echo ""
echo "Press Ctrl+C to stop the server."
echo ""

python -m uvicorn app.main:app --reload --port 8000
