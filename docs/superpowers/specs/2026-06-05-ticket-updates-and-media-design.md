# Ticket Updates & Media Attachments — Design Spec

**Date:** 2026-06-05
**Status:** Approved

## Summary

Two related features:

1. **Ticket updates** — follow-up WhatsApp messages in a group are detected (via a two-stage LLM pipeline) as updates to an existing open ticket rather than always creating new tickets. Admins can manually re-link updates from the dashboard.
2. **Media attachments** — images, videos, and documents sent to a group are downloaded to server disk and attached to tickets (or updates), visible in the dashboard via a badge + modal pattern.

---

## Data Model

### New table: `incident_updates`

Stores follow-up messages linked to a parent incident.

| Column | Type | Notes |
|---|---|---|
| `id` | int PK | autoincrement |
| `incident_id` | int FK → incidents | non-nullable |
| `message_id` | text UNIQUE | deduplication, nullable (same convention as incidents) |
| `reporter_name` | text | |
| `reporter_phone` | text | nullable |
| `message_body` | text | |
| `received_at` | timestamptz | |
| `ai_linked` | bool | true = LLM routed; false = admin manually linked |

### New table: `incident_media`

One row per media file, attached to a parent ticket and optionally to a specific update.

| Column | Type | Notes |
|---|---|---|
| `id` | int PK | autoincrement |
| `incident_id` | int FK → incidents | always set |
| `update_id` | int FK → incident_updates | nullable — set when media arrived with an update |
| `filename` | text | UUID-based, e.g. `a3f2...jpg` |
| `mimetype` | text | e.g. `image/jpeg`, `video/mp4` |
| `file_path` | text | absolute path inside container, e.g. `/app/media/a3f2...jpg` |
| `received_at` | timestamptz | |

### Modified table: `incidents`

One new column: `updated_at` (timestamptz, nullable). Set whenever a new `IncidentUpdate` or `IncidentMedia` arrives. Used for sorting by recent activity.

### Media file storage

Files are saved to `/app/media/` inside the backend container, backed by a named Docker volume `media_data` mounted at `/app/media`. Files are named `<uuid>.<ext>` to prevent collisions and make URLs unguessable.

---

## Ingest Pipeline

### Text messages (`type: "chat"`)

Unchanged Stage 1, new Stage 2:

1. **Stage 1 — classify_message()** (existing): returns `{is_incident, category, severity, confidence}`. If noise or low confidence → discard, return `"noise"`.
2. **Stage 2 — route_or_new()** (new): queries for open tickets (`status` not in `resolved`, `ignored`) in the same `group_id`. If none → create `Incident` as before. If open tickets exist → call `classify_update_or_new()`.
3. **classify_update_or_new(message, open_tickets)** (new classifier function): sends a focused Ollama prompt listing open ticket summaries and asking whether the message is an update to one or a new issue. Returns `{"routing": "new"}` or `{"routing": "update", "ticket_id": 42}`. On LLM failure → falls back to `"new"` (safe default).

### Media messages (`type: "image"` | `"video"` | `"document"`)

1. **Caption handling**: if `data.caption` is present and non-empty, run it through Stage 1 + Stage 2 exactly as a text message. The caption becomes the `message_body` of the new/updated ticket.
2. **Media download**: call `download_media(media_url, dest_dir)` — streams the file from OpenWA's `mediaUrl` field, saves as `<uuid>.<ext>` under `/app/media/`, returns `(filename, mimetype)`.
3. **Attachment routing**:
   - If caption was classified as a new incident → attach `IncidentMedia` to the new `Incident`.
   - If caption was classified as an update → attach `IncidentMedia` to the new `IncidentUpdate`.
   - If no caption (or caption = noise) → find the most recent open ticket in the group and attach `IncidentMedia` directly to it (no update row). If no open ticket exists → store media anyway with a warning log, return `"staged_media"`.
4. **`incidents.updated_at`** is set in all attachment cases.

### Anything else

Non-group, non-chat/media event types → ignored (existing behaviour, unchanged).

---

## API

### Modified endpoints

**`GET /incidents`**
Response adds two new fields per incident object — no breaking changes:
```json
{
  "id": 1,
  "update_count": 2,
  "media_count": 3,
  ...existing fields...
}
```

**`GET /incidents/{id}`** (new)
Full detail view:
```json
{
  ...incident fields...,
  "updates": [
    {
      "id": 1,
      "reporter_name": "...",
      "message_body": "...",
      "received_at": "...",
      "ai_linked": true,
      "media_count": 1
    }
  ],
  "media": [
    {
      "id": 1,
      "filename": "abc.jpg",
      "mimetype": "image/jpeg",
      "update_id": null
    }
  ]
}
```

### New endpoints

**`GET /media/{media_id}`**
Serves the file from disk with correct `Content-Type` header. No auth required (UUID filenames are unguessable).

**`PATCH /incidents/{update_id}/relink`**
Admin override to move an `IncidentUpdate` to a different parent ticket.
- Auth: `X-API-Key` required.
- Body: `{"incident_id": 5}` or `{"incident_id": null}` to promote the update to a standalone `Incident`.
- Returns: `{"update_id": ..., "incident_id": ...}`.

### Unchanged endpoints

`POST /api/v1/ops/ingest`, `PATCH /incidents/{id}/status`, `GET /health`, `GET /api/webhook-url`.

---

## Dashboard UI

### Ticket cards

A badge row is added below the existing category/severity chips:
- `↩ N updates` — shown only if `update_count > 0`
- `📎 N attachments` — shown only if `media_count > 0`

Clicking either badge opens the ticket detail modal.

### Ticket detail modal

Triggered by badge click or a "View detail" button on each card. Contents:
1. **Original message** — reporter, timestamp, full message body.
2. **Update thread** — chronological list of `IncidentUpdate` rows. Each row shows reporter name, timestamp, message body, and an attachment badge if it has media.
3. **Attachments section** — all `IncidentMedia` for the ticket (parent + updates combined). Images render as thumbnails (click → full size in new tab). Videos and documents show a file icon, filename, and direct `/media/{id}` link.

### Re-link control

Each update row in the modal has a "Re-link" button (shown only when `api_key` is available in the page context). Clicking it shows a dropdown of other open tickets in the same group. Selecting one calls `PATCH /incidents/{update_id}/relink` and refreshes the modal.

### Polling

The existing `GET /incidents?since_id=...` real-time poll picks up `update_count` and `media_count` changes automatically since they are new fields in the same response shape.

---

## Infrastructure

**`docker-compose.yml`** — add named volume `media_data` and mount it at `/app/media` in the backend service:

```yaml
backend:
  volumes:
    - media_data:/app/media

volumes:
  media_data:
```

---

## Error Handling & Edge Cases

| Scenario | Behaviour |
|---|---|
| OpenWA `mediaUrl` is missing or download fails | Log error, skip media attachment, still create ticket/update from caption if present |
| `classify_update_or_new` Ollama call fails | Fall back to `"new"` — create a standalone incident |
| Media message with no caption and no open ticket in group | Save media to disk, log warning, return `"staged_media"` |
| `message_id` collision on `IncidentUpdate` | `IntegrityError` → return `"duplicate"` (same as incident deduplication) |
| Admin re-links update to non-existent ticket | 404 from `PATCH /incidents/{update_id}/relink` |
| `incident_id: null` in relink body | Promote update: create new `Incident` from update's fields, detach update record |

---

## Testing

- Unit tests for `classify_update_or_new` (mock Ollama, cover: LLM says new, LLM says update, LLM fails → fallback)
- Integration tests for ingest with media payload (mock `download_media`, assert `IncidentMedia` row created)
- Integration test: two messages in same group → second routed as update when LLM says so
- Integration test: relink endpoint moves update and updates `incident_id`
- `GET /incidents` response includes `update_count` and `media_count` fields
- `GET /media/{id}` returns correct `Content-Type` and file bytes
