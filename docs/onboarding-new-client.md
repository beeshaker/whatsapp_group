# Onboarding a New Client

Each client gets their own subdomain (e.g. `pixiilive.whats2manage.com`), their own backend + OpenWA containers, and their own PostgreSQL database on the shared instance.

---

## Prerequisites

- SSH access: `ssh deploy@167.86.81.124`
- Billing dashboard: `https://whats2manage.com` (admin / changeme)
- Shared PostgreSQL running at `/opt/clients/shared-postgres`
- A dedicated WhatsApp SIM for the client's bot number
- Sudo access on the server (needed for nginx reload only)

---

## Port allocation

Pick the next free pair. Current allocations:

| Client     | Backend port | OpenWA port |
|------------|-------------|-------------|
| pixie      | 8001        | 2001        |
| nineonetwo | 8002        | 2002        | 
| pixiilive  | 8003        | 2003        |
| next       | 8004        | 2004        | 

**Naming note:** Client names must be valid hostnames — no leading digits. If a client name starts with a number (e.g. `912`), spell it out: `nineonetwo`.

---

## Step 1 — Create the database

```bash
ssh deploy@167.86.81.124
cd /opt/clients/shared-postgres
docker compose exec postgres psql -U ops_user -d postgres -c "CREATE DATABASE client_CLIENTNAME;"
```

---

## Step 2 — Create the client directory

```bash
mkdir -p /opt/clients/CLIENTNAME
cp -r /opt/whatsapp-ticketing/backend /opt/clients/CLIENTNAME/
cp -r /opt/whatsapp-ticketing/openwa /opt/clients/CLIENTNAME/
```

> **Do not copy the docker-compose.yml from the template** — it is outdated. Write it fresh in Step 4.

---

## Step 3 — Create the `.env` file

```bash
GW=$(openssl rand -hex 32)
SK=$(openssl rand -hex 32)

cat > /opt/clients/CLIENTNAME/.env << EOF
BACKEND_PORT=800X
OPENWA_PORT=200X

POSTGRES_USER=ops_user
POSTGRES_PASSWORD=CHANGE_THIS_STRONG_PASSWORD
POSTGRES_DB=client_CLIENTNAME
DATABASE_URL=postgresql+asyncpg://ops_user:CHANGE_THIS_STRONG_PASSWORD@postgres:5432/client_CLIENTNAME

BILLING_WEBHOOK_SECRET=secertbilling

GATEWAY_SECRET_TOKEN=${GW}
SECRET_KEY=${SK}

ADMIN_USERNAME=admin
ADMIN_PASSWORD=CHANGE_THIS_PASSWORD
SUPER_ADMIN_USERNAME=superadmin
SUPER_ADMIN_PASSWORD=CHANGE_THIS_PASSWORD

OPENWA_URL=http://openwa:2785
OPENWA_SESSION=opsgateway
OPENWA_API_KEY=dev-admin-key

OLLAMA_HOST=http://host.docker.internal:11434
OLLAMA_MODEL=qwen2.5:7b
OLLAMA_TIMEOUT=60
MIN_CONFIDENCE=0.80

DASHBOARD_TITLE=ClientName Incident Monitor
DASHBOARD_URL=https://CLIENTNAME.whats2manage.com
SUMMARY_TIMEZONE=Africa/Nairobi
SUMMARY_SCHEDULE_HOUR=8

CLIENT_SUBDOMAIN=CLIENTNAME
BILLING_SERVICE_URL=https://whats2manage.com
EOF
```

Replace `800X` / `200X` with the allocated ports and `CHANGE_THIS_STRONG_PASSWORD` with the shared postgres password (copy from `/opt/clients/pixie/.env`).

---

## Step 4 — Create `docker-compose.yml`

The template file is outdated. Always write this from scratch:

```bash
cat > /opt/clients/CLIENTNAME/docker-compose.yml << 'EOF'
services:
  backend:
    build:
      context: ./backend
    restart: always
    ports:
      - "127.0.0.1:${BACKEND_PORT}:8000"
    env_file: .env
    extra_hosts:
      - "host.docker.internal:host-gateway"
    volumes:
      - media_data:/app/media
    networks:
      - client-net
      - shared-db

  openwa:
    build:
      context: ./openwa
    restart: always
    ports:
      - "127.0.0.1:${OPENWA_PORT}:2785"
    environment:
      - API_MASTER_KEY=${OPENWA_API_KEY}
    env_file: .env
    volumes:
      - openwa_data:/app/data
    networks:
      - client-net
      - services-net
    depends_on:
      - backend

networks:
  client-net:
    driver: bridge
  shared-db:
    external: true
  services-net:
    external: true

volumes:
  openwa_data:
  media_data:
EOF
```

The `shared-db` network is what lets the backend reach the shared postgres container. Without it the backend will crash-loop with a DNS resolution error.

---

## Step 5 — Add to nginx port map

```bash
sudo nano /etc/nginx/conf.d/client-ports.conf
```

Add the new client line inside the map block:

```nginx
map $client $backend_port {
    Pixii        8001;
    nineonetwo   8002;
    pixiilive    8003;
    CLIENTNAME   800X;
    default      8001;
}
```

Reload nginx:

```bash
sudo nginx -t && sudo systemctl reload nginx
```

---

## Step 6 — Build and start

```bash
cd /opt/clients/CLIENTNAME
docker compose build
docker compose up -d
docker compose ps   # both containers should show "Up"
```

Health check:
```bash
curl http://localhost:800X/health   # should return {"status":"ok"}
```

---

## Step 7 — Scan WhatsApp QR code

Send the client this URL:
```
https://CLIENTNAME.whats2manage.com/setup
```

On the dedicated bot SIM: **WhatsApp → three dots → Linked Devices → Link a Device → scan the QR**.

---

## Step 8 — Get the session name and session ID

```bash
curl -s "http://localhost:200X/api/sessions" \
  -H "X-API-Key: dev-admin-key" | python3 -m json.tool
```

Note the `name` and `id` fields. Update `.env` if the session name differs from `opsgateway`:

```bash
nano /opt/clients/CLIENTNAME/.env
# Set OPENWA_SESSION=<actual session name>
docker compose restart backend
```

---

## Step 9 — Get WhatsApp group IDs

The client needs two groups (both must include the bot number):
- **Support group** — where staff send tickets
- **Billing group** — where `/payment` is typed

```bash
SESSION_ID="paste-id-from-step-8"
curl -s "http://localhost:200X/api/sessions/${SESSION_ID}/groups" \
  -H "X-API-Key: dev-admin-key" | python3 -c "
import sys, json
for g in json.load(sys.stdin):
    print(g.get('id'), '-', g.get('name') or g.get('subject', 'unknown'))
"
```

---

## Step 10 — Register billing webhook

```bash
SESSION_ID="paste-session-id"
BILLING_IP=$(docker inspect billing-app | python3 -c "import sys,json; nets=json.load(sys.stdin)[0]['NetworkSettings']['Networks']; print(list(nets.values())[0]['IPAddress'])")
BILLING_GROUP_ID="120363XXXXXXXXXX@g.us"

curl -X POST "http://localhost:200X/api/sessions/${SESSION_ID}/webhooks" \
  -H "X-API-Key: dev-admin-key" \
  -H "Content-Type: application/json" \
  -d "{
    \"url\": \"http://${BILLING_IP}:9000/webhook/by-group/${BILLING_GROUP_ID}\",
    \"events\": [\"message.received\"]
  }"
```

---

## Step 11 — Register client in billing dashboard

Go to `https://whats2manage.com` → **New Client**:

| Field | Value |
|-------|-------|
| Company Name | Client display name |
| Subdomain | CLIENTNAME |
| Backend Port | 800X |
| Admin WhatsApp Phone | 254XXXXXXXXX |
| Plan | Monthly |

Click **Create Client**, then open the record and fill in:

| Field | Value |
|-------|-------|
| WhatsApp Group ID | billing group ID (`120363...@g.us`) |
| OpenWA URL | `http://CLIENTNAME-openwa-1:2785` |
| OpenWA Session Name | session name from Step 8 |
| OpenWA API Key | `dev-admin-key` |
| Docker Project Name | `CLIENTNAME` |

---

## Step 12 — Register support group in client dashboard

Go to `https://CLIENTNAME.whats2manage.com` → log in → **Settings** → enter the support group ID → save.

---

## Step 13 — Verify

```bash
curl https://CLIENTNAME.whats2manage.com/health
cd /opt/clients/CLIENTNAME && docker compose ps
```

Send a test message in the support group — it should appear in the Live Queue.
Type `/payment` in the billing group — the client should receive an M-Pesa STK push.
