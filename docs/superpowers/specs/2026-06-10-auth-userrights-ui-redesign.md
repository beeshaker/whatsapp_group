# Auth, User Rights & UI Redesign — Design Spec

**Date:** 2026-06-10  
**Status:** Approved

---

## Overview

Add session-based login, a user management page, per-action audit attribution, and redesign the dashboard as a Kanban board that looks and feels like a ticketing system.

---

## Goals

1. Lock the dashboard behind a username/password login (session cookie).
2. Single role — all logged-in users have full access (view, change status, reply, relink).
3. Every write action is attributed to the logged-in user and visible in the incident audit trail.
4. Admins can add and remove users from inside the dashboard.
5. Dashboard redesigned as a Kanban board (status columns) with a top nav bar.

---

## Architecture

### New dependencies

- `passlib[bcrypt]` — password hashing (add to `requirements.txt`)
- `itsdangerous` — already pulled in transitively by Starlette/FastAPI; used for signing session cookies
- `starlette.middleware.sessions.SessionMiddleware` — already available, just needs to be wired up

### Database changes

**New table: `users`**

| Column | Type | Notes |
|--------|------|-------|
| id | Integer PK | |
| username | Text UNIQUE NOT NULL | |
| hashed_password | Text NOT NULL | bcrypt via passlib |
| created_at | DateTime(tz) NOT NULL | |
| created_by | Text nullable | username of creator, NULL for bootstrap admin |

**New table: `audit_log`**

| Column | Type | Notes |
|--------|------|-------|
| id | Integer PK | |
| username | Text NOT NULL | denormalised — survives user deletion |
| action | String(30) NOT NULL | `status_change`, `reply`, `relink` |
| incident_id | Integer NOT NULL | not a FK — survives incident archival |
| detail | Text nullable | human-readable string e.g. `"new → resolved"` or first 120 chars of reply |
| created_at | DateTime(tz) NOT NULL | |

**Modified table: `incident_status_history`**

- Add column `changed_by` (Text, nullable) — NULL for rows created before this feature.

### Bootstrap

On app startup (`lifespan`), if `users` table is empty:
- Read `ADMIN_USERNAME` and `ADMIN_PASSWORD` from env (defaults: `admin` / `changeme`).
- Hash the password and insert the bootstrap admin user.
- Log a warning if default credentials are used.

---

## Routes

### New routes

| Method | Path | Description |
|--------|------|-------------|
| GET | `/login` | Render login form (redirect to `/` if already logged in) |
| POST | `/login` | Validate credentials, set session cookie, redirect to `/` |
| POST | `/logout` | Clear session, redirect to `/login` |
| GET | `/users` | Render user management page (auth required) |
| POST | `/users` | Create a new user (auth required) |
| POST | `/users/{id}/delete` | Remove a user (auth required; cannot delete yourself) |

### Modified routes

| Route | Change |
|-------|--------|
| `GET /` | Require auth — redirect to `/login` if no session |
| `GET /archive` | Require auth — redirect to `/login` if no session |
| `PATCH /incidents/{id}/status` | Read `username` from session; write to `incident_status_history.changed_by` and `audit_log` |
| `POST /incidents/{id}/reply` | Use logged-in username as `reporter_name` in `IncidentUpdate` (replaces hardcoded `"Dashboard"`); write to `audit_log` |
| `PATCH /incidents/{id}/relink` | Write to `audit_log` |

### Auth dependency

```python
async def require_login(request: Request) -> str:
    username = request.session.get("username")
    if not username:
        raise HTTPException(status_code=302, headers={"Location": "/login"})
    return username
```

Applied to all dashboard HTML routes and all write API endpoints.

---

## Session

- `SessionMiddleware` mounted with `SECRET_KEY` env var (required; app refuses to start if unset or `"change-me"`).
- Session stores only `{"username": "<str>"}`.
- Cookie is `HttpOnly`, `SameSite=Lax`.

---

## UI Design

### Layout: Kanban board

The existing `dashboard.html` is replaced with a new Kanban-style layout. The archive page reuses the same template with a `mode` flag.

**Top nav bar** (fixed, full width):
- Left: logo mark + "Ops Ticketing" wordmark
- Centre: nav links — `📋 Live Queue` · `📁 Archive` · `👥 Users`
- Right: user avatar pill (initials + username) + "Sign out" button

**Filter bar** (below nav):
- Category filter pills (All / Electrical / Plumbing / Security / …)
- "High only" toggle pill
- Stats: High open · In review · Active total

**Kanban board** (5 columns):
- New · Review · Acknowledged · Resolved · Ignored
- Each column shows ticket cards with: ticket ID, property name, category icon, severity left-border colour, time, update/attachment badge
- Clicking a card opens the existing detail modal (unchanged except audit section added)

**Ticket detail modal additions**:
- Audit trail section at the bottom: `who · action · detail · time`
- Reply bar shows logged-in username next to the send button

### Login page

- Centred card on dark background with radial gradient blobs
- Logo mark + app name
- Username field + password field + "Sign in →" button
- No "forgot password" link (out of scope)

### User management page (`/users`)

- Same nav as dashboard
- Page title "Team Members" + subtitle showing count
- "+ Add user" reveals an inline form: username field, password field, "Create user" button
- Table: avatar (colour-coded initials) · username · created date · added by · Remove button
- Cannot remove yourself (Remove button disabled on own row)

---

## Audit trail display

Inside the ticket detail modal, a new "Activity" section renders `audit_log` rows for that incident:

```
sarah   changed status   new → review      09:15
tom     replied          "We are investig…" 09:22
abhishek changed status  review → acknowledged  09:41
```

---

## Security notes

- Passwords hashed with bcrypt (cost 12).
- `SECRET_KEY` must be set in env; app logs a startup error and exits if missing or default.
- `GATEWAY_SECRET_TOKEN` (ingest webhook secret) is **not** exposed in the HTML template after this change — it's only used server-side for ingest and media endpoints.
- PATCH/POST write endpoints continue to accept `X-API-Key` for the OpenWA gateway callbacks; session auth is only required for browser-facing HTML routes.
- Path traversal protection on `/media/{id}` is already in place.

---

## Out of scope

- Password reset / forgot password flow
- Email notifications
- Role-based permissions (all users have full access)
- Mobile layout improvements
- Drag-and-drop ticket moving between Kanban columns
