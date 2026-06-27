# Multi-Tenant Architecture Plan

How multiple companies (clients) are isolated from each other using one server.

---

## The Core Idea

Each company gets:
- Their own subdomain: `acme.whats2eat.com`
- Their own backend process (separate Docker container)
- Their own WhatsApp session (separate OpenWA container)
- Their own database (separate PostgreSQL database, same PostgreSQL server)

They share:
- One VPS
- One PostgreSQL server process (~200 MB RAM)
- One Ollama instance (AI classifier)
- One Nginx (routes subdomains to the right backend)

---

## Architecture Diagram

```
Internet
    │
    ▼
Nginx (whats2eat.com)
    │
    ├── acme.whats2eat.com ──────► Backend :8001 ──► DB: client_acme
    │                              OpenWA  :2001        (WhatsApp session: acme)
    │
    ├── riverside.whats2eat.com ──► Backend :8002 ──► DB: client_riverside
    │                               OpenWA  :2002       (WhatsApp session: riverside)
    │
    └── plaza.whats2eat.com ──────► Backend :8003 ──► DB: client_plaza
                                    OpenWA  :2003       (WhatsApp session: plaza)

All backends share:
    - PostgreSQL server (one process, port 5432, localhost only)
    - Ollama (one process, port 11434, localhost only)
```

---

## Database Structure

One PostgreSQL **server** runs on the VPS. Each client has their own **database** within that server.

```
PostgreSQL server
├── database: client_acme
│   ├── incidents
│   ├── incident_updates
│   ├── incident_media
│   ├── incident_status_history
│   ├── incident_categories
│   ├── users
│   ├── user_groups
│   ├── audit_log
│   ├── admin_profiles
│   ├── admin_group_subscriptions
│   └── chat_sessions
│
├── database: client_riverside
│   └── (same tables, completely separate data)
│
└── database: client_plaza
    └── (same tables, completely separate data)
```

Each backend container connects only to its own database via `DATABASE_URL`. There is no way for one client's backend to read another client's data — they connect to different databases entirely.

### Why not one database with a tenant_id column?

| Approach | Isolation | Code changes | Risk |
|----------|-----------|--------------|------|
| Separate database per client | Complete | None | None |
| One database, schema per client | Complete | Moderate | Low |
| One database, tenant_id column | Logical only | Large | Data leak if query bug |

Separate databases require zero code changes and give the strongest isolation. A bug in one client's backend cannot affect another client's data.

---

## Subdomain Routing

DNS: a single wildcard A record `*.whats2eat.com → server IP` covers all clients. No new DNS record is needed when adding a client.

Nginx reads the subdomain from the request and looks up the backend port:

```
Request: GET https://acme.whats2eat.com/incidents
    │
    ▼
Nginx extracts subdomain: "acme"
    │
    ▼
Port map: acme → 8001
    │
    ▼
Proxied to: http://127.0.0.1:8001/incidents
    │
    ▼
Backend for Acme (connects to client_acme database)
```

Subdomains are free — they are DNS records, not purchased names. You own `whats2eat.com` so you automatically own every subdomain of it.

---

## Per-Client Configuration

Each client has its own `.env` file. The only values that change between clients:

| Variable | Purpose | Example |
|----------|---------|---------|
| `POSTGRES_DB` | Which database to connect to | `client_acme` |
| `DATABASE_URL` | Full connection string | `...@host:5432/client_acme` |
| `GATEWAY_SECRET_TOKEN` | Validates webhooks from OpenWA | unique random token |
| `SECRET_KEY` | Signs session cookies | unique random token |
| `OPENWA_SESSION` | WhatsApp session name in OpenWA | `acme` |
| `OPENWA_API_KEY` | Authenticates backend → OpenWA calls | unique random token |
| `DASHBOARD_TITLE` | Page title in the UI | `Acme Incident Monitor` |
| `DASHBOARD_URL` | Used in WhatsApp summary messages | `https://acme.whats2eat.com` |
| `ADMIN_USERNAME` | Login username for the first admin account | `admin` |
| `ADMIN_PASSWORD` | Password for the first admin account | strong unique password |
| `SUPER_ADMIN_USERNAME` | Login username for the super-admin account | `superadmin` |
| `SUPER_ADMIN_PASSWORD` | Password for the super-admin account | strong unique password |

`ADMIN_USERNAME`/`ADMIN_PASSWORD` seed the regular admin on first startup. `SUPER_ADMIN_USERNAME`/`SUPER_ADMIN_PASSWORD` seed a super-admin with elevated privileges. Both are per-client — each database gets its own independent set of accounts.

Everything else (Ollama config, timezone, thresholds) can be the same across all clients or overridden per client.

---

## Port Allocation

Ports are only open on localhost — Nginx proxies them, they are never exposed directly to the internet.

| Client | Backend port | OpenWA port |
|--------|-------------|-------------|
| acme | 8001 | 2001 |
| riverside | 8002 | 2002 |
| plaza | 8003 | 2003 |
| *(next)* | 8004 | 2004 |

---

## Adding a New Client Checklist

1. `CREATE DATABASE client_name;` on the shared PostgreSQL
2. Copy a client directory, update `.env` with new DB name, session name, ports, tokens
3. Update `docker-compose.yml` with the new port numbers
4. Add the port to Nginx map and reload Nginx
5. `docker compose build && docker compose up -d`
6. Scan WhatsApp QR code via setup UI

New client is live at `clientname.whats2eat.com` with no impact on existing clients.

---

## Data Isolation Summary

| What | Isolated? | How |
|------|-----------|-----|
| Incidents & tickets | Yes | Separate database |
| Users & passwords | Yes | Separate database |
| WhatsApp session | Yes | Separate OpenWA container + session name |
| Media files | Yes | Separate Docker volume per client |
| AI classifier | No | Shared Ollama, but stateless — no data stored |
| Dashboard | Yes | Separate backend, separate subdomain |
