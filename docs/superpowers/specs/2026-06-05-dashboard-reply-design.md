# Dashboard Reply — Design Spec

**Date:** 2026-06-05
**Status:** Approved

## Summary

Allow admins to type a message from the incident detail modal that is sent directly to the WhatsApp group associated with the ticket. The sent message is saved as an `IncidentUpdate` (reporter: "Dashboard") so it appears in the update thread. The WhatsApp echo of the sent message is deduplicated automatically.

---

## Architecture

New `backend/whatsapp.py` module holds the async send function. A new `POST /incidents/{id}/reply` FastAPI endpoint in `main.py` orchestrates the send + DB write. The dashboard detail modal gains a reply compose box at the bottom. No new dependencies — uses `httpx` (already installed).

---

## Backend

### New env vars (`docker-compose.yml` backend service)

| Variable | Example | Purpose |
|---|---|---|
| `OPENWA_URL` | `http://openwa:2785` | OpenWA container base URL |
| `OPENWA_SESSION` | `opsgateway` | Session name created during setup |
| `OPENWA_API_KEY` | `<key>` | OpenWA API key (same as used in webhook registration) |

### New file: `backend/whatsapp.py`

```python
import logging
import os
import httpx

logger = logging.getLogger(__name__)

OPENWA_URL = os.getenv("OPENWA_URL", "http://openwa:2785")
OPENWA_SESSION = os.getenv("OPENWA_SESSION", "opsgateway")
OPENWA_API_KEY = os.getenv("OPENWA_API_KEY", "")


async def send_group_message(chat_id: str, text: str) -> str:
    """Send text to a WhatsApp group. Returns the WhatsApp message ID."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(
            f"{OPENWA_URL}/api/sessions/{OPENWA_SESSION}/messages/text",
            headers={"X-API-Key": OPENWA_API_KEY, "Content-Type": "application/json"},
            json={"chatId": chat_id, "text": text},
        )
        response.raise_for_status()
        return response.json()["messageId"]
```

Raises `httpx.HTTPStatusError` on non-2xx. Caller handles the exception and returns an appropriate HTTP error to the dashboard.

### New Pydantic model in `main.py`

```python
class ReplyBody(BaseModel):
    text: str
```

Validation: non-empty, max 4000 chars (same truncation used elsewhere in the codebase).

### New endpoint: `POST /incidents/{incident_id}/reply`

- **Auth:** `X-API-Key` required (same `GATEWAY_SECRET_TOKEN` check)
- **422** if `text` is empty or > 4000 chars
- **404** if incident not found
- Fetches `Incident` by id to get `group_id`
- Calls `send_group_message(incident.group_id, body.text)`
- On success: creates `IncidentUpdate` with:
  - `incident_id` = incident.id
  - `message_id` = returned WhatsApp message ID (enables echo dedup)
  - `reporter_name` = `"Dashboard"`
  - `reporter_phone` = `None`
  - `message_body` = body.text
  - `received_at` = `datetime.now(timezone.utc)`
  - `ai_linked` = `False`
- Sets `incident.updated_at = received_at`
- Commits, refreshes update
- Returns the update as JSON: `{id, reporter_name, message_body, received_at, ai_linked, media_count: 0}`
- **502** if `send_group_message` raises (OpenWA unreachable or error) — does NOT save the update

### Echo deduplication

When the OpenWA gateway sends a message, it echoes back through the webhook. The existing dedup check in `ingest` checks `IncidentUpdate.message_id` — since the update is stored with the WhatsApp message ID, the echo returns `{"status": "duplicate"}` with no further processing.

---

## Dashboard UI (`dashboard.html`)

### Reply section in detail modal

Added as a fourth section at the bottom of `renderDetailModal`, after the Attachments section. Only shown when `API_KEY` is truthy (same condition as the re-link control).

```html
<div>
  <div class="modal-section-label">Reply to group</div>
  <div class="reply-wrap">
    <textarea class="reply-textarea" id="reply-input-${detail.id}"
      placeholder="Type a message to send to the group…" rows="3"></textarea>
    <button class="reply-send-btn" id="reply-send-${detail.id}"
      onclick="sendReply(${detail.id})">↑ Send</button>
  </div>
</div>
```

### CSS

```css
.reply-wrap {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.reply-textarea {
  width: 100%;
  padding: 10px 12px;
  border-radius: var(--radius-md);
  border: 1px solid var(--line);
  background: var(--surface-2);
  color: var(--text);
  font: inherit;
  font-size: 13px;
  resize: vertical;
  min-height: 72px;
}

.reply-textarea:focus {
  outline: none;
  border-color: rgba(56, 189, 248, 0.4);
}

.reply-send-btn {
  align-self: flex-end;
  padding: 7px 16px;
  border-radius: var(--radius-sm);
  border: none;
  background: linear-gradient(135deg, #0ea5e9, #0284c7);
  color: white;
  font-size: 13px;
  font-weight: 800;
  cursor: pointer;
  transition: filter 0.15s ease, opacity 0.15s ease;
}

.reply-send-btn:hover { filter: brightness(1.08); }
.reply-send-btn:disabled { opacity: 0.45; cursor: default; filter: none; }
```

### `sendReply(incidentId)` JS function

```javascript
async function sendReply(incidentId) {
  const textarea = document.getElementById(`reply-input-${incidentId}`);
  const btn = document.getElementById(`reply-send-${incidentId}`);
  const text = textarea.value.trim();
  if (!text) return;

  btn.disabled = true;
  btn.textContent = '…';
  textarea.disabled = true;

  try {
    const r = await fetch(`/incidents/${incidentId}/reply`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-API-Key': API_KEY },
      body: JSON.stringify({ text }),
    });
    if (!r.ok) throw new Error('Send failed');
    textarea.value = '';
    showToast('Message sent to group');
    await openDetailModal(incidentId);  // re-render to show new update
  } catch(e) {
    showToast('Failed to send — please try again');
    btn.disabled = false;
    btn.textContent = '↑ Send';
    textarea.disabled = false;
  }
}
```

### Dashboard-sent update styling

In `renderDetailModal`, dashboard-originated updates (where `reporter_name === 'Dashboard'`) render with a distinct background so they're visually identifiable as outbound messages:

```javascript
const isDashboard = u.reporter_name === 'Dashboard';
// Add class "update-row-outbound" when isDashboard is true
```

```css
.update-row-outbound {
  background: rgba(7, 89, 133, 0.22);
  border-color: rgba(56, 189, 248, 0.2);
}
```

---

## Error Handling

| Scenario | Behaviour |
|---|---|
| OpenWA unreachable | `send_group_message` raises; endpoint returns 502; update NOT saved; dashboard shows "Failed to send" toast |
| Empty text body | 422 from Pydantic validation |
| Text > 4000 chars | 422 from validator |
| Incident not found | 404 |
| DB commit fails after successful send | Log error, return 500; message already sent to WhatsApp but not tracked — acceptable edge case |

---

## Testing

- `POST /incidents/{id}/reply` with valid text + mocked `send_group_message` → 200, `IncidentUpdate` created with `reporter_name="Dashboard"`, `incident.updated_at` set
- `POST /incidents/{id}/reply` with empty text → 422
- `POST /incidents/{id}/reply` with wrong API key → 401
- `POST /incidents/{id}/reply` when `send_group_message` raises → 502, no update created
- Echo dedup: update saved with message_id → same message_id ingest → `{"status": "duplicate"}`
- `GET /incidents/{id}` includes the dashboard-sent update in the `updates` list

### New file: `backend/tests/test_reply.py`
