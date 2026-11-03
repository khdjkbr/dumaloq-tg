#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

if [[ ! -x .venv/bin/python ]] \
  || ! .venv/bin/python -c "import aiogram, dotenv" >/dev/null 2>&1 \
  || ! command -v "${FFMPEG_BIN:-ffmpeg}" >/dev/null \
  || ! command -v "${FFPROBE_BIN:-ffprobe}" >/dev/null; then
  bash scripts/setup.sh
fi

exec .venv/bin/python -u bot.py
