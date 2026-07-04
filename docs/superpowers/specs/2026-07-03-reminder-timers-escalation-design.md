# Per-Ticket Reminder Timers & Auto-Escalation

**Date:** 2026-07-03
**Status:** Approved

## Overview

This is Group C of a larger 6-item ticketing request (Group A — priority/category/dates/escalation UI — and Group B — multi-ticket message splitting — already specced/shipped). Group A added an `end_date` (deadline) field and a manually-toggled `escalated` flag to tickets, and explicitly deferred two things to this spec: (1) letting admins configure a reminder to be sent to the ticket's WhatsApp group as its deadline approaches, and (2) automatically flipping `escalated` to `true` once a ticket's deadline passes without resolution.

Groups B (multi-ticket splitting) and D (billing group-lock) are unrelated and specced separately.

---

## 1. Data Model

`Incident` gains two nullable columns:

- `reminder_offset_hours: Mapped[Optional[int]]` (`Integer`, nullable) — hours before `end_date` to send the one-time reminder. `0` = at the deadline itself; `1`, `6`, or `24` = that many hours before. `None` (the default — reminders are opt-in per ticket) means reminders are disabled for this ticket.
- `reminder_sent_at: Mapped[Optional[datetime]]` (`DateTime(timezone=True)`, nullable) — set the moment the reminder is sent, so the scheduler never sends it twice. `None` means not yet sent.

No new column is needed for escalation — `Incident.escalated` already exists from Group A.

Migration follows the existing `database.py` try/except-per-statement pattern:
```python
ALTER TABLE incidents ADD COLUMN reminder_offset_hours INTEGER
ALTER TABLE incidents ADD COLUMN reminder_sent_at TIMESTAMP
```

---

## 2. Scheduler Job (`main.py`)

A new job function, `_check_ticket_reminders()`, is registered on the same `AsyncIOScheduler` instance already created in `lifespan()` (currently running only the daily `_push_summaries` job). Unlike that job's once-daily `CronTrigger`, this one uses an `IntervalTrigger(minutes=15)` — reminders need finer granularity to land close to their configured hour-offset.

```python
from apscheduler.triggers.interval import IntervalTrigger
# ...
scheduler.add_job(_check_ticket_reminders, IntervalTrigger(minutes=15))
```

Each run, query all open tickets (`status NOT IN ('resolved', 'ignored')`) that have an `end_date` set. For each one, two fully independent checks run (both can fire in the same run for the same ticket — see below):

**Reminder check:**
```
reminder_offset_hours IS NOT NULL
AND reminder_sent_at IS NULL
AND now() >= end_date - reminder_offset_hours (as a timedelta)
```
On match: send a WhatsApp message to the ticket's own `group_id` via the existing `send_group_message(chat_id, text)` (not an admin DM — this goes into the group the ticket came from), then set `reminder_sent_at = now()`.

**Escalation check (fully decoupled from the reminder):**
```
NOT escalated
AND now() >= end_date
```
On match: set `escalated = True`, and also send a WhatsApp message to the group announcing the escalation, via the same `send_group_message`.

These two conditions are deliberately independent: a ticket with no reminder configured still auto-escalates when overdue; a ticket configured with an "at deadline" (`reminder_offset_hours=0`) reminder will see both checks match around the same run and send two separate messages (the reminder, then — once truly overdue — the escalation notice). This overlap is accepted, not a bug.

Each ticket's checks run inside their own try/except so one failure (e.g. a `send_group_message` error) is logged and skipped without blocking the rest of the batch — matching the existing pattern in `_push_summaries`.

---

## 3. `PATCH /incidents/{incident_id}` Changes (`main.py`)

`TicketDetailUpdateBody` (from Group A) gains a fifth optional field:

```python
reminder_offset_hours: Optional[int] = None
```
validated against `{0, 1, 6, 24}` when present in the request (same "in `fields_set`" partial-update semantics as the existing four fields — omitted means untouched, an explicit value including `null` sets/clears it).

New side effect when `end_date` is present in the request body and its value differs from the current one:
- `reminder_sent_at` is reset to `None` (so a reminder can fire again against the new deadline).
- If the new `end_date` is in the future (later than `now()`), `escalated` is reset to `False` (extending a deadline undoes "this is overdue"). If the new `end_date` is not in the future, `escalated` is left as-is (don't un-escalate a ticket whose new deadline is still in the past).

Both are logged to `AuditLog` as part of the same per-field change list Group A already established, e.g. `"reminder_sent_at: 2026-07-04T10:00:00 → None (end_date changed)"`.

---

## 4. UI (`dashboard.html`)

The existing "Ticket details" section (added in Group A, admin/super_admin only) gains a fifth control, alongside Priority/Category/End date/Escalated:

- **Reminder** `<select>`: `None` / `At deadline` / `1 hour before` / `6 hours before` / `24 hours before`, mapping to `reminder_offset_hours` values `null`/`0`/`1`/`6`/`24`. Same auto-save-on-change pattern as the other four controls, calling the existing `updateTicketField(incidentId, 'reminder_offset_hours', value)`.
- Read-only `user`-role rendering shows the current setting as plain text (e.g. "Reminder: 6 hours before" or "Reminder: Off"), consistent with how the other four fields already render read-only.

---

## 5. Error Handling

| Scenario | Behavior |
|---|---|
| `send_group_message` fails for one ticket's reminder or escalation message | Logged, that ticket's check is skipped for this run; state (`reminder_sent_at`/`escalated`) is only updated after a successful send, so it will retry on the next 15-minute run |
| Invalid `reminder_offset_hours` value in PATCH body (not one of 0/1/6/24) | 422, same pattern as the existing `priority` validation |
| `end_date` cleared (`null`) while a reminder/escalation was pending | Ticket no longer matches either scheduler query (both require `end_date` set), so nothing fires until a new `end_date` is set |
| Ticket resolved/ignored before its reminder or escalation time | Scheduler query excludes non-open tickets, so nothing fires |

---

## 6. Testing

- **Migration tests**: both new columns exist, default to `NULL`.
- **Scheduler job tests**: reminder fires exactly once for a ticket past its offset time and not before; reminder does not re-fire on a later run (`reminder_sent_at` gate); escalation fires for an overdue ticket with no reminder configured at all; a ticket with `reminder_offset_hours=0` receives both messages; a resolved/ignored ticket past its deadline is skipped by both checks; a `send_group_message` failure for one ticket doesn't prevent other tickets' checks from running.
- **PATCH endpoint tests**: setting/clearing `reminder_offset_hours`; invalid value rejected with 422; changing `end_date` resets `reminder_sent_at` to `None`; changing `end_date` to a future date resets `escalated` to `False`; changing `end_date` to a past date leaves `escalated` untouched.
- **Manual UI verification**: Reminder dropdown appears in the ticket detail modal for admins, saves on change, renders as read-only text for the `user` role.

---

## 7. Out of Scope

- Repeating/recurring reminders (this spec is single-shot only, per the approved design).
- Per-client or global default reminder settings — every ticket starts with reminders disabled (`None`) and must be explicitly configured.
- Any change to the daily summary feature (`_push_summaries`) — it remains a separate, unrelated job on the same scheduler instance.
- Multi-level escalation (still binary, per Group A's decision — this spec only automates the existing binary flag).
