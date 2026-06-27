# Onboarding a New Client

This guide walks through adding a new client to the Whats2Manage platform. Each client gets their own subdomain (e.g. `acme.whats2manage.com`), their own backend + OpenWA containers, and their own PostgreSQL database.

---

## Prerequisites

- You have SSH access to the VPS as `deploy@143.86.81.124`
- The billing dashboard is live at `https://whats2manage.com`
- Shared PostgreSQL is running at `/opt/clients/shared-postgres`
- You have a dedicated WhatsApp SIM for the client's bot number

---

## Step 1 — Decide port numbers

Each client needs two unique ports. Keep a running list:

| Client | Backend port | OpenWA port |
|--------|-------------|-------------|
| pixii  | 8001        | 2001        |
| acme   | 8002        | 2002        |
| next   | 8003        | 2003        |

---

## Step 2 — Create the database

```bash
cd /opt/clients/shared-postgres
docker compose exec postgres psql -U ops_user -d postgres -c "CREATE DATABASE client_acme;"
```

Replace `acme` with the client's subdomain.

---

## Step 3 — Create the client directory

```bash
mkdir -p /opt/clients/acme
cp /opt/whatsapp-ticketing/deploy/client-template/docker-compose.yml /opt/clients/acme/
cp -r /opt/whatsapp-ticketing/backend /opt/clients/acme/
cp -r /opt/whatsapp-ticketing/openwa /opt/clients/acme/
```

---

## Step 4 — Create the `.env` file

```bash
nano /opt/clients/acme/.env
```

Fill in all values:

```env
BACKEND_PORT=8002
OPENWA_PORT=2002

POSTGRES_USER=ops_user
POSTGRES_PASSWORD=YOUR_SHARED_POSTGRES_PASSWORD
POSTGRES_DB=client_acme
DATABASE_URL=postgresql+asyncpg://ops_user:YOUR_SHARED_POSTGRES_PASSWORD@postgres:5432/client_acme

GATEWAY_SECRET_TOKEN=$(openssl rand -hex 32)
SECRET_KEY=$(openssl rand -hex 32)

ADMIN_USERNAME=admin
ADMIN_PASSWORD=STRONG_PASSWORD_HERE
SUPER_ADMIN_USERNAME=superadmin
SUPER_ADMIN_PASSWORD=STRONG_PASSWORD_HERE

OPENWA_URL=http://openwa:2785
OPENWA_SESSION=opsgateway
OPENWA_API_KEY=dev-admin-key

OLLAMA_HOST=http://host.docker.internal:11434
OLLAMA_MODEL=qwen2.5:7b
OLLAMA_TIMEOUT=60
MIN_CONFIDENCE=0.80

DASHBOARD_TITLE=Acme Incident Monitor
DASHBOARD_URL=https://acme.whats2manage.com
SUMMARY_TIMEZONE=Africa/Nairobi
SUMMARY_SCHEDULE_HOUR=8

CLIENT_SUBDOMAIN=acme
BILLING_SERVICE_URL=https://whats2manage.com
BILLING_WEBHOOK_SECRET=
```

Generate the random tokens before saving:
```bash
openssl rand -hex 32  # use for GATEWAY_SECRET_TOKEN
openssl rand -hex 32  # use for SECRET_KEY
```

---

## Step 5 — Update `docker-compose.yml`

```bash
nano /opt/clients/acme/docker-compose.yml
```

Replace the entire file with:

```yaml
services:
  backend:
    build:
      context: ./backend
    restart: always
    ports:
      - "127.0.0.1:${BACKEND_PORT}:8000"
    env_file: .env
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
```

---

## Step 6 — Add to Nginx port map

```bash
sudo nano /etc/nginx/conf.d/client-ports.conf
```

Add the new client:
```nginx
map $client $backend_port {
    pixii     8001;
    acme      8002;
    default   8001;
}
```

Reload Nginx:
```bash
sudo nginx -t && sudo systemctl reload nginx
```

---

## Step 7 — Build and start

```bash
cd /opt/clients/acme
docker compose build
docker compose up -d
docker compose ps  # verify both containers are running
```

---

## Step 8 — Scan WhatsApp QR code

Send the client this URL:
```
https://acme.whats2manage.com/setup
```

They open it in a browser and scan the QR code with the dedicated bot SIM via:
**WhatsApp → three dots → Linked Devices → Link a Device**

---

## Step 9 — Get the OpenWA API key and session name

After scanning, get the actual API key and session name:

```bash
# Get the session name
curl -s "http://localhost:2002/api/sessions" \
  -H "X-API-Key: dev-admin-key" | python3 -m json.tool
```

Note the `name` and `id` fields. Update the client's `.env`:
```bash
nano /opt/clients/acme/.env
```
Set `OPENWA_SESSION=` to the actual session name, then restart the backend:
```bash
docker compose restart backend
```

---

## Step 10 — Get WhatsApp group IDs

The client needs two WhatsApp groups — both must include the bot number:

1. **Support group** — where client staff send tickets
2. **Billing admin group** — where the client types `/payment`

Get the group IDs:
```bash
SESSION_ID="paste-session-id-here"
curl -s "http://localhost:2002/api/sessions/${SESSION_ID}/groups" \
  -H "X-API-Key: dev-admin-key" | python3 -c "
import sys, json
groups = json.load(sys.stdin)
for g in groups:
    print(g.get('id'), '-', g.get('name') or g.get('subject', 'unknown'))
"
```

---

## Step 11 — Register billing webhook

Get the billing container IP on `services-net`:
```bash
docker inspect billing-app | python3 -c "import sys,json; nets=json.load(sys.stdin)[0]['NetworkSettings']['Networks']; print([(k,v['IPAddress']) for k,v in nets.items()])"
```

Register the webhook for the billing group:
```bash
SESSION_ID="paste-session-id-here"
BILLING_IP="172.23.0.3"  # from above
BILLING_GROUP_ID="120363XXXXXXXXXX@g.us"

curl -X POST "http://localhost:2002/api/sessions/${SESSION_ID}/webhooks" \
  -H "X-API-Key: dev-admin-key" \
  -H "Content-Type: application/json" \
  -d "{
    \"url\": \"http://${BILLING_IP}:9000/webhook/by-group/${BILLING_GROUP_ID}\",
    \"events\": [\"message.received\"]
  }"
```

---

## Step 12 — Register client in billing dashboard

Go to `https://whats2manage.com` → **New Client** and fill in:

| Field | Value |
|-------|-------|
| Company Name | Acme Ltd |
| Subdomain | acme |
| Backend Port | 8002 |
| Admin WhatsApp Phone | 254XXXXXXXXX |
| Plan | Monthly |

Click **Create Client**, then open the client record and fill in:

| Field | Value |
|-------|-------|
| WhatsApp Group ID | billing group ID (`120363...@g.us`) |
| OpenWA URL | `http://acme-openwa-1:2785` |
| OpenWA Session Name | actual session name from Step 9 |
| OpenWA API Key | `dev-admin-key` |
| Docker Project Name | `acme` |

Click **Save Changes**.

---

## Step 13 — Register support group in client dashboard

Go to `https://acme.whats2manage.com` → log in → **Settings** → enter the support group ID → save.

---

## Step 14 — Verify everything works

```bash
# Health check
curl https://acme.whats2manage.com/health

# Check containers
cd /opt/clients/acme && docker compose ps
```

Send a test message in the support group — it should appear in the Live Queue at `https://acme.whats2manage.com`.

Type `/payment` in the billing group — the client should receive an M-Pesa STK push.
