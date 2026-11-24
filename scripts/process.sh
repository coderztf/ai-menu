#!/usr/bin/env bash
# Sourced by start.sh after obtaining the deployment lock.

is_running() {
  kill -0 "$1" 2>/dev/null || return 1
  local status
  status="$(ps -p "$1" -o stat= 2>/dev/null)" || return 1
  [[ "$status" != Z* ]]
}

verify_server() {
  local pid="$1" project="$2" args process_dir
  args="$(ps -p "$pid" -o args= 2>/dev/null)" || return 1
  [[ "$args" =~ (^|[[:space:]])-m[[:space:]]+ai_menu.server([[:space:]]|$) ]] || return 1
  process_dir="$(readlink "/proc/$pid/cwd")" || return 1
  [[ "$process_dir" == "$project" ]]
}

wait_for_exit() {
  local pid="$1" seconds="$2"
  for ((elapsed=0; elapsed<seconds; elapsed++)); do
    is_running "$pid" || return 0
    sleep 1
  done
  ! is_running "$pid"
}

stop_previous_server() {
  local pidfile="$1" project="$2" pid
  [[ -f "$pidfile" ]] || return 0
  pid="$(cat "$pidfile")"
  [[ "$pid" =~ ^[0-9]+$ && "$pid" -gt 1 ]] || {
    echo "Invalid PID file: $pidfile"; return 1;
  }
  if is_running "$pid"; then
    verify_server "$pid" "$project" || {
      echo "PID $pid is not this project's server; deployment stopped"; return 1;
    }
    echo "Stopping old server PID $pid"
    kill -TERM "$pid" || { ! is_running "$pid" || return 1; }
    if ! wait_for_exit "$pid" 30; then
      verify_server "$pid" "$project" || {
        echo "Process identity changed; deployment stopped"; return 1;
      }
      echo "Old server did not exit; sending KILL to PID $pid"
      kill -KILL "$pid" || { ! is_running "$pid" || return 1; }
      wait_for_exit "$pid" 5 || {
        echo "Unable to stop old server PID $pid"; return 1;
      }
    fi
    echo "Old server stopped"
  else
    echo "Removing stale PID file"
  fi
  rm -f -- "$pidfile"
}
