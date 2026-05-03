#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$ROOT/logs"
mkdir -p "$LOG_DIR"

day="$(date +%F)"
log_file="$LOG_DIR/group-daily-$day.log"

{
  echo "===== $(date '+%Y-%m-%d %H:%M:%S %Z') start ====="
  cd "$ROOT"
  /usr/bin/python3 scripts/group_daily_pipeline.py --config configs/group_daily.json --date yesterday
  echo "===== $(date '+%Y-%m-%d %H:%M:%S %Z') done ====="
} >>"$log_file" 2>&1

