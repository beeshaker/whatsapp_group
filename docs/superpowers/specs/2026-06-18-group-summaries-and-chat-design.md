# Group Summaries & LLM Chat — Design Spec
**Date:** 2026-06-18  
**Status:** Approved

---

## Overview

Two related features added to the WhatsApp Ops Gateway:

1. **Daily / weekend summaries** — a scheduled engine that pushes per-group incident summaries to admin's personal WhatsApp numbers (Mon–Fri, 8am Kenya time). Also viewable on demand via a new Summaries tab on the dashboard.
2. **LLM chat assistant** — read-only natural language queries about incident status, available via a floating chat widget on the dashboard and via WhatsApp DM to the bot's number.

---

## 1. Data Model

Three new tables. No existing tables modified.

### `admin_profiles`
Extends `User` with optional notification settings. One row per admin user (created on first profile save).

| Column | Type | Notes |
|---|---|---|
| `user_id` | Integer, FK → `users.id`, PK | |
| `whatsapp_phone` | Text, nullable | Personal number e.g. `254712345678`. No `+`, no spaces. |

### `admin_group_subscriptions`
Controls which groups each admin receives WhatsApp summaries for. Opt-in per admin per group.

| Column | Type | Notes |
|---|---|---|
| `id` | Integer, PK |  |
| `user_id` | Integer, FK → `users.id` | |
| `group_id` | Text | WhatsApp chat ID (same value as `incidents.group_id`) |

Unique constraint on `(user_id, group_id)`.

### `chat_sessions`
Stores conversation history for context-aware LLM chat. One row per active session.

| Column | Type | Notes |
|---|---|---|
| `id` | Integer, PK | |
| `session_key` | Text, unique | `wa:{phone}` for WhatsApp DMs, `web:{user_id}` for dashboard |
| `messages` | JSON | List of `{role, content}` pairs. Capped at last 10 exchanges. |
| `updated_at` | DateTime (UTC) | Sessions older than 24h are reset on next use |

---

## 2. Summary Engine (`summaries.py`)

New module. No LLM involved — pure DB aggregation.

### `build_summary(group_id, date_from, date_to, db) → dict`

Queries `incidents` for the given group and time window (Kenya-midnight to Kenya-midnight). Returns:

```python
{
  "group_id": str,
  "period_label": str,          # e.g. "Tuesday 17 Jun" or "Weekend 14–15 Jun"
  "new_count": int,
  "resolved_count": int,
  "still_open_count": int,
  "new_incidents": [
    {"id": int, "title": str, "severity": str, "status": str}
  ],
  "open_backlog": {"high": int, "medium": int, "low": int}
}
```

`title` is the first 80 characters of `message_body`.

### `format_whatsapp_summary(summary, dashboard_url) → str`

Formats the dict as a WhatsApp message:

```
📊 Daily Summary — {group_id}
{period_label}

New issues: {new_count}
  🔴 High:  {title} ({status})
  ...

Still unresolved: {still_open_count}
  {high} high · {medium} medium · {low} low

Resolved: {resolved_count}

🔗 {dashboard_url}
```

### `GET /api/summaries`

Query params: `group_id` (optional), `date` (YYYY-MM-DD, optional — defaults to today).  
Returns a list of summary dicts (one per group if `group_id` omitted).  
Admin only. Computes live on each request.

**Date window logic:**
- If `date` is a Monday → window is the preceding Saturday 00:00 to Sunday 23:59 (Kenya time)
- Otherwise → midnight to midnight of the requested date (Kenya time)
- Weekend dates (Sat/Sun) are valid — returns the data for that day

### APScheduler — WhatsApp push

Registered inside the FastAPI lifespan using `APScheduler` (added to `requirements.txt`).

Schedule: `SUMMARY_SCHEDULE_HOUR` (default `8`) o'clock in `SUMMARY_TIMEZONE` (default `Africa/Nairobi`), Mon–Fri only.

At each fire:
1. Query all `admin_profiles` rows where `whatsapp_phone IS NOT NULL`
2. For each admin, query their `admin_group_subscriptions`
3. For each subscribed group, call `build_summary()` with the correct window (Monday → weekend, else yesterday)
4. If `new_count == 0` → **skip** (no WhatsApp message sent)
5. If `new_count > 0` → call `send_group_message(admin.whatsapp_phone + "@c.us", formatted_text)`

WhatsApp DM chat IDs use `{phone}@c.us` format.

---

## 3. LLM Chat (`chat.py`)

New module. Uses the same Ollama instance as the classifier.

### `answer_query(question, session_key, db) → str`

1. Load `chat_sessions` row for `session_key`. If missing or older than 24h, start fresh.
2. Build live context snapshot from DB:
   - Per group: open count, resolved today, breakdown by severity
   - Last 5 open high-severity incidents (title + group + age in days)
3. Construct Ollama prompt:
   ```
   System: You are a read-only incident management assistant for a property operations team.
   Today is {date} ({day_of_week}), timezone Africa/Nairobi.
   
   Current incident data:
   {context_snapshot}
   
   Answer questions concisely. Never suggest actions or pretend you can change data.
   If asked about something not in the data above, say so clearly.
   
   Conversation so far:
   {history}
   ```
4. Append new user message, call Ollama, get reply.
5. Append both user message and reply to `messages` list. Trim to last 10 pairs. Save.
6. Return reply text.

### `POST /api/chat`

Body: `{message: str}`.  
Session key: `web:{current_user.id}`.  
Returns: `{reply: str}`.  
Requires login. Admin only.

### WhatsApp DM routing

Change to the existing `ingest` endpoint (`POST /api/v1/ops/ingest`):

- If `chatId` ends in `@c.us` (DM, not a group):
  - Extract phone by stripping `@c.us` from `chatId` (e.g. `254712345678@c.us` → `254712345678`)
  - If phone matches any `admin_profiles.whatsapp_phone`:
    → call `answer_query(message_body, f"wa:{phone}", db)`
    → call `send_group_message(chatId, reply)` to send the answer back
    → return 200, do not create an incident
  - If phone is NOT a known admin → silently ignore (return 200, no reply)
- All `@g.us` messages continue through the existing incident creation flow unchanged

---

## 4. Dashboard UI

### 4a. Summaries Tab

New nav link "Summaries" (admin-only, hidden for regular users).

**Page layout:**
- Filter bar: Group dropdown (all groups from distinct `incidents.group_id`), Date picker (defaults to today/most recent weekday)
- One card per group showing the summary dict fields (new count, resolved, backlog by severity, list of new incidents with severity badge + status + "→ view" link to incident detail)
- Groups with `new_count == 0` and `still_open_count == 0` show a collapsed "Nothing to report" row
- Monday date selection automatically labels the period "Weekend Sat–Sun" and adjusts the window

### 4b. Admin Profile Page

New page at `/admin/profile` (admin-only).

Fields:
- **Personal WhatsApp number** — text input, saved to `admin_profiles.whatsapp_phone`. Hint text: "Include country code, no +. e.g. 254712345678"
- **Summary subscriptions** — checklist of all known groups (populated live from `SELECT DISTINCT group_id FROM incidents`). Checking a group opts in to receiving that group's WhatsApp summary. Groups are displayed by their raw WhatsApp chat ID (e.g. `120363XXXXXXXX@g.us`) — no human-readable group names are stored in the current data model. Admin will recognise their groups by ID.
- Save button — `PUT /api/admin/profile` + `POST /api/admin/subscriptions`

### 4c. Floating Chat Widget

Present on all pages (injected into base template). Visible to logged-in users only.

- Default state: 💬 bubble, bottom-right corner
- Expanded state: 320×420px panel, chat history, text input, Send button
- Sends `POST /api/chat`, appends reply to local history (not re-fetched from server — kept in JS memory for the page session)
- Shows a spinner while waiting for Ollama response
- Input disabled during in-flight request to prevent double-sends

---

## 5. API Endpoints

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/api/summaries` | Admin | Query params: `group_id`, `date`. Returns list of summary dicts. |
| `POST` | `/api/chat` | Admin | Body: `{message}`. Returns `{reply}`. |
| `GET` | `/api/admin/profile` | Admin | Returns phone + subscribed group IDs for current user. |
| `PUT` | `/api/admin/profile` | Admin | Body: `{whatsapp_phone}`. Upserts `admin_profiles`. |
| `POST` | `/api/admin/subscriptions` | Admin | Body: `{group_ids: [...]}`. Replaces all subscriptions for current user. |

---

## 6. Configuration

New env vars (added to `docker-compose.yml` environment block and `.env.example`):

| Variable | Default | Purpose |
|---|---|---|
| `SUMMARY_TIMEZONE` | `Africa/Nairobi` | Timezone for 8am schedule anchor and date-window boundaries |
| `SUMMARY_SCHEDULE_HOUR` | `8` | Hour of day (24h) for WhatsApp push |
| `DASHBOARD_URL` | `http://localhost:8000` | Base URL appended to WhatsApp summary messages |

New dependency: `apscheduler` added to `requirements.txt`.

---

## 7. Out of Scope

- Email delivery
- Admins triggering summaries manually (on-demand via dashboard is read, not push)
- LLM write actions (resolving, acknowledging incidents via chat)
- Summary archiving (summaries are always computed live; no historical summary records stored)
- Multi-language support

---

## 8. Testing Plan

- Unit tests for `build_summary()` — correct window logic for Monday vs weekday, correct counts
- Unit test for WhatsApp DM routing — `@c.us` vs `@g.us` branching, known vs unknown phone
- Unit test for `chat_sessions` expiry (24h reset)
- Integration test for `GET /api/summaries` — correct date math, admin-only gate
- Manual smoke test: register admin phone, send DM to bot, verify reply
