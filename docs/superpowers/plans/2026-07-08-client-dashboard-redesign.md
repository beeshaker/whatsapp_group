# Client Dashboard UI/UX Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the client-tenant dashboard's dark navy/blue-purple theme with a light "Warm Neutral" + teal design system, rework the tickets dashboard's filter sidebar and card list into a toolbar-with-dropdown-chips and a dense table, and eliminate the ~7x duplicated per-template CSS token blocks — without changing any backend/business logic or existing JS behavior (polling, inline field updates, filter logic, escalation toggle, quick status actions).

**Architecture:** One new static asset, `backend/static/css/base.css`, holds every shared design token (`:root` custom properties) and shared component style (nav, buttons, inputs, section-card, dense table, badges/pills, toast) — served via a new `StaticFiles` mount at `/static` added to `backend/main.py`. One new Jinja partial, `backend/templates/_nav.html`, holds the shared top-nav markup (parameterized by `username`, `role`, `active_page`), included via `{% include "_nav.html" %}` from all 8 nav-bearing templates (the existing `_chat_widget.html` include is the precedent for this pattern). Each of the 8 templates (`dashboard.html`, `settings.html`, `billing.html`, `users.html`, `profile.html`, `login.html`, `summaries.html`, `super_admin_categories.html`) keeps its own `<style>` block, shrunk down to only the CSS unique to that page's layout. `dashboard.html` additionally gets its filter sidebar (`<aside id="sidebar">`) replaced by toolbar dropdown-chip filters, its card list (`#card-list` of `.card` elements) replaced by a `<table>` of `<tr class="ticket-row">` rows, and the JS functions tightly coupled to that markup (`buildCard`, `extractIncidentFromCard`, `applyFilters`, `setStatus`, `toggleFilterGroup`, `renderDetailModal`, `renderTicketDetailsSection`) updated in lockstep in the same task. No Python file changes beyond the `StaticFiles` mount in `backend/main.py`; no route signatures, response shapes, or database access change anywhere.

**Tech Stack:** FastAPI, Jinja2 3.1.4, Starlette `StaticFiles`, vanilla CSS (custom properties, no preprocessor/bundler), vanilla JS (no framework), pytest + pytest-asyncio + httpx `ASGITransport` for the existing test suite, Playwright MCP tools for manual/visual verification.

**Spec:** `docs/superpowers/specs/2026-07-08-client-dashboard-redesign-design.md`

## Global Constraints

- **Out of scope, do not touch:** `backend/templates/_chat_widget.html`, anything under `billing/templates/*` or `billing/`, anything under `openwa/`, any Python route handler logic in `backend/main.py` beyond the one `StaticFiles` mount + import line, any database/model change.
- **Run all backend commands from `backend/`** using `backend/.venv/bin/python -m pytest ...` (the venv at `backend/.venv` already has all deps installed — a bare `python`/`pytest` is not guaranteed to be the right interpreter).
- **The "Warm Neutral" palette tokens below are canonical** — every task must use these exact custom-property names/values from `base.css`, never reintroduce a local `--bg`/`--blue`/`--radius-xl` etc. definition in a page's own `<style>` block:
  ```
  --bg: #faf8f5;            /* page background */
  --surface: #fffdfa;       /* cards, nav, modal, table surfaces */
  --surface-2: #f5f1ea;     /* secondary surface: table header row, hover row, input bg */
  --line: #ece7de;          /* all borders */
  --text: #2b2620;          /* primary text */
  --muted: #8a7f6c;         /* secondary text */
  --muted-2: #6b6152;       /* meta/tertiary text, slightly darker than --muted */
  --teal: #0d9488;          /* accent: links, primary buttons, active nav underline/text, focus rings, selected chip fill */
  --teal-deep: #0f766e;     /* hover/active state of accent */
  --teal-soft: #ccfbf1;     /* soft accent bg: avatar bg, selected chip bg, focus-ring glow */
  --red: #dc2626;           --red-soft: #fee2e2;      /* high priority / destructive */
  --urgent: #be123c;        --urgent-soft: #ffe4e6;   /* urgent priority */
  --amber: #d97706;         --amber-soft: #fef3c7;    /* medium priority */
  --green: #16a34a;         --green-soft: #dcfce7;    /* low priority / resolved */
  --purple: #7e22ce;        --purple-soft: #f3e8ff;   /* review status */
  --cyan: #0891b2;          --cyan-soft: #cffafe;     /* acknowledged status — deliberately distinct hue from --teal so "acknowledged" badges don't visually collide with interactive accent elements */
  --blue: #2563eb;          --blue-soft: #dbeafe;     /* "new" status — distinct from --teal for the same reason */
  --shadow: 0 4px 16px rgba(43, 38, 32, 0.06);
  --radius-xl: 14px;
  --radius-lg: 10px;
  --radius-md: 8px;
  --radius-sm: 6px;
  ```
  Typography stays Inter/system-ui stack, unchanged from today.
- **Preserve every id listed in the "ids that must not change" section of each task** — these are read directly by JS via `getElementById`/`querySelector` and by `backend/tests/test_dashboard.py`. Class names are free to rename as long as the JS/CSS referencing them is updated in the same task (never leave a stale selector).
- **`renderDetailModal()` and `renderTicketDetailsSection()` in `dashboard.html` are JS template-string functions, not Jinja** — restyling the modal means editing these functions' generated-HTML strings, not just CSS. Their `onclick`/`onchange` handler wiring (`updateTicketField`, `relinkUpdate`, `openDetailModal`, `sendReplyFromBar`) and argument lists must be preserved exactly; only the purely-visual class names inside the generated markup may be renamed, and only if updated in the same function in the same step.
- **Known behavior gap to resolve, not silently drop:** the current `.card` provides two *separate* interaction paths — direct quick-status buttons (Acknowledge/Resolve/Ignore/Review in live mode, Reopen in archive mode, wired to `setStatus(id, newStatus, event)`, disabled/re-enabled via `document.querySelectorAll('#actions-${id} button')`) AND a distinct "View / Reply" button that opens the modal. The modal itself never calls `setStatus()` — status can only be changed from the row's own quick-action buttons today. The spec's table-column list ("priority dot, id, reporter/property, category, status, update/attachment counts, age") has no explicit slot for these buttons. **Task 2 must add a compact actions area to each row** (e.g. a narrow trailing column with icon-labeled buttons, or a small per-row menu) that preserves `setStatus()`'s exact call signature and the `id="actions-{{ i.id }}"` container id (or updates `setStatus()`'s selector to match wherever the new id lives) — dropping these buttons is a functional regression, not a valid interpretation of "restyle."
- **The sidebar-hide feature (`toggleSidebar()`, `restoreSidebarState()`, `localStorage['incidentSidebarHidden']`, `#sidebar-toggle`/`#sidebar-toggle-text`, `.body.sidebar-hidden` CSS) has no equivalent surface once `<aside id="sidebar">` is removed** — Task 2 deletes this feature entirely (toolbar filter chips are always visible; there is nothing left to hide/show). This is an intentional removal, not an oversight — call it out in Task 2's step list so a reviewer doesn't go looking for a "restore" behavior.
- **Nav partial scope decision:** Task 1 creates `backend/templates/_nav.html` as a Jinja include taking `username`, `role`, `active_page` (values: `live`, `archive`, `users`, `summaries`, `profile`, `super_admin_categories`, `settings`, `billing`). It renders the **fullest** link set seen today (Live Queue, Archive, Users[admin+], Summaries, Profile, System Config[super_admin only], Settings[admin+], Billing) with `id="nav-avatar"` standardized. Each of Tasks 2–9 wires this include into its own template. **Known side effect, verified safe:** `super_admin_categories.html` currently omits Settings and Billing links entirely (not role-gated — just absent); adopting the shared partial gives that page working Settings/Billing links for the first time, since `super_admin` role passes both `require_admin` (Settings) and `require_login` (Billing) checks in `backend/auth.py`. This is a deliberate, low-risk consequence of extraction, not a bug — call it out in Task 9. If wiring the include into any single page turns out fiddly for reasons not anticipated here, that page's task may fall back to an inline nav block styled with the same `base.css` classes instead — do not let it block that task, but note the fallback explicitly if taken.
- **Do not add a `backend/static/css/dashboard.css` or any second shared CSS file** — per spec §1, only ONE shared stylesheet is in scope; everything else stays as a page-local `<style>` block.
- **No new Python dependency needed** — `fastapi.staticfiles.StaticFiles` is already importable with the pinned `fastapi==0.115.0`/starlette combo (verified: `from fastapi.staticfiles import StaticFiles` succeeds in `backend/.venv`).

---

## File Structure

- **Create** `backend/static/css/base.css` — shared design tokens + base components (nav, buttons, inputs, section-card, dense table, badges/pills, toast).
- **Create** `backend/templates/_nav.html` — shared top-nav Jinja partial (`username`, `role`, `active_page`).
- **Modify** `backend/main.py` — add `StaticFiles` import + `/static` mount (Task 1 only; no other Python changes anywhere in this plan).
- **Modify** `backend/templates/dashboard.html` — full restyle + sidebar→toolbar-chips + cards→table (Task 2, biggest task).
- **Modify** `backend/tests/test_dashboard.py` — update the 2–3 assertions hardcoded to old `.card`/`#sidebar` markup (Task 2).
- **Modify** `backend/templates/settings.html`, `billing.html`, `users.html`, `profile.html`, `login.html`, `summaries.html`, `super_admin_categories.html` — restyle to shared design system, converge `.section`/`.section-card`, apply dense-table styling to any `<table>`, adopt `_nav.html` (Tasks 3–9).

---

### Task 1: Static asset foundation — `StaticFiles` mount + shared CSS + shared nav partial

**Files:**
- Modify: `backend/main.py` (import block ~line 19-20; mount block right after `SessionMiddleware`, currently lines 328-333, before line 336's `_SETUP_HTML`)
- Create: `backend/static/css/base.css`
- Create: `backend/templates/_nav.html`
- Test: `backend/tests/` (full suite, regression-only — nothing yet consumes the new files)

**Interfaces:**
- Produces: `/static/css/base.css` served at runtime; the CSS custom properties and component classes listed in Global Constraints, plus (at minimum) `.nav`, `.nav-brand`, `.nav-brand-icon`, `.nav-links`, `.nav-link`, `.nav-link.active`, `.nav-right`, `.user-pill`, `.user-avatar`, `.btn-signout`, `.btn`/`.btn-primary`/`.btn-ghost`, base `input`/`select`/`textarea` styles with a `--teal`-based focus ring, `.section-card`/`.section-title`/`.section-desc`, `.data-table` (with `thead th`, `tbody tr:hover`, `td` padding rules), `.badge` + `.badge-{variant}` for every status/priority modifier used today (`urgent`, `high`, `medium`, `low`, `new`, `review`, `acknowledged`, `resolved`, `ignored`), `.toast`.
- Produces: `backend/templates/_nav.html`, consumed by `{% include "_nav.html" %}` from Tasks 2–9 (all templates except `login.html`, which has no nav).
- Consumes: nothing (foundation task).

- [ ] **Step 1: Confirm baseline**
  Run `grep -n "StaticFiles" backend/main.py` (expect zero matches) and `ls backend/static` (expect "No such file or directory"). This confirms no prior mount exists to conflict with.

- [ ] **Step 2: Create `backend/static/css/base.css`**
  Write the full token block from Global Constraints as `:root { ... }`, followed by a CSS reset (`*, *::before, *::after { box-sizing: border-box; }`, `* { margin:0; padding:0; }`, `html, body { height: 100%; }`, `body { font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: var(--bg); color: var(--text); }`), then the base component rules listed under "Produces" above. Nav styling must be **solid** `background: var(--surface)` with `border-bottom: 1px solid var(--line)` — do not carry over today's `backdrop-filter: blur(...)` + translucent-rgba nav background; the spec explicitly calls for "lighter visual weight than today's glow/blur dark bar."

- [ ] **Step 3: Wire the `/static` mount in `backend/main.py`**
  Add `from fastapi.staticfiles import StaticFiles` directly below the existing `from fastapi.responses import ...` import (current line 19). Add `app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")` immediately after the `SessionMiddleware` `app.add_middleware(...)` block (currently ends line 333) and before the `_SETUP_HTML` line (currently line 336). `os` is already imported at the top of `main.py`. Order matters: the `backend/static/` directory must already contain `css/base.css` (Step 2) before this mount is added, or `StaticFiles.__init__` raises `RuntimeError: Directory does not exist` at import time.

- [ ] **Step 4: Create `backend/templates/_nav.html`**
  A single `<nav class="nav">...</nav>` block taking Jinja context `username`, `role`, `active_page`. Reproduce the fullest link set (Live Queue → `/`, Archive → `/archive`, Users → `/users` gated `role in ('admin','super_admin')`, Summaries → `/summaries`, Profile → `/admin/profile`, System Config → `/super-admin/categories` gated `role == 'super_admin'`, Settings → `/settings` gated `role in ('admin','super_admin')`, Billing → `/billing`), each link's `active` class driven by `{% if active_page == 'live' %}active{% endif %}` (etc. — `live`/`archive` are the two dashboard states; the rest map 1:1 to page names). Standardize `id="nav-avatar"` (not `id="avatar"`) and include the harmless `id="nav-username"` span (only `users.html` currently uses it, unused by any other page's JS — safe to include everywhere). Preserve the `<form method="post" action="/logout">` sign-out button exactly.

- [ ] **Step 5: Verify the static mount serves the file**
  Start the app locally (e.g. `backend/.venv/bin/uvicorn main:app --reload --port 8000` from `backend/`, with whatever env vars the project's `.env`/`docker-compose.yml` requires) and `curl -sI http://localhost:8000/static/css/base.css` — expect `200` and a `text/css`-ish content-type. Stop the server after confirming.

- [ ] **Step 6: Run the full backend test suite to confirm zero regressions**
  `cd backend && .venv/bin/python -m pytest -v`
  Expected: all tests pass, identical to pre-change baseline (this task adds a mount and two new files; nothing existing references them yet, so behavior must be byte-for-byte unchanged for every existing route).

- [ ] **Step 7: Commit**
  ```bash
  git add backend/main.py backend/static/css/base.css backend/templates/_nav.html
  git commit -m "feat: add shared static CSS mount and nav partial for dashboard redesign"
  ```

---

### Task 2: Tickets dashboard rework (`dashboard.html`) — the core structural change

**Files:**
- Modify: `backend/templates/dashboard.html` (entire file — `<style>` block currently lines 7-1191, nav currently ~1196-1226, sidebar currently ~1234-1286, toolbar/stats currently ~1289-1316, card list currently ~1318-1401, modal shell ~1409-1430+, JS ~1432-2089)
- Modify: `backend/tests/test_dashboard.py` (assertions at current lines 69, 76-77; re-verify line 86-87)
- Test: `backend/tests/test_dashboard.py`, `backend/tests/test_ticket_detail_update.py`, `backend/tests/test_login.py`

**Interfaces:**
- Consumes: `backend/static/css/base.css`, `backend/templates/_nav.html` (Task 1).
- Produces: nothing consumed by later tasks — this page is a leaf; Task 10's manual verification exercises it.

**IDs that must not change** (read directly by this file's own JS or by `test_dashboard.py`): `search-input`, `visible-count`, `no-results`, `empty-state`, `stat-high`, `stat-review`, `stat-total` (reused/relabeled "Resolved" in archive mode — do not introduce a second id), `selected-status-count`, `selected-priority-count`, `selected-cat-count`, `new-banner`, `new-count`, `dashboard-body` (may keep even though `toggleSidebar` referencing it is being deleted — harmless if unused), `detail-modal-overlay`, `modal-title`, `modal-body`, `modal-reply-bar`, `reply-who-name`, `modal-reply-input`, `modal-reply-send`, `toast`, per-ticket `status-badge-{{ i.id }}` and `actions-{{ i.id }}` (read by `setStatus()`).

**Functions that must keep their exact signature and network calls** (only their DOM-selector bodies change): `applyFilters()`, `toggleFilter(el)`, `toggleFilterGroup(button)`, `clearFilters()`, `updateStats()`, `updateCounts()`, `updateSelectedFilterCounts()`, `setStatus(id, newStatus, event)`, `openDetailModal(incidentId)`, `closeDetailModal()`, `renderDetailModal(detail)`, `renderTicketDetailsSection(detail)`, `updateTicketField(incidentId, field, value)`, `sendReplyFromBar()`, `relinkUpdate(updateId, currentIncidentId)`, `poll()`, `loadNewIncidents()`, `init()`, `hydrateCategoryIcons(root)`, `categoryIcon(category)`, `formatTime(value)`, `showToast(message)`. `POLL_INTERVAL = 30000` must remain unchanged.

- [ ] **Step 1: Re-read the current JS function bodies before touching markup**
  Specifically re-confirm (they were already read during planning, but re-verify against the live file before editing since line numbers shift as you go): `applyFilters()`'s `document.querySelectorAll('.card[data-id]')` + `.querySelector('.title'|'.message'|'.meta')` search-text extraction; `toggleFilterGroup()`'s `button.closest('.filter-group')` + `.collapsed` toggle; `setStatus()`'s `document.querySelector('.card[data-id="${id}"]')` + `#actions-${id} button` + `#status-badge-${id}`; `init()`'s `document.querySelectorAll('.card[data-id]')` → `extractIncidentFromCard`; `loadNewIncidents()`'s `list.insertAdjacentHTML('afterbegin', buildCard(inc))` target element. Every one of these selectors must be updated in this same task to match whatever new markup you introduce — do not leave any of them pointing at `.card`/`.filter-group`/`.fopt` once those elements no longer exist.

- [ ] **Step 2: Replace `<head>` and the `<style>` block**
  Add `<link rel="stylesheet" href="/static/css/base.css">` in `<head>` before the remaining local `<style>` block. Delete the entire `:root {...}` token block (lines 8-35) and every component rule now covered by `base.css` (nav, buttons, inputs, badges, toast, generic card/section chrome). Keep a page-local `<style>` block containing only: `.app-shell`, the toolbar layout (search input wrap + filter-chip dropdowns + stats pills + legend), the new `.data-table`-based ticket-row column layout (priority-dot cell, id cell, reporter/property cell, category cell, status cell, activity/counts cell, age cell, actions cell), `#new-banner`, and the modal's ticket-detail-specific generated-content classes (`.ticket-details-grid`, `.ticket-details-readonly`, `.message-box`/`.message`, `.update-thread`/`.update-row`, `.status-history-list`/`.sh-dot-{status}`, `.audit-row`, `.media-grid`/`.media-thumb`/`.media-file-row`) restyled to reference the new palette tokens instead of the deleted local ones.

- [ ] **Step 3: Replace the nav block with the shared partial**
  Replace the inline `<nav class="nav">...</nav>` block with `{% set active_page = 'archive' if mode == 'archive' else 'live' %}{% include "_nav.html" %}` placed at the same location in `<body>`.

- [ ] **Step 4: Replace the filter sidebar with toolbar dropdown-chip filters**
  Delete `<aside id="sidebar">...</aside>` entirely. In the toolbar (inside `<main class="main">`, existing `.toolbar`/`.toolbar-actions` structure), add a `.toolbar-filters` group with one dropdown-chip component per filter group: a trigger button (e.g. `.chip-btn[data-group="status"]`, `onclick="toggleFilterGroup(this)"`, containing the group label plus a count badge — reuse the exact ids `selected-status-count`/`selected-priority-count`/`selected-cat-count` for these badges) and a dropdown panel (e.g. `.chip-menu`) containing the option rows. Each option row keeps the exact `data-filter`/`data-val` attributes and the `.dot` swatch + `.cnt[data-cnt=...]`/`[data-cnt-priority=...]`/`[data-cnt-cat=...]` counter span (these attribute names are read by `updateCounts()` via `document.querySelector('[data-cnt="${s}"]')` etc. — do not change the attribute names, only whatever wrapper class you choose to rename `.fopt` to). Status group stays conditional on `{% if mode != 'archive' %}`, Category group stays driven by the same `{% for cat in categories %}` loop and `cat_icons` map. Move "Clear filters" (`onclick="clearFilters()"`) into the toolbar as well. Update `toggleFilterGroup(button)`'s body to open/close this dropdown panel (e.g. `button.closest('.chip-dropdown')?.classList.toggle('open')`) instead of `.closest('.filter-group')`/`.collapsed`.

- [ ] **Step 5: Delete the sidebar-hide feature**
  Remove `toggleSidebar()`, `restoreSidebarState()`, the `#sidebar-toggle`/`#sidebar-toggle-text` button and its markup, the `.body.sidebar-hidden`/`#sidebar-toggle`/`.toggle-icon` CSS, the `localStorage.getItem('incidentSidebarHidden')`/`setItem(...)` calls, and the `restoreSidebarState()` call inside `init()`. This is intentional per Global Constraints — there is no longer anything to hide.

- [ ] **Step 6: Convert the card list to a dense table**
  Replace `<section id="card-list">...</section>` with `<table class="data-table" id="ticket-table"><thead>...</thead><tbody id="ticket-table-body">...</tbody></table>`. Header columns: priority (dot only, no label), ID, Reporter / Property, Category, Status, Activity (update + attachment counts), Age, Actions. Each data row: `<tr class="ticket-row" data-id="{{ i.id }}" data-priority="{{ i.priority }}" data-status="{{ i.status }}" data-cat="{{ i.category }}" data-updates="{{ update_count }}" data-media="{{ media_count }}" data-search="{{ (i.property_name ~ ' ' ~ (i.reporter_name or '') ~ ' ' ~ i.message_body ~ ' ' ~ i.status ~ ' ' ~ i.priority ~ ' ' ~ i.category) | lower }}">` with `onclick="openDetailModal({{ i.id }})"` on the row (this is the row-click-opens-modal behavior replacing `.card-head`'s click-to-expand — `toggleCard()` and its `.open`/`.card-body` expand mechanism are deleted entirely, along with its only caller). Cells: priority dot (colored span, color driven by the priority-specific token), `#{{ i.id }}`, reporter/property text (`{{ i.property_name }}` + `{{ i.reporter_name or "Unknown reporter" }}`), category icon+label (`<span class="category-icon" data-cat="{{ i.category }}">📋</span>` unchanged mechanism — still hydrated client-side via `hydrateCategoryIcons()`), status badge (`<span class="badge badge-{{ i.status }}" id="status-badge-{{ i.id }}">{{ i.status }}</span>` — keep the exact `badge-{status}` modifier naming, `test_dashboard.py` asserts `badge-review` literally), activity counts (update/attachment counts, each still individually clickable via `openDetailModal({{ i.id }})` if you keep them as separate buttons — `event.stopPropagation()` is no longer required once the row itself opens the same modal, but is harmless to keep), age (compute from `i.received_at` — check whether a relative-age helper already exists in the file before writing a new one; if none exists, add one, e.g. `formatAge(value)`, following the same pattern as the existing `formatTime()`), and an Actions cell containing `<div id="actions-{{ i.id }}">` with the same button set as today (live mode: Acknowledge/Resolve/Ignore/Review; archive mode: Reopen), each `onclick="setStatus({{ i.id }}, '<status>', event)"` — this satisfies the Global Constraint requiring quick-status-change parity. `{% if incidents_with_counts %}...{% else %}<div class="empty" id="empty-state">...{% endif %}` and the trailing `<div class="empty" id="no-results" ...>` stay structurally the same, just inside/after the table instead of the old `<section>`.

- [ ] **Step 7: Update `buildCard()` (client-side row builder for polled-in tickets)**
  Rewrite this function (rename to `buildRow()` if you like — update its one call site in `loadNewIncidents()`) to emit a `<tr class="ticket-row" ...>` string matching the exact same columns/classes/data-attributes as the server-rendered rows in Step 6, so newly-polled tickets are visually identical to page-load ones. Update `loadNewIncidents()`'s DOM target from `#card-list` to `#ticket-table-body` (`document.getElementById('ticket-table-body').insertAdjacentHTML('afterbegin', buildRow(inc))`).

- [ ] **Step 8: Update `extractIncidentFromCard()`**
  Rename to `extractIncidentFromRow()` if you like (update the one call site in `init()`). Read from the new `<tr>`'s `data-*` attributes (id, priority, status, cat) rather than re-parsing `.title`/`.message`/`.meta` child text — the `data-search` attribute added in Step 6 already carries everything `applyFilters()` needs for substring search, so this function only needs to reconstruct the same `{id, priority, status, category, ...}` shape `updateStats()`/`updateCounts()` already expect.

- [ ] **Step 9: Update `applyFilters()`**
  Change `document.querySelectorAll('.card[data-id]')` → `document.querySelectorAll('.ticket-row[data-id]')` (or whatever final row class you used — must match Step 6/7 exactly). Replace the `.querySelector('.title'|'.message'|'.meta')` search-text construction with a read of the row's `data-search` attribute (`row.dataset.search`) plus `row.dataset.status`/`priority`/`cat` — same substring-match semantics against the same lowercased query, just against one attribute instead of three DOM lookups. Keep the `.hidden` class toggle and `#visible-count`/`#no-results` wiring unchanged.

- [ ] **Step 10: Update `setStatus()`**
  Change `document.querySelector('.card[data-id="${id}"]')` (both occurrences — the "should remove" branch and the "update in place" branch) → `document.querySelector('.ticket-row[data-id="${id}"]')`. The `card.classList.add('removing')` / `setTimeout(() => card.remove(), 320)` pattern and the `#status-badge-${id}` class/text update stay identical — only the selector's target class name changes.

- [ ] **Step 11: Restyle the detail modal shell and edit `renderDetailModal()`/`renderTicketDetailsSection()`**
  Restyle `.modal-overlay`/`.modal`/`.modal-header`/`.modal-body`/`.modal-reply-bar` CSS to the new palette (page-local `<style>`, since only this page uses this modal). Then edit the two JS template-string functions per Global Constraints: update every purely-visual class name in their generated HTML to match the new page-local class names from Step 2, while leaving `onclick`/`onchange` handler wiring and argument lists (`updateTicketField(${detail.id}, 'field', value)`, `relinkUpdate(updateId, currentIncidentId)`, `openDetailModal(${s.id})`, `sendReplyFromBar()`) byte-for-byte identical.

- [ ] **Step 12: Restyle the stats pills**
  `.stats-bar`/`.stat`/`.stat.red`/`.stat.purple` already live inside `.toolbar-actions` today (not a separate full-width bar) — this step is a palette/visual restyle only, not a structural move. Keep `stat-high`/`stat-review`/`stat-total` ids exactly, including the archive-mode branch that relabels `#stat-total` to "Resolved" via the same id.

- [ ] **Step 13: Update `backend/tests/test_dashboard.py`**
  - `test_dashboard_contains_incident_card_markup` (current lines 49-70): change `assert b'class="card' in response.content` to assert whatever the new row's class attribute literally renders as (e.g. `assert b'class="ticket-row' in response.content` — must match Step 6's exact class string).
  - `test_dashboard_has_filter_controls` (current lines 73-77): keep `assert b'id="search-input"' in response.content` unchanged; replace `assert b'id="sidebar"' in response.content` with an assertion on the new toolbar-filter markup (e.g. `assert b'data-group="status"' in response.content` or equivalent, matching whatever Step 4 actually emits).
  - `test_dashboard_shows_review_badge` (current lines 80-87): run as-is first — `badge-review` and `data-id=` should still be present per Steps 6/10's preserved naming; only edit if they genuinely no longer appear.
  - Leave `test_archive_route_shows_only_resolved_incidents` and `test_live_dashboard_excludes_resolved_incidents` untouched (they assert on property-name text content only, not structural markup).

- [ ] **Step 14: Run the targeted test files**
  `cd backend && .venv/bin/python -m pytest tests/test_dashboard.py tests/test_ticket_detail_update.py tests/test_login.py -v`
  Expected: all pass.

- [ ] **Step 15: Manual/Playwright verification**
  Start the dev server, ensure at least one admin/super_admin user exists (bootstrap one directly via a short script using `auth.hash_password` + a DB insert if none exists — mirror the pattern in `backend/tests/conftest.py`'s `client`/`authenticated_client` fixtures), log in, navigate to `/`. Use `browser_snapshot` to confirm: no element matches `#sidebar`; a `<table id="ticket-table">` exists with `<tr class="ticket-row" data-id="...">` rows; clicking a row's data-id opens `#detail-modal-overlay` with an `.open` class; typing into `#search-input` narrows visible rows and updates `#visible-count`; clicking a Status/Priority/Category chip filters rows and updates its `selected-*-count` badge; `#stat-high`/`#stat-review`/`#stat-total` show non-placeholder numbers; each row's Acknowledge/Resolve/Ignore/Review buttons still call `setStatus()` and update `#status-badge-{id}` in place. Take a screenshot confirming the light "Warm Neutral" palette (background is `#faf8f5`-family, not `#07111f`). Check `browser_console_messages` for zero JS errors. Repeat the row-click + modal-open check on `/archive`.

- [ ] **Step 16: Run the full backend suite**
  `cd backend && .venv/bin/python -m pytest -v`
  Expected: all pass.

- [ ] **Step 17: Commit**
  ```bash
  git add backend/templates/dashboard.html backend/tests/test_dashboard.py
  git commit -m "feat: redesign dashboard with Warm Neutral palette, toolbar filters, and dense ticket table"
  ```

---

### Task 3: Settings page restyle (`settings.html`)

**Files:**
- Modify: `backend/templates/settings.html` (`<style>` currently lines 7-113, nav ~117-149, `.section` blocks at ~149, ~179)
- Test: `backend/tests/test_settings_ticket_groups.py`

**Interfaces:**
- Consumes: `backend/static/css/base.css`, `backend/templates/_nav.html` (Task 1).

- [ ] **Step 1: Confirm current baseline** — re-read the file to reconfirm exact line ranges haven't drifted from a prior task's edits (unrelated, but cheap insurance).
- [ ] **Step 2: Replace `<head>`/`<style>`** — add the `base.css` `<link>`; delete the local `:root` block (condensed subset: bg/bg-soft/surface/surface-2/line/text/muted/blue/red/green/amber/radius-xl/lg/md) and any component CSS now covered by `base.css` (nav, buttons, inputs). Keep only layout CSS unique to this page's WhatsApp-connection/ticket-groups sections, if any exists beyond what `.section-card` already covers.
- [ ] **Step 3: Replace the nav block** with `{% set active_page = 'settings' %}{% include "_nav.html" %}`.
- [ ] **Step 4: Rename `.section` → `.section-card`** in both the remaining local `<style>` (delete the local `.section` rule entirely, rely on `base.css`'s `.section-card`) and in the two `<div class="section">` usages (WhatsApp Connection, Ticket-Raising Groups) — rename to `<div class="section-card">`. `.section-title` stays as-is (already shared-shaped).
- [ ] **Step 5: Verify no other id/class this page's own inline `<script>` depends on has moved** — this page's avatar-hydration script reads `document.getElementById('nav-avatar')`, already satisfied by the new `_nav.html` partial's standardized id (no change needed here since this page already used `nav-avatar`, not `avatar`).
- [ ] **Step 6: Run tests** — `cd backend && .venv/bin/python -m pytest tests/test_settings_ticket_groups.py -v` (these test the `/settings/ticket-groups` proxy APIs, not HTML content — expect unaffected, all pass).
- [ ] **Step 7: Manual verification** — navigate to `/settings` as an admin, screenshot, confirm light palette, confirm nav "Settings" link is active/highlighted, confirm both section cards render identically to `profile.html`'s section cards (visual parity check against Task 6's output once both are done, or against the shared component spec if done first).
- [ ] **Step 8: Commit**
  ```bash
  git add backend/templates/settings.html
  git commit -m "style: restyle settings page with Warm Neutral design system"
  ```

---

### Task 4: Billing page restyle (`billing.html`)

**Files:**
- Modify: `backend/templates/billing.html` (`<style>` currently lines 7-115, nav ~119-158, `.section` blocks ~158, ~182, `<table>` ~185)
- Test: `backend/tests/test_billing_forward.py` (unrelated ingestion-gating tests, run for regression only)

**Interfaces:**
- Consumes: `backend/static/css/base.css`, `backend/templates/_nav.html` (Task 1).

- [ ] **Step 1: Replace `<head>`/`<style>`** — add `base.css` link, delete the local `:root`/nav/button/input CSS (same condensed subset as `settings.html`). Keep only the `.receipt`/statement-specific layout rules unique to this page, if any survive after the table gets `.data-table`.
- [ ] **Step 2: Replace the nav block** with `{% set active_page = 'billing' %}{% include "_nav.html" %}`.
- [ ] **Step 3: Rename `.section` → `.section-card`** for both the summary section and the "Payment History" section.
- [ ] **Step 4: Apply `.data-table` to the payment-history `<table>`** (Date/Phone/Amount/M-Pesa Receipt/Status/Period columns) — add `class="data-table"` to the `<table>` element; remove any now-redundant local table CSS in favor of `base.css`'s `.data-table` rules. Keep the `<span class="badge badge-{{ p.status }}">` cell exactly as-is (badge component already shared).
- [ ] **Step 5: Preserve the `{% if statement %}...{% else %}No billing data available.{% endif %}` and `{% if statement.payments %}...{% else %}<div class="empty">No payments yet...{% endif %}` conditional structure exactly** — this is Jinja logic, not styling, and must not change.
- [ ] **Step 6: Run tests** — `cd backend && .venv/bin/python -m pytest tests/test_billing_forward.py -v` (regression check only; these tests don't touch `/billing`'s HTML).
- [ ] **Step 7: Manual verification** — navigate to `/billing`, screenshot, confirm payment-history table renders with dense-table styling consistent with `users.html`'s table (cross-check once Task 5 is also done).
- [ ] **Step 8: Commit**
  ```bash
  git add backend/templates/billing.html
  git commit -m "style: restyle billing page with Warm Neutral design system and dense table"
  ```

---

### Task 5: Users page restyle (`users.html`)

**Files:**
- Modify: `backend/templates/users.html` (`<style>` currently lines 7-498, nav ~503-535, table ~569)
- Test: `backend/tests/test_users.py`

**Interfaces:**
- Consumes: `backend/static/css/base.css`, `backend/templates/_nav.html` (Task 1).

- [ ] **Step 1: Replace `<head>`/`<style>`** — add `base.css` link, delete the local `:root` block (note this file's `--radius-sm: 8px` differs from the old dashboard's `10px` — both are gone now, replaced uniformly by `base.css`'s `--radius-sm: 6px`) and component CSS now shared. Keep only layout CSS unique to the user-management form/modal (add-user dialog, group-assignment checkboxes) if not already covered by shared inputs/buttons.
- [ ] **Step 2: Replace the nav block** with `{% set active_page = 'users' %}{% include "_nav.html" %}`.
- [ ] **Step 3: Apply `.data-table` to the user-list `<table>`** (Member/Role/Groups/Joined/Added by columns, currently lines ~569-577) — add `class="data-table"`.
- [ ] **Step 4: Verify this page's own inline `<script>` avatar-hydration still targets `nav-avatar`** (it already does — `document.getElementById("nav-avatar")` at current line 618 — no change needed since this page never had the `id="avatar"` inconsistency).
- [ ] **Step 5: Run tests** — `cd backend && .venv/bin/python -m pytest tests/test_users.py -v` (these hit the `/users` JSON API, not HTML rendering — expect unaffected, all pass).
- [ ] **Step 6: Manual verification** — navigate to `/users` as an admin, screenshot, confirm the user table has dense-row styling, confirm add/delete-user flows still work end to end (create a throwaway test user, delete it) with no console errors.
- [ ] **Step 7: Commit**
  ```bash
  git add backend/templates/users.html
  git commit -m "style: restyle users page with Warm Neutral design system and dense table"
  ```

---

### Task 6: Profile page restyle (`profile.html`)

**Files:**
- Modify: `backend/templates/profile.html` (`<style>` currently lines 8-311, nav ~316-348, `.section-card` blocks ~352, ~368)
- Test: `backend/tests/test_admin_profile.py`

**Interfaces:**
- Consumes: `backend/static/css/base.css`, `backend/templates/_nav.html` (Task 1).

- [ ] **Step 1: Replace `<head>`/`<style>`** — add `base.css` link, delete the local `:root` block (this file's token set is the closest to the old dashboard's full set, including `--purple` without a `-soft` variant — all superseded by `base.css`) and shared component CSS. Keep only layout CSS unique to the WhatsApp-phone form and group-subscription checkbox list.
- [ ] **Step 2: Replace the nav block** with `{% set active_page = 'profile' %}{% include "_nav.html" %}`.
- [ ] **Step 3: Confirm `.section-card` naming already matches the shared component** (this file already uses `.section-card`, unlike `settings.html`/`billing.html`'s `.section`) — just delete the local `.section-card` CSS rule so it inherits from `base.css` instead; no HTML class renaming needed here.
- [ ] **Step 4: Run tests** — `cd backend && .venv/bin/python -m pytest tests/test_admin_profile.py -v` (JSON API tests for phone/subscriptions, not HTML — expect unaffected, all pass).
- [ ] **Step 5: Manual verification** — navigate to `/admin/profile`, screenshot, confirm both section cards render identically (visually) to `settings.html`'s converged `.section-card`s.
- [ ] **Step 6: Commit**
  ```bash
  git add backend/templates/profile.html
  git commit -m "style: restyle profile page with Warm Neutral design system"
  ```

---

### Task 7: Login page restyle (`login.html`)

**Files:**
- Modify: `backend/templates/login.html` (`<style>` currently lines 7-163 — no nav on this page)
- Test: `backend/tests/test_login.py`

**Interfaces:**
- Consumes: `backend/static/css/base.css` only (no `_nav.html` — this page has no nav bar).

- [ ] **Step 1: Replace `<head>`/`<style>`** — add `base.css` link, delete the local minimal `:root` block (bg/surface/surface-2/line/text/muted/blue/blue-deep/red/red-soft/radius-lg/md) and delete any button/input CSS now covered by `base.css`. Keep the page-specific centering/card-shell CSS (the centered-card structure is explicitly unchanged per spec §3 — "no structural change, it's already minimal").
- [ ] **Step 2: Restyle the card background/gradient** — this page currently has (per other similarly-styled pages) a radial-gradient dark background; replace with the flat `var(--bg)` warm background, card surface `var(--surface)` with `var(--shadow)` and `var(--radius-lg)` border-radius, matching the rest of the app. No structural DOM change.
- [ ] **Step 3: Verify the `<form>` element and its `error` conditional block are untouched** (`{{ error }}` display logic, username/password inputs, submit button) — restyle only.
- [ ] **Step 4: Run tests** — `cd backend && .venv/bin/python -m pytest tests/test_login.py -v`. Expected: all pass, including `test_login_get_returns_200` and the `assert b"form" in r.content.lower()` check (must still find a `<form>` tag — trivially true since the form isn't removed).
- [ ] **Step 5: Manual verification** — navigate to `/login` logged out, screenshot, confirm light palette, confirm login still works (submit valid credentials → redirect to `/`).
- [ ] **Step 6: Commit**
  ```bash
  git add backend/templates/login.html
  git commit -m "style: restyle login page with Warm Neutral design system"
  ```

---

### Task 8: Summaries page restyle (`summaries.html`) — scope amendment page

**Files:**
- Modify: `backend/templates/summaries.html` (`<style>` currently lines 7-358, nav ~363-395, date-picker form ~398-406)
- Test: `backend/tests/test_summaries.py`

**Interfaces:**
- Consumes: `backend/static/css/base.css`, `backend/templates/_nav.html` (Task 1).

**Scope note:** this page is in scope only via the SCOPE AMENDMENT, not the original approved spec — treat it exactly like Tasks 3/4/6 ("consistent restyle," no structural changes beyond what's needed for visual consistency). Do not invent new layout/behavior for it.

- [ ] **Step 1: Replace `<head>`/`<style>`** — add `base.css` link, delete the local `:root` block (full dark-navy set including `--purple`/`-soft` variants, matching the old dashboard's fullest token set) and shared component CSS (nav, buttons, inputs, badges if any). Keep only the date-picker form (`#date-form`/`#date-input`) and summary-list-specific layout CSS unique to this page.
- [ ] **Step 2: Replace the nav block** with `{% set active_page = 'summaries' %}{% include "_nav.html" %}`.
- [ ] **Step 3: Restyle the summaries-list rendering** (whatever cards/rows `#summaries-container`'s JS renders into `#loading-state` etc.) to reference the new palette tokens — this is client-side JS-generated markup similar to `dashboard.html`'s `buildCard()`/`renderDetailModal()` pattern; find the equivalent render function in this file's `<script>` block and update its generated class names/inline styles to the new tokens, without changing its data-fetching logic (`/api/summaries` call) or DOM target ids.
- [ ] **Step 4: Run tests** — `cd backend && .venv/bin/python -m pytest tests/test_summaries.py -v` (these hit `/api/summaries` JSON, not the `/summaries` HTML page — expect unaffected, all pass).
- [ ] **Step 5: Manual verification** — navigate to `/summaries`, screenshot, confirm light palette, confirm the date picker and summary list still populate correctly with no console errors, confirm nav no longer causes a dark→light flip when arriving from `/` or `/settings`.
- [ ] **Step 6: Commit**
  ```bash
  git add backend/templates/summaries.html
  git commit -m "style: restyle summaries page with Warm Neutral design system (scope amendment)"
  ```

---

### Task 9: Super admin categories page restyle (`super_admin_categories.html`) — scope amendment page

**Files:**
- Modify: `backend/templates/super_admin_categories.html` (`<style>` currently lines 7-365, nav ~369-396, category table ~411-430, own modal ~441-449)
- Test: `backend/tests/test_super_admin_categories.py`

**Interfaces:**
- Consumes: `backend/static/css/base.css`, `backend/templates/_nav.html` (Task 1).

**Scope note:** same treatment as Task 8 — restyle only, per the scope amendment.

- [ ] **Step 1: Replace `<head>`/`<style>`** — add `base.css` link, delete the local `:root` block and shared component CSS (nav, buttons, inputs, this page's own `.modal-overlay`/`.modal` shell CSS can be restyled to the new palette but its ids/structure stay — this modal, `id="modal"`/`id="modal-body"`, is unrelated to `dashboard.html`'s `#detail-modal-overlay` and must not be confused with it or merged with it).
- [ ] **Step 2: Replace the nav block** with `{% set active_page = 'super_admin_categories' %}{% include "_nav.html" %}`. **Note the expected side effect**, per Global Constraints: this page currently has no Settings or Billing nav links at all (not role-gated, just absent); after adopting `_nav.html`, both will appear and both will work correctly for a `super_admin` user (verified: `require_admin`/`require_login` both accept `super_admin`). This is intentional — mention it in the commit message so it's not mistaken for an accidental scope creep later.
- [ ] **Step 3: Fix the `id="avatar"` inconsistency** — change `<div class="user-avatar" id="avatar">` to `id="nav-avatar"` (now supplied automatically by `_nav.html`, so this HTML edit happens as part of Step 2's nav replacement) AND update this page's own inline `<script>` at the current `document.getElementById('avatar')` call (line 456) to `document.getElementById('nav-avatar')` — both must change together or the avatar-initials hydration silently breaks.
- [ ] **Step 4: Apply `.data-table` to the category `<table>`** (`id="cat-tbody"` rows) — add `class="data-table"` to the `<table>` element; keep the `id="cat-tbody"` `<tbody>` id and every `id="row-{{ cat.slug }}"` row id exactly (read by this page's own add/delete/remap JS).
- [ ] **Step 5: Restyle the page's own modal** (`id="modal"`, `id="modal-body"`, `id="modal-error"`, `id="modal-remap"`, `id="btn-confirm"`) to the new palette — ids/structure unchanged, CSS/classes only.
- [ ] **Step 6: Run tests** — `cd backend && .venv/bin/python -m pytest tests/test_super_admin_categories.py -v` (these hit `/api/super-admin/categories*` JSON, not the HTML page — expect unaffected, all pass).
- [ ] **Step 7: Manual verification** — log in as a super_admin, navigate to `/super-admin/categories`, screenshot, confirm light palette, confirm the new Settings/Billing nav links work (click through to each, confirm 200), confirm add/delete-category flows and the remap-delete modal still function with no console errors, confirm avatar initials render (validates the `nav-avatar` id fix from Step 3).
- [ ] **Step 8: Commit**
  ```bash
  git add backend/templates/super_admin_categories.html
  git commit -m "style: restyle super admin categories page with Warm Neutral design system (scope amendment); fix nav-avatar id and add missing Settings/Billing nav links via shared partial"
  ```

---

### Task 10: Full regression + manual verification pass (final task, verification-only)

**Files:**
- None modified — this task only runs and observes.

**Interfaces:**
- Consumes: all of Tasks 1-9.

- [ ] **Step 1: Run the full backend automated test suite**
  `cd backend && .venv/bin/python -m pytest -v`
  Expected: 100% pass, zero regressions across all 8 redesigned templates' backing routes and every unrelated test file.

- [ ] **Step 2: Grep for stray dark-theme remnants**
  From the repo root: `grep -rn "#07111f\|#0b1728\|#101f34\|#132840\|#18324f\|#38bdf8\|#a855f7" backend/templates/dashboard.html backend/templates/settings.html backend/templates/billing.html backend/templates/users.html backend/templates/profile.html backend/templates/login.html backend/templates/summaries.html backend/templates/super_admin_categories.html`
  Expected: zero matches (all old dark-navy hex values fully replaced by the new token references). Also grep each of these 8 files for `:root {` inside their own `<style>` blocks and confirm none redefine `--bg`/`--surface`/`--text`/`--radius-xl` etc. locally anymore (a stray local `:root` redefinition would silently override `base.css`).

- [ ] **Step 3: Confirm `_chat_widget.html` and `billing/templates/*` are untouched**
  `git diff --stat` (or `git diff --stat <base-branch>` if working on a feature branch) — confirm `backend/templates/_chat_widget.html` and every file under `billing/` and `openwa/` show zero changes.

- [ ] **Step 4: End-to-end Playwright pass across all 8 pages**
  Start the dev server, log in as an admin (and separately as a super_admin, to reach `/super-admin/categories`). For each of `/`, `/archive`, `/settings`, `/billing`, `/users`, `/admin/profile`, `/login` (logged out), `/summaries`, `/super-admin/categories`: navigate, take a screenshot, confirm the light Warm Neutral background and teal accent are visible, confirm the top nav's active-link highlighting matches the current page, confirm `browser_console_messages` reports zero JS errors. On `/` specifically, re-run the full interaction check from Task 2 Step 15 (search, chip filters, row click → modal open, quick status-change buttons, polling banner if a new incident is ingested via a test API call during the session).

- [ ] **Step 5: Confirm no visual dark→light flip navigating between any two pages**
  Click through the nav links in sequence (Live Queue → Archive → Users → Summaries → Profile → System Config → Settings → Billing → back to Live Queue) and confirm every page renders the same background/surface/accent colors — this is the specific risk the scope amendment was written to close.

- [ ] **Step 6: Report final status**
  Summarize: test suite pass/fail counts, any remaining dark-theme greps, any console errors found, and confirmation that all 8 pages share one visual system with no functional regressions in ticket filtering, search, status changes, or modal interactions.

(No commit for this task — it is verification-only. If Step 4 or 5 surfaces a defect, open a small follow-up fix in whichever earlier task's file it belongs to, re-run that task's tests, and re-run this task's Steps 1-5 before considering the plan complete.)

---

### Critical Files for Implementation

- backend/main.py
- backend/static/css/base.css
- backend/templates/_nav.html
- backend/templates/dashboard.html
- backend/tests/test_dashboard.py
