# Pixiilive: License-Plate-Based Ticketing Flow

**Date:** 2026-07-11
**Status:** Approved

## Overview

Pixiilive is a delivery company client. Their riders report bike issues into one shared WhatsApp group, and today the system would classify each report only by issue "type" (category), the same way every other client's property-management tickets work. Pixiilive needs tickets tracked per-bike instead — identified by the bike's license plate (Kenyan format: `K` + 3 letters, optional space, 3 digits, optional trailing letter, e.g. `KMGQ 947Z`) — while still keeping a category for what's actually wrong (brakes/tyres/engine/etc).

Each client in this repo is a fully separate deployment (own backend container, DB, `.env`) with no shared multi-tenant `Client` model in `backend/` — per-client behavior differences are handled purely through env vars (`CLIENT_SUBDOMAIN`, `DASHBOARD_TITLE`, etc.), not a plugin system. This spec follows that exact pattern: a new env flag gates the new behavior, defaulting off, so every other client is byte-for-byte unaffected.

Confirmed requirements:
- One shared WhatsApp group for all riders — license plate is a new sub-dimension within that existing "property" group, not a group-per-bike scheme.
- Keep a problem-type category alongside the plate (both dimensions matter).
- A new message mentioning a plate that already has an open ticket threads in as an update, not a duplicate.
- A message with no recognizable plate becomes an "unassigned" ticket (reporter still recorded); an admin fills in the plate manually later.

## 1. Data Model (`backend/models.py`, `backend/database.py`)

`Incident` gains `vehicle_plate: Mapped[Optional[str]] = mapped_column(String(10), nullable=True, index=True)`, placed after `category`. Lives only on `Incident`, not `IncidentUpdate` — mirrors how `category`/`priority` already work.

`database.py`'s `init_db()` gets two more idempotent try/except migration blocks matching the existing pattern: `ALTER TABLE incidents ADD COLUMN vehicle_plate TEXT`, then `CREATE INDEX IF NOT EXISTS ix_incidents_vehicle_plate ON incidents (vehicle_plate)`. Purely additive/nullable — zero risk to clients that never set the new env flag.

## 2. Plate Extraction (`backend/vehicle_plate.py`, new module)

Deterministic regex, no LLM: `_PLATE_RE = re.compile(r'\bK[A-Z]{3}\s?\d{3}[A-Z]?\b', re.IGNORECASE)`.

- `normalize_plate(raw) -> str` — uppercase, strip whitespace (`"kmgq 947z"` → `"KMGQ947Z"`).
- `extract_plates(text) -> list[str]` — all normalized matches in order of appearance.
- `resolve_plate_for_issue(message_snippet, full_text) -> Optional[str]` — try the issue's own snippet first; if none found there, fall back to the full message only if it contains exactly one distinct plate. Ambiguous multi-plate cases resolve to `None` rather than guessing an order-based mapping (a wrong guess would silently misassign a bike, worse than leaving it unassigned).
- `is_valid_plate(value) -> bool` — strict whole-string match, used to validate direct admin input via the PATCH endpoint.

`\b` word boundaries reject a plate embedded in a longer alphanumeric run; `\s?` covers the sometimes-missing space; the optional trailing `[A-Z]?` covers the rarely-missing final letter. Known accepted limitation: a coincidental substring matching the pattern elsewhere in a message is treated as a plate — judged rare enough in this domain not to mitigate further.

## 3. Routing (`backend/main.py`)

New shared helper `_route_issue(issue, full_text, open_tickets) -> dict`, placed after `_get_open_tickets`, called from both `_handle_text_ingest` and `_handle_media_ingest` (which otherwise duplicate the per-issue routing loop):

- `FLEET_PLATE_MODE` on: resolve a plate for the issue; exact-match it against open tickets' `vehicle_plate` → `{"routing": "update", "ticket_id": ..., "vehicle_plate": ...}`, else `{"routing": "new", "vehicle_plate": <plate-or-None>}`. No LLM call in this branch — a deterministic identity match is not mixed with the fuzzy LLM router, since that could misthread two different bikes' issues.
- `FLEET_PLATE_MODE` off (default): delegates unchanged to today's `classify_update_or_new`, `vehicle_plate` always `None`.

`_get_open_tickets` gains a `limit` param (default 5, called with a larger value like 50 from the fleet-mode path only — its cap exists to bound LLM prompt size, which doesn't apply to a plain equality scan) and returns `vehicle_plate` per ticket.

## 4. Surfacing the Field

- `TicketDetailUpdateBody` gains `vehicle_plate: Optional[str] = None` with a validator that normalizes and rejects malformed input via `is_valid_plate` — how an admin assigns a plate to an unassigned ticket.
- `update_incident_fields` (`PATCH /incidents/{id}`) gains a diff/audit-log block for `vehicle_plate`, mirroring `priority`/`escalated`. Not gated by `FLEET_PLATE_MODE` — always available.
- `list_incidents` and `get_incident_detail` add `vehicle_plate` to their JSON output (including `sibling_tickets`).
- `dashboard()`/`archive_dashboard()` pass `fleet_plate_mode: FLEET_PLATE_MODE` into the template context.
- `dashboard.html`: a `FLEET_PLATE_MODE` JS global; a plate badge on the incident card (both Jinja-rendered and JS `buildCard`/`normalizeIncident` polling paths); a plate field in the ticket detail panel's read-only and editable views, wired through the existing `updateTicketField(id, field, value)` → `PATCH /incidents/{id}` mechanism.

## 5. Classifier Prompt (`backend/classifier.py`)

`_build_prompt` hardcodes "You are classifying WhatsApp messages from a property management company...". Replace the opening lines with a new `CLASSIFIER_CONTEXT` env var (default = today's exact wording), read the same way as `OLLAMA_HOST`/`OLLAMA_MODEL`.

## 6. Env Vars

| Var | Default | Purpose |
|---|---|---|
| `FLEET_PLATE_MODE` | `false` | Gates plate extraction, exact-match routing, and dashboard plate UI |
| `CLASSIFIER_CONTEXT` | today's property-management sentence | Domain wording injected into the classifier prompt |

## 7. Testing

- `backend/tests/test_vehicle_plate.py`: unit tests for `normalize_plate`, `extract_plates` (spaced/unspaced/missing-letter/lowercase/embedded/multiple/non-match), `resolve_plate_for_issue`, `is_valid_plate`.
- Unit tests for `_route_issue`: plate matches → update; plate no match → new; no plate → always new and the LLM router is never called; `FLEET_PLATE_MODE=False` → unchanged delegation.
- Integration tests (existing `monkeypatch.setenv` + module-reload pattern from `test_billing_forward.py`) through `/api/v1/ops/ingest`: new plated ticket, repeat-plate threading, plateless unassigned ticket, two-issue/two-plate split.
- `test_ticket_detail_update.py`-style tests for the new PATCH field; `test_db_migrations.py` check for the new column.
- Full existing suite must stay green with `FLEET_PLATE_MODE` unset — the key regression bar.
- Dashboard rendering verified manually (server-rendered + JS polling paths), not by unit test.

## 8. Rollout (config only, no code differences from other clients)

Pixiilive is already onboarded (port 8003) — this is an update, not fresh onboarding. Deploy via the existing `update-clients.sh pixiilive` flow, add `FLEET_PLATE_MODE=true` and a fleet-appropriate `CLASSIFIER_CONTEXT` to pixiilive's `.env` only, restart, reseed its category list via the existing `/super-admin/categories` UI (brakes/tyres/engine/electrical/bodywork, keeping protected `other`), then smoke-test in the rider group.

## Out of Scope

- Any change to `billing/` — it only models `Client` for provisioning, unaffected by this ticketing-side flag.
- Retroactively linking an unassigned ticket to a later message that resolves the same plate — admins use the existing ticket-update "relink" feature for manual cleanup if needed.
- A generalized "asset ID" abstraction for future non-plate use cases — scoped strictly to license plates for this client.
