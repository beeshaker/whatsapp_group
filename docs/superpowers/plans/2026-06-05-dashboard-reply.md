# Dashboard Reply Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow admins to type a message in the incident detail modal that is sent to the associated WhatsApp group and saved as an IncidentUpdate in the update thread.

**Architecture:** New `backend/whatsapp.py` holds `send_group_message()` (async httpx call to OpenWA). A new `POST /incidents/{id}/reply` endpoint in `main.py` orchestrates send + DB write. The detail modal in `dashboard.html` gains a reply compose box and outbound message styling.

**Tech Stack:** FastAPI, SQLAlchemy async, httpx (already installed), Jinja2 / vanilla JS.

---

## File Map

| File | Change |
|---|---|
| `backend/whatsapp.py` | New — `send_group_message()` async function |
| `backend/tests/test_whatsapp.py` | New — unit tests for `send_group_message` |
| `backend/main.py` | Add `ReplyBody` model, import `send_group_message`, add `POST /incidents/{id}/reply` endpoint |
| `backend/tests/test_reply.py` | New — integration tests for the reply endpoint |
| `docker-compose.yml` | Add `OPENWA_URL`, `OPENWA_SESSION`, `OPENWA_API_KEY` env vars to backend service |
| `backend/templates/dashboard.html` | Add CSS, `sendReply()` JS function, reply section in modal, outbound update styling |

---

## Task 1: `backend/whatsapp.py` — Send Group Message

**Files:**
- Create: `backend/whatsapp.py`
- Create: `backend/tests/test_whatsapp.py`

- [ ] **Step 1: Write failing tests — create `backend/tests/test_whatsapp.py`**

```python
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from whatsapp import send_group_message


async def test_send_group_message_returns_message_id():
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {"messageId": "wa-msg-123"}
    with patch("whatsapp.httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.post = AsyncMock(return_value=mock_resp)
        result = await send_group_message("120363@g.us", "Hello group")
    assert result == "wa-msg-123"


async def test_send_group_message_posts_to_correct_endpoint():
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {"messageId": "wa-msg-456"}
    with patch("whatsapp.httpx.AsyncClient") as mock_client:
        mock_post = AsyncMock(return_value=mock_resp)
        mock_client.return_value.__aenter__.return_value.post = mock_post
        await send_group_message("999@g.us", "Test message")
    call_args = mock_post.call_args
    assert "messages/text" in call_args[0][0]
    assert call_args[1]["json"]["chatId"] == "999@g.us"
    assert call_args[1]["json"]["text"] == "Test message"


async def test_send_group_message_raises_on_http_error():
    with patch("whatsapp.httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.post = AsyncMock(
            side_effect=Exception("connection refused")
        )
        with pytest.raises(Exception, match="connection refused"):
            await send_group_message("120363@g.us", "Test")
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /home/abhishek/projects/whatsappgroup/whatsapp-ticketing/backend && .venv/bin/python -m pytest tests/test_whatsapp.py -v
```
Expected: `ModuleNotFoundError: No module named 'whatsapp'`

- [ ] **Step 3: Create `backend/whatsapp.py`**

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

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /home/abhishek/projects/whatsappgroup/whatsapp-ticketing/backend && .venv/bin/python -m pytest tests/test_whatsapp.py -v
```
Expected: 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
git -C /home/abhishek/projects/whatsappgroup/whatsapp-ticketing add backend/whatsapp.py backend/tests/test_whatsapp.py
git -C /home/abhishek/projects/whatsappgroup/whatsapp-ticketing commit -m "feat: add send_group_message helper for WhatsApp outbound messages"
```

---

## Task 2: `POST /incidents/{id}/reply` Endpoint

**Files:**
- Modify: `backend/main.py`
- Create: `backend/tests/test_reply.py`

- [ ] **Step 1: Write failing tests — create `backend/tests/test_reply.py`**

```python
from unittest.mock import AsyncMock, patch

_INCIDENT_CLASS = {"is_incident": True, "category": "plumbing", "severity": "high", "confidence": 0.92}

_ORIGINAL = {
    "event": "message.received",
    "data": {
        "id": "msg-r1",
        "type": "chat",
        "isGroup": True,
        "chatId": "123@g.us",
        "chat": {"name": "Block A"},
        "author": "2541@c.us",
        "notifyName": "Alice",
        "body": "Pump leaking",
        "timestamp": 1782293340,
    },
}


async def _create_incident(client) -> int:
    with patch("main.classify_message", new=AsyncMock(return_value=_INCIDENT_CLASS)):
        with patch("main.push_incident", new=AsyncMock()):
            await client.post("/api/v1/ops/ingest", json=_ORIGINAL, headers={"X-API-Key": "test-secret"})
    return (await client.get("/incidents")).json()[0]["id"]


async def test_reply_creates_update_and_returns_it(client):
    incident_id = await _create_incident(client)
    with patch("main.send_group_message", new=AsyncMock(return_value="wa-outgoing-123")):
        r = await client.post(
            f"/incidents/{incident_id}/reply",
            json={"text": "We are on our way"},
            headers={"X-API-Key": "test-secret"},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["reporter_name"] == "Dashboard"
    assert body["message_body"] == "We are on our way"
    assert body["ai_linked"] is False
    assert body["media_count"] == 0


async def test_reply_sets_incident_updated_at(client):
    incident_id = await _create_incident(client)
    with patch("main.send_group_message", new=AsyncMock(return_value="wa-msg-upd")):
        await client.post(
            f"/incidents/{incident_id}/reply",
            json={"text": "Acknowledged"},
            headers={"X-API-Key": "test-secret"},
        )
    detail = (await client.get(f"/incidents/{incident_id}")).json()
    assert detail["updated_at"] is not None


async def test_reply_update_appears_in_detail_endpoint(client):
    incident_id = await _create_incident(client)
    with patch("main.send_group_message", new=AsyncMock(return_value="wa-msg-detail")):
        await client.post(
            f"/incidents/{incident_id}/reply",
            json={"text": "Technician dispatched"},
            headers={"X-API-Key": "test-secret"},
        )
    detail = (await client.get(f"/incidents/{incident_id}")).json()
    assert len(detail["updates"]) == 1
    assert detail["updates"][0]["reporter_name"] == "Dashboard"
    assert detail["updates"][0]["message_body"] == "Technician dispatched"


async def test_reply_requires_auth(client):
    r = await client.post(
        "/incidents/1/reply",
        json={"text": "Hello"},
        headers={"X-API-Key": "wrong"},
    )
    assert r.status_code == 401


async def test_reply_returns_422_for_empty_text(client):
    incident_id = await _create_incident(client)
    r = await client.post(
        f"/incidents/{incident_id}/reply",
        json={"text": "   "},
        headers={"X-API-Key": "test-secret"},
    )
    assert r.status_code == 422


async def test_reply_returns_404_for_missing_incident(client):
    r = await client.post(
        "/incidents/9999/reply",
        json={"text": "Hello"},
        headers={"X-API-Key": "test-secret"},
    )
    assert r.status_code == 404


async def test_reply_returns_502_when_openwa_fails(client):
    incident_id = await _create_incident(client)
    with patch("main.send_group_message", new=AsyncMock(side_effect=Exception("connection refused"))):
        r = await client.post(
            f"/incidents/{incident_id}/reply",
            json={"text": "Test message"},
            headers={"X-API-Key": "test-secret"},
        )
    assert r.status_code == 502
    detail = (await client.get(f"/incidents/{incident_id}")).json()
    assert len(detail["updates"]) == 0


async def test_reply_echo_dedup(client):
    incident_id = await _create_incident(client)
    wa_id = "wa-echo-test-999"
    with patch("main.send_group_message", new=AsyncMock(return_value=wa_id)):
        await client.post(
            f"/incidents/{incident_id}/reply",
            json={"text": "Echo test"},
            headers={"X-API-Key": "test-secret"},
        )
    # Simulate the echo coming back through the webhook with the same message ID
    echo_payload = {
        "event": "message.received",
        "data": {
            "id": wa_id,
            "type": "chat",
            "isGroup": True,
            "chatId": "123@g.us",
            "chat": {"name": "Block A"},
            "author": "2541@c.us",
            "body": "Echo test",
            "timestamp": 1782293400,
        },
    }
    r = await client.post("/api/v1/ops/ingest", json=echo_payload, headers={"X-API-Key": "test-secret"})
    assert r.json()["status"] == "duplicate"
    # Only the dashboard-sent update exists, no duplicate
    detail = (await client.get(f"/incidents/{incident_id}")).json()
    assert len(detail["updates"]) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /home/abhishek/projects/whatsappgroup/whatsapp-ticketing/backend && .venv/bin/python -m pytest tests/test_reply.py -v
```
Expected: all fail — route `POST /incidents/{id}/reply` not yet defined.

- [ ] **Step 3: Add `ReplyBody` model and import to `backend/main.py`**

Add `ReplyBody` after the existing `RelinkBody` model:
```python
class ReplyBody(BaseModel):
    text: str
```

Add the import of `send_group_message` to the imports block — find:
```python
from odoo_stub import push_incident
```
Replace with:
```python
from odoo_stub import push_incident
from whatsapp import send_group_message
```

- [ ] **Step 4: Add the `POST /incidents/{incident_id}/reply` endpoint to `backend/main.py`**

Add this after the `update_incident_status` endpoint (after `PATCH /incidents/{incident_id}/status`), before the `GET /` dashboard route:

```python
@app.post("/incidents/{incident_id}/reply")
async def reply_to_incident(
    incident_id: int,
    body: ReplyBody,
    x_api_key: str = Header(None, alias="X-API-Key"),
    db: AsyncSession = Depends(get_db),
):
    if not hmac.compare_digest(x_api_key or "", GATEWAY_SECRET_TOKEN):
        raise HTTPException(status_code=401, detail="Unauthorized")

    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=422, detail="text must not be empty")
    text = text[:4000]

    result = await db.execute(select(Incident).where(Incident.id == incident_id))
    incident = result.scalar_one_or_none()
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")

    try:
        wa_message_id = await send_group_message(incident.group_id, text)
    except Exception as exc:
        logger.error("send_group_message failed: %s", exc)
        raise HTTPException(status_code=502, detail="Failed to send message to WhatsApp")

    now = datetime.now(timezone.utc)
    update = IncidentUpdate(
        incident_id=incident_id,
        message_id=wa_message_id,
        reporter_name="Dashboard",
        reporter_phone=None,
        message_body=text,
        received_at=now,
        ai_linked=False,
    )
    db.add(update)
    incident.updated_at = now
    try:
        await db.commit()
        await db.refresh(update)
    except Exception as exc:
        await db.rollback()
        logger.error("DB commit failed after send: %s", exc)
        raise HTTPException(status_code=500, detail="Message sent but could not be saved")

    return {
        "id": update.id,
        "reporter_name": update.reporter_name,
        "message_body": update.message_body,
        "received_at": update.received_at.isoformat(),
        "ai_linked": update.ai_linked,
        "media_count": 0,
    }
```

- [ ] **Step 5: Run all tests**

```bash
cd /home/abhishek/projects/whatsappgroup/whatsapp-ticketing/backend && .venv/bin/python -m pytest tests/ -v --tb=short 2>&1 | tail -15
```
Expected: all 67 tests pass (59 existing + 3 whatsapp + 8 reply — note: `test_reply_returns_422_for_empty_text` needs the `create_incident` helper which uses a unique `msg-r1` id; since tests run in clean DB each time via the `clean_tables` fixture this is fine).

- [ ] **Step 6: Commit**

```bash
git -C /home/abhishek/projects/whatsappgroup/whatsapp-ticketing add backend/main.py backend/tests/test_reply.py
git -C /home/abhishek/projects/whatsappgroup/whatsapp-ticketing commit -m "feat: add POST /incidents/{id}/reply endpoint with echo deduplication"
```

---

## Task 3: Docker Compose — OpenWA Env Vars

**Files:**
- Modify: `docker-compose.yml`

- [ ] **Step 1: Add three env vars to the backend service `environment` block**

Find the `environment` block in the `backend` service:
```yaml
    environment:
      DATABASE_URL: ${DATABASE_URL}
      GATEWAY_SECRET_TOKEN: ${GATEWAY_SECRET_TOKEN}
      OLLAMA_HOST: ${OLLAMA_HOST}
      OLLAMA_MODEL: ${OLLAMA_MODEL}
      OLLAMA_TIMEOUT: ${OLLAMA_TIMEOUT:-10}
      MIN_CONFIDENCE: ${MIN_CONFIDENCE:-0.65}
      DASHBOARD_TITLE: ${DASHBOARD_TITLE:-Ops Incident Monitor}
```
Replace with:
```yaml
    environment:
      DATABASE_URL: ${DATABASE_URL}
      GATEWAY_SECRET_TOKEN: ${GATEWAY_SECRET_TOKEN}
      OLLAMA_HOST: ${OLLAMA_HOST}
      OLLAMA_MODEL: ${OLLAMA_MODEL}
      OLLAMA_TIMEOUT: ${OLLAMA_TIMEOUT:-10}
      MIN_CONFIDENCE: ${MIN_CONFIDENCE:-0.65}
      DASHBOARD_TITLE: ${DASHBOARD_TITLE:-Ops Incident Monitor}
      OPENWA_URL: ${OPENWA_URL:-http://openwa:2785}
      OPENWA_SESSION: ${OPENWA_SESSION:-opsgateway}
      OPENWA_API_KEY: ${OPENWA_API_KEY:-}
```

- [ ] **Step 2: Verify compose file parses**

```bash
cd /home/abhishek/projects/whatsappgroup/whatsapp-ticketing && docker compose config --quiet
```
Expected: no errors (exit code 0).

- [ ] **Step 3: Commit**

```bash
git -C /home/abhishek/projects/whatsappgroup/whatsapp-ticketing add docker-compose.yml
git -C /home/abhishek/projects/whatsappgroup/whatsapp-ticketing commit -m "infra: add OPENWA_URL/SESSION/API_KEY env vars to backend service"
```

---

## Task 4: Dashboard UI — Reply Box and Outbound Styling

**Files:**
- Modify: `backend/templates/dashboard.html`

- [ ] **Step 1: Add CSS for reply box and outbound update styling**

Find the closing `</style>` tag and add just before it:

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

    .update-row-outbound {
      background: rgba(7, 89, 133, 0.22);
      border-color: rgba(56, 189, 248, 0.2);
    }
```

- [ ] **Step 2: Add `sendReply` JS function**

In the `<script>` block, find the `relinkUpdate` function and add `sendReply` right before it:

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
    await openDetailModal(incidentId);
  } catch(e) {
    showToast('Failed to send — please try again');
    btn.disabled = false;
    btn.textContent = '↑ Send';
    textarea.disabled = false;
  }
}
```

- [ ] **Step 3: Update `renderDetailModal` — add outbound styling and reply section**

Find the `renderDetailModal` function. Inside the update map, find the line:
```javascript
        return `<div class="update-row">
```
Replace with:
```javascript
        const isDashboard = u.reporter_name === 'Dashboard';
        return `<div class="update-row${isDashboard ? ' update-row-outbound' : ''}">
```

Then find the closing of `renderDetailModal` — the return statement that ends with the Attachments section:
```javascript
  return `
    <div>
      <div class="modal-section-label">Original report</div>
      <div class="message-box"><div class="message">${esc(detail.message_body)}</div></div>
    </div>
    <div>
      <div class="modal-section-label">Updates (${detail.updates.length})</div>
      <div class="update-thread">${updatesHtml}</div>
    </div>
    <div>
      <div class="modal-section-label">Attachments (${detail.media.length})</div>
      ${mediaHtml}
    </div>`;
```
Replace with:
```javascript
  const replyHtml = API_KEY ? `
    <div>
      <div class="modal-section-label">Reply to group</div>
      <div class="reply-wrap">
        <textarea class="reply-textarea" id="reply-input-${detail.id}"
          placeholder="Type a message to send to the group…" rows="3"></textarea>
        <button class="reply-send-btn" id="reply-send-${detail.id}"
          onclick="sendReply(${detail.id})">↑ Send</button>
      </div>
    </div>` : '';

  return `
    <div>
      <div class="modal-section-label">Original report</div>
      <div class="message-box"><div class="message">${esc(detail.message_body)}</div></div>
    </div>
    <div>
      <div class="modal-section-label">Updates (${detail.updates.length})</div>
      <div class="update-thread">${updatesHtml}</div>
    </div>
    <div>
      <div class="modal-section-label">Attachments (${detail.media.length})</div>
      ${mediaHtml}
    </div>
    ${replyHtml}`;
```

- [ ] **Step 4: Run full test suite**

```bash
cd /home/abhishek/projects/whatsappgroup/whatsapp-ticketing/backend && .venv/bin/python -m pytest tests/ -q 2>&1 | tail -5
```
Expected: all 67 tests PASS.

- [ ] **Step 5: Commit**

```bash
git -C /home/abhishek/projects/whatsappgroup/whatsapp-ticketing add backend/templates/dashboard.html
git -C /home/abhishek/projects/whatsappgroup/whatsapp-ticketing commit -m "feat: add reply compose box and outbound message styling to detail modal"
```

---

## Spec Coverage Check

| Spec requirement | Task |
|---|---|
| `backend/whatsapp.py` with `send_group_message()` | Task 1 |
| `OPENWA_URL`, `OPENWA_SESSION`, `OPENWA_API_KEY` env vars | Task 3 |
| `ReplyBody` model | Task 2 Step 3 |
| `POST /incidents/{id}/reply` endpoint | Task 2 Step 4 |
| Auth (401), empty text (422), missing incident (404) | Task 2 Step 4 |
| 502 on OpenWA failure — no update saved | Task 2 Step 4 |
| Update saved with `reporter_name="Dashboard"`, `ai_linked=False` | Task 2 Step 4 |
| `incident.updated_at` set on reply | Task 2 Step 4 |
| Echo dedup via `message_id` on `IncidentUpdate` | Task 2 Step 4 + test |
| Reply section in detail modal (textarea + send button) | Task 4 Step 3 |
| Only shown when `API_KEY` truthy | Task 4 Step 3 (`replyHtml = API_KEY ? ...`) |
| `sendReply()` JS function with loading state + toast | Task 4 Step 2 |
| Re-renders modal after success | Task 4 Step 2 |
| `.update-row-outbound` styling for Dashboard sender | Task 4 Steps 1 + 3 |
| docker-compose env vars for OpenWA | Task 3 |
