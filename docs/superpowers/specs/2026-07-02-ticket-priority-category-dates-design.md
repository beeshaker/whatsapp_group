# Ticket Detail — Priority, Category, End Date & Escalation

**Date:** 2026-07-02
**Status:** Approved

## Overview

This is Group A of a larger 6-item request (see conversation). It adds admin-editable **priority**, **category**, **end date** (deadline), and **escalated** fields to the existing ticket detail modal in `backend/templates/dashboard.html`. The modal (`openDetailModal` / `renderDetailModal`) already exists and shows message body, updates, status history, activity, and attachments — this spec adds an editable "Ticket details" section to it, backed by one new PATCH endpoint.

Two follow-on groups build on this work but are **out of scope here**:
- **Group C** (reminder timers) will add a scheduled job that auto-flips `escalated` to `true` when `end_date` passes without resolution, and sends WhatsApp reminders. This spec only adds the field and a manual toggle.
- **Group B** (multi-ticket creation) and **Group D** (billing group-lock) are unrelated services, specced separately.

---

## 1. Data Model

`backend/models.py`, `Incident` class:

- `severity: Mapped[str]` (String(20)) is **renamed** to `priority: Mapped[str]`. Column name changes from `severity` to `priority` in the DB.
- Valid values change from `{"low", "medium", "high"}` to `{"low", "medium", "high", "urgent"}`.
- New column: `end_date: Mapped[Optional[datetime]]` (`DateTime(timezone=True)`, nullable) — the deadline, admin-set only, never auto-filled.
- New column: `escalated: Mapped[bool]` (`Boolean`, `nullable=False`, `default=False`, `server_default="false"`).

### Migration (`backend/database.py`, `init_db()`)

Follows the existing pattern: each migration runs in its own `try/except` transaction so an expected failure (column already exists/renamed) doesn't abort the ones that follow (see `updated_at`, `relinked`, `role` migrations at lines ~30-120):

```python
try:
    async with engine.begin() as conn:
        await conn.execute(text("ALTER TABLE incidents RENAME COLUMN severity TO priority"))
except Exception:
    pass

try:
    async with engine.begin() as conn:
        await conn.execute(text("ALTER TABLE incidents ADD COLUMN end_date TIMESTAMP"))
except Exception:
    pass

try:
    async with engine.begin() as conn:
        await conn.execute(text(
            "ALTER TABLE incidents ADD COLUMN escalated BOOLEAN NOT NULL DEFAULT FALSE"
        ))
except Exception:
    pass
```

Existing values in the renamed column (`"low"`, `"medium"`, `"high"`) remain valid under the new 4-value `priority` enum — no data rewrite needed. No existing row is ever assigned `"urgent"` automatically.

Every other reference to `Incident.severity` / `"severity"` in `backend/main.py` (lines ~446, 477-490, 545, 791, 890) and `classifier.py` is renamed to `priority`.

---

## 2. Classifier Changes

`backend/classifier.py`:

- The prompt's severity instructions are relabeled to "priority" with four options: `low`, `medium`, `high`, `urgent`.
- `classify_message()` still returns a `priority` key (was `severity`) with an AI-assigned initial value, same as today — admins can change it afterward, they don't have to.
- Post-parse validation set becomes `{"low", "medium", "high", "urgent"}`; unrecognized values fall back to `"medium"` (mirrors today's fallback behavior for severity).

---

## 3. Backend API

### `PATCH /incidents/{incident_id}`

New general-purpose update endpoint, admin-only (`Depends(require_admin)` — covers both `admin` and `super_admin` roles per the existing dependency).

Request body (all fields optional, at least one required):
```json
{
  "priority": "urgent",
  "category": "plumbing",
  "end_date": "2026-07-10T00:00:00Z",
  "escalated": true
}
```

Validation:
- `priority`, if present, must be one of `low` / `medium` / `high` / `urgent` → 422 otherwise.
- `category`, if present, must be an existing `incident_categories.slug` → 422 otherwise.
- `end_date`, if present, must be a valid ISO datetime or `null` (to clear it).
- `escalated`, if present, must be boolean.

Behavior:
- Loads the incident (404 if missing).
- Applies only the fields present in the body.
- Writes an `AuditLog` row per changed field (pattern matches the existing status-change audit log at `main.py:1033-1040`), e.g. `"priority: medium → urgent"`.
- Returns the updated incident's `{id, priority, category, end_date, escalated}`.

The existing `GET /incidents/{incident_id}` response (`main.py:889-890`) gains `end_date` and `escalated`; `severity` key is renamed to `priority`.

---

## 4. Frontend — Detail Modal

In `renderDetailModal()` (`dashboard.html`), a new **"Ticket details"** section is added above "Original report", visible to everyone but only editable when `CURRENT_ROLE` is `admin` or `super_admin` (a new `const CURRENT_ROLE = {{ role | tojson }};` is added alongside the existing `CURRENT_USER` line, sourced from the same Jinja `role` variable already used elsewhere in this template for nav visibility).

```
Ticket details
  Priority:  [ Urgent ▾ ]        Category: [ Plumbing ▾ ]
  End date:  [ 2026-07-10 ]      Escalated: [ Escalate ]   <- toggles to "Un-escalate" when true
```

- Priority `<select>`: Low / Medium / High / Urgent.
- Category `<select>`: populated from the existing categories list (already fetched for the sidebar filter; reused here, not a new API call).
- End date: `<input type="date">`, empty means no deadline set.
- Escalated: a toggle button, not a checkbox, matching the existing `act-btn` style used for status buttons — labeled "Escalate" (red-tinted) when `false`, "Un-escalate" when `true`.
- Non-admins (`user` role) see these same four values rendered as plain read-only text, no inputs.

**Save behavior:** each control fires its PATCH on change (`<select>` change event, date `input` blur/change event, button click) — no separate save button, consistent with how `setStatus()` already works instantly today. On success, the modal is re-rendered via `openDetailModal(_currentDetailId)` (same pattern `sendReplyFromBar()` uses) and a toast confirms; on failure, a toast shows the error and the control reverts to its prior value.

A new JS function `updateTicketField(incidentId, field, value)` wraps the PATCH call and is shared by all four controls.

---

## 5. Error Handling

| Scenario | Behavior |
|---|---|
| Non-admin calls PATCH directly (bypassing UI) | 403 from `require_admin` |
| Invalid `priority` / `category` value | 422, toast shows error, control reverts |
| Incident not found | 404 |
| `end_date` cleared (set to empty string) | Treated as `null` — deadline removed |
| Network/API failure mid-update | Toast: "Failed to update — please try again", control reverts to previous value |

---

## 6. Testing

- Migration test: renamed column preserves existing `low`/`medium`/`high` values; new `end_date`/`escalated` columns exist with correct defaults.
- `PATCH /incidents/{id}` endpoint tests: admin-only enforcement (403 for `user` role), valid partial updates, invalid enum values (422), invalid category slug (422), audit log entries created.
- `classifier.py` test: prompt/parsing produces one of the four priority values, unrecognized values fall back to `medium`.
- Manual browser verification: open a ticket, change each of the four fields, confirm persistence on reload, confirm `user`-role account sees read-only values.

---

## 7. Out of Scope

- Automatic escalation on overdue `end_date` (Group C).
- WhatsApp reminder messages tied to `end_date` (Group C).
- Multi-level escalation (binary only, per decision).
- Regular admins managing the category list itself (already exists, super-admin only, unchanged).
- Start date field (intentionally decided to just mean ticket creation time, i.e. existing `received_at` — no new column).
