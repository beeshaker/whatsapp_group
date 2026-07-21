# VPS Architecture (as deployed)

What's actually running on the production VPS, as verified by direct inspection on 2026-07-14. This documents the live setup, which has drifted from the original design in `multi-tenant-architecture.md` and `contabo-deployment.md` — those describe the intended/initial setup; this describes what's really there today.

---

## Directory layout

Three kinds of directories live under `/opt`, and they are **not** the same thing:

```
/opt/whatsapp-ticketing/     ← git clone, source of truth. Nothing runs from here directly.
/opt/billing/                ← LIVE deployment dir for the billing service. Not a git clone.
/opt/clients/<name>/         ← LIVE deployment dir per client. Not a git clone.
```

- **`/opt/whatsapp-ticketing`** is a real `git clone` (`origin` → `github.com/beeshaker/whatsapp_group`). It exists so `git pull` has somewhere to land. `deploy/scripts/update-clients.sh` runs from here.
- **`/opt/billing`** and **`/opt/clients/<name>`** are flat, disconnected copies of the relevant repo subtree (`billing/` or `backend/`+`openwa/`). No `.git`, no remote. Code only reaches them via manual `cp`/`rsync` from the git clone — a `git pull` inside `/opt/whatsapp-ticketing` does **not** update them.
- Because these live directories aren't tracked, they can silently accumulate hand-edits that were never committed back to the repo, and they can drift arbitrarily far behind the repo if nobody redeploys for a while. **Always `diff` a live file against the freshly-pulled repo copy before overwriting it** — don't assume a clean rsync/cp is safe. (Found `/opt/billing/main.py` several commits stale on 2026-07-14; no undocumented hand-edits that time, but the risk is real.)

---

## Services

### Billing (central, single instance)

Lives in `/opt/billing`. One instance serves **all** clients — this is not per-client.

| Container | Image built from | Purpose |
|---|---|---|
| `billing-app` | `/opt/billing` (`build: .`) | FastAPI billing/payments service. Serves `whats2manage.com`. M-Pesa STK push, statements, per-client admin dashboard (`/clients/{id}`), the `/webhook/by-group/{group_id}` and `/webhook/mpesa` endpoints. |
| `billing-nginx-1` | `nginx:alpine` | Reverse proxy in front of `billing-app` and (per `NGINX_CONTAINER_NAME`/`NGINX_CONF_DIR` env vars) auto-registers each client's public subdomain. |

Data: SQLite at `/app/data/billing.db` inside `billing-app` (Docker volume `billing_data`). The `clients` table is the single source of truth for every client's plan, WhatsApp billing-group JID, OpenWA connection info, and status.

### Per-client (one set per client)

Lives in `/opt/clients/<name>` (e.g. `/opt/clients/pixiilive`).

| Container | Purpose |
|---|---|
| `<name>-backend-1` | The ticketing backend (`backend/main.py`) — incident intake, dashboard, WhatsApp command handling for that client only. |
| `<name>-openwa-1` | That client's WhatsApp session (OpenWA gateway). |

Each client has its own Postgres database (shared Postgres *server*, separate *database* per client — see `multi-tenant-architecture.md`) and its own isolated Docker network.

---

## Networking — the fragile part

Each client's compose file declares its own network, auto-named `<name>_client-net` (e.g. `pixiilive_client-net`), containing just that client's `backend` and `openwa` containers. `billing-app` has its own declared network, `billing_billing-net`. These are isolated from each other **by design** — except for one thing:

`billing-app`'s `send_to_group()` (used for `/payment` replies, statements, push reminders — any outbound message *from billing*) posts directly to a client's OpenWA container by Docker hostname, e.g. `http://pixiilive-openwa-1:2785`. For that hostname to resolve, **`billing-app` must be attached to that client's `<name>_client-net` network too** — on top of its own `billing_billing-net`.

This extra attachment is **not declared in any `docker-compose.yml`**. It only exists as an imperative `docker network connect <name>_client-net billing-app`, presumably run once by hand per client at onboarding time. It is not captured anywhere in version control or the compose files.

**Consequence, confirmed 2026-07-14:** any time `billing-app` is recreated — a normal `docker compose build billing && docker compose up -d --no-deps billing` after a code change — Docker Compose reattaches the new container *only* to the networks declared in `billing/docker-compose.yml` (`billing_billing-net`). Every manual client-network attachment is silently dropped. This breaks `send_to_group` for **every client simultaneously** (DNS resolution failure: `[Errno -3] Temporary failure in name resolution`) until each client's network is manually reconnected again:

```bash
docker network connect <name>_client-net billing-app
# repeat for every client
```

**Any future billing deploy must reconnect every client network afterward, or every client's outbound WhatsApp messaging silently breaks until someone notices.** This is the single biggest operational landmine in the current setup. Worth fixing properly at some point — e.g. by declaring the client networks as `external: true` additional networks in `billing/docker-compose.yml`, or documenting/scripting the reconnect step as a mandatory last step of a billing deploy.

---

## Deployment procedure

### Ticketing backend (per client) — scripted

```bash
cd /opt/whatsapp-ticketing
./deploy/scripts/update-clients.sh              # all clients
./deploy/scripts/update-clients.sh pixiilive     # one client
```

Pulls the repo, rsyncs `backend/` into `/opt/clients/<name>/backend/` (excludes `.env`), rebuilds, restarts only the `backend` container. `openwa` is never touched — no QR re-scan needed. See `docs/deploying-backend-updates.md`.

### Billing service — manual, not scripted

```bash
cd /opt/whatsapp-ticketing && git pull
cp /opt/whatsapp-ticketing/billing/main.py /opt/billing/main.py   # or whichever files changed — diff first
cd /opt/billing
docker compose build billing
docker compose up -d --no-deps billing
# REQUIRED: reconnect every client network (see Networking above)
docker network connect nineonetwo_client-net billing-app
docker network connect pixie_client-net billing-app
docker network connect pixiilive_client-net billing-app
```

There is no script for this yet. `deploy/scripts/update-clients.sh` explicitly does not touch `billing/`.

---

## Known data/config gotchas (found 2026-07-14)

1. **Duplicate `whatsapp_group_id` across client rows.** The billing DB had two `clients` rows sharing the same WhatsApp group JID — a stale, `closed` client (`pixii`, id 2, an abandoned/duplicate onboarding attempt) and the real active client (`pixiilive`, id 3). `group_webhook`'s lookup (`billing/main.py`) does `select(Client).where(Client.whatsapp_group_id == group_id)` with no `status='active'` filter and no DB-level uniqueness constraint, so it silently matched the wrong (dead) row every time, misrouting every message for that group. Fixed for this one pair by nulling the stale row's `whatsapp_group_id`. **Worth auditing the rest of the `clients` table for the same collision**, and ideally adding a uniqueness constraint (or at least a `status='active'` filter) so this can't recur silently.

2. **`billing-nginx-1` found disconnected from `billing_billing-net`**, created back on 2026-06-27, crash-looping on `host not found in upstream "billing"`. The public site was reachable throughout regardless, which suggests this container may not actually be in the live traffic path (possibly superseded by something else) — this was not fully root-caused and is worth a dedicated investigation rather than assuming it's fixed.

3. **Live deployment directories drift silently.** `/opt/billing/main.py` was found several commits behind the repo before this session's deploy — nothing else was watching for that. There's no automated check that live directories match a known-good repo commit.

---

## Related docs

- `docs/deploying-backend-updates.md` — the scripted per-client backend update flow in detail.
- `docs/multi-tenant-architecture.md` — the original multi-tenant design (Postgres-per-client, subdomain routing intent).
- `docs/contabo-deployment.md` — original VPS bring-up guide; note its Nginx section (host-level Nginx + `client-ports.conf`) describes the *original* plan and does not reflect the dockerized `billing-nginx-1` auto-registration actually in use today.
- `docs/onboarding-new-client.md` — new client setup steps.
