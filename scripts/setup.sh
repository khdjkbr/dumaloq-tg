#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

if ! command -v ffmpeg >/dev/null || ! command -v ffprobe >/dev/null; then
  echo "Installing FFmpeg and FFprobe..."
  sudo apt-get update
  sudo apt-get install -y --no-install-recommends ffmpeg
fi

if ! python3 -m venv .venv; then
  echo "Installing Python venv support..."
  sudo apt-get update
  sudo apt-get install -y --no-install-recommends python3-venv
  python3 -m venv .venv
fi

.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -c "import aiogram; import dotenv; print('Python dependencies: OK')"
ffmpeg -version >/dev/null
ffprobe -version >/dev/null
echo "Setup complete. Run: bash scripts/start.sh"
