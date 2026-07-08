# Client Dashboard UI/UX Redesign

**Date:** 2026-07-08
**Status:** Approved

## Overview

The client-tenant dashboard — `backend/templates/dashboard.html` (tickets), `settings.html`, `billing.html`, `users.html`, `profile.html`, and `login.html` — currently uses a dark navy/blue gradient theme with a blue+purple accent combo, and each template duplicates its own `<style>` block with near-identical design tokens (colors, radii, spacing). This spec reimagines the visual style, the ticket queue's layout, and the shared navigation, while keeping the rest of the pages functionally unchanged.

This is a client-tenant-facing redesign only. The embeddable customer chat widget (`_chat_widget.html`), the billing-admin app (`billing/templates/*`), and the openwa React dashboard are out of scope.

Decided through visual brainstorming (mockup comparisons in a browser companion) plus terminal discussion. No backend/business-logic changes are in scope — this is templates, CSS, and DOM structure only. Existing JS behavior (polling, inline field updates, filter logic, escalation toggle, etc.) is preserved; only the filter *UI* (sidebar → toolbar chips) and ticket list *presentation* (cards → table rows) change structurally.

---

## 1. Shared Design System

**Palette — "Warm Neutral":** light warm backgrounds instead of dark navy.
- Background: `#faf8f5` (page), `#fffdfa` (surfaces/nav/cards)
- Borders: warm gray, e.g. `#ece7de`
- Text: dark warm gray/near-black for primary, muted warm gray (`#8a7f6c`-family) for secondary
- Accent: teal (`#0d9488`-family) — replaces the current blue+purple gradient everywhere (links, primary buttons, active nav state, focus rings, selected-chip fill)
- Status colors (red/amber/green/purple for priority/status badges) are retained but re-tuned for contrast against the light background instead of the dark one
- Corners: softer, ~8–10px radius (down from today's 12–22px `--radius-xl/lg/md/sm` scale)
- Typography: Inter stays, unchanged

**Shared CSS extraction:** Today all 7 templates duplicate their own token/component CSS inline. Extract shared tokens (colors, radii, spacing, shadows) and base components (buttons, inputs, nav, cards, table, badges/pills, chips) into one static CSS file. Since `backend/main.py` currently has no `StaticFiles` mount, add one (e.g. `backend/static/`, mounted at `/static`) and link the shared stylesheet from every template's `<head>`. Each template keeps a small page-specific `<style>` block only for layout unique to that page (e.g. the dashboard's table/toolbar, the login card's centering).

This does two jobs at once: gives every page a consistent look, and removes ~7x duplication of the same CSS.

---

## 2. Tickets Dashboard (`dashboard.html`) — the core rework

This page gets the real structural change; the others get restyled with shared components.

- **Top nav**, kept (not moved to a sidebar), restyled to the light palette: same links (Tickets/Settings/Billing/Users/Profile, sign-out), lighter visual weight than today's glow/blur dark bar.
- **Toolbar replaces the filter sidebar:** search input plus Status/Priority/Category as dropdown chip filters, each showing a count badge when active (parity with today's sidebar group counts). This reclaims the ~60–70px the collapsible sidebar currently takes, and gives the table full width.
- **Ticket list becomes a dense table** instead of expandable cards: one row per ticket showing priority dot, id, reporter/property, category, status, update/attachment counts, age. Replaces `#card-list`'s `.card` elements; row click behavior replaces `.card-head` click-to-expand.
- **Detail view stays a modal** (`#detail-modal` / `openDetailModal`), restyled to the new palette — content unchanged (ticket details form, status history, media, sibling tickets, message composer).
- **Stats bar** (High·Open / In Review / Total, plus the archive view's Resolved count) is retained but restyled as compact pills inside the toolbar rather than a separate full-width bar.
- Polling (30s), inline field updates (`updateTicketField`), filter logic (`applyFilters`), and all other existing JS behavior are unchanged — only the DOM/CSS they render into changes.

---

## 3. Settings, Billing, Users, Profile, Login — consistent restyle

Lighter touch than the dashboard: these pages already use simple section-card/form/table patterns, so the work is applying the shared design system from §1, not rethinking structure.

- Converge on one shared section-card component — today `profile.html` uses `.section-card` and `settings.html` uses `.section`; both should render identically using the shared component.
- `users.html` and `billing.html`'s `<table>` elements (user list, payment history) pick up the same dense-table row styling introduced for the ticket queue, so tables look consistent across the app.
- `login.html` keeps its current centered-card structure, restyled to the new palette — no structural change, it's already minimal.
- No new features, no page reorganization beyond adopting shared components.

---

## 4. Out of Scope

- `_chat_widget.html` (customer-facing chat widget)
- `billing/templates/*` (billing-admin app)
- `openwa/dashboard/*` (React infra dashboard)
- Any backend endpoint, business logic, or data model changes
- Mobile/responsive redesign beyond whatever the shared components naturally provide (not a stated requirement; not explicitly explored in this pass)

---

## 5. Rollout

All pages are redesigned together in one pass (not phased page-by-page), sharing the same design-system extraction work from §1. The implementation plan should still sequence work sensibly (e.g. shared CSS + dashboard first since it's the biggest change and validates the component set, then the remaining pages), but all are part of this single spec's scope.
