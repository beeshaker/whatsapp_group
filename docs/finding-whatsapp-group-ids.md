# Finding WhatsApp Group IDs and Client Details

When registering a client in the billing dashboard, you need 5 things. Here's exactly where each one comes from.

---

## Step 1 — Get the session ID

Your OpenWA instance is running on port 2785. First, get the session ID:

```bash
curl -s http://localhost:2785/api/sessions -H "X-API-Key: dev-admin-key"
```

You'll get something like:

```json
[
  {
    "id": "52a1afbf-559b-474f-848b-c11819221663",
    "name": "opsgateway",
    "status": "ready",
    "phone": "254141707105"
  }
]
```

Copy the `id` value.

---

## Step 2 — List all WhatsApp groups

```bash
SESSION_ID="52a1afbf-559b-474f-848b-c11819221663"
curl -s "http://localhost:2785/api/sessions/$SESSION_ID/groups" \
  -H "X-API-Key: dev-admin-key" | python3 -m json.tool
```

You'll get a list like:

```json
[
  { "id": "120363410401890011@g.us", "name": "Admingroup" },
  { "id": "120363408267876531@g.us", "name": "Test group" }
]
```

The `id` ending in `@g.us` is your **WhatsApp Group ID**.

---

## Step 3 — Fill in the billing dashboard

Open the client's detail page at `http://localhost:9001/clients/<id>` and fill in:

| Field | Where it comes from |
|---|---|
| **WhatsApp Group ID** | The `@g.us` ID from Step 2 |
| **OpenWA URL** | `http://localhost:2785` (the port OpenWA runs on) |
| **OpenWA Session Name** | `OPENWA_SESSION` value in your `.env` file |
| **OpenWA API Key** | `OPENWA_API_KEY` value in your `.env` file |
| **Docker Project Name** | Run `docker ps` — the prefix before `-backend-1` (e.g. `whatsapp_group`) |

To quickly grab the session name and API key:

```bash
grep -E "OPENWA_SESSION=|OPENWA_API_KEY=" .env
```

To find the Docker project name:

```bash
docker ps --format "{{.Names}}" | grep backend
# Output: whatsapp_group-backend-1  →  project name is "whatsapp_group"
```

---

## Quick reference (this deployment)

| Field | Value |
|---|---|
| OpenWA URL | `http://localhost:2785` |
| OpenWA Session Name | `opsgateway` |
| OpenWA API Key | `dev-admin-key` |
| Docker Project Name | `whatsapp_group` |

Group IDs change per environment — always look them up with Step 2.
