# Changing a Client's WhatsApp Number

Use this when a client needs to switch the bot to a different SIM/phone number.

---

## Step 1 — Stop the openwa container

```bash
cd /opt/clients/CLIENTNAME
docker compose stop openwa
```

---

## Step 2 — Delete the existing session data

```bash
docker compose run --rm openwa sh -c "rm -rf /app/data/sessions/*"
```

Or exec into the volume directly:

```bash
docker run --rm -v CLIENTNAME_openwa_data:/data alpine sh -c "rm -rf /data/sessions/*"
```

---

## Step 3 — Restart openwa

```bash
docker compose up -d openwa
```

---

## Step 4 — Scan the new QR code

Open the setup page in a browser:
```
https://CLIENTNAME.whats2manage.com/setup
```

On the **new** SIM: **WhatsApp → three dots → Linked Devices → Link a Device → scan**.

---

## Step 5 — Get the new session details

```bash
curl -s "http://localhost:200X/api/sessions" \
  -H "X-API-Key: dev-admin-key" | python3 -m json.tool
```

Note the new `name` and `id`.

---

## Step 6 — Update `.env` if session name changed

```bash
nano /opt/clients/CLIENTNAME/.env
# Update OPENWA_SESSION=<new session name>
docker compose restart backend
```

---

## Step 7 — Get new group IDs

The new number must be added to both the support group and billing group by the client before doing this.

```bash
SESSION_ID="new-session-id"
curl -s "http://localhost:200X/api/sessions/${SESSION_ID}/groups" \
  -H "X-API-Key: dev-admin-key" | python3 -c "
import sys, json
for g in json.load(sys.stdin):
    print(g.get('id'), '-', g.get('name') or g.get('subject', 'unknown'))
"
```

---

## Step 8 — Re-register the billing webhook

Delete the old webhook first (if the API supports it), then register with the new session ID:

```bash
SESSION_ID="new-session-id"
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

## Step 9 — Update billing dashboard

Go to `https://whats2manage.com` → open the client record → update:

| Field | New value |
|-------|-----------|
| OpenWA Session Name | new session name from Step 5 |
| Admin WhatsApp Phone | new phone number |

Save changes.

---

## Step 10 — Verify

Send a test message in the support group and type `/payment` in the billing group to confirm both are working with the new number.
