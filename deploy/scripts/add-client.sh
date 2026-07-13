#!/usr/bin/env bash
# Usage: ./add-client.sh <client-name> <backend-port> <openwa-port>
# Example: ./add-client.sh acme 8001 2001
set -euo pipefail

CLIENT="${1:?Usage: $0 <client-name> <backend-port> <openwa-port>}"
BACKEND_PORT="${2:?Usage: $0 <client-name> <backend-port> <openwa-port>}"
OPENWA_PORT="${3:?Usage: $0 <client-name> <backend-port> <openwa-port>}"

REPO_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
CLIENTS_DIR="/opt/clients"
CLIENT_DIR="$CLIENTS_DIR/$CLIENT"
SHARED_PG_DIR="$CLIENTS_DIR/shared-postgres"
NGINX_PORTS="/etc/nginx/client-ports.conf"

echo "==> Adding client: $CLIENT (backend :$BACKEND_PORT, openwa :$OPENWA_PORT)"

# ── 1. Create client directory ────────────────────────────────────────────────
if [ -d "$CLIENT_DIR" ]; then
  echo "ERROR: $CLIENT_DIR already exists. Aborting."
  exit 1
fi
mkdir -p "$CLIENT_DIR"

# Copy source code (backend + openwa build contexts)
cp -r "$REPO_DIR/backend" "$CLIENT_DIR/backend"
cp -r "$REPO_DIR/openwa"  "$CLIENT_DIR/openwa"

# Copy docker-compose template
cp "$REPO_DIR/deploy/client-template/docker-compose.yml" "$CLIENT_DIR/docker-compose.yml"

# ── 2. Generate .env from template ───────────────────────────────────────────
GATEWAY_TOKEN=$(openssl rand -hex 32)
SECRET_KEY=$(openssl rand -hex 32)
OPENWA_API_KEY=$(openssl rand -hex 32)

sed \
  -e "s/CLIENTNAME/$CLIENT/g" \
  -e "s/BACKEND_PORT=8001/BACKEND_PORT=$BACKEND_PORT/" \
  -e "s/OPENWA_PORT=2001/OPENWA_PORT=$OPENWA_PORT/" \
  -e "s/GENERATE_RANDOM_TOKEN/$GATEWAY_TOKEN/" \
  "$REPO_DIR/deploy/client-template/.env.template" > "$CLIENT_DIR/.env.tmp"

# Replace the second and third GENERATE_RANDOM_TOKEN occurrences individually
sed -i "0,/GENERATE_RANDOM_TOKEN/! { 0,/GENERATE_RANDOM_TOKEN/ s/GENERATE_RANDOM_TOKEN/$(openssl rand -hex 32)/ }" "$CLIENT_DIR/.env.tmp" || true
sed -i "0,/GENERATE_RANDOM_TOKEN/! { 0,/GENERATE_RANDOM_TOKEN/ s/GENERATE_RANDOM_TOKEN/$OPENWA_API_KEY/ }" "$CLIENT_DIR/.env.tmp" || true
mv "$CLIENT_DIR/.env.tmp" "$CLIENT_DIR/.env"

echo ""
echo "==> .env created at $CLIENT_DIR/.env"
echo "    IMPORTANT: Set these before starting:"
echo "      POSTGRES_PASSWORD   (match shared-postgres .env)"
echo "      ADMIN_PASSWORD"
echo "      SUPER_ADMIN_PASSWORD"
echo "      OLLAMA_MODEL        (if different from qwen2.5:7b)"
echo "      DASHBOARD_URL       (your actual domain)"
echo ""

# ── 3. Create PostgreSQL database ────────────────────────────────────────────
echo "==> Creating database: client_$CLIENT"
cd "$SHARED_PG_DIR"
docker compose exec postgres psql -U ops_user -c "CREATE DATABASE client_$CLIENT;" 2>/dev/null \
  && echo "    Database created." \
  || echo "    Database may already exist — continuing."

# ── 4. Register port in Nginx ─────────────────────────────────────────────────
if grep -q "^    $CLIENT " "$NGINX_PORTS" 2>/dev/null; then
  echo "==> Nginx port entry already exists for $CLIENT — skipping."
else
  echo "==> Adding $CLIENT → $BACKEND_PORT to $NGINX_PORTS"
  # Insert before the closing brace
  sed -i "s/^    default.*$/    $CLIENT      $BACKEND_PORT;\n    default   $BACKEND_PORT;/" "$NGINX_PORTS"
  nginx -t && systemctl reload nginx
  echo "    Nginx reloaded."
fi

# ── 5. Build and start ────────────────────────────────────────────────────────
echo ""
echo "==> Building and starting $CLIENT..."
cd "$CLIENT_DIR"
docker compose build
docker compose up -d

# ── 6. Attach billing dashboard to this client's Docker network ───────────────
# OpenWA's port is bound to 127.0.0.1 only (see client-template/docker-compose.yml),
# so billing must reach it via container DNS on this client's network, not
# host.docker.internal (which can't reach a loopback-bound port from another
# container even though it resolves fine).
echo ""
echo "==> Connecting billing-app to ${CLIENT}'s Docker network..."
if docker inspect billing-app >/dev/null 2>&1; then
  CLIENT_NET=$(docker inspect "${CLIENT}-openwa-1" \
    --format '{{range $net, $cfg := .NetworkSettings.Networks}}{{$net}}{{end}}' 2>/dev/null || true)
  if [ -z "$CLIENT_NET" ]; then
    echo "    WARNING: couldn't determine ${CLIENT}-openwa-1's network — connect billing-app manually:"
    echo "      docker network connect <network> billing-app"
  elif docker network connect "$CLIENT_NET" billing-app 2>/dev/null; then
    echo "    Connected billing-app -> $CLIENT_NET"
  else
    echo "    billing-app already attached to $CLIENT_NET — continuing."
  fi
  echo "    In the billing client record, set OpenWA URL to: http://${CLIENT}-openwa-1:2785"
else
  echo "    billing-app not found on this host — skipping (attach manually if/when billing is deployed here)."
fi

echo ""
echo "╔══════════════════════════════════════════════════════════╗"
echo "║  Client '$CLIENT' is starting up.                        "
echo "║                                                           "
echo "║  Next: scan WhatsApp QR code                             "
echo "║  On your server, tunnel the OpenWA port:                 "
echo "║    ssh -L ${OPENWA_PORT}:localhost:${OPENWA_PORT} deploy@YOUR_SERVER_IP"
echo "║  Then run setup.sh (update API port to $OPENWA_PORT)     "
echo "║                                                           "
echo "║  Dashboard will be at:                                    "
echo "║    https://$CLIENT.yourdomain.com                        "
echo "╚══════════════════════════════════════════════════════════╝"
