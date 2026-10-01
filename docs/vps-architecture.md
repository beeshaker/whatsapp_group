# VPS Architecture (as deployed)

What's actually running on the production VPS, as verified by direct inspection on 2026-07-14 (and updated 2026-07-31 after a full-outage incident). This documents the live setup, which has drifted from the original design in `multi-tenant-architecture.md` and `contabo-deployment.md` — those describe the intended/initial setup; this describes what's really there today.

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

**OpenWA's WhatsApp engine is selectable per client, but only at container-boot time, not live.** As of 2026-07-23, `openwa/` supports two engines via an internal plugin system (`EngineFactory`, `src/engine/engine.factory.ts`): the original `whatsapp-web.js` (a headless-Chromium session — ~300–500MB, breaks whenever WhatsApp ships an incompatible Web-client update) and `@whiskeysockets/baileys` (a direct protocol/WebSocket client — ~30–80MB, no browser to break). Which engine a session uses is controlled by the `ENGINE_TYPE` env var, read once in `EngineFactory`'s constructor at process startup — there is **no** dashboard toggle that switches it live; the Dashboard's Plugins page can enable/configure a plugin, but `EngineFactory`'s active engine is fixed until the container restarts. Because `docker-compose.yml`'s `openwa` service has no `env_file:` and the production image doesn't bake `.env` in (only `dist/` is copied), the only place `ENGINE_TYPE` can actually reach a running container is the **persisted `data/.env.generated`** file inside that client's `openwa_data` volume (read by `src/main.ts` at boot; survives rebuilds/restarts). To switch a client's engine: write `ENGINE_TYPE=baileys` into that file (`docker run --rm -v <name>_openwa_data:/data alpine sh -c "echo 'ENGINE_TYPE=baileys' >> /data/.env.generated"`), then recreate the container. Switching engines is never a hot-swap — Baileys' auth-state format is entirely different from `whatsapp-web.js`'s `LocalAuth`, so it always needs a fresh QR re-link (`docs/change-whatsapp-number.md`'s pairing steps apply, minus the session-wipe step — Baileys keeps its own separate `data/baileys/<sessionId>` dir). **Dunhill moved to `baileys` on 2026-07-23** (its `whatsapp-web.js` session had been broken by an upstream WhatsApp Web change with no fix available); Pixie, Pixiilive, and Nineonetwo remain on `whatsapp-web.js`.

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

## Gotcha: LEAD_MODE clients need `message.reaction` added to their webhook manually

Every client's OpenWA webhook is registered once at onboarding with `"events": ["message.received"]` only (see `docs/onboarding-new-client.md` Step 10, `setup.sh`). The reaction-triggered status-update feature (see
`docs/superpowers/specs/2026-07-21-dunhill-reaction-status-and-reply-quoting-design.md`)
requires OpenWA to also dispatch `message.reaction` — but there's no automatic migration
for existing clients' webhook subscriptions.

**Any time this feature is deployed for a new or existing `LEAD_MODE` client, update
that client's webhook's `events` list manually:**

```bash
# From the client's OpenWA session:
SESSION_ID="<client's session id>"
WEBHOOK_ID="<existing webhook id, from GET /sessions/$SESSION_ID/webhooks>"
curl -X PUT "http://localhost:200X/api/sessions/${SESSION_ID}/webhooks/${WEBHOOK_ID}" \
  -H "X-API-Key: dev-admin-key" \
  -H "Content-Type: application/json" \
  -d '{"events": ["message.received", "message.reaction"]}'
```

Forgetting this step doesn't error anywhere — the feature just silently never fires,
since OpenWA never dispatches `message.reaction` to a webhook that isn't subscribed to it.

---

## Gotcha: `billing-app`'s Docker-socket mount can take down Docker for the entire box

**Confirmed 2026-07-31, caused a full outage (every client + billing simultaneously unreachable).**

`/opt/billing/docker-compose.yml` bind-mounts `/var/run/docker.sock:/var/run/docker.sock` into `billing-app` (used by its nginx-container-management code — the `NGINX_CONF_DIR`/`NGINX_CONTAINER_NAME` env vars from the Services table above). `/var/run` is normally just a symlink to `/run`, where `dockerd`'s real socket lives.

**The failure mode:** any time `docker.service` itself restarts (not a redeploy — the daemon process itself, e.g. `systemctl restart docker`, a `docker-ce` package upgrade, host reboot), systemd starts recreating every `restart: always` container in parallel while `dockerd` is still coming back up. If `billing-app` tries to start and bind-mount the socket path *before* `dockerd` has recreated the real socket file, Docker's default bind-mount behavior silently creates an **empty directory** at that path instead of waiting — permanently shadowing where the real socket should be. From that point on, `docker` CLI/API calls at the default path fail with `Cannot connect to the Docker daemon at unix:///var/run/docker.sock` — even though `dockerd` itself is perfectly healthy (`systemctl status docker` shows `active (running)`, `lsof` shows it listening) — because literally nothing can reach it through the shadowed path. This breaks **every container's tooling on the host simultaneously**, not just billing.

**Diagnose:**
```bash
file /run/docker.sock            # should say "socket" -- if it says "directory", this is the bug
systemctl status docker          # will likely show healthy/running regardless
```

**Fix (safe — the shadow is always an empty directory):**
```bash
fuser -v /var/run/docker.sock    # confirm nothing has it open first
rmdir /var/run/docker.sock       # /var/run is a symlink to /run, so this clears both paths
systemctl restart docker.socket
systemctl restart docker
file /run/docker.sock            # confirm it now says "socket"
docker ps -a                     # confirm every container restarted
```

Restarting `docker.service` again can retrigger the same race if `billing-app` starts concurrently — check `docker ps -a` afterward for exactly the container mounting `docker.sock` failing again with the same "not a directory" error before assuming it's fully fixed.

**Not yet fixed properly** — the real fix is avoiding the race entirely, e.g. giving `billing-app` a `depends_on` + retry/wait-for-socket loop instead of a bare bind-mount, or moving its nginx-management responsibility off a raw Docker-socket mount. Until then, **any planned `docker.service` restart or `docker-ce` upgrade should stop `billing-app` first**, restart Docker, confirm `/run/docker.sock` is a real socket, then start `billing-app` back up.

---

## WhatsApp session recovery (OpenWA, since 2026-10-01)

Before 2026-10-01, a session that dropped was effectively never recovered: `FAILED` sessions were never retried, nothing restarted sessions after a container restart, auto-retry gave up after 5 attempts, and every "Reconnect" path reloaded the same saved login — so a dead login (phone unlinked the device) made every fresh QR fail too. `SessionService` (`openwa/src/modules/session/session.service.ts`) now:

- **Auto-starts on boot** every previously linked session (any session with a `phone`), including `FAILED` ones (`onApplicationBootstrap`).
- **Logged-out drops** (whatsapp-web.js `LOGOUT`/`UNPAIRED*`/`auth_failure`, Baileys close code 401) wipe the saved login and go straight to a fresh QR; the session reports `needsRelink: true` until scanned.
- **Transient drops** retry with backoff (5 attempts), then mark `FAILED` but **keep retrying every 5 minutes**.
- **Delete** also wipes the saved login (it is keyed by session *name*, so it used to survive delete-and-recreate).
- `GET /api/sessions` includes in-memory `lastError`, `lastDisconnectReason`, `needsRelink`; `GET /sessions/:id/qr` always returns 200 with `{qrCode|null, status, lastError, needsRelink}`.

Two actions, exposed on **both** the tenant `/settings` page and the billing admin client page:

| Action | OpenWA endpoint | Effect |
|---|---|---|
| Restart connection | `POST /api/sessions/:id/restart` | Tear down + start again, **keeping** the login. For transient drops. |
| Link with new QR | `POST /api/sessions/:id/relink` | Tear down, **wipe** the login, start fresh → new QR. For dead logins or a phone change. |

Both work from any state. A backend/billing deployed against an older OpenWA falls back to `stop`+`start` for Restart, and reports that Link needs an OpenWA deploy.

**Manual equivalent** (production `openwa` image has no `curl`):
```bash
docker compose exec openwa node -e "
require('http').request({
  host: 'localhost', port: 2785,
  path: '/api/sessions/<session-id>/restart',   // or /relink for a fresh QR
  method: 'POST',
  headers: { 'X-API-Key': 'dev-admin-key' }
}, res => { let b=''; res.on('data', c=>b+=c); res.on('end', ()=>console.log(res.statusCode, b)); }).end();
"
```
(Note: the production `openwa` image has no `curl` — use `node -e` like above, or exec in and check what's actually available.)

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

### OpenWA (per client) — manual, not scripted, needs a rebuild

`update-clients.sh` explicitly never touches `openwa/` (by design — normally `openwa` holds a live WhatsApp session and should survive a routine backend update untouched, no QR re-scan). But when `openwa/` source itself changes (e.g. the Baileys engine addition), it does need a manual, per-client deploy — confirmed working 2026-07-23:

```bash
cd /opt/whatsapp-ticketing && git pull
rsync -a --delete --exclude='.env' --exclude='data' \
  /opt/whatsapp-ticketing/openwa/ /opt/clients/<name>/openwa/
cd /opt/clients/<name>
docker compose build openwa
docker compose up -d --no-deps openwa
```

This **does** require a fresh QR re-link if it also involves an engine switch (see the engine-selection note under Services above) — a plain code update with no engine change does not. **Common trap:** forgetting `git pull` in `/opt/whatsapp-ticketing` first leaves the rsync copying stale source — the rebuilt image silently lacks the new code with no error, it just runs the old behavior (confirmed 2026-07-23: a missing `git pull` meant the rebuilt image didn't have a newly-added engine plugin registered at all — no crash, just silent absence). Always verify the pulled commit (`git log -1 --oneline`) before rsyncing.

---

## Known data/config gotchas (found 2026-07-14)

1. **Duplicate `whatsapp_group_id` across client rows.** The billing DB had two `clients` rows sharing the same WhatsApp group JID — a stale, `closed` client (`pixii`, id 2, an abandoned/duplicate onboarding attempt) and the real active client (`pixiilive`, id 3). `group_webhook`'s lookup (`billing/main.py`) does `select(Client).where(Client.whatsapp_group_id == group_id)` with no `status='active'` filter and no DB-level uniqueness constraint, so it silently matched the wrong (dead) row every time, misrouting every message for that group. Fixed for this one pair by nulling the stale row's `whatsapp_group_id`. **Worth auditing the rest of the `clients` table for the same collision**, and ideally adding a uniqueness constraint (or at least a `status='active'` filter) so this can't recur silently.

2. **`billing-nginx-1` found disconnected from `billing_billing-net`**, created back on 2026-06-27, crash-looping on `host not found in upstream "billing"`. The public site was reachable throughout regardless, which suggests this container may not actually be in the live traffic path (possibly superseded by something else) — this was not fully root-caused and is worth a dedicated investigation rather than assuming it's fixed.

3. **Live deployment directories drift silently.** `/opt/billing/main.py` was found several commits behind the repo before this session's deploy — nothing else was watching for that. There's no automated check that live directories match a known-good repo commit.

4. **Full-outage incident, 2026-07-31: `docker.service` was sent a `terminated` signal at 07:33 CEST for an unconfirmed reason** (no matching system reboot — `last reboot` showed uptime since 2026-06-21 — and no matching entry in `/var/log/apt/history.log`; root cause of *why* it restarted was not established this session). The restart itself then triggered the `billing-app`/Docker-socket race documented above, breaking `docker` CLI/API access host-wide until manually fixed. Worth a dedicated follow-up to find what actually issued the restart (check for a cron job, a monitoring/alerting agent with a restart action, or ask whoever has root access if they ran it manually) so it can be prevented or at least anticipated.

5. **`whatsapp_group-backend-1`, `whatsapp_group-openwa-1`, `whatsapp_group-postgres-1` containers exist and are stale/stopped** (`openwa` dead for 2+ weeks, the other two exited cleanly during the 2026-07-31 restart and never came back). These look like leftover artifacts from running `docker compose up` directly inside `/opt/whatsapp-ticketing` at some point — which the Directory layout section above says nothing should do. Not confirmed whether they're safe to remove; flagged for cleanup rather than deleted outright.

---

## Related docs

- `docs/deploying-backend-updates.md` — the scripted per-client backend update flow in detail.
- `docs/multi-tenant-architecture.md` — the original multi-tenant design (Postgres-per-client, subdomain routing intent).
- `docs/contabo-deployment.md` — original VPS bring-up guide; note its Nginx section (host-level Nginx + `client-ports.conf`) describes the *original* plan and does not reflect the dockerized `billing-nginx-1` auto-registration actually in use today.
- `docs/onboarding-new-client.md` — new client setup steps.

---

## Marketing site (`marketing-site/`) — deployment, found 2026-07-24

`billing/nginx_manager.py` writes `00-client-ports.conf` (an nginx `map $client $backend_port {...}` block) into `NGINX_CONF_DIR` and reloads the `billing-nginx-1` container. That mechanism is specifically for routing `<client>.whats2manage.com` to a live client's backend port and is driven by client onboarding/offboarding (`add_client_port`/`remove_client_port`) — the marketing site is not a client and should not be added to that map.

The base server blocks that actually terminate TLS on `whats2manage.com` today (SSL cert paths, whether it's still host-level Nginx per `docs/contabo-deployment.md` or fully absorbed into the `billing-nginx-1` container) were **not fully verifiable from the repo alone** — this needs a quick check directly on the VPS before wiring anything up:

```bash
# What's actually bound to 80/443?
ss -tlnp | grep -E ':80|:443'
docker ps   # is there a host-level nginx, or does billing-nginx-1 own 80/443?
# Does the existing cert already cover a new subdomain?
sudo certbot certificates   # look for a *.whats2manage.com wildcard SAN
```

### Is the ops-gateway a "billing client"? No.

Give it nginx routing like a client (see below — it's just a backend on a port, so the same subdomain mechanism fits), but **do not** register it in the billing dashboard, do not run the billing-webhook onboarding step, and leave `BILLING_SERVICE_URL`/`CLIENT_SUBDOMAIN` unset in its `.env`. Confirmed in `backend/main.py`: `_fetch_billing_client_info()` fails open to `{"status": "active", "whatsapp_group_id": None}` whenever `BILLING_SERVICE_URL` or `CLIENT_SUBDOMAIN` is empty, so the backend runs correctly with no billing awareness at all — no tier locks, no M-Pesa expectations, doesn't show up in the client list. It's not a paying customer; it's the operator's own sales-bot instance.

### Ops-gateway (sales bot) deployment steps

Mirrors `docs/onboarding-new-client.md`'s real per-client pattern (shared Postgres, `client-net`/`shared-db`/`services-net` networks) — **not** the root repo's `docker-compose.yml`, which bundles its own Postgres container and is a local-dev-only template, not what's actually deployed for any client.

1. **Directory + source**, on the VPS (`ssh deploy@167.86.81.124`):
   ```bash
   mkdir -p /opt/ops-gateway
   cp -r /opt/whatsapp-ticketing/backend /opt/whatsapp-ticketing/openwa /opt/ops-gateway/
   ```
2. **Database** (shared Postgres, not a new container):
   ```bash
   cd /opt/clients/shared-postgres
   docker compose exec postgres psql -U ops_user -d postgres -c "CREATE DATABASE ops_gateway;"
   ```
3. **Ports** — check what's actually free before assuming (`docs/onboarding-new-client.md`'s port table may be stale): `ss -tlnp | grep -E ':(800|20)[0-9]'`. Pick the next free `BACKEND_PORT`/`OPENWA_PORT` pair (8000/2785 if genuinely unused — the OPENWA_SESSION default `opsgateway` suggests these were originally reserved for this exact purpose).
4. **`.env`** — same shape as a client's `.env` (see onboarding doc Step 3), with these ops-gateway-specific differences:
   - `OPENWA_SESSION=opsgateway`
   - `SALES_DM_MODE=true`
   - `CLIENT_SUBDOMAIN` and `BILLING_SERVICE_URL` **left unset**
   - `POSTGRES_DB=ops_gateway`, `DATABASE_URL` pointing at the shared Postgres with that DB name
   - `DASHBOARD_URL=https://opsgateway.whats2manage.com` (or whichever subdomain chosen)
5. **`docker-compose.yml`** — copy the real per-client template from onboarding doc Step 4 verbatim (backend + openwa services, `client-net`/`shared-db`/`services-net` networks), substituting the ports from step 3.
6. **Build & start**: `docker compose build && docker compose up -d && docker compose ps`; health check `curl http://127.0.0.1:<BACKEND_PORT>/health`.
7. **nginx routing** — reuse the *same* port-map mechanism real clients use (no new server block or cert needed; the wildcard `*.whats2manage.com` cert already covers any subdomain): add `opsgateway   <BACKEND_PORT>;` to the client-ports map (onboarding doc Step 5 shows the exact file/format) and reload. Confirm first whether that reload is `sudo systemctl reload nginx` (host-level, as the onboarding doc's own instructions show) or `docker exec billing-nginx-1 nginx -s reload` (matching this doc's earlier dockerized-nginx finding) — the two existing docs disagree here and it wasn't re-verified this session; check `docker ps` / `systemctl status nginx` on the VPS to see which is actually true before reloading.
8. **Pair WhatsApp**: visit `https://opsgateway.whats2manage.com/setup`. On the physical phone with SIM +254 141 707105: WhatsApp → ⋮ → Linked Devices → Link a Device → scan the QR. Once status turns green, click "Register Webhook" on the same page.
9. **Verify**: from a different phone, DM +254 141 707105 and confirm an LLM sales-agent reply comes back. `docker compose logs -f backend` from `/opt/ops-gateway` if it doesn't.

**Chosen approach for the marketing site specifically: fully standalone, decoupled from billing infra** (the site is static files, not a backend-on-a-port, so it doesn't fit the client-ports map the way the ops-gateway does). `marketing-site/docker-compose.yml` runs a plain `nginx:alpine` container serving `marketing-site/index.html` as static content on host port `8090` — it does not touch `billing-nginx-1`, the client-ports map, or any client's routing.

To go live, on the VPS:
1. `cd /opt/whatsapp-ticketing/marketing-site && docker compose up -d` (after a `git pull` in `/opt/whatsapp-ticketing`, per the usual deploy pattern — this directory is not yet part of `deploy/scripts/update-clients.sh`).
2. Point DNS for the chosen subdomain (e.g. `get.whats2manage.com`) at the VPS IP.
3. Add TLS termination for that subdomain, in whichever layer step 1's check found is actually live:
   - If a wildcard cert (`*.whats2manage.com`) is already active: add one more `server { listen 443 ssl; server_name get.whats2manage.com; location / { proxy_pass http://127.0.0.1:8090; } }` block alongside wherever the existing `whats2manage.com` root server block lives, reusing the existing cert.
   - If only `billing-nginx-1` terminates TLS and there's no separate host-level nginx: add an equivalent `server_name get.whats2manage.com` block inside that container's config (or its mounted conf dir) proxying to `host.docker.internal:8090` (or the marketing container's Docker network address) — being careful not to touch the existing `00-client-ports.conf`-driven block for other clients.
4. `curl -I https://whats2manage.com` and one existing client subdomain afterward, to confirm nothing regressed.
