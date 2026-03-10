#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
RUNS_DIR="$SCRIPT_DIR/.translator-runs"
LAST_RUN_FILE="$RUNS_DIR/last-run"
declare -a PYTHON_CMD=()

read_status_value() {
  local key="$1"
  local file="$2"
  local line

  [[ -f "$file" ]] || return 1

  while IFS= read -r line; do
    if [[ "$line" == "$key="* ]]; then
      printf '%s\n' "${line#*=}"
      return 0
    fi
  done < "$file"

  return 1
}

safe_slug() {
  python3 - "$1" <<'PY'
import re
import sys

slug = re.sub(r"[^A-Za-z0-9._-]+", "-", sys.argv[1]).strip("-")
print(slug or "chapter")
PY
}

chapter_index_from_path() {
  local path="$1"
  local name="${path##*/}"

  if [[ "$name" =~ ^([0-9]+)- ]]; then
    printf '%d\n' "$((10#${BASH_REMATCH[1]}))"
    return 0
  fi

  return 1
}

resolve_checkpoint_dir() {
  local work_dir="$1"
  local input_path="$2"
  local input_stem
  local candidate
  local -a child_dirs=()

  input_stem="$(basename -- "${input_path%.*}")"
  candidate="$work_dir/$(safe_slug "$input_stem")"

  if [[ -d "$candidate" ]]; then
    printf '%s\n' "$candidate"
    return 0
  fi

  if compgen -G "$work_dir/*.translated.xhtml" > /dev/null; then
    printf '%s\n' "$work_dir"
    return 0
  fi

  shopt -s nullglob
  child_dirs=("$work_dir"/*/)
  shopt -u nullglob

  if ((${#child_dirs[@]} == 1)); then
    printf '%s\n' "${child_dirs[0]%/}"
    return 0
  fi

  printf '%s\n' "$work_dir"
}

resolve_python_cmd() {
  if [[ -x "$SCRIPT_DIR/.venv/bin/python" ]]; then
    PYTHON_CMD=("$SCRIPT_DIR/.venv/bin/python")
    return 0
  fi

  if command -v uv > /dev/null 2>&1; then
    PYTHON_CMD=(uv run python)
    return 0
  fi

  if command -v python3 > /dev/null 2>&1; then
    PYTHON_CMD=(python3)
    return 0
  fi

  return 1
}

compute_total_chapters() {
  local input_path="$1"

  resolve_python_cmd || return 1

  (
    cd "$SCRIPT_DIR"
    env "PYTHONPATH=$SCRIPT_DIR/src${PYTHONPATH:+:$PYTHONPATH}" "${PYTHON_CMD[@]}" - "$input_path" <<'PY'
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import sys
import tempfile

from epub_fa_translator.main import collect_translation_targets
from epub_fa_translator.main import find_opf_path
from epub_fa_translator.main import parse_xml_file
from epub_fa_translator.main import prepare_source_book

input_path = Path(sys.argv[1]).resolve()

with tempfile.TemporaryDirectory(prefix="check-progress-") as tmp:
    extracted_dir = Path(tmp) / "book"
    with redirect_stdout(StringIO()):
        prepare_source_book(input_path, extracted_dir)
    opf_path = find_opf_path(extracted_dir)
    opf_tree = parse_xml_file(opf_path)
    print(len(collect_translation_targets(opf_tree, opf_path)))
PY
  )
}

if [[ ! -f "$LAST_RUN_FILE" ]]; then
  echo "No previous run has been started yet."
  exit 1
fi

RUN_DIR="$(< "$LAST_RUN_FILE")"
STATUS_FILE="$RUN_DIR/status.txt"
PID_FILE="$RUN_DIR/pid"
LOG_FILE="$RUN_DIR/output.log"
COMMAND_FILE="$RUN_DIR/command.sh"
TOTAL_CACHE_FILE="$RUN_DIR/total-chapters"
WORK_DIR="$(read_status_value "work_dir" "$STATUS_FILE" || true)"
INPUT_PATH="$(read_status_value "input" "$STATUS_FILE" || true)"
TOTAL_CHAPTERS="$(read_status_value "total_chapters" "$STATUS_FILE" || true)"

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

if [[ -n "$WORK_DIR" && -d "$WORK_DIR" ]]; then
  CHECKPOINT_DIR="$(resolve_checkpoint_dir "$WORK_DIR" "$INPUT_PATH")"
else
  CHECKPOINT_DIR=""
fi

if [[ -z "$TOTAL_CHAPTERS" && -f "$TOTAL_CACHE_FILE" ]]; then
  TOTAL_CHAPTERS="$(< "$TOTAL_CACHE_FILE")"
fi

if [[ -z "$TOTAL_CHAPTERS" && -n "$INPUT_PATH" && -f "$INPUT_PATH" ]]; then
  TOTAL_CHAPTERS="$(compute_total_chapters "$INPUT_PATH" || true)"
  if [[ "$TOTAL_CHAPTERS" =~ ^[0-9]+$ ]]; then
    printf '%s\n' "$TOTAL_CHAPTERS" > "$TOTAL_CACHE_FILE"
  else
    TOTAL_CHAPTERS=""
  fi
fi

echo
echo "Status:"
if [[ -f "$STATUS_FILE" ]]; then
  sed 's/^/  /' "$STATUS_FILE"
else
  echo "  status file not found"
fi

if [[ -n "$CHECKPOINT_DIR" && -d "$CHECKPOINT_DIR" ]]; then
  shopt -s nullglob
  TRANSLATED_FILES=("$CHECKPOINT_DIR"/*.translated.xhtml)
  SOURCE_FILES=("$CHECKPOINT_DIR"/*.source.xhtml)
  shopt -u nullglob

  TRANSLATED_COUNT=${#TRANSLATED_FILES[@]}
  SOURCE_COUNT=${#SOURCE_FILES[@]}
  LAST_COMPLETED_INDEX=0
  LAST_STARTED_INDEX=0
  LAST_COMPLETED_FILE=""
  LAST_STARTED_FILE=""
  REMAINING_COUNT=""
  COMPLETED_PERCENT=""

  for path in "${TRANSLATED_FILES[@]}"; do
    if index="$(chapter_index_from_path "$path")" && (( index > LAST_COMPLETED_INDEX )); then
      LAST_COMPLETED_INDEX=$index
      LAST_COMPLETED_FILE="$(basename -- "$path")"
    fi
  done

  for path in "${SOURCE_FILES[@]}"; do
    if index="$(chapter_index_from_path "$path")" && (( index > LAST_STARTED_INDEX )); then
      LAST_STARTED_INDEX=$index
      LAST_STARTED_FILE="$(basename -- "$path")"
    fi
  done

  echo
  echo "Checkpoint progress:"
  echo "  checkpoint_dir=$CHECKPOINT_DIR"
  echo "  translated_chapters=$TRANSLATED_COUNT"
  echo "  source_cache_files=$SOURCE_COUNT"

  if [[ "$TOTAL_CHAPTERS" =~ ^[0-9]+$ ]]; then
    REMAINING_COUNT=$(( TOTAL_CHAPTERS - TRANSLATED_COUNT ))
    if (( REMAINING_COUNT < 0 )); then
      REMAINING_COUNT=0
    fi
    COMPLETED_PERCENT=$(( TRANSLATED_COUNT * 100 / TOTAL_CHAPTERS ))
    echo "  total_chapters=$TOTAL_CHAPTERS"
    echo "  remaining_chapters=$REMAINING_COUNT"
    echo "  completed_percent=$COMPLETED_PERCENT"
    if (( LAST_STARTED_INDEX > LAST_COMPLETED_INDEX )); then
      echo "  summary=$TRANSLATED_COUNT/$TOTAL_CHAPTERS completed, $REMAINING_COUNT left, chapter $LAST_STARTED_INDEX in progress"
    else
      echo "  summary=$TRANSLATED_COUNT/$TOTAL_CHAPTERS completed, $REMAINING_COUNT left"
    fi
  fi

  if (( LAST_COMPLETED_INDEX > 0 )); then
    echo "  last_completed_index=$LAST_COMPLETED_INDEX"
    echo "  last_completed_file=$LAST_COMPLETED_FILE"
  fi

  if (( LAST_STARTED_INDEX > LAST_COMPLETED_INDEX )); then
    echo "  current_started_index=$LAST_STARTED_INDEX"
    echo "  current_started_file=$LAST_STARTED_FILE"
  fi
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
