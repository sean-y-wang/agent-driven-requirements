#!/usr/bin/env bash
# Generate + deliver daily/weekly confirmation cards and manager reports.
# Intended for cron on the production host (Asia/Shanghai). Credentials via env.
set -euo pipefail

ROOT="${ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
VENV_BIN="${VENV_BIN:-$ROOT/.venv/bin}"
CONFIG="${BOT_CONFIG:-$ROOT/config/roles.yaml}"
MEMBERS="${REPORT_MEMBERS:-$ROOT/private/members.json}"
MANAGER_RECIPIENTS="${REPORT_MANAGER_RECIPIENTS:-$ROOT/private/managers.json}"
DB="${REPORT_DB_PATH:-$ROOT/data/reports.db}"
LEDGER="${REPORT_LEDGER_PATH:-$DB}"
OUTBOX_DIR="${REPORT_OUTBOX_DIR:-$ROOT/data/outbox}"
NOW_ARG=()
if [[ -n "${REPORT_NOW:-}" ]]; then
  NOW_ARG=(--now "$REPORT_NOW")
fi
DRY=()
if [[ "${REPORT_DRY_RUN:-0}" == "1" ]]; then
  DRY=(--dry-run)
fi

mkdir -p "$(dirname "$DB")" "$OUTBOX_DIR"
STAMP=$(date +%Y%m%d-%H%M%S)
OUTBOX_FILE="$OUTBOX_DIR/outbox-$STAMP.json"

CMD=("$VENV_BIN/requirements-deliver")
if [[ ! -x "${CMD[0]}" ]]; then
  CMD=("$VENV_BIN/python" "-m" "bitable_bot.requirements_v09.send_adapter")
fi

ARGS=(
  --config "$CONFIG"
  --members "$MEMBERS"
  --db "$DB"
  --ledger "$LEDGER"
  --write-outbox "$OUTBOX_FILE"
  "${NOW_ARG[@]}"
  "${DRY[@]}"
)
if [[ -f "$MANAGER_RECIPIENTS" ]]; then
  ARGS+=(--manager-recipients "$MANAGER_RECIPIENTS")
fi
if [[ -n "${REPORT_MANAGER_CHAT:-}" ]]; then
  ARGS+=(--manager-chat "$REPORT_MANAGER_CHAT")
fi

exec "${CMD[@]}" "${ARGS[@]}"
