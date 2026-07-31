# Deploying Backend Updates to Clients

## Why clients need manual updates

Each client's backend is a **copy** of the repo source, built into its own Docker image. When `add-client.sh` provisions a new client it runs `cp -r` — the client directory is not a git clone, so there is no remote to pull from.

This means changes committed to the repo do **not** automatically reach any client. You must push the new code to each client and rebuild their backend image.

---

## What is safe to update without disrupting clients

Only the **backend** service changes between code releases. The **openwa** service holds the WhatsApp session and is never touched during a backend update.

| Service | Restarted during update? | WhatsApp QR re-scan needed? |
|---------|-------------------------|----------------------------|
| `backend` | Yes — rebuilt and restarted | No |
| `openwa` | No — completely untouched | No |

The `docker compose up -d --no-deps backend` command restarts only the backend container. The openwa container keeps running with its existing session.

---

## How to deploy an update

### Update all clients at once

SSH into the server, then run:

```bash
cd /home/deploy/whatsapp-ticketing   # or wherever the repo lives on the server
./deploy/scripts/update-clients.sh
```

This will:
1. `git pull` the latest code from the repo
2. Sync the `backend/` source into every client directory under `/opt/clients/`
3. Rebuild each client's backend Docker image
4. Restart only the backend container per client

### Update specific clients only

```bash
./deploy/scripts/update-clients.sh acme riverside
```

### Manual update for one client

If you need to update a single client without the script:

```bash
rsync -a --delete --exclude='.env' \
  /path/to/repo/backend/ /opt/clients/<client>/backend/

cd /opt/clients/<client>
docker compose build backend
docker compose up -d --no-deps backend
```

---

## What the update script does NOT touch

- `.env` files — client secrets and config are preserved
- `openwa` containers — WhatsApp sessions stay connected
- PostgreSQL data — each client's database is untouched
- Docker volumes (`media_data`, `openwa_data`) — files are preserved

---

## When do I need to run this?

Any time you merge a change to the `backend/` directory or `backend/templates/`. Common cases:

- Bug fixes in `main.py`
- Template changes in `backend/templates/`
- New API endpoints
- Model or schema changes (note: schema migrations run automatically on startup via SQLAlchemy)

Changes to `billing/`, `openwa/`, or `deploy/` do **not** require running *this script* — but that doesn't mean they never need deploying, just that each has its own separate, manual process:

- `openwa/` changes (e.g. adding a new engine) still need a manual per-client rebuild — see "OpenWA (per client) — manual, not scripted" in `docs/vps-architecture.md`.
- `billing/` changes are also manual — see "Billing service — manual, not scripted" in the same doc.
