# Archive Page — Design Spec

**Date:** 2026-06-05
**Status:** Approved

## Summary

Add a separate `/archive` page to the dashboard. When an incident is marked **Resolved** on the live board it is removed from the live view and becomes visible only on the archive page. The archive page is a full replica of the live board — same filters, search, card layout, detail modal — but scoped exclusively to resolved incidents.

---

## Architecture

Single shared template (`dashboard.html`) with a `mode` variable injected by the backend: `"live"` or `"archive"`. Two FastAPI routes serve the two views. A few `{% if mode == 'archive' %}` branches in the template handle the differences.

---

## Backend Changes (`main.py`)

### `GET /` — Live dashboard route
- Add `mode="live"` to the template context.
- Query changes: exclude `status == "resolved"` incidents so the initial server-side render never contains resolved tickets.
- Template context key stays `incidents_with_counts`.

### `GET /archive` — New archive route
- Mirrors `GET /` but queries only `Incident.status == "resolved"`, ordered by `received_at DESC`.
- Passes `mode="archive"` and `incidents_with_counts` (resolved only) to the template.
- No authentication required (same as live board).

### `GET /incidents` — List endpoint
- Add optional repeatable query param `statuses: list[str] = Query(default=None)`.
- If provided, filters `Incident.status.in_(statuses)`.
- If omitted, returns all statuses (existing behaviour — no breaking change).
- Used by the archive page poll: `GET /incidents?statuses=resolved&since_id=N`.

---

## Template Changes (`dashboard.html`)

### JS constant
Inject mode as a JS constant at the top of the `<script>` block:
```javascript
const MODE = '{{ mode }}';
```

### Topbar nav link
A new button in the topbar right area (alongside the stats):
- Live mode: `Archive →` — links to `/archive`
- Archive mode: `← Live Board` — links to `/`

### Page identity
- Live mode: eyebrow text = `"Incident Command Centre"`
- Archive mode: eyebrow text = `"Incident Archive"`, title = `"Resolved Issues"`

### Stats bar
- Live mode: unchanged (High · Open, Needs Review, Active Open, Total Today)
- Archive mode: single stat — `Total Resolved` count

### Status filter sidebar
- Live mode: "Resolved" filter option removed (resolved incidents never appear on the live board)
- Archive mode: entire status filter group hidden (all incidents are already resolved)

### Card action buttons
- Live mode: unchanged (Acknowledge, Resolve, Ignore, Send to Review)
- Archive mode: four buttons replaced with a single **Reopen** button → calls `setStatus(id, 'review')`

### `setStatus` behaviour
```
if MODE === 'live' && newStatus === 'resolved':
    → API call succeeds → animate card out (fade + collapse, ~300ms) → remove from DOM → splice from allIncidents

if MODE === 'archive' && newStatus !== 'resolved':  (i.e. Reopen)
    → API call succeeds → animate card out → remove from DOM → splice from allIncidents
```
CSS for the exit animation:
```css
.card.removing {
  transition: opacity 0.25s ease, max-height 0.3s ease, margin 0.3s ease;
  opacity: 0;
  max-height: 0;
  overflow: hidden;
  margin-bottom: 0;
}
```

### Real-time poll
- Live board: `GET /incidents?since_id=N` — filter out any incidents with `status === 'resolved'` in the `poll()` function client-side before adding to `pendingNew`. No new query params.
- Archive page: `GET /incidents?statuses=resolved&since_id=N` — same poll interval (30s), same new-items banner behaviour.

---

## Navigation

- Live board topbar: `Archive →` button (right side, subtle ghost style)
- Archive page topbar: `← Live Board` button (same style)
- No changes to any existing routes or redirects

---

## Error Handling

| Scenario | Behaviour |
|---|---|
| `setStatus` API call fails on live board | Card stays, buttons re-enabled, error toast shown (existing behaviour) |
| `setStatus` API call fails on archive (Reopen) | Card stays, buttons re-enabled, error toast shown |
| Archive page loaded with no resolved incidents | Empty state: "No resolved incidents yet." |

---

## Testing

- `GET /archive` returns 200 with only resolved incidents in template context
- `GET /incidents?statuses=resolved` returns only resolved incidents
- `GET /incidents?statuses=resolved&since_id=N` respects both filters
- `GET /incidents` (no param) still returns all statuses (backward compat)
- Existing live board tests unaffected
