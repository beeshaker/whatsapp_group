# WhatsApp Ticketing — Debugging Guide

---

## Issue 1: WhatsApp Session Stuck in `failed` State

### Symptoms
- `setup.html` shows **Session status: failed**
- Log: `Connecting to Whats2Manage at localhost:2785...`
- Dashboard at `http://localhost:8000/` loads but no new incidents appear

### Diagnosis Steps

**Step 1 — Check container status**
```bash
docker compose ps
```
All three containers (`backend`, `openwa`, `postgres`) should show `Up`. If `openwa` is missing, it crashed (see Issue 1c below).

**Step 2 — Check session state via API**
```bash
curl -s http://localhost:2785/api/sessions -H "x-api-key: dev-admin-key"
```
Look at the `status` field:
| Status | Meaning |
|---|---|
| `ready` | Working — nothing to fix |
| `failed` | Session broken, needs restart |
| `disconnected` | Stopped cleanly, needs `start` |

**Step 3 — Check openwa logs for root cause**
```bash
docker compose logs openwa --tail=40
```

---

### Root Cause A: Stale Chromium Lock Files

**Error in logs:**
```
The profile appears to be in use by another Chromium process (36) on another computer (6a4b90893c1a).
Chromium has locked the profile so that it doesn't get corrupted.
```

**Why it happens:** When the container is stopped ungracefully (crash, `docker compose down`, system restart), Chromium leaves `SingletonLock`, `SingletonSocket`, and `SingletonCookie` files behind in the session profile directory. On next start, Chromium refuses to launch because it thinks another instance is running.

**Fix:**
```bash
# 1. Stop the session via API
SESSION_ID=$(curl -s http://localhost:2785/api/sessions -H "x-api-key: dev-admin-key" | python3 -c "import json,sys; print(json.load(sys.stdin)[0]['id'])")
curl -s -X POST "http://localhost:2785/api/sessions/${SESSION_ID}/stop" -H "x-api-key: dev-admin-key" -H "Content-Type: application/json"

# 2. Remove all stale lock files
docker exec whatsapp-ticketing-openwa-1 find /app/data/sessions/session-opsgateway \
  \( -name "SingletonLock" -o -name "SingletonSocket" -o -name "SingletonCookie" \) \
  -print -delete

# 3. Start the session again
curl -s -X POST "http://localhost:2785/api/sessions/${SESSION_ID}/start" -H "x-api-key: dev-admin-key" -H "Content-Type: application/json"
```

**Verify:** Wait ~10 seconds then check status is `ready`:
```bash
curl -s "http://localhost:2785/api/sessions/${SESSION_ID}" -H "x-api-key: dev-admin-key"
```

---

### Root Cause B: WhatsApp Session Logged Out (LOGOUT)

**Error in logs:**
```
Session disconnected: LOGOUT
```

**Why it happens:** WhatsApp revoked the linked device — either due to inactivity (>14 days), the phone removed it under *Linked Devices*, or WhatsApp servers reset it.

**Fix — re-scan the QR code:**
```bash
# 1. Start the Python setup server in a terminal
python3 -m http.server 9999 --directory /home/abhishek/projects/whatsappgroup/whatsapp-ticketing

# 2. Open http://localhost:9999/setup.html
# 3. On your phone: WhatsApp > ⋮ > Linked Devices > Link a Device > scan QR
```

---

### Root Cause C: openwa Container Crashed

**Symptom:** `docker compose ps` shows `openwa` container is absent or exited.

**Why it happens:** A Puppeteer crash (`window['onQRChangedEvent'] already exists!`) kills the Node.js process when a reconnect is attempted against a stale browser session.

**Fix:**
```bash
# Remove lock files first, then restart the container
docker exec whatsapp-ticketing-openwa-1 find /app/data/sessions/session-opsgateway \
  \( -name "SingletonLock" -o -name "SingletonSocket" -o -name "SingletonCookie" \) \
  -delete 2>/dev/null || true

docker compose up -d openwa
```

---

## Issue 2: New WhatsApp Messages Not Creating Incidents on Dashboard

### Symptoms
- Dashboard is stuck — no new incidents appear despite messages being sent to the group
- Backend logs show repeated `[UPDATE] incident_id=X reporter=Unknown` but no `[INCIDENT]` lines
- `GET /incidents?since_id=<N>` always returns empty (no incidents beyond the last known one)
- Ingest endpoint returns `202 Accepted` — messages ARE being received

### Diagnosis Steps

**Step 1 — Confirm messages are reaching the backend**
```bash
docker compose logs backend --tail=30 | grep -E "ingest|UPDATE|INCIDENT"
```
If you see `202 Accepted` but only `[UPDATE]` lines (never `[INCIDENT]`), the AI router is misclassifying new messages as updates to existing tickets.

**Step 2 — Check how many open (unresolved) incidents exist**
```bash
curl -s http://localhost:8000/incidents | python3 -c "
import json, sys
from collections import Counter
data = json.load(sys.stdin)
print('Total:', len(data))
print('By status:', dict(Counter(i['status'] for i in data)))
"
```
If most incidents are in `review` status and never get resolved/ignored, they pile up as "open tickets" and the AI keeps routing new messages as updates to them.

**Step 3 — Check what the AI classifier is deciding**

The routing logic in [backend/classifier.py](../backend/classifier.py) works like this:
1. `classify_message()` — decides if the message is an incident at all (`is_incident: true/false`, `confidence: 0.0–1.0`)
2. If confidence ≥ `MIN_CONFIDENCE` (default `0.65`), it proceeds to routing
3. `classify_update_or_new()` — given up to 5 open tickets, decides if the message is a new incident or an update to an existing one

**The problem:** With many old unresolved incidents in `review` status, the router always finds matching open tickets and routes new messages as updates.

### Root Cause: Open Tickets Never Closed

The AI routing prompt feeds the LLM up to 5 open (non-resolved, non-ignored) tickets. If there are 15 tickets all stuck in `review`, every new message will be matched against them, and the model frequently returns `"routing": "update"` even for genuinely new issues.

### Fix — Resolve or ignore old incidents

Close out stale tickets so they no longer pollute the routing context:
```bash
# Mark old incidents as resolved via the dashboard UI at http://localhost:8000/
# OR via the API:
curl -s -X PATCH http://localhost:8000/incidents/<ID>/status \
  -H "Content-Type: application/json" \
  -d '{"status": "resolved"}'
```

Once old incidents are closed, new messages will be routed as fresh incidents again.

---

## Quick Health Check Checklist

Run this after any restart or when something seems wrong:

```bash
# 1. All containers running?
docker compose ps

# 2. Backend healthy?
curl -s http://localhost:8000/health

# 3. WhatsApp session ready?
curl -s http://localhost:2785/api/sessions -H "x-api-key: dev-admin-key" | python3 -c "import json,sys; s=json.load(sys.stdin)[0]; print(s['status'], s.get('phone',''))"

# 4. Any errors in logs?
docker compose logs backend --tail=20 | grep -i error
docker compose logs openwa --tail=20 | grep -i error
```

All good when: containers `Up (healthy)`, backend returns `ok`, session returns `ready`.
