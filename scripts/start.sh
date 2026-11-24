#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
exec 9>.deploy.lock
flock -n 9 || { echo "Another deployment is running"; exit 1; }
test -z "$(git status --porcelain --untracked-files=no)" || { echo "Tracked files have local changes"; exit 1; }
git fetch origin main
git checkout main
git merge --ff-only origin/main
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
export PYTHONPATH="$PWD/src"
export MENU_DATABASE="${MENU_DATABASE:-$PWD/db.sqlite3}"
export MENU_LOG_DIR="${MENU_LOG_DIR:-/opt/ai-menu/logs}"
mkdir -p "$MENU_LOG_DIR"
pidfile="$PWD/.server.pid"
source scripts/process.sh
stop_previous_server "$pidfile" "$PWD"
.venv/bin/python -m ai_menu.migrate
logdate="$(TZ=Asia/Shanghai date +%F)"
nohup .venv/bin/python -u -m ai_menu.server >>"$MENU_LOG_DIR/$logdate.log" 2>&1 9>&- &
pid=$!
echo "$pid" > "$pidfile"
for _ in {1..30}; do
  kill -0 "$pid" 2>/dev/null || { echo "Startup failed; inspect $MENU_LOG_DIR"; exit 1; }
  if .venv/bin/python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:${MENU_PORT:-8000}/', timeout=1)" 2>/dev/null; then
    echo "Started PID $pid; logs: $MENU_LOG_DIR"
    exit 0
  fi
  sleep 1
done
echo "Health check timed out; inspect $MENU_LOG_DIR"
exit 1
