# WhatsApp Group Name Picker (Tenant Settings + Billing Ops Panel)

**Date:** 2026-07-12
**Status:** Approved

## Overview

Today, WhatsApp groups are represented everywhere in the UI as raw JIDs (e.g. `120363XXXXXXXXXX@g.us`) — there is no `Group` table with names anywhere in the system. This causes two related problems:

1. On the tenant-facing `/settings` page, the client admin/superadmin's "Ticket-Raising Groups" list shows bare JIDs, and the "Add Group" form is a blind free-text box — the admin must already know the exact JID of the group they want to add before they can add it.
2. The same problem exists twice over in the internal billing/ops panel (`client_detail.html`): once in its own "Ticket-Raising Groups" list + add form (mirroring the tenant page), and once in the single `WhatsApp Group ID (superusers group)` field used to designate the client's billing/payment-command group. Ops staff currently work around this by manually `curl`ing OpenWA's groups endpoint and hand-copying JIDs (documented in `docs/finding-whatsapp-group-ids.md`).

OpenWA (the `whatsapp-web.js` gateway microservice) already exposes `GET /api/sessions/:sessionId/groups`, returning `{id, name}` for every group the connected WhatsApp session currently belongs to — live, with no dependency on ticket history. This spec wires that existing endpoint into both UIs so names are shown wherever a JID is shown, and "Add Group" / "set billing group" become name-labeled dropdowns instead of blind text entry.

No new data model. `allowed_ticket_groups` and `whatsapp_group_id` keep storing raw JIDs exactly as today — OpenWA still needs the JID to send messages. This spec only changes what's *displayed* and how a JID gets *selected*.

---

## 1. Backend/API changes

### Tenant `backend` service

- `backend/whatsapp.py` gains `list_groups() -> list[dict] | None`, reusing the existing `_resolve_session_uuid()` helper. Calls `GET {OPENWA_URL}/api/sessions/{session_id}/groups`. Returns `None` (not a raised exception) on any failure — session not found, OpenWA unreachable, timeout — so callers can distinguish "no groups" from "couldn't fetch."
- New route `GET /api/settings/whatsapp-groups` in `backend/main.py`, gated by the existing `require_admin` dependency (same as the rest of `/settings`), placed next to the existing `/api/settings/ticket-groups` routes. Calls `whatsapp.list_groups()` and returns `{"groups": [...] | null}`.

### `billing` (ops) service

- `billing/main.py` gains `_get_groups(client) -> list[dict] | None`, mirroring the existing `_get_session_id(client)` helper (same pattern already used for QR/reconnect: resolve `client.openwa_url` / `client.openwa_session` / `client.openwa_api_key`, look up the session UUID, call `/api/sessions/{session_id}/groups`).
- New route `GET /clients/{client_id}/whatsapp-groups`, gated by the existing `require_login` dependency used on the rest of `client_detail.html`'s routes. Returns the same `{"groups": [...] | null}` shape.

Both routes are read-only, admin-gated, thin proxies to OpenWA — no DB writes, no new tables, no new columns.

---

## 2. UI behavior

### Displaying already-added groups
(tenant `settings.html` Ticket-Raising Groups list; billing `client_detail.html` Ticket-Raising Groups table)

- On page load, fetch the live groups list and build an `{id → name}` lookup in JS.
- For each already-added JID, render the resolved name with the JID shown as smaller/muted secondary text underneath (kept visible for debugging/support use).
- If a JID isn't found in the live list (bot removed from that group, or the live fetch failed/returned `null`), fall back to showing the raw JID alone — unchanged from today's behavior. Never block rendering the list on the live fetch succeeding.

### Add Group form
(both pages)

- Replace the free-text JID input with a `<select>` populated from the live list, **excluding** groups already present in `allowed_ticket_groups` (no duplicates offered), options sorted alphabetically by name, value = JID.
- Submitting still posts `group_id` (the JID) to the existing `/ticket-groups/add` endpoints on both services — no backend contract change, no validation regex change (`_GROUP_JID_RE` keeps working since the submitted value is always a real JID either way).
- If the live fetch fails or returns `null`, show a small inline warning ("Couldn't load live group list — enter the group ID manually") and fall back to today's free-text input, unchanged.

### `WhatsApp Group ID (superusers group)` field
(billing `client_detail.html` only)

- Becomes a `<select>` populated the same way, but **not** filtered against `allowed_ticket_groups` (it's an unrelated single-value field — the billing/payment-command group, distinct from ticket-raising groups).
- The client's currently-stored value is pre-selected if it's found in the live list. If the live fetch succeeds but that specific stored JID isn't in the returned list (e.g. the group predates this feature, or the bot later left it), the `<select>` gets one extra option representing the current value verbatim (labeled with the raw JID, e.g. `Current: 120363XXXXXXXXXX@g.us`), pre-selected — so saving the form without touching this field is always a no-op, never a silent clear.
- If the live fetch fails entirely (OpenWA unreachable), same fallback as Add Group: warning + free-text box, pre-filled with the current stored JID.

### Implementation note

`backend` and `billing` are separate services with no shared frontend build step (plain Jinja2 templates + vanilla JS, no bundler, no shared static assets between services) — so the fetch/render/fallback logic is written twice, once per template, each following that page's existing JS conventions. `settings.html` follows the newer Warm Neutral vanilla-JS patterns already used elsewhere on that page; `client_detail.html` stays in its current plain inline styling (restyling it is a separate, explicitly out-of-scope redesign).

---

## 3. Error Handling

| Scenario | Behavior |
|---|---|
| OpenWA session unreachable/disconnected when loading either page | Live list fetch returns `null`; already-added groups show raw JIDs (today's behavior); Add Group and the billing-group field fall back to free-text inputs with an inline warning |
| A group JID already stored in `allowed_ticket_groups` / `whatsapp_group_id` is no longer in the live list (bot removed from it) | Shown as raw JID only, no name — not an error, no warning needed |
| Client has zero groups in the live list (e.g. bot freshly connected, not yet added to any group) | Add Group dropdown shows an empty/disabled state with a hint, free-text fallback still available |
| Live fetch is slow | No loading blocker on the rest of the page — the groups section shows a lightweight "Loading groups…" placeholder while the fetch is in flight, same as any other async section on these pages |

---

## 4. Testing

- **`backend/whatsapp.py`**: `list_groups()` returns the parsed `[{id, name}]` list on success, and `None` on HTTP error/timeout/session-not-found (mock `httpx`, mirroring existing `_post_message` tests).
- **`backend` endpoint**: `GET /api/settings/whatsapp-groups` returns `{"groups": [...]}` when `list_groups()` succeeds and `{"groups": null}` when it returns `None`; gated by `require_admin` same as sibling routes.
- **`billing`**: `_get_groups()` unit test (mock `httpx`, same pattern as existing `_get_session_id` tests); `GET /clients/{id}/whatsapp-groups` endpoint test for both success and unreachable-OpenWA cases.
- **Manual verification**: on a connected sandbox/test OpenWA session, load `/settings` and confirm already-added groups show real names; add a new group via the dropdown and confirm it's excluded from the dropdown afterward; disconnect the session and confirm the page falls back to free-text entry without erroring. Repeat for `client_detail.html`'s Ticket-Raising Groups section and the `WhatsApp Group ID (superusers group)` field.

---

## 5. Out of Scope

- Any new `Group` database table — group identity remains a bare JID string everywhere, as today.
- Restyling `billing/templates/client_detail.html` to the Warm Neutral design system (separate, already-deferred redesign effort).
- A searchable/filterable combobox — a plain native `<select>` is sufficient for expected group-list sizes; revisit only if a client's group count grows large enough to make a flat dropdown unwieldy.
- Periodic/background syncing or caching of group names — every page load fetches live, no persistence.
- Changes to `GroupUpgradeRequest` or the billing-group-tier-lock flow (`2026-07-03-billing-group-tier-lock-design.md`) — this spec only changes how a JID is *displayed and selected*, not the tier/limit logic around it.
