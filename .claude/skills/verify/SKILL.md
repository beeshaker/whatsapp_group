---
name: verify
description: Run and drive the backend/billing FastAPI apps locally (without Docker) to observe real behavior, using a fake OpenWA stub in place of the real WhatsApp gateway.
---

# Verifying backend / billing changes without Docker

Both `backend/` and `billing/` are plain FastAPI apps that run fine outside
Docker with a local sqlite DB — no need to spin up the full multi-container
stack (postgres, openwa, nginx) just to exercise a code change.

## Fake OpenWA stub

Neither app needs the *real* OpenWA/WhatsApp gateway to exercise the HTTP
plumbing — a tiny stub implementing the same REST surface is enough:

```
GET    /api/sessions
POST   /api/sessions            {"name": ...} -> {"id", "name", "status": "created", "phone": null}
POST   /api/sessions/{id}/start -> flips status to "qr_ready"
POST   /api/sessions/{id}/stop  -> flips status to "disconnected"
DELETE /api/sessions/{id}       -> removes it (204)
GET    /api/sessions/{id}       -> the session dict
GET    /api/sessions/{id}/qr    -> {"qrCode": "data:image/png;base64,...", "status": "qr_ready"} once ready, else 400
```

A minimal FastAPI implementation of this (in-memory dict, no auth) is enough
to drive create-if-missing / reconnect / disconnect / QR flows end-to-end.
Run it with `uvicorn <file>:app --port <port>`.

To simulate a real device having scanned the QR (needed to test phone-number
mismatch logic), add a test-only helper route on the stub, e.g.
`POST /__test__/set-ready/{id}/{phone}` that sets `status="ready"` and
`phone=<phone>` directly — there's no way to trigger this through the real
API without an actual WhatsApp scan.

## Running backend/main.py

```bash
export DATABASE_URL="sqlite+aiosqlite:////absolute/path/to/scratch.db"
export GATEWAY_SECRET_TOKEN="verify-secret"
export SECRET_KEY="verify-secret-key-for-verification-only1"
export MIN_CONFIDENCE="0.80"
export ADMIN_USERNAME="admin"
export ADMIN_PASSWORD="verifypass"
export OPENWA_URL="http://localhost:<fake-openwa-port>"
export OPENWA_SESSION="<tenant-name>"   # mirrors per-client .env OPENWA_SESSION
export OPENWA_API_KEY="test-key"
cd backend && python3 -m uvicorn main:app --port <port> --env-file /dev/null
```

Admin user is auto-seeded from `ADMIN_USERNAME`/`ADMIN_PASSWORD` on startup
(see `main.py` lifespan, ~line 268). Log in via `POST /login` with a cookie
jar (`curl -c cookies.txt ...`), then hit any `/api/settings/*` or
`/api/openwa/*` route with `-b cookies.txt`.

## Running billing/main.py

```bash
export DATABASE_URL="sqlite+aiosqlite:////absolute/path/to/scratch_billing.db"
export ADMIN_USERNAME="admin"
export ADMIN_PASSWORD="verifypass"
export SECRET_KEY="verify-secret-key-billing-only1"
export NGINX_CONF_DIR="/absolute/path/to/scratch/nginx-conf"   # just needs to exist
mkdir -p "$NGINX_CONF_DIR"
cd billing && python3 -m uvicorn main:app --port <port> --env-file /dev/null
```

Admin is auto-seeded the same way (`main.py` ~line 55). Create a client via
`POST /clients` (`name`, `subdomain`, `plan`), then configure its OpenWA
fields via `POST /clients/{id}` (`openwa_url`, `openwa_session`,
`openwa_api_key`, `docker_project`, `admin_whatsapp_phone`) pointing
`openwa_url` at the fake stub.

## Gotchas

- Backgrounding two `uvicorn` processes plus the fake stub in one Bash tool
  call sometimes returns a nonzero/odd exit code (observed: 144) even though
  the processes start fine — `disown` each background job and verify with a
  separate `curl` call rather than trusting that command's own exit code.
- Playwright's Chromium is **not installed** in this sandbox
  (`Chromium distribution 'chrome' is not found at /opt/google/chrome/chrome`)
  — browser-driven clicking isn't available here. Fall back to `curl` against
  the real running server (still real runtime behavior, just not through a
  rendered DOM) and note the gap in any verification report.
- `billing/main.py`'s admin-seed and client-create routes don't touch Docker
  or the real filesystem beyond `NGINX_CONF_DIR`, so the billing app runs
  standalone fine without `docker.sock` mounted — only the *docker_manager*
  start/stop-container helpers need real Docker, and those aren't on the
  QR/session code path.
