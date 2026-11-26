#!/bin/bash
# run_maint.sh — entry for cron. Routes to the right python script.
# Usage: run_maint.sh {daily|weekly-dry|weekly-apply}
set -e
SCRIPT_DIR="/opt/cortex-deploy/scripts"
APPLY_FLAG="/opt/cortex-deploy/config/maint_apply_enabled"
MODE="$1"

# weekly-apply is gated by flag file to prevent accidental destructive cron
if [ "$MODE" = "weekly-apply" ] && [ ! -f "$APPLY_FLAG" ]; then
  echo "[$(date -Iseconds)] weekly-apply skipped: $APPLY_FLAG missing" \
    >> /opt/cortex-deploy/logs/apply_skip.log
  exit 0
fi

case "$MODE" in
  daily)         cd "$SCRIPT_DIR" && exec python3 cortex_maint_daily.py ;;
  weekly-dry)    cd "$SCRIPT_DIR" && exec python3 cortex_maint_weekly.py --mode=dry ;;
  weekly-apply)  cd "$SCRIPT_DIR" && exec python3 cortex_maint_weekly.py --mode=apply ;;
  *)
    echo "usage: $0 {daily|weekly-dry|weekly-apply}" >&2
    exit 2
    ;;
esac
