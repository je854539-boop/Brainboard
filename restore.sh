#!/usr/bin/env bash
# Restore a backup produced by backup.sh. DESTRUCTIVE -- drops and
# recreates the database before loading the dump, so this wipes whatever
# is currently in it. Requires typing a confirmation phrase; there is no
# --force/--yes flag on purpose.
#
# Usage: ./restore.sh backups/brainboard_20260820T120000Z.sql.gz
set -euo pipefail

cd "$(dirname "$0")"

if [ $# -ne 1 ]; then
  echo "Usage: $0 <path-to-backup.sql.gz>" >&2
  exit 1
fi

DUMP_FILE="$1"
if [ ! -f "$DUMP_FILE" ]; then
  echo "Error: $DUMP_FILE not found" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1091
[ -f .env ] && source .env
set +a

PG_USER="${POSTGRES_USER:-brainboard}"
PG_DB="${POSTGRES_DB:-brainboard}"

echo "!! This will PERMANENTLY ERASE the current '$PG_DB' database and"
echo "!! replace it with the contents of: $DUMP_FILE"
echo "!! This cannot be undone. If you want a safety net, run ./backup.sh"
echo "!! first to snapshot the current (about-to-be-erased) state."
echo ""
read -rp "Type the database name ($PG_DB) to confirm: " CONFIRM
if [ "$CONFIRM" != "$PG_DB" ]; then
  echo "Confirmation did not match. Aborted, nothing was touched."
  exit 1
fi

echo "==> Dropping and recreating '$PG_DB'"
docker compose exec -T postgres psql -U "$PG_USER" -d postgres -c "DROP DATABASE IF EXISTS \"$PG_DB\";"
docker compose exec -T postgres psql -U "$PG_USER" -d postgres -c "CREATE DATABASE \"$PG_DB\";"

echo "==> Loading $DUMP_FILE"
gunzip -c "$DUMP_FILE" | docker compose exec -T postgres psql -U "$PG_USER" -d "$PG_DB"

echo "==> Restore complete. Current Alembic revision:"
docker compose exec -T api alembic current
