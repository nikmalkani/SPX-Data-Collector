#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="/home/ubuntu/SPX-Data-Collector"
DEFAULT_DB_PATH="${REPO_DIR}/spx_options.db"
BACKUP_DIR="${REPO_DIR}/backups/sqlite"
KEEP_DAYS="${KEEP_DAYS:-14}"

db_url="${DB_URL:-sqlite:////home/ubuntu/SPX-Data-Collector/spx_options.db}"

case "$db_url" in
  sqlite:////*)
    db_path="/${db_url#sqlite:////}"
    ;;
  sqlite:///*)
    db_path="${db_url#sqlite:///}"
    ;;
  *)
    echo "backup_sqlite.sh only supports sqlite DB_URL values. Got: ${db_url}" >&2
    exit 1
    ;;
esac

if [[ -z "$db_path" ]]; then
  db_path="$DEFAULT_DB_PATH"
fi

if [[ ! -f "$db_path" ]]; then
  echo "SQLite DB not found at ${db_path}" >&2
  exit 1
fi

mkdir -p "$BACKUP_DIR"

timestamp="$(date -u +%Y-%m-%d_%H%M%S)"
backup_path="${BACKUP_DIR}/spx_options.db.bak.${timestamp}"

cp "$db_path" "$backup_path"
find "$BACKUP_DIR" -type f -name 'spx_options.db.bak.*' -mtime +"$KEEP_DAYS" -delete

echo "Created backup at ${backup_path}"
