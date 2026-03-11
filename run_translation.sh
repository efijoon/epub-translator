#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_PATH="$SCRIPT_DIR/$(basename -- "${BASH_SOURCE[0]}")"
RUNS_DIR="$SCRIPT_DIR/.translator-runs"
LAST_RUN_FILE="$RUNS_DIR/last-run"
declare -a TRANSLATOR_CMD=()

print_usage() {
  cat <<'EOF'
Usage: ./run_translation.sh [input] [output] [extra epub-fa-translator args...]

Starts a detached translation run that keeps going after SSH disconnects.

Defaults:
  input  = ./denial-of-death.pdf
  output = ./book.epub

Examples:
  ./run_translation.sh
  ./run_translation.sh "/srv/books/book.pdf" "/srv/books/book.fa.epub"
  ./run_translation.sh "/srv/books/book.pdf" "/srv/books/book.fa.epub" --force
EOF
}

abs_path() {
  python3 - "$1" <<'PY'
from pathlib import Path
import sys

print(Path(sys.argv[1]).expanduser().resolve())
PY
}

slugify() {
  printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9]+/-/g; s/^-+//; s/-+$//'
}

resolve_translator_cmd() {
  if command -v uv > /dev/null 2>&1; then
    TRANSLATOR_CMD=(uv run epub-fa-translator)
    return 0
  fi

  if [[ -x "$SCRIPT_DIR/.venv/bin/epub-fa-translator" ]]; then
    TRANSLATOR_CMD=("$SCRIPT_DIR/.venv/bin/epub-fa-translator")
    return 0
  fi

  if [[ -x "$SCRIPT_DIR/.venv/bin/python" ]]; then
    TRANSLATOR_CMD=(env "PYTHONPATH=$SCRIPT_DIR/src${PYTHONPATH:+:$PYTHONPATH}" "$SCRIPT_DIR/.venv/bin/python" -m epub_fa_translator.main)
    return 0
  fi

  if command -v epub-fa-translator > /dev/null 2>&1; then
    TRANSLATOR_CMD=("$(command -v epub-fa-translator)")
    return 0
  fi

  cat <<'EOF'
No supported translator launcher was found.

Supported options:
  1. Install uv, then run: uv sync
  2. Or create a local virtualenv and install the project:
     python3 -m venv .venv
     ./.venv/bin/pip install -U pip
     ./.venv/bin/pip install -e .

Then run ./run_translation.sh again.
EOF
  exit 1
}

is_pid_running() {
  local pid="${1:-}"
  [[ -n "$pid" ]] && ps -p "$pid" > /dev/null 2>&1
}

write_status_file() {
  local status_file="$1"
  local state="$2"
  local started_at="$3"
  local finished_at="$4"
  local exit_code="$5"
  local pid="$6"
  local input_path="$7"
  local output_path="$8"
  local work_dir="$9"
  local log_file="${10}"

  {
    printf 'state=%s\n' "$state"
    printf 'started_at=%s\n' "$started_at"
    if [[ -n "$finished_at" ]]; then
      printf 'finished_at=%s\n' "$finished_at"
    fi
    if [[ -n "$exit_code" ]]; then
      printf 'exit_code=%s\n' "$exit_code"
    fi
    if [[ -n "$pid" ]]; then
      printf 'pid=%s\n' "$pid"
    fi
    printf 'input=%s\n' "$input_path"
    printf 'output=%s\n' "$output_path"
    printf 'work_dir=%s\n' "$work_dir"
    printf 'log=%s\n' "$log_file"
  } > "$status_file"
}

run_internal() {
  local run_dir="$1"
  local input_path="$2"
  local output_path="$3"
  local work_dir="$4"
  shift 4

  local status_file="$run_dir/status.txt"
  local meta_file="$run_dir/meta.txt"
  local command_file="$run_dir/command.sh"
  local log_file="$run_dir/output.log"
  local pid_file="$run_dir/pid"
  local pid=""
  local started_at
  local finished_at
  local exit_code
  local state="finished"
  local -a cmd
  local -a exec_cmd

  if [[ -f "$pid_file" ]]; then
    pid="$(< "$pid_file")"
  fi

  resolve_translator_cmd

  mkdir -p "$run_dir" "$work_dir"
  started_at="$(date -Is)"

  write_status_file "$status_file" "running" "$started_at" "" "" "$pid" "$input_path" "$output_path" "$work_dir" "$log_file"

  {
    printf 'run_dir=%s\n' "$run_dir"
    printf 'input=%s\n' "$input_path"
    printf 'output=%s\n' "$output_path"
    printf 'work_dir=%s\n' "$work_dir"
    printf 'log=%s\n' "$log_file"
    if [[ -n "$pid" ]]; then
      printf 'pid=%s\n' "$pid"
    fi
  } > "$meta_file"

  cmd=(
    "${TRANSLATOR_CMD[@]}"
    "$input_path"
    "$output_path"
    --env-file "$SCRIPT_DIR/.env"
    --context-file "$SCRIPT_DIR/translation-context.txt"
    --anchor-scan-chapters "0"
    --anchor-max-terms "120"
    --anchor-review-interval "3"
    --work-dir "$work_dir"
  )

  if (($#)); then
    cmd+=("$@")
  fi

  exec_cmd=(
    env
    "PYTHONUNBUFFERED=1"
    stdbuf -oL -eL
    "${cmd[@]}"
  )

  {
    printf '#!/usr/bin/env bash\n'
    printf 'cd %q\n' "$SCRIPT_DIR"
    printf 'exec '
    printf '%q ' "${exec_cmd[@]}"
    printf '\n'
  } > "$command_file"
  chmod +x "$command_file"

  cd "$SCRIPT_DIR"

  printf 'Started at: %s\n' "$started_at"
  printf 'Run dir: %s\n' "$run_dir"
  printf 'Input: %s\n' "$input_path"
  printf 'Output: %s\n' "$output_path"
  printf 'Work dir: %s\n' "$work_dir"
  printf 'Command: '
  printf '%q ' "${exec_cmd[@]}"
  printf '\n\n'

  set +e
  "${exec_cmd[@]}"
  exit_code=$?
  set -e

  finished_at="$(date -Is)"
  if (( exit_code != 0 )); then
    state="failed"
  fi

  write_status_file "$status_file" "$state" "$started_at" "$finished_at" "$exit_code" "$pid" "$input_path" "$output_path" "$work_dir" "$log_file"
  return "$exit_code"
}

main() {
  local input_arg
  local output_arg
  local input_path
  local output_path
  local job_name
  local work_dir
  local run_id
  local run_dir
  local log_file
  local status_file
  local pid_file
  local pid
  local previous_run_dir
  local previous_pid
  local previous_pid_file
  local -a extra_args

  if [[ "${1:-}" == "--internal-run" ]]; then
    shift
    run_internal "$@"
    return "$?"
  fi

  if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    print_usage
    return 0
  fi

  resolve_translator_cmd

  mkdir -p "$RUNS_DIR"

  input_arg="${1:-$SCRIPT_DIR/denial-of-death.pdf}"
  output_arg="${2:-$SCRIPT_DIR/book.epub}"

  if (($# >= 1)); then
    shift
  fi
  if (($# >= 1)); then
    shift
  fi
  extra_args=("$@")

  input_path="$(abs_path "$input_arg")"
  output_path="$(abs_path "$output_arg")"

  if [[ ! -f "$input_path" ]]; then
    echo "Input file not found: $input_path"
    exit 1
  fi

  if [[ ! -f "$SCRIPT_DIR/.env" ]]; then
    echo "Missing env file: $SCRIPT_DIR/.env"
    exit 1
  fi

  if [[ ! -f "$SCRIPT_DIR/translation-context.txt" ]]; then
    echo "Missing context file: $SCRIPT_DIR/translation-context.txt"
    exit 1
  fi

  mkdir -p "$(dirname -- "$output_path")"

  job_name="$(slugify "$(basename -- "${output_path%.*}")")"
  if [[ -z "$job_name" ]]; then
    job_name="translation-job"
  fi
  work_dir="$SCRIPT_DIR/.translator-work/$job_name"

  if [[ -f "$LAST_RUN_FILE" ]]; then
    previous_run_dir="$(< "$LAST_RUN_FILE")"
    previous_pid_file="$previous_run_dir/pid"

    if [[ -f "$previous_pid_file" ]]; then
      previous_pid="$(< "$previous_pid_file")"
      if is_pid_running "$previous_pid"; then
        echo "The last recorded run is still active with PID $previous_pid."
        echo "Check it with: ./check_last_run.sh"
        exit 1
      fi
    fi
  fi

  run_id="$(date -u +%Y%m%dT%H%M%SZ)"
  run_dir="$RUNS_DIR/$run_id"
  log_file="$run_dir/output.log"
  status_file="$run_dir/status.txt"
  pid_file="$run_dir/pid"

  mkdir -p "$run_dir"

  nohup "$SCRIPT_PATH" --internal-run "$run_dir" "$input_path" "$output_path" "$work_dir" "${extra_args[@]}" > "$log_file" 2>&1 < /dev/null &
  pid=$!

  printf '%s\n' "$pid" > "$pid_file"
  printf '%s\n' "$run_dir" > "$LAST_RUN_FILE"

  write_status_file "$status_file" "starting" "" "" "" "$pid" "$input_path" "$output_path" "$work_dir" "$log_file"

  echo "Started translation in the background."
  echo "PID: $pid"
  echo "Run dir: $run_dir"
  echo "Log: $log_file"
  echo "Check progress with: ./check_last_run.sh"
}

main "$@"
