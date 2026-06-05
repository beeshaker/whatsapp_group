# Ticket Trail Modal Design

**Date:** 2026-06-05  
**Status:** Approved

## Overview

Replace the current detail modal (which shows original report, updates, and attachments in a minimal layout) with a wider, richer modal that displays the complete lifecycle of a ticket: the original report, all updates with clear sender/direction badges, a persisted status change history, and attachments — plus the reply compose box pinned at the bottom.

## Container

- Wide modal overlay (max-width 860px, 90vh max-height)
- Same trigger as today: clicking the update/attachment badge buttons on a card, plus the new "↩ Reply" button added to every expanded card
- Scrollable body; header and reply bar are fixed (do not scroll)

## Sections

### 1. Modal Header (fixed, does not scroll)

Displays: `#<id> — <property_name> · <category>`, severity badge, status badge, reporter name and phone. Close button (✕) top-right.

### 2. Original Report

The first message that created the incident. Shows reporter name, timestamp, and full message body. Styled with a dark box, distinct from the updates below it.

### 3. Updates

All `IncidentUpdate` rows for this incident, sorted by `received_at` ascending. Each row shows:

- **Inbound** (any `reporter_name` that is not `"Dashboard"`): neutral dark background, reporter name + timestamp
- **Outbound** (reporter_name === `"Dashboard"`): blue-tinted background, blue reporter label, `sent ↑` badge top-right
- **Moved in** (re-linked from another incident, identified by `relinked === true`): amber border, amber reporter label, `moved in ↩` badge top-right

Each update also shows the re-link control (Move to… dropdown + Re-link button) for staff to reassign it to a different incident.

### 4. Status History

A dot timeline showing every status transition, sourced from the new `incident_status_history` table. Each row shows: coloured dot (colour matches the destination status), `<from> → <to>` label, and timestamp.

The initial creation row has no `from_status` and displays as "Created as New · <time>".

Status colours: new = blue, review = purple, acknowledged = teal, resolved = grey, ignored = dark grey.

### 5. Attachments

Grid of media thumbnails/file links, same as today.

### 6. Reply Bar (fixed, does not scroll)

Pinned to the bottom of the modal. Textarea + Send button. Sends a quoted WhatsApp reply to the original incident message when `message_id` is present, or a plain group message when it is not.

## Backend Changes

### New model: `IncidentStatusHistory`

```
Table: incident_status_history
  id           INTEGER PRIMARY KEY
  incident_id  INTEGER NOT NULL  FK → incidents.id
  from_status  TEXT NULLABLE      (null on creation)
  to_status    TEXT NOT NULL
  changed_at   DATETIME NOT NULL
```

No `changed_by` field — there is no user auth system yet.

### Updated model: `IncidentUpdate`

Add a `relinked` boolean column (default `False`). The relink endpoint (`PATCH /incidents/{update_id}/relink`) sets `relinked = True` after moving an update to a different incident. This field is included in the `GET /incidents/{id}` updates array.

### Migration

A new Alembic migration creates the `incident_status_history` table and adds the `relinked` boolean column to `incident_updates` (default `False`, not null).

### Incident creation

When a new `Incident` row is inserted, also insert an `IncidentStatusHistory` row: `from_status=None`, `to_status='new'`, `changed_at=received_at`.

### Status update endpoint

`PATCH /incidents/{id}/status` — after updating `incident.status`, insert an `IncidentStatusHistory` row: `from_status=<previous status>`, `to_status=<new status>`, `changed_at=now`.

### GET /incidents/{id}

Add `status_history` field to the response: list of `{from_status, to_status, changed_at}` objects sorted by `changed_at` ascending.

## Frontend Changes (`backend/templates/dashboard.html`)

All changes are within `dashboard.html` — no new files.

### CSS

- `.modal` max-width: 860px (up from 680px)
- `.modal-body` becomes a flex column with `flex: 1; overflow-y: auto` — the header and reply bar sit outside it as fixed siblings
- `.reply-bar` — new wrapper for the pinned reply section (currently inside `.modal-body`, move outside)
- New badge classes: `.badge-sent` (blue), `.badge-moved-in` (amber)
- `.status-history` dot-timeline styles

### `renderDetailModal(detail)`

Rewritten to produce the four-section layout. Uses `detail.status_history` for section 4. Moved-in detection uses `u.relinked === true`. Outbound detection uses `u.reporter_name === 'Dashboard'`.

The reply textarea and send button move from inside the scrollable body to a new pinned `.reply-bar` div below `.modal-body`, inside `.modal`.

### `openDetailModal(incidentId)`

No change needed — already fetches `/incidents/{id}` and calls `renderDetailModal`.

## Out of Scope

- User authentication / `changed_by` tracking
- Editing or deleting updates
- Real-time push updates to the open modal
- Pagination of updates
