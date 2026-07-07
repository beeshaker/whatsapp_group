#!/usr/bin/env bash
# Update all client backends to the latest code from this repo.
#
# What it does:
#   1. git pull on the repo
#   2. For each client in /opt/clients/, copies backend source files
#   3. Rebuilds only the backend Docker image
#   4. Restarts only the backend container (openwa is untouched — no QR re-scan)
#
# Usage:
#   ./update-clients.sh                  # update all clients
#   ./update-clients.sh acme riverside   # update specific clients only

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
CLIENTS_DIR="/opt/clients"

# ── 1. Pull latest code ───────────────────────────────────────────────────────
echo "==> Pulling latest code..."
git -C "$REPO_DIR" pull

# ── 2. Determine which clients to update ─────────────────────────────────────
if [ $# -gt 0 ]; then
  CLIENTS=("$@")
else
  mapfile -t CLIENTS < <(
    find "$CLIENTS_DIR" -mindepth 1 -maxdepth 1 -type d \
      ! -name "shared-postgres" \
      -exec basename {} \;
  )
fi

if [ ${#CLIENTS[@]} -eq 0 ]; then
  echo "No clients found in $CLIENTS_DIR — nothing to do."
  exit 0
fi

echo "==> Clients to update: ${CLIENTS[*]}"

# ── 3. Update each client ─────────────────────────────────────────────────────
FAILED=()

for CLIENT in "${CLIENTS[@]}"; do
  CLIENT_DIR="$CLIENTS_DIR/$CLIENT"

  if [ ! -d "$CLIENT_DIR" ]; then
    echo ""
    echo "  [SKIP] $CLIENT — directory not found at $CLIENT_DIR"
    continue
  fi

  echo ""
  echo "──────────────────────────────────────────"
  echo "  Updating: $CLIENT"
  echo "──────────────────────────────────────────"

  # Copy backend source (preserves .env and volumes, replaces only code)
  rsync -a --delete \
    --exclude='.env' \
    "$REPO_DIR/backend/" "$CLIENT_DIR/backend/"

  # Rebuild backend image and restart only that service
  if ! (cd "$CLIENT_DIR" && docker compose build backend && docker compose up -d --no-deps backend); then
    echo "  [ERROR] Failed to update $CLIENT"
    FAILED+=("$CLIENT")
  else
    echo "  [OK] $CLIENT backend updated and restarted"
  fi
done

# ── 4. Summary ────────────────────────────────────────────────────────────────
echo ""
echo "══════════════════════════════════════════"
echo "  Update complete"
echo "  Clients updated: ${#CLIENTS[@]}"
if [ ${#FAILED[@]} -gt 0 ]; then
  echo "  FAILED: ${FAILED[*]}"
  exit 1
else
  echo "  All succeeded"
fi
echo "══════════════════════════════════════════"
