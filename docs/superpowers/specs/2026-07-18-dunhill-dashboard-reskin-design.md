# Dunhill Dashboard Reskin — Design

## 1. Goal

Restyle Dunhill's (`LEAD_MODE=true`) dashboard to match a client-supplied mockup: a sidebar-nav app with a real-time Overview page, a polished Leads table, and a tabbed slide-in lead detail panel — while leaving every other client's dashboard (the shared top-navbar, warm-neutral theme) byte-for-byte unchanged, exactly as every prior `LEAD_MODE` task has done.

## 2. Scope Decision

The client-supplied mockup bundles three distinct things that must not be conflated:

1. **A visual/layout reskin** of what already exists (this spec's entire scope).
2. **Dunhill's own Phase 2** (PDF property listings + lead-to-property matching, per `docs/superpowers/specs/2026-07-17-dunhill-lead-capture-design.md` §9) — explicitly **not** built here. The mockup's "Properties" page and "Top 3 Matches" panel become disabled placeholders, continuing the "Matches — coming in Phase 2" hook already established in Phase 1.
3. **Unspecced new features** the mockup assumes but that don't exist in any phase: an SLA-based "Acknowledged" stage with due-by clocks, an "Agent Performance" analytics page, a notifications system. **None of these are built here.** Where the mockup's Overview/Leads pages depend on them, the design below either collapses to Dunhill's real 4-status lifecycle (`new` / `contacted` / `closed_won` / `closed_lost`) or drops the widget entirely — never fabricates data.

**This is reskin-only. Zero backend/API changes.** Everything consumed here already exists from Tasks 1–9 of `2026-07-17-dunhill-lead-capture-phase1.md`.

## 3. Architecture

- **New sidebar partial** `backend/templates/_nav_lead.html`, included by lead-mode page templates in place of the existing shared `_nav.html` (top-navbar). `_nav.html` itself is untouched — every other client keeps its current navigation exactly as-is.
- **New route `GET /overview`** (lead-mode only) + new template `backend/templates/overview.html`. Requires the same auth (`require_login`) as `/` and `/archive` today. Returns **404 when `LEAD_MODE` is unset** — this is a lead-only feature, not a generically-available route.
- **Existing routes unchanged**: `/` (live) and `/archive` keep their current handlers, queries, and all existing JS (filtering, polling, status updates, PATCH calls) from Task 9. Only their rendered markup changes — new shell wrapper, restyled stat bar/table — gated by `{% if lead_mode %}` exactly like the rest of the codebase's mode-branching convention.
- **Sidebar nav items**:

  | Item | State |
  |---|---|
  | Overview | Real — links to `/overview` |
  | Leads | Real — links to `/` |
  | Archive | Real — links to `/archive` |
  | Properties | Disabled (no `href`/inert), "Coming soon" tooltip |
  | Agent Performance | Disabled (no `href`/inert), "Coming soon" tooltip |
  | Administration | Real — links to existing `/users` (role-gated admin/super_admin, same rule as `_nav.html` today) |

  Disabled items must not be reachable by click (no dead-link 404s) — render as a non-anchor element (e.g. `<span class="nav-item-disabled">`) or an anchor with no `href`.

## 4. Visual Language — New Token Set

The mockup's navy-sidebar/blue-accent palette is visually distinct from `base.css`'s shared warm-cream/teal tokens (used by every other client). Rather than force-fitting Dunhill into those tokens or mutating the shared file, add a **new, additive CSS block scoped to lead-mode templates only** (e.g. `backend/static/css/lead-theme.css`, linked only by `_nav_lead.html`/`overview.html`/lead-mode branches of `dashboard.html`) defining Dunhill-specific tokens — navy sidebar background, blue/orange stat-card accents, matching the reference image. `base.css`'s existing `:root` block and every token in it stay exactly as they are; nothing in this new file overrides or redefines an existing token name.

## 5. Overview Page (`/overview`)

All widgets are computed from existing `Incident`/`IncidentStatusHistory` columns — no schema changes.

**Stat cards:**
- Leads Received Today (`received_at` = today)
- New (`status = 'new'`)
- Contacted (`status = 'contacted'`)
- Won this month (`status = 'closed_won'` AND became so this calendar month — via `IncidentStatusHistory` `to_status='closed_won'` + `changed_at` in current month)
- Lost this month (same pattern for `closed_lost`)

**Lead Flow (Today) widget:** leads received today, broken down by **current** status (not true stage-by-stage funnel tracking, since no per-stage timestamp exists) — e.g. "12 received today → 5 New · 4 Contacted · 2 Won · 1 Lost."

**Conversion Rate widget:** `closed_won / (contacted + closed_won + closed_lost)`, expressed as a percentage. Simple arithmetic on existing counts.

**Newest Unactioned Leads widget** (replaces the mockup's SLA-based "Overdue Leads" list): the 5 most recent `status='new'` leads, sorted by `received_at desc`, each linking to its detail panel. Preserves the "what needs attention" purpose using entirely real data.

**Explicitly dropped** (no real data to back them): Agent Response Performance donut, Overdue count/clock, Viewings Arranged. Per-property-type breakdown is **not** duplicated here — it already lives on the Leads page (Task 9) and isn't part of the reference mockup's Overview section either.

## 6. Leads (`/`) and Archive Pages

- Wrapped in the new sidebar shell + navy/blue theme.
- Stat tiles and table restyled to match the mockup's card/color language — markup and CSS classes change, but every `data-*` attribute the existing JS (`buildRow`, `applyFilters`, etc.) depends on is preserved unchanged, so Task 9's filtering/search/polling logic keeps working without modification.
- Status filter tabs collapse to Dunhill's real 4 statuses: **All / New / Contacted / Won / Lost** (mockup's Awaiting Ack / Ack Not Contacted / Overdue / Viewings are dropped, consistent with §2).
- Toolbar: **Filters** button opens the existing category/agent filter chips (relabeled/restyled into this button, not re-implemented). **Columns** and **Download** render as disabled/"coming soon" — no CSV export, no column show/hide in this pass.

## 7. Lead Detail Panel

Converts from the current modal to a right-side slide-in panel. Header: contact name, status badge, phone, lead ID, received timestamp — all from existing fields.

**Tabs:**
- **Overview** (default) — Assigned Agent (existing `lead_agent` field, edit action relabeled "Reassign," same `updateTicketField` call as today) + Client Requirement grid (**Transaction, Property type, Location, Budget** — the 4 fields with real backing columns; no Bedrooms/Furnishing/Other, since no such columns exist) + Original WhatsApp Message card (`message_body`, unabridged).
- **Activity** — the existing reply/update thread (Task 5-8's `IncidentUpdate` rows), restyled as a message-thread timeline.
- **History** — the existing status-history dots/timeline (Task 6), broken into its own tab instead of rendered inline.
- **Matches** — visible, disabled, "coming in Phase 2" (per §2).
- **No Notes tab** — no backing field or feature; not fabricated.

The mockup's "Accountability" section (Ack Status / Contact Status with SLA due-by clocks) is **not built** — replaced by the Assigned Agent + current status badge already described above, which is everything the real data supports.

## 8. Testing

- Extend Task 9's `test_non_lead_dashboard_unaffected` pattern: `/` and `/archive` render byte-for-byte identical to today when `LEAD_MODE` is unset. This is the standing regression bar for every task in this project and applies here too.
- `/overview`: 404 when `LEAD_MODE` unset. When on, seed leads across all 4 statuses at varying `received_at`/`changed_at` times and assert each stat card and widget computes the correct number.
- Detail panel: tests confirm Overview/Activity/History tabs render correct content; Matches tab present but inert (no fetch, no click handler).
- Sidebar: Properties/Agent Performance are non-navigable (no `href` reachable, no route exists behind them) — no dead-link 404s possible.
- Auth/role gating on `/overview` and the Administration link mirrors `_nav.html`'s existing role checks exactly.
- No backend/API test changes needed — this consumes Tasks 6–8's endpoints as-is.

## 9. Out of Scope (this spec)

- Anything from Dunhill's Phase 2 (property listings, matching) or Phase 3 (follow-up nudges, closure parsing) — see `2026-07-17-dunhill-lead-capture-design.md` §9 for those hooks.
- SLA/due-by tracking, Ack-status-distinct-from-Contacted, Agent Performance analytics, notifications — none of these have a spec yet; if wanted, they need their own brainstorming pass, not a bolt-on here.
- CSV export ("Download") and column show/hide ("Columns") — toolbar placeholders only.
- Any change to `base.css`'s shared token set or to `_nav.html` (both used by every non-Dunhill client).

## 10. Explicit Hooks Left for Later

- The new `lead-theme.css` file is additive-only and scoped to lead-mode templates — a future phase can extend it without touching `base.css`.
- Properties/Agent Performance sidebar items exist in the markup now (disabled) so a future phase only needs to make them real routes, not redesign the nav.
- The Matches tab in the detail panel is the same "coming in Phase 2" integration point already identified in the Phase 1 spec §9 — this reskin doesn't move that goalpost, just gives it a home in the new tabbed layout.
