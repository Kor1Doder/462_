#!/usr/bin/env bash
# dökan CNC — one-click GUI launcher. Works on the Pi (no uv) and the dev PC.
#   ./run-gui.sh              normal window
#   ./run-gui.sh --fullscreen fullscreen (touch panel / kiosk)
#
# First run builds a venv and installs the dependencies; afterwards it just
# launches. No 'uv' needed — plain python3 venv. cncctl is imported straight
# from src/ (PYTHONPATH) so no package install / Python-version fuss.
set -euo pipefail

cd "$(dirname "$0")"          # the project folder (pitrofit)
unset PYTHONPATH || true      # drop any ROS/dev-box pollution

VENV=".venv"
PY="$VENV/bin/python"

if [ ! -x "$PY" ]; then
  echo ">> first run — creating venv and installing dependencies (a few minutes)…"
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install --upgrade pip >/dev/null
  "$VENV/bin/pip" install -r requirements.txt
fi

export PYTHONPATH="$PWD/src"  # import cncctl from src/ without installing it
echo ">> launching CNC panel…"
exec "$PY" examples/gui.py "$@"
