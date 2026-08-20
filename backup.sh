#!/usr/bin/env bash
# Full Postgres backup -- run this ON THE VPS, either by hand or on a cron
# schedule (see the crontab line printed at the bottom of this file's
# header comment, and in README.md's Backups section).
#
# Dumps the entire brainboard database (every lead, every activity ledger
# entry, every hazard snapshot -- everything) to a gzip'd SQL file under
# ./backups/, then deletes local backups older than BACKUP_RETENTION_DAYS
# (default 14). Safe to run while the app is live -- pg_dump takes a
# consistent snapshot without blocking reads/writes.
#
# This does NOT replace off-box storage. A backup that lives on the same
# disk as the database it backs up does not survive that disk failing, the
# VPS being destroyed, or the account being compromised. Copy ./backups/
# somewhere else too (rclone/rsync to S3, DigitalOcean Spaces, another
# machine -- anything off this box) once this is generating real backups
# you'd actually need. See README.md's Backups section for a copy-paste
# rclone example.
#
# Suggested crontab entry for every 6 hours (edit with `crontab -e`):
#   0 */6 * * * cd /path/to/Brainboard && ./backup.sh >> backup.log 2>&1
set -euo pipefail

cd "$(dirname "$0")"

BACKUP_DIR="./backups"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT_FILE="${BACKUP_DIR}/brainboard_${TIMESTAMP}.sql.gz"

mkdir -p "$BACKUP_DIR"

set -a
# shellcheck disable=SC1091
[ -f .env ] && source .env
set +a

PG_USER="${POSTGRES_USER:-brainboard}"
PG_DB="${POSTGRES_DB:-brainboard}"

echo "==> Dumping database '$PG_DB' to $OUT_FILE"
docker compose exec -T postgres pg_dump -U "$PG_USER" "$PG_DB" | gzip > "$OUT_FILE"

SIZE=$(du -h "$OUT_FILE" | cut -f1)
echo "==> Backup complete: $OUT_FILE ($SIZE)"

echo "==> Pruning local backups older than ${RETENTION_DAYS} days"
find "$BACKUP_DIR" -name 'brainboard_*.sql.gz' -mtime "+${RETENTION_DAYS}" -print -delete

echo "==> Current local backups:"
ls -lh "$BACKUP_DIR"/brainboard_*.sql.gz 2>/dev/null || echo "  (none yet)"
