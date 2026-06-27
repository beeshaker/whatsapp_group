# Contabo VPS Deployment Guide

Multi-instance deployment of the WhatsApp Ticketing system on a single Contabo VPS, with one subdomain per client.

---

## 1. Choose a Contabo Plan

| Plan | RAM | vCPU | NVMe | Price | Instances |
|------|-----|------|------|-------|-----------|
| VPS S | 8 GB | 4 | 200 GB | ~€7/mo | 3–4 |
| VPS M | 16 GB | 6 | 400 GB | ~€14/mo | 8–10 |
| VPS L | 30 GB | 8 | 800 GB | ~€27/mo | 18–20 |
| VPS XL | 60 GB | 10 | 1.6 TB | ~€53/mo | 35–40 |

Each instance needs ~1.5 GB RAM (OpenWA runs a headless Chromium browser).
Pick **VPS L** or **VPS XL** for production. Choose **Ubuntu 24.04 LTS** as the OS.

---

## 2. Initial Server Setup

SSH in as root using the credentials Contabo emails you:

```bash
ssh root@YOUR_SERVER_IP
```

### Create a non-root user

```bash
adduser deploy
usermod -aG sudo docker deploy   # docker group added later
```

### Copy your SSH key to the new user

```bash
# Run this on your LOCAL machine, not the server
ssh-copy-id deploy@YOUR_SERVER_IP
```

### Disable root SSH login

```bash
sed -i 's/PermitRootLogin yes/PermitRootLogin no/' /etc/ssh/sshd_config
systemctl restart ssh
```

### Firewall

```bash
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw enable
```

---

## 3. Install Docker

```bash
curl -fsSL https://get.docker.com | sh
usermod -aG docker deploy
newgrp docker   # apply group without re-login
```

---

## 4. Install Ollama (AI classifier)

Ollama runs on the host (not inside Docker) so all backend containers can share it via `host.docker.internal`.

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull llama3.2
```

Verify it's running:

```bash
curl http://localhost:11434/api/tags
```

Ollama starts automatically as a systemd service. No further config needed — the backend containers reach it via `http://host.docker.internal:11434`.

---

## 5. DNS Setup

Go to your domain registrar (or Cloudflare — recommended) and add:

| Type | Name | Value |
|------|------|-------|
| A | `@` | `YOUR_SERVER_IP` |
| A | `*` | `YOUR_SERVER_IP` |

The wildcard `*` record means every subdomain (`clientx.whats2eat.com`, `clienty.whats2eat.com`, etc.) automatically points to your server — no new DNS record needed per client.

Wait a few minutes for DNS to propagate before proceeding.

---

## 6. Install Nginx + Wildcard SSL

```bash
apt install nginx certbot python3-certbot-nginx -y
```

Get a wildcard SSL certificate (requires DNS challenge — do this on Cloudflare for easiest setup):

```bash
certbot certonly \
  --dns-cloudflare \
  --dns-cloudflare-credentials ~/.secrets/cloudflare.ini \
  -d "whats2eat.com" \
  -d "*.whats2eat.com"
```

> **Cloudflare credentials file** (`~/.secrets/cloudflare.ini`):
> ```
> dns_cloudflare_api_token = YOUR_CLOUDFLARE_API_TOKEN
> ```
> Create an API token in Cloudflare dashboard → My Profile → API Tokens → Edit zone DNS.

If you're not using Cloudflare, use the manual DNS challenge instead:
```bash
certbot certonly --manual --preferred-challenges dns \
  -d "whats2eat.com" -d "*.whats2eat.com"
```
Follow the prompts — it asks you to add a TXT record to your DNS.

### Nginx config

Create `/etc/nginx/sites-available/whatsapp-ticketing`:

```nginx
# Redirect HTTP to HTTPS
server {
    listen 80;
    server_name *.whats2eat.com whats2eat.com;
    return 301 https://$host$request_uri;
}

# HTTPS — route each subdomain to its backend port
server {
    listen 443 ssl;
    server_name ~^(?<client>[^.]+)\.whats2eat\.com$;

    ssl_certificate     /etc/letsencrypt/live/whats2eat.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/whats2eat.com/privkey.pem;

    # Map client name to port using an include file (see Section 8)
    include /etc/nginx/client-ports.conf;

    location / {
        proxy_pass http://127.0.0.1:$backend_port;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;
    }
}
```

Enable it:

```bash
ln -s /etc/nginx/sites-available/whatsapp-ticketing /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx
```

---

## 7. Clone the Repository

```bash
su - deploy
git clone https://github.com/YOUR_USERNAME/whatsapp-ticketing.git /opt/whatsapp-ticketing
```

---

## 8. Deploy the First Client

Each client lives in its own directory with its own `.env` and its own PostgreSQL database.

### Directory layout

```
/opt/whatsapp-ticketing/          ← source code (read-only reference)
/opt/clients/
├── acme/
│   ├── docker-compose.yml        ← symlink or copy
│   └── .env
├── riverside/
│   ├── docker-compose.yml
│   └── .env
└── shared-postgres/
    └── docker-compose.yml        ← one shared PostgreSQL
```

### Start shared PostgreSQL

```bash
mkdir -p /opt/clients/shared-postgres
```

Create `/opt/clients/shared-postgres/docker-compose.yml`:

```yaml
services:
  postgres:
    image: postgres:16-alpine
    restart: always
    environment:
      POSTGRES_USER: ops_user
      POSTGRES_PASSWORD: CHANGE_THIS_STRONG_PASSWORD
    volumes:
      - postgres_data:/var/lib/postgresql/data
    ports:
      - "127.0.0.1:5432:5432"   # localhost only, not exposed externally

volumes:
  postgres_data:
```

```bash
cd /opt/clients/shared-postgres
docker compose up -d

# Create a database for your first client
docker compose exec postgres psql -U ops_user -c "CREATE DATABASE client_acme;"
```

### Port allocation

Keep track of which port each client uses. Backend ports start at 8001, OpenWA ports at 2001:

| Client | Backend port | OpenWA port |
|--------|-------------|-------------|
| acme | 8001 | 2001 |
| riverside | 8002 | 2002 |
| plaza | 8003 | 2003 |

### Create the client directory

```bash
mkdir -p /opt/clients/acme
cp -r /opt/whatsapp-ticketing/backend /opt/clients/acme/
cp -r /opt/whatsapp-ticketing/openwa /opt/clients/acme/
```

Create `/opt/clients/acme/.env`:

```env
# PostgreSQL — shared server, dedicated database
POSTGRES_USER=ops_user
POSTGRES_PASSWORD=CHANGE_THIS_STRONG_PASSWORD
POSTGRES_DB=client_acme
DATABASE_URL=postgresql+asyncpg://ops_user:CHANGE_THIS_STRONG_PASSWORD@host.docker.internal:5432/client_acme

# Security — generate unique values per client
# Run: openssl rand -hex 32
GATEWAY_SECRET_TOKEN=generate-a-random-token-here
SECRET_KEY=generate-another-random-token-here

# WhatsApp session name — must be unique per client
OPENWA_SESSION=acme
OPENWA_API_KEY=generate-a-third-random-token-here
OPENWA_URL=http://openwa:2785

# Ollama — shared instance on the host
OLLAMA_HOST=http://host.docker.internal:11434
OLLAMA_MODEL=llama3.2
OLLAMA_TIMEOUT=10
MIN_CONFIDENCE=0.65

# Dashboard
DASHBOARD_TITLE=Acme Incident Monitor
DASHBOARD_URL=https://acme.whats2eat.com
SUMMARY_TIMEZONE=Africa/Nairobi
SUMMARY_SCHEDULE_HOUR=8
```

Create `/opt/clients/acme/docker-compose.yml`:

```yaml
services:
  backend:
    build:
      context: ./backend
    restart: always
    ports:
      - "127.0.0.1:8001:8000"    # ← increment per client
    env_file: .env
    extra_hosts:
      - "host.docker.internal:host-gateway"
    volumes:
      - media_data:/app/media
    networks:
      - client-net

  openwa:
    build:
      context: ./openwa
    restart: always
    ports:
      - "127.0.0.1:2001:2785"    # ← increment per client
    volumes:
      - openwa_data:/app/data
    networks:
      - client-net
    depends_on:
      - backend

networks:
  client-net:
    driver: bridge

volumes:
  openwa_data:
  media_data:
```

> Note: PostgreSQL is removed from this compose file — it's handled by `shared-postgres`.

### Register the port in Nginx

Append to `/etc/nginx/client-ports.conf`:

```nginx
map $client $backend_port {
    acme      8001;
    riverside 8002;
    plaza     8003;
    default   8001;
}
```

Reload Nginx:

```bash
nginx -t && systemctl reload nginx
```

### Build and start

```bash
cd /opt/clients/acme
docker compose build
docker compose up -d
```

---

## 9. WhatsApp QR Setup (per client)

This must be done once per client after first deploy (and again after a volume wipe).

### Option A: Web UI (easiest)

On your **local machine**, run:

```bash
ssh -L 9999:localhost:9999 deploy@YOUR_SERVER_IP
```

Then on the **server**, in another terminal:

```bash
cd /opt/whatsapp-ticketing
python3 -m http.server 9999
```

Open `http://localhost:9999/setup.html` in your browser. The UI guides you through:
1. Creating and starting the WhatsApp session
2. Displaying the QR code (refreshes every 15 seconds)
3. Registering the webhook once your phone is linked

On your phone: **WhatsApp → three dots → Linked Devices → Link a Device → scan QR**

> You have ~15 seconds after "QR updated" appears. Scan immediately.

### Option B: Script

```bash
cd /opt/whatsapp-ticketing
# Edit setup.sh: set API port to match this client's OpenWA port (e.g. 2001)
# API="http://localhost:2001/api"
bash setup.sh
```

### Verify

```bash
curl https://acme.whats2eat.com/health
```

Should return `{"status":"ok"}`.

---

## 10. Adding More Clients

For each new client (e.g. `riverside`):

1. **Create the database:**
   ```bash
   cd /opt/clients/shared-postgres
   docker compose exec postgres psql -U ops_user -c "CREATE DATABASE client_riverside;"
   ```

2. **Copy and configure:**
   ```bash
   cp -r /opt/clients/acme /opt/clients/riverside
   # Edit .env: update POSTGRES_DB, OPENWA_SESSION, DASHBOARD_TITLE, DASHBOARD_URL
   # Edit docker-compose.yml: increment port numbers (8002, 2002)
   ```

3. **Add to Nginx port map** (`/etc/nginx/client-ports.conf`):
   ```nginx
   riverside  8002;
   ```
   Then `nginx -t && systemctl reload nginx`.

4. **Build and start:**
   ```bash
   cd /opt/clients/riverside
   docker compose build
   docker compose up -d
   ```

5. **Scan QR** (same process as Section 9).

---

## 11. SSL Certificate Auto-Renewal

Certbot installs a systemd timer automatically. Verify it:

```bash
systemctl status certbot.timer
```

Test renewal:

```bash
certbot renew --dry-run
```

---

## 12. Backups

### Database backup (all clients)

```bash
#!/bin/bash
# /opt/scripts/backup.sh
DATE=$(date +%Y%m%d_%H%M)
BACKUP_DIR=/opt/backups/$DATE
mkdir -p $BACKUP_DIR

cd /opt/clients/shared-postgres
for db in $(docker compose exec -T postgres psql -U ops_user -t -c "SELECT datname FROM pg_database WHERE datname LIKE 'client_%'"); do
    db=$(echo $db | xargs)
    docker compose exec -T postgres pg_dump -U ops_user $db > $BACKUP_DIR/$db.sql
done

# Keep last 30 days
find /opt/backups -maxdepth 1 -type d -mtime +30 -exec rm -rf {} +
echo "Backup complete: $BACKUP_DIR"
```

```bash
chmod +x /opt/scripts/backup.sh
# Add to crontab: daily at 2am
crontab -e
# 0 2 * * * /opt/scripts/backup.sh
```

---

## 13. Useful Commands

```bash
# View logs for a client
cd /opt/clients/acme && docker compose logs backend -f
cd /opt/clients/acme && docker compose logs openwa -f

# Restart a client
cd /opt/clients/acme && docker compose restart

# Check all running containers
docker ps

# Update a client (after git pull on source)
cd /opt/clients/acme && docker compose build && docker compose up -d

# Check disk usage
df -h
docker system df
```

---

## Troubleshooting

**Backend can't connect to PostgreSQL**
- Check `DATABASE_URL` in `.env` uses `host.docker.internal:5432`
- Confirm shared postgres is running: `cd /opt/clients/shared-postgres && docker compose ps`

**OpenWA QR code not appearing**
- Check openwa logs: `docker compose logs openwa -f`
- Chromium needs ~30 seconds to start on first boot

**SSL certificate errors**
- Confirm DNS is pointing to your server IP: `dig acme.whats2eat.com`
- Check cert covers the subdomain: `certbot certificates`

**Nginx 502 Bad Gateway**
- Backend container isn't running or is still starting up
- Check port mapping matches `client-ports.conf`
