# Archive Page Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `/archive` page that shows only resolved incidents, auto-removes resolved cards from the live board, and shares the same template/filters/search as the live dashboard.

**Architecture:** Single `dashboard.html` template receives a `mode` Jinja2 variable (`"live"` or `"archive"`). Two FastAPI routes (`GET /` and `GET /archive`) query different status subsets and inject the mode. `GET /incidents` gains an optional `statuses` query param for the archive page's poll.

**Tech Stack:** FastAPI, SQLAlchemy async, Jinja2, vanilla JS.

---

## File Map

| File | Change |
|---|---|
| `backend/main.py` | Add `statuses` param to `list_incidents`; update `GET /` to exclude resolved + pass `mode="live"`; add `GET /archive` route |
| `backend/templates/dashboard.html` | Add `MODE` JS constant, topbar nav, mode-aware stats/filters/buttons/poll/animation |
| `backend/tests/test_dashboard.py` | Add tests for `statuses` filter, archive route, live-excludes-resolved |

---

## Task 1: Backend — `statuses` filter, archive route, live board excludes resolved

**Files:**
- Modify: `backend/main.py`
- Modify: `backend/tests/test_dashboard.py`

- [ ] **Step 1: Write failing tests — append to `backend/tests/test_dashboard.py`**

```python
async def test_list_incidents_statuses_filter_returns_only_resolved(client):
    from unittest.mock import AsyncMock, patch
    classification = {"is_incident": True, "category": "plumbing", "severity": "high", "confidence": 0.92}
    payload_a = {
        "event": "message.received",
        "data": {"id": "msg-s1", "type": "chat", "isGroup": True, "chatId": "1@g.us",
                 "chat": {"name": "Block A"}, "author": "2541@c.us", "body": "Issue A", "timestamp": 1782293340},
    }
    payload_b = {
        "event": "message.received",
        "data": {"id": "msg-s2", "type": "chat", "isGroup": True, "chatId": "1@g.us",
                 "chat": {"name": "Block A"}, "author": "2541@c.us", "body": "Issue B", "timestamp": 1782293341},
    }
    with patch("main.classify_message", new=AsyncMock(return_value=classification)):
        with patch("main.push_incident", new=AsyncMock()):
            await client.post("/api/v1/ops/ingest", json=payload_a, headers={"X-API-Key": "test-secret"})
            await client.post("/api/v1/ops/ingest", json=payload_b, headers={"X-API-Key": "test-secret"})

    all_ids = [i["id"] for i in (await client.get("/incidents")).json()]
    assert len(all_ids) == 2

    # Resolve only the first
    await client.patch(f"/incidents/{all_ids[0]}/status",
                       json={"status": "resolved"}, headers={"X-API-Key": "test-secret"})

    resolved = (await client.get("/incidents?statuses=resolved")).json()
    assert len(resolved) == 1
    assert resolved[0]["status"] == "resolved"

    review = (await client.get("/incidents?statuses=review")).json()
    assert len(review) == 1
    assert review[0]["status"] == "review"

    all_back = (await client.get("/incidents")).json()
    assert len(all_back) == 2


async def test_list_incidents_statuses_filter_multiple(client):
    from unittest.mock import AsyncMock, patch
    classification = {"is_incident": True, "category": "plumbing", "severity": "high", "confidence": 0.92}
    for msg_id, body_text in [("msg-m1", "Issue M1"), ("msg-m2", "Issue M2")]:
        payload = {
            "event": "message.received",
            "data": {"id": msg_id, "type": "chat", "isGroup": True, "chatId": "2@g.us",
                     "chat": {"name": "Block B"}, "author": "2541@c.us",
                     "body": body_text, "timestamp": 1782293340},
        }
        with patch("main.classify_message", new=AsyncMock(return_value=classification)):
            with patch("main.push_incident", new=AsyncMock()):
                await client.post("/api/v1/ops/ingest", json=payload, headers={"X-API-Key": "test-secret"})

    all_ids = [i["id"] for i in (await client.get("/incidents")).json()]
    await client.patch(f"/incidents/{all_ids[0]}/status",
                       json={"status": "resolved"}, headers={"X-API-Key": "test-secret"})

    both = (await client.get("/incidents?statuses=resolved&statuses=review")).json()
    assert len(both) == 2


async def test_archive_route_returns_html(client):
    r = await client.get("/archive")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


async def test_archive_route_shows_only_resolved_incidents(client):
    from unittest.mock import AsyncMock, patch
    classification = {"is_incident": True, "category": "plumbing", "severity": "high", "confidence": 0.92}
    payload_live = {
        "event": "message.received",
        "data": {"id": "msg-arc1", "type": "chat", "isGroup": True, "chatId": "3@g.us",
                 "chat": {"name": "Live Property"}, "author": "2541@c.us",
                 "body": "Live issue", "timestamp": 1782293340},
    }
    payload_resolved = {
        "event": "message.received",
        "data": {"id": "msg-arc2", "type": "chat", "isGroup": True, "chatId": "3@g.us",
                 "chat": {"name": "Resolved Property"}, "author": "2541@c.us",
                 "body": "Resolved issue", "timestamp": 1782293341},
    }
    with patch("main.classify_message", new=AsyncMock(return_value=classification)):
        with patch("main.push_incident", new=AsyncMock()):
            await client.post("/api/v1/ops/ingest", json=payload_live, headers={"X-API-Key": "test-secret"})
            await client.post("/api/v1/ops/ingest", json=payload_resolved, headers={"X-API-Key": "test-secret"})

    all_ids = sorted([i["id"] for i in (await client.get("/incidents")).json()])
    await client.patch(f"/incidents/{all_ids[-1]}/status",
                       json={"status": "resolved"}, headers={"X-API-Key": "test-secret"})

    r = await client.get("/archive")
    assert r.status_code == 200
    assert b"Resolved Property" in r.content
    assert b"Live Property" not in r.content


async def test_live_dashboard_excludes_resolved_incidents(client):
    from unittest.mock import AsyncMock, patch
    classification = {"is_incident": True, "category": "plumbing", "severity": "high", "confidence": 0.92}
    payload = {
        "event": "message.received",
        "data": {"id": "msg-exc1", "type": "chat", "isGroup": True, "chatId": "4@g.us",
                 "chat": {"name": "Exclude Me"}, "author": "2541@c.us",
                 "body": "To be resolved", "timestamp": 1782293340},
    }
    with patch("main.classify_message", new=AsyncMock(return_value=classification)):
        with patch("main.push_incident", new=AsyncMock()):
            await client.post("/api/v1/ops/ingest", json=payload, headers={"X-API-Key": "test-secret"})

    incident_id = (await client.get("/incidents")).json()[0]["id"]
    await client.patch(f"/incidents/{incident_id}/status",
                       json={"status": "resolved"}, headers={"X-API-Key": "test-secret"})

    r = await client.get("/")
    assert r.status_code == 200
    assert b"Exclude Me" not in r.content
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /home/abhishek/projects/whatsappgroup/whatsapp-ticketing/backend && .venv/bin/python -m pytest tests/test_dashboard.py -k "statuses or archive or excludes" -v
```
Expected: 5 failures — routes/params not yet defined.

- [ ] **Step 3: Add `Query` to FastAPI imports in `backend/main.py`**

Find the line:
```python
from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
```
Replace with:
```python
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
```

- [ ] **Step 4: Update `list_incidents` to accept `statuses` param**

Find the function signature:
```python
@app.get("/incidents")
async def list_incidents(
    since_id: Optional[int] = None,
    db: AsyncSession = Depends(get_db),
):
```
Replace with:
```python
@app.get("/incidents")
async def list_incidents(
    since_id: Optional[int] = None,
    statuses: Optional[list[str]] = Query(default=None),
    db: AsyncSession = Depends(get_db),
):
```

Then find the query block inside `list_incidents`:
```python
    query = (
        select(Incident, update_count_sq.label("uc"), media_count_sq.label("mc"))
        .order_by(Incident.received_at.desc())
    )
    if since_id is not None:
        query = query.where(Incident.id > since_id)
    result = await db.execute(query)
```
Replace with:
```python
    query = (
        select(Incident, update_count_sq.label("uc"), media_count_sq.label("mc"))
        .order_by(Incident.received_at.desc())
    )
    if since_id is not None:
        query = query.where(Incident.id > since_id)
    if statuses is not None:
        query = query.where(Incident.status.in_(statuses))
    result = await db.execute(query)
```

- [ ] **Step 5: Update the `GET /` dashboard route to exclude resolved and pass `mode="live"`**

Find the existing `dashboard` function's query (the `result = await db.execute(...)` block):
```python
    result = await db.execute(
        select(Incident, update_count_sq.label("uc"), media_count_sq.label("mc"))
        .order_by(Incident.received_at.desc())
    )
```
Replace with:
```python
    result = await db.execute(
        select(Incident, update_count_sq.label("uc"), media_count_sq.label("mc"))
        .where(~Incident.status.in_(["resolved"]))
        .order_by(Incident.received_at.desc())
    )
```

Then find the `templates.TemplateResponse` call in the `dashboard` function:
```python
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "incidents": incidents,
            "incidents_with_counts": incidents_with_counts,
            "title": os.getenv("DASHBOARD_TITLE", "Ops Incident Monitor"),
            "api_key": GATEWAY_SECRET_TOKEN,
        },
```
Replace with:
```python
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "incidents": incidents,
            "incidents_with_counts": incidents_with_counts,
            "title": os.getenv("DASHBOARD_TITLE", "Ops Incident Monitor"),
            "api_key": GATEWAY_SECRET_TOKEN,
            "mode": "live",
        },
```

- [ ] **Step 6: Add the `GET /archive` route to `backend/main.py`**

Add this function immediately after the closing `}` of the `dashboard` function (before `if __name__` or end of file):

```python
@app.get("/archive", response_class=HTMLResponse)
async def archive_dashboard(request: Request, db: AsyncSession = Depends(get_db)):
    update_count_sq = (
        select(func.count(IncidentUpdate.id))
        .where(IncidentUpdate.incident_id == Incident.id)
        .correlate(Incident)
        .scalar_subquery()
    )
    media_count_sq = (
        select(func.count(IncidentMedia.id))
        .where(IncidentMedia.incident_id == Incident.id)
        .correlate(Incident)
        .scalar_subquery()
    )
    result = await db.execute(
        select(Incident, update_count_sq.label("uc"), media_count_sq.label("mc"))
        .where(Incident.status == "resolved")
        .order_by(Incident.received_at.desc())
    )
    rows = result.all()
    incidents_with_counts = [
        {"incident": i, "update_count": uc, "media_count": mc}
        for i, uc, mc in rows
    ]
    incidents = [row["incident"] for row in incidents_with_counts]
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "incidents": incidents,
            "incidents_with_counts": incidents_with_counts,
            "title": os.getenv("DASHBOARD_TITLE", "Ops Incident Monitor"),
            "api_key": GATEWAY_SECRET_TOKEN,
            "mode": "archive",
        },
    )
```

- [ ] **Step 7: Run tests**

```bash
cd /home/abhishek/projects/whatsappgroup/whatsapp-ticketing/backend && .venv/bin/python -m pytest tests/ -v --tb=short 2>&1 | tail -15
```
Expected: all 59 tests pass (54 existing + 5 new).

- [ ] **Step 8: Commit**

```bash
git -C /home/abhishek/projects/whatsappgroup/whatsapp-ticketing add backend/main.py backend/tests/test_dashboard.py
git -C /home/abhishek/projects/whatsappgroup/whatsapp-ticketing commit -m "feat: add GET /archive route, statuses filter param, exclude resolved from live board"
```

---

## Task 2: Template — mode-aware dashboard

**Files:**
- Modify: `backend/templates/dashboard.html`

This task makes all template changes in one commit. The steps below are ordered from least to most risky so each can be verified independently before the next.

- [ ] **Step 1: Add CSS for card exit animation**

Inside the `<style>` block, find the closing `</style>` tag and add just before it:

```css
    .card.removing {
      transition: opacity 0.25s ease, max-height 0.3s ease, margin-bottom 0.3s ease;
      opacity: 0;
      max-height: 0 !important;
      overflow: hidden;
      margin-bottom: 0;
    }
```

- [ ] **Step 2: Add `MODE` JS constant**

In the `<script>` block, find:
```javascript
const API_KEY = '{{ api_key }}';
```
Replace with:
```javascript
const API_KEY = '{{ api_key }}';
const MODE = '{{ mode | default("live") }}';
```

- [ ] **Step 3: Update topbar eyebrow, subline, and add nav link**

Find the brand block:
```html
          <div class="eyebrow">Incident Command Centre</div>
          <h1>{{ title }}</h1>
          <div class="subline">Monitor WhatsApp property group incidents, triage priority cases, and update status.</div>
```
Replace with:
```html
          <div class="eyebrow">{% if mode == 'archive' %}Incident Archive{% else %}Incident Command Centre{% endif %}</div>
          <h1>{{ title }}</h1>
          <div class="subline">{% if mode == 'archive' %}Browse and reopen resolved incidents.{% else %}Monitor WhatsApp property group incidents, triage priority cases, and update status.{% endif %}</div>
```

Then find the closing `</header>` tag and add just before it:
```html
      {% if mode == 'archive' %}
      <a href="/" class="ghost-btn" style="align-self:center;text-decoration:none;white-space:nowrap">← Live Board</a>
      {% else %}
      <a href="/archive" class="ghost-btn" style="align-self:center;text-decoration:none;white-space:nowrap">Archive →</a>
      {% endif %}
```

- [ ] **Step 4: Make stats bar mode-aware**

Find the entire `<div class="stats">` block:
```html
      <div class="stats">
        <div class="stat red">
          <div class="n" id="stat-high">0</div>
          <div class="l">High · Open</div>
        </div>
        <div class="stat purple">
          <div class="n" id="stat-review">0</div>
          <div class="l">Needs Review</div>
        </div>
        <div class="stat green">
          <div class="n" id="stat-open">0</div>
          <div class="l">Active Open</div>
        </div>
        <div class="stat">
          <div class="n" id="stat-total">0</div>
          <div class="l">Total Today</div>
        </div>
      </div>
```
Replace with:
```html
      {% if mode == 'archive' %}
      <div class="stats">
        <div class="stat">
          <div class="n" id="stat-total">0</div>
          <div class="l">Total Resolved</div>
        </div>
      </div>
      {% else %}
      <div class="stats">
        <div class="stat red">
          <div class="n" id="stat-high">0</div>
          <div class="l">High · Open</div>
        </div>
        <div class="stat purple">
          <div class="n" id="stat-review">0</div>
          <div class="l">Needs Review</div>
        </div>
        <div class="stat green">
          <div class="n" id="stat-open">0</div>
          <div class="l">Active Open</div>
        </div>
        <div class="stat">
          <div class="n" id="stat-total">0</div>
          <div class="l">Total Today</div>
        </div>
      </div>
      {% endif %}
```

- [ ] **Step 5: Make status filter sidebar mode-aware**

Find the entire status filter group (from `<div class="filter-group collapsible" data-group="status">` to its closing `</div>`):
```html
          <div class="filter-group collapsible" data-group="status">
            <button type="button" class="filter-header" onclick="toggleFilterGroup(this)">
              <h4>Status</h4>
              <div class="filter-header-meta"><span class="cnt" id="selected-status-count">0</span><span class="filter-chevron">⌄</span></div>
            </button>
            <div class="filter-content">
              <div class="fopt" data-filter="status" data-val="review" onclick="toggleFilter(this)"><span class="left"><span class="dot" style="background:#a855f7"></span>Review</span><span class="cnt" data-cnt="review">0</span></div>
              <div class="fopt" data-filter="status" data-val="new" onclick="toggleFilter(this)"><span class="left"><span class="dot" style="background:#38bdf8"></span>New</span><span class="cnt" data-cnt="new">0</span></div>
              <div class="fopt" data-filter="status" data-val="acknowledged" onclick="toggleFilter(this)"><span class="left"><span class="dot" style="background:#14b8a6"></span>Acknowledged</span><span class="cnt" data-cnt="acknowledged">0</span></div>
              <div class="fopt" data-filter="status" data-val="resolved" onclick="toggleFilter(this)"><span class="left"><span class="dot" style="background:#94a3b8"></span>Resolved</span><span class="cnt" data-cnt="resolved">0</span></div>
              <div class="fopt" data-filter="status" data-val="ignored" onclick="toggleFilter(this)"><span class="left"><span class="dot" style="background:#6b7280"></span>Ignored</span><span class="cnt" data-cnt="ignored">0</span></div>
            </div>
          </div>
```
Replace with:
```html
          {% if mode != 'archive' %}
          <div class="filter-group collapsible" data-group="status">
            <button type="button" class="filter-header" onclick="toggleFilterGroup(this)">
              <h4>Status</h4>
              <div class="filter-header-meta"><span class="cnt" id="selected-status-count">0</span><span class="filter-chevron">⌄</span></div>
            </button>
            <div class="filter-content">
              <div class="fopt" data-filter="status" data-val="review" onclick="toggleFilter(this)"><span class="left"><span class="dot" style="background:#a855f7"></span>Review</span><span class="cnt" data-cnt="review">0</span></div>
              <div class="fopt" data-filter="status" data-val="new" onclick="toggleFilter(this)"><span class="left"><span class="dot" style="background:#38bdf8"></span>New</span><span class="cnt" data-cnt="new">0</span></div>
              <div class="fopt" data-filter="status" data-val="acknowledged" onclick="toggleFilter(this)"><span class="left"><span class="dot" style="background:#14b8a6"></span>Acknowledged</span><span class="cnt" data-cnt="acknowledged">0</span></div>
              <div class="fopt" data-filter="status" data-val="ignored" onclick="toggleFilter(this)"><span class="left"><span class="dot" style="background:#6b7280"></span>Ignored</span><span class="cnt" data-cnt="ignored">0</span></div>
            </div>
          </div>
          {% endif %}
```
Note: "Resolved" filter option is removed from the live board since resolved incidents never appear there.

- [ ] **Step 6: Make toolbar title mode-aware**

Find:
```html
            <div class="toolbar-title">Live incident queue</div>
```
Replace with:
```html
            <div class="toolbar-title">{% if mode == 'archive' %}Resolved incidents{% else %}Live incident queue{% endif %}</div>
```

- [ ] **Step 7: Make Jinja2 card action buttons mode-aware**

Find the actions div in the Jinja2 card template:
```html
                    <div class="actions" id="actions-{{ i.id }}">
                      <button class="act-btn btn-ack" onclick="setStatus({{ i.id }}, 'acknowledged', event)">✓ Acknowledge</button>
                      <button class="act-btn btn-resolve" onclick="setStatus({{ i.id }}, 'resolved', event)">✓ Resolve</button>
                      <button class="act-btn btn-ignore" onclick="setStatus({{ i.id }}, 'ignored', event)">✗ Ignore</button>
                      <button class="act-btn btn-review" onclick="setStatus({{ i.id }}, 'review', event)">⟳ Send to Review</button>
                    </div>
```
Replace with:
```html
                    <div class="actions" id="actions-{{ i.id }}">
                      {% if mode == 'archive' %}
                      <button class="act-btn btn-review" onclick="setStatus({{ i.id }}, 'review', event)">↩ Reopen</button>
                      {% else %}
                      <button class="act-btn btn-ack" onclick="setStatus({{ i.id }}, 'acknowledged', event)">✓ Acknowledge</button>
                      <button class="act-btn btn-resolve" onclick="setStatus({{ i.id }}, 'resolved', event)">✓ Resolve</button>
                      <button class="act-btn btn-ignore" onclick="setStatus({{ i.id }}, 'ignored', event)">✗ Ignore</button>
                      <button class="act-btn btn-review" onclick="setStatus({{ i.id }}, 'review', event)">⟳ Send to Review</button>
                      {% endif %}
                    </div>
```

- [ ] **Step 8: Make `buildCard` JS action buttons mode-aware**

In the `buildCard` function, find the actions div in the JS template literal:
```javascript
        <div class="actions" id="actions-${i.id}">
          <button class="act-btn btn-ack" onclick="setStatus(${i.id}, 'acknowledged', event)">✓ Acknowledge</button>
          <button class="act-btn btn-resolve" onclick="setStatus(${i.id}, 'resolved', event)">✓ Resolve</button>
          <button class="act-btn btn-ignore" onclick="setStatus(${i.id}, 'ignored', event)">✗ Ignore</button>
          <button class="act-btn btn-review" onclick="setStatus(${i.id}, 'review', event)">⟳ Send to Review</button>
        </div>
```
Replace with:
```javascript
        <div class="actions" id="actions-${i.id}">
          ${MODE === 'archive'
            ? `<button class="act-btn btn-review" onclick="setStatus(${i.id}, 'review', event)">↩ Reopen</button>`
            : `<button class="act-btn btn-ack" onclick="setStatus(${i.id}, 'acknowledged', event)">✓ Acknowledge</button>
               <button class="act-btn btn-resolve" onclick="setStatus(${i.id}, 'resolved', event)">✓ Resolve</button>
               <button class="act-btn btn-ignore" onclick="setStatus(${i.id}, 'ignored', event)">✗ Ignore</button>
               <button class="act-btn btn-review" onclick="setStatus(${i.id}, 'review', event)">⟳ Send to Review</button>`}
        </div>
```

- [ ] **Step 9: Replace `setStatus` to animate-out on resolve/reopen**

Find the entire `try` block inside `setStatus` (from `if (!r.ok)` to the `showToast` call):
```javascript
    if (!r.ok) throw new Error('Failed to update status');

    const card = document.querySelector(`.card[data-id="${id}"]`);
    if (card) card.dataset.status = newStatus;

    const badge = document.getElementById(`status-badge-${id}`);
    if (badge) {
      badge.className = `badge badge-${newStatus}`;
      badge.textContent = statusLabels[newStatus] || newStatus;
    }

    const inc = allIncidents.find(i => i.id === id);
    if (inc) inc.status = newStatus;

    updateStats();
    updateCounts();
    applyFilters();
    showToast(`Incident #${id} marked as ${newStatus}`);
```
Replace with:
```javascript
    if (!r.ok) throw new Error('Failed to update status');

    const inc = allIncidents.find(i => i.id === id);
    if (inc) inc.status = newStatus;

    const shouldRemove =
      (MODE === 'live' && newStatus === 'resolved') ||
      (MODE === 'archive' && newStatus !== 'resolved');

    if (shouldRemove) {
      const card = document.querySelector(`.card[data-id="${id}"]`);
      if (card) {
        card.classList.add('removing');
        setTimeout(() => {
          card.remove();
          const idx = allIncidents.findIndex(i => i.id === id);
          if (idx !== -1) allIncidents.splice(idx, 1);
          updateStats();
          updateCounts();
          applyFilters();
        }, 320);
      }
    } else {
      const card = document.querySelector(`.card[data-id="${id}"]`);
      if (card) card.dataset.status = newStatus;
      const badge = document.getElementById(`status-badge-${id}`);
      if (badge) {
        badge.className = `badge badge-${newStatus}`;
        badge.textContent = statusLabels[newStatus] || newStatus;
      }
      updateStats();
      updateCounts();
      applyFilters();
    }
    showToast(`Incident #${id} marked as ${newStatus}`);
```

- [ ] **Step 10: Make `updateStats` mode-aware**

Find the entire `function updateStats()`:
```javascript
function updateStats() {
  const highOpen = allIncidents.filter(i => i.severity === 'high' && !['resolved','ignored'].includes(i.status)).length;
  const inReview = allIncidents.filter(i => i.status === 'review').length;
  const activeOpen = allIncidents.filter(i => !['resolved','ignored'].includes(i.status)).length;

  document.getElementById('stat-high').textContent = highOpen;
  document.getElementById('stat-review').textContent = inReview;
  document.getElementById('stat-open').textContent = activeOpen;
  document.getElementById('stat-total').textContent = allIncidents.length;
}
```
Replace with:
```javascript
function updateStats() {
  if (MODE === 'archive') {
    const total = document.getElementById('stat-total');
    if (total) total.textContent = allIncidents.length;
    return;
  }
  const highOpen = allIncidents.filter(i => i.severity === 'high' && !['resolved','ignored'].includes(i.status)).length;
  const inReview = allIncidents.filter(i => i.status === 'review').length;
  const activeOpen = allIncidents.filter(i => !['resolved','ignored'].includes(i.status)).length;
  document.getElementById('stat-high').textContent = highOpen;
  document.getElementById('stat-review').textContent = inReview;
  document.getElementById('stat-open').textContent = activeOpen;
  document.getElementById('stat-total').textContent = allIncidents.length;
}
```

- [ ] **Step 11: Make `poll` mode-aware (URL and filter)**

Find the `poll` function:
```javascript
async function poll() {
  try {
    const data = await fetch('/incidents?since_id=' + maxId).then(r => r.json());
    if (!Array.isArray(data) || data.length === 0) return;

    const newMax = Math.max(...data.map(i => i.id));
    if (newMax > maxId) maxId = newMax;

    const existingPending = new Set(pendingNew.map(i => i.id));
    pendingNew = [...pendingNew, ...data.filter(i => !existingPending.has(i.id))];

    document.getElementById('new-count').textContent = pendingNew.length;
    document.getElementById('new-banner').style.display = 'block';
    showToast(`${pendingNew.length} new incident${pendingNew.length === 1 ? '' : 's'} waiting to load`);

    const highOpen = [...allIncidents, ...pendingNew].filter(
      i => i.severity === 'high' && !['resolved','ignored'].includes(i.status)
    ).length;
    document.title = highOpen > 0 ? `(${highOpen} HIGH) {{ title }}` : '{{ title }}';
  } catch(e) {
    console.warn('Incident polling failed', e);
  }
}
```
Replace with:
```javascript
async function poll() {
  try {
    const pollUrl = MODE === 'archive'
      ? '/incidents?statuses=resolved&since_id=' + maxId
      : '/incidents?since_id=' + maxId;
    const data = await fetch(pollUrl).then(r => r.json());
    if (!Array.isArray(data) || data.length === 0) return;

    const newMax = Math.max(...data.map(i => i.id));
    if (newMax > maxId) maxId = newMax;

    const incoming = MODE === 'live'
      ? data.filter(i => i.status !== 'resolved')
      : data;

    const existingPending = new Set(pendingNew.map(i => i.id));
    pendingNew = [...pendingNew, ...incoming.filter(i => !existingPending.has(i.id))];
    if (pendingNew.length === 0) return;

    document.getElementById('new-count').textContent = pendingNew.length;
    document.getElementById('new-banner').style.display = 'block';
    showToast(`${pendingNew.length} new incident${pendingNew.length === 1 ? '' : 's'} waiting to load`);

    if (MODE === 'live') {
      const highOpen = [...allIncidents, ...pendingNew].filter(
        i => i.severity === 'high' && !['resolved','ignored'].includes(i.status)
      ).length;
      document.title = highOpen > 0 ? `(${highOpen} HIGH) {{ title }}` : '{{ title }}';
    }
  } catch(e) {
    console.warn('Incident polling failed', e);
  }
}
```

- [ ] **Step 12: Run full test suite**

```bash
cd /home/abhishek/projects/whatsappgroup/whatsapp-ticketing/backend && .venv/bin/python -m pytest tests/ -v --tb=short 2>&1 | tail -15
```
Expected: all 59 tests PASS.

- [ ] **Step 13: Commit**

```bash
git -C /home/abhishek/projects/whatsappgroup/whatsapp-ticketing add backend/templates/dashboard.html
git -C /home/abhishek/projects/whatsappgroup/whatsapp-ticketing commit -m "feat: add mode-aware archive dashboard — badges, nav link, card animation, poll"
```

---

## Spec Coverage Check

| Spec requirement | Task |
|---|---|
| `GET /archive` route — resolved incidents only | Task 1 Step 6 |
| `GET /` excludes resolved, passes `mode="live"` | Task 1 Step 5 |
| `GET /incidents?statuses=resolved` filter | Task 1 Step 4 |
| `MODE` JS constant injected from Jinja2 | Task 2 Step 2 |
| Topbar nav link (Archive ↔ Live Board) | Task 2 Step 3 |
| Page identity (eyebrow, subline) | Task 2 Step 3 |
| Stats bar — archive shows only Total Resolved | Task 2 Step 4 |
| Status filter — Resolved removed on live; hidden on archive | Task 2 Step 5 |
| Toolbar title | Task 2 Step 6 |
| Card action buttons — Reopen on archive, triage on live | Task 2 Steps 7–8 |
| `.card.removing` CSS exit animation | Task 2 Step 1 |
| `setStatus` animates card out on resolve/reopen | Task 2 Step 9 |
| `updateStats` mode guard (archive only has stat-total) | Task 2 Step 10 |
| `poll` uses `?statuses=resolved` on archive; filters resolved on live | Task 2 Step 11 |
| Empty state on archive with no resolved incidents | Handled by existing `{% else %}` empty-state in template (no change needed — text "No incidents yet" shows) |
