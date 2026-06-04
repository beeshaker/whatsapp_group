#!/usr/bin/env bash
# One-time setup: creates session, registers webhook.
# Only needed after volume wipe or first-time install.
# Normal restarts: docker compose up -d (no setup needed)
set -euo pipefail

API="http://localhost:2785/api"
KEY="dev-admin-key"
BACKEND_SECRET="ops-gateway-secret-2026"

echo "==> Waiting for OpenWA to be ready..."
until curl -sf "$API/health" -H "X-API-Key: $KEY" > /dev/null 2>&1; do
  sleep 2
done
echo "    OpenWA is up."

# Check if a session already exists and is connected
EXISTING=$(curl -s "$API/sessions" -H "X-API-Key: $KEY" | \
  python3 -c "import sys,json; s=json.load(sys.stdin); print(next((x['id'] for x in s if x.get('status')=='ready'),''))" 2>/dev/null)

if [ -n "$EXISTING" ]; then
  SESSION_ID="$EXISTING"
  echo "==> Session already connected: $SESSION_ID"
else
  # Delete stale disconnected sessions
  STALE=$(curl -s "$API/sessions" -H "X-API-Key: $KEY" | \
    python3 -c "import sys,json; [print(x['id']) for x in json.load(sys.stdin)]" 2>/dev/null || true)
  for id in $STALE; do
    curl -s -X DELETE "$API/sessions/$id" -H "X-API-Key: $KEY" > /dev/null
  done

  echo "==> Creating session..."
  SESSION_ID=$(curl -s -X POST "$API/sessions" \
    -H "Content-Type: application/json" \
    -H "X-API-Key: $KEY" \
    -d '{"name":"opsgateway"}' | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
  echo "    Session ID: $SESSION_ID"

  echo "==> Starting session..."
  curl -s -X POST "$API/sessions/$SESSION_ID/start" -H "X-API-Key: $KEY" > /dev/null

  echo ""
  echo "==> SCAN QR CODE NOW"
  echo "    Run in another terminal: python3 -m http.server 9999 --directory /tmp"
  echo "    Then open: http://localhost:9999/qr.png"
  echo ""

  # Keep refreshing QR until session is ready
  while true; do
    STATUS=$(curl -s "$API/sessions/$SESSION_ID" -H "X-API-Key: $KEY" | \
      python3 -c "import sys,json; print(json.load(sys.stdin).get('status',''))" 2>/dev/null)

    if [ "$STATUS" = "ready" ]; then
      echo "==> Session connected!"
      break
    fi

    curl -s "$API/sessions/$SESSION_ID/qr" -H "X-API-Key: $KEY" | python3 -c "
import sys, json, base64
try:
    data = json.load(sys.stdin)
    qr = data['qrCode'].split(',', 1)[1]
    sys.stdout.buffer.write(base64.b64decode(qr))
except: pass
" > /tmp/qr.png 2>/dev/null
    echo "    QR updated at $(date +%H:%M:%S) — refresh http://localhost:9999/qr.png and scan"
    sleep 15
  done
fi

# Register webhook if not already registered
BACKEND_IP=$(docker inspect whatsapp-ticketing-backend-1 \
  --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' 2>/dev/null || echo "172.18.0.3")
WEBHOOK_URL="http://$BACKEND_IP:8000/api/v1/ops/ingest"

EXISTING_HOOK=$(curl -s "$API/sessions/$SESSION_ID/webhooks" -H "X-API-Key: $KEY" | \
  python3 -c "import sys,json; hooks=json.load(sys.stdin); print(next((h['id'] for h in hooks if h.get('active')),''))" 2>/dev/null)

if [ -n "$EXISTING_HOOK" ]; then
  echo "==> Webhook already registered: $EXISTING_HOOK"
else
  echo "==> Registering webhook -> $WEBHOOK_URL"
  curl -s -X POST "$API/sessions/$SESSION_ID/webhooks" \
    -H "Content-Type: application/json" \
    -H "X-API-Key: $KEY" \
    -d "{
      \"url\": \"$WEBHOOK_URL\",
      \"events\": [\"message.received\"],
      \"headers\": {\"X-API-Key\": \"$BACKEND_SECRET\"}
    }" | python3 -c "import sys,json; h=json.load(sys.stdin); print(f'    Webhook registered: {h.get(\"id\",\"?\")} active={h.get(\"active\",False)}')"
fi

echo ""
echo "==> Setup complete. Dashboard: http://localhost:8000/"
