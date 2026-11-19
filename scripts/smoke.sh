#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
repo=$(pwd)
work=$(mktemp -d)
optimizer_pid=
web_pid=
cleanup() {
  if [ -n "$web_pid" ]; then kill "$web_pid" 2>/dev/null || true; fi
  if [ -n "$optimizer_pid" ]; then kill "$optimizer_pid" 2>/dev/null || true; fi
  rm -rf "$work"
}
trap cleanup EXIT
trap 'exit 1' INT TERM
python_bin=${PYTHON_BIN:-"$repo/.venv/bin/python"}
web_port=${SMOKE_WEB_PORT:-18080}
optimizer_port=${SMOKE_OPTIMIZER_PORT:-18081}
solver=${SMOKE_SOLVER:-demo}
(cd web && go build -o "$work/shift-web" .)
OPTIMIZER_SOLVER="$solver" DEMO_DATA_PATH="$repo/contracts/demo.json" PYTHONPATH="$repo/optimizer" \
  "$python_bin" -m uvicorn shift_optimizer.api:create_app --factory --host 127.0.0.1 \
  --port "$optimizer_port" >"$work/optimizer.log" 2>&1 &
optimizer_pid=$!
DATA_DIR="$work/data" DEMO_DATA_PATH="$repo/contracts/demo.json" PORT="$web_port" \
  OPTIMIZER_URL="http://127.0.0.1:$optimizer_port" \
  "$work/shift-web" >"$work/web.log" 2>&1 &
web_pid=$!
if ! SMOKE_SOLVER="$solver" SMOKE_WEB_URL="http://127.0.0.1:$web_port" "$python_bin" scripts/smoke.py; then
  cat "$work/optimizer.log" "$work/web.log"
  exit 1
fi
