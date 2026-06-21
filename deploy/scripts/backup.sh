#!/usr/bin/env bash
# Daily backup of all client databases.
# Crontab: 0 2 * * * /opt/whatsapp-ticketing/deploy/scripts/backup.sh
set -euo pipefail

SHARED_PG_DIR="/opt/clients/shared-postgres"
BACKUP_ROOT="/opt/backups"
DATE=$(date +%Y%m%d_%H%M)
BACKUP_DIR="$BACKUP_ROOT/$DATE"

mkdir -p "$BACKUP_DIR"
cd "$SHARED_PG_DIR"

echo "==> Backing up all client databases to $BACKUP_DIR"

DATABASES=$(docker compose exec -T postgres psql -U ops_user -t -c \
  "SELECT datname FROM pg_database WHERE datname LIKE 'client_%' ORDER BY datname;")

for db in $DATABASES; do
  db=$(echo "$db" | xargs)
  [ -z "$db" ] && continue
  echo "    $db..."
  docker compose exec -T postgres pg_dump -U ops_user "$db" > "$BACKUP_DIR/$db.sql"
done

# Remove backups older than 30 days
find "$BACKUP_ROOT" -maxdepth 1 -type d -mtime +30 -exec rm -rf {} + 2>/dev/null || true

echo "==> Done. Backup saved to $BACKUP_DIR"
