#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
RUNS_DIR="$SCRIPT_DIR/.translator-runs"
LAST_RUN_FILE="$RUNS_DIR/last-run"

if [[ ! -f "$LAST_RUN_FILE" ]]; then
  echo "No previous run has been started yet."
  exit 1
fi

RUN_DIR="$(< "$LAST_RUN_FILE")"
STATUS_FILE="$RUN_DIR/status.txt"
PID_FILE="$RUN_DIR/pid"
LOG_FILE="$RUN_DIR/output.log"
COMMAND_FILE="$RUN_DIR/command.sh"

if [[ ! -d "$RUN_DIR" ]]; then
  echo "The last run directory no longer exists: $RUN_DIR"
  exit 1
fi

echo "Last run: $RUN_DIR"

if [[ -f "$PID_FILE" ]]; then
  PID="$(< "$PID_FILE")"
  if ps -p "$PID" > /dev/null 2>&1; then
    echo "Process: running"
    echo "PID: $PID"
  else
    echo "Process: not running"
    echo "PID: $PID"
  fi
else
  echo "Process: unknown"
fi

echo
echo "Status:"
if [[ -f "$STATUS_FILE" ]]; then
  sed 's/^/  /' "$STATUS_FILE"
else
  echo "  status file not found"
fi

echo
echo "Command:"
if [[ -f "$COMMAND_FILE" ]]; then
  sed 's/^/  /' "$COMMAND_FILE"
else
  echo "  command file not found"
fi

echo
echo "Last 40 log lines:"
if [[ -f "$LOG_FILE" ]]; then
  tail -n 40 "$LOG_FILE"
else
  echo "log file not found"
fi
