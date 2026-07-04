# Multi-Ticket Creation From One Message

**Date:** 2026-07-03
**Status:** Approved

## Overview

This is Group B of a larger 6-item ticketing request (Group A — priority/category/dates/escalation UI — already shipped). Today, one incoming WhatsApp message is classified once and produces at most one outcome: a new `Incident` row, or an `IncidentUpdate` row on an existing open ticket, or nothing (noise). This spec adds the ability for a single message that describes multiple distinct issues (e.g. "1. pump leaking 2. lift stuck on floor 3") to become multiple tickets/updates instead of one.

Two other groups (C — reminder timers, D — billing group-lock) are unrelated and specced separately.

---

## 1. Data Model & Deduplication

### The problem

`Incident.message_id` and `IncidentUpdate.message_id` each carry a DB `UNIQUE` constraint today — this is the entire mechanism that prevents a retried WhatsApp webhook delivery from creating duplicate tickets (one message_id → at most one row). `message_id` is also the *real* WhatsApp message ID, used unmodified for in-thread replies (`reply_to_message(group_id, incident.message_id, text)` in `main.py`), so it can't be suffixed or altered per issue.

Once one message can legitimately produce 2–5 rows, they must all share the same real `message_id` — which a single-column `UNIQUE(message_id)` constraint would incorrectly reject as duplicates.

### The fix

- Both `Incident` and `IncidentUpdate` gain a new column: `issue_index: Mapped[int]` (`Integer`, `nullable=False`, `default=0`, `server_default="0"`) — which issue-within-the-split this row represents. An ordinary single-issue message always produces `issue_index=0`.
- The unique constraint on both tables changes from `UniqueConstraint("message_id", ...)` to `UniqueConstraint("message_id", "issue_index", ...)`.
- Migration (manual SQL, same try/except pattern as existing migrations in `database.py`): `ALTER TABLE incidents ADD COLUMN issue_index INTEGER NOT NULL DEFAULT 0`, drop the old unique index, create the new compound one. Same for `incident_updates`. Existing rows automatically get `issue_index=0`, so their original uniqueness guarantee (one row per message_id) is preserved unchanged.
- A hard cap of **5 issues per message** applies. If the classifier identifies more, only the top 5 by confidence are kept (consistent with the media-attachment rule in §3).
- Sibling tickets from the same split require no new linkage table — they're queryable via `WHERE message_id = X ORDER BY issue_index`.

---

## 2. Classifier Changes (`classifier.py`)

`classify_message(message: str, db) -> dict` changes its return shape:

**Today:**
```python
{"is_incident": bool, "category": str, "priority": str, "confidence": float}
```

**New:**
```python
{"issues": [
    {"category": str, "priority": str, "confidence": float, "message_snippet": str},
    ...
]}
```

An empty `issues` list is the new "noise" signal (replaces `is_incident: False`).

- One LLM call per message, regardless of how many issues it contains. The prompt is rewritten to ask for a JSON array: for each distinct, actionable issue in the message, extract a clean `message_snippet` (the relevant portion of text for that issue) and classify its `category`/`priority`/`confidence` independently, using the same category list and priority levels as today.
- Fallback on any parse/LLM failure: `{"issues": []}` (equivalent to today's noise fallback).
- Per-field fallbacks for an individual issue (unrecognized category → `"other"`, unrecognized priority → `"medium"`) are unchanged from today's single-issue logic, just applied per issue in the list.
- Capping to top 5 by confidence happens inside `classify_message`, so callers always receive an already-capped list.

`classify_update_or_new(message, open_tickets)` is **unchanged** — it now gets called once per issue snippet (instead of once per message) by the ingest handler.

---

## 3. Ingest Flow (`main.py`)

Both `ingest()` (text messages) and `_handle_media_ingest()` (media + captions) change from "classify once, branch once" to "classify once, loop over issues":

1. Call `classify_message(message_body, db)` → get `issues` (already capped at 5, ordered as returned).
2. Filter to issues where `confidence >= MIN_CONFIDENCE`. If none survive, respond exactly like today's noise case (no rows created).
3. For each surviving issue, in order, with `issue_index` = its position in the **original** (pre-filter) list:
   - Duplicate check: `SELECT ... WHERE message_id = X AND issue_index = N`. If found, skip this issue (already processed) — this makes partial-retry-after-crash safe: if issues 0–1 were committed before a crash and the webhook retries the whole message, issues 0–1 are skipped and 2+ still get created.
   - Call `classify_update_or_new(issue["message_snippet"], open_tickets)`:
     - `"update"` → insert an `IncidentUpdate` row (`message_body` = the snippet, plus `message_id`, `issue_index`).
     - `"new"` → insert an `Incident` row (`message_body`, `category`, `priority`, `confidence` from that issue, plus `message_id`, `issue_index`).
   - `open_tickets` is refreshed after each new ticket is created within the same message, so a later issue in the same split can correctly route as an update to a ticket the split itself just created moments earlier.
   - The DB's compound `UNIQUE(message_id, issue_index)` constraint remains the race-condition backstop — an `IntegrityError` on insert is still caught and treated as "duplicate," exactly like today, just scoped to the pair.
4. **Media attachment:** when the source was a captioned media message, the media file attaches only to the single surviving issue with the highest confidence. The other resulting tickets/updates from the same split get no media row. If the caption yields zero surviving issues, the existing no-caption/orphaned-media fallback behavior is preserved unchanged (this spec does not change that path).
5. Response shape: today's `{"status": "staged", "property": ..., "category": ..., "priority": ...}` (single-ticket-shaped) is replaced with `{"status": "staged", "tickets_created": N, "updates_created": M}`. The existing `"noise"`, `"duplicate"`, and `"error"` statuses and their response shapes are unchanged.

---

## 4. API & UI — Sibling Ticket Indicator

- `GET /incidents/{incident_id}` gains a `sibling_tickets` field: a list of `{id, property_name, category, priority, status}` for other `Incident` rows sharing the same `message_id` (excluding the current ticket), ordered by `issue_index`. Empty list for the overwhelming common case (a non-split ticket).
- The ticket detail modal (`dashboard.html`) gets a new section, shown only when `sibling_tickets` is non-empty: "Also reported in this message:" followed by each sibling as a clickable chip/link (e.g. `#12 Electrical · High`) that calls the existing `openDetailModal(id)` to jump to that sibling's own detail view — reusing the same cross-linking pattern the relink dropdown already uses.
- `IncidentUpdate` rows from a split need no equivalent treatment — an update-routed issue already appears in its parent ticket's existing "Updates" thread, so it's visible in context without a new indicator.

---

## 5. Error Handling

| Scenario | Behavior |
|---|---|
| LLM returns malformed JSON / call fails | `{"issues": []}` — whole message treated as noise, matches today's fallback pattern |
| More than 5 issues returned | Keep top 5 by confidence, drop the rest silently |
| All issues below `MIN_CONFIDENCE` | Whole message treated as noise, no rows created |
| Unrecognized category/priority on one issue | Falls back to `"other"` / `"medium"` for that issue only, same as today's single-issue fallback |
| Retried webhook delivery (same message_id) | Per-issue dedup check skips already-created `(message_id, issue_index)` pairs; a true concurrent race is still caught by the DB's compound unique constraint |
| Captioned media, caption yields zero issues | Falls back to the existing (unchanged) no-caption/orphaned-media handling |

---

## 6. Testing

- **Migration tests**: `issue_index` column exists on both tables with default 0; existing (pre-migration) rows preserved; new compound unique constraint accepts differing `issue_index` for the same `message_id` but rejects an exact duplicate pair.
- **Classifier tests**: empty/single/multi-issue (2–5) JSON array parsing; cap-to-top-5-by-confidence when the model returns more than 5; per-issue category/priority fallback; whole-call fallback on malformed response.
- **Ingest tests**: a 3-issue split creates 3 `Incident` rows with correct `issue_index` values; a mixed split (one issue routes "new," another routes "update" to an existing open ticket) produces one `Incident` + one `IncidentUpdate`; per-issue confidence filtering drops only the low-confidence issue(s) from a split, not the whole message; a later issue in a split correctly updates a ticket the same split just created; media caption split attaches the media file only to the highest-confidence issue; retried delivery of an already-fully-processed message creates nothing; retried delivery of a partially-processed message (crash after issue 0) completes only the missing issues.
- **Sibling ticket tests**: `GET /incidents/{id}` returns the correct `sibling_tickets` list (right siblings, right order, excludes self) for a split ticket, and an empty list for a non-split ticket.
- **Manual dashboard verification**: open a split ticket's detail modal, confirm the "Also reported in this message" section appears with clickable sibling links; confirm it's absent for an ordinary ticket.

---

## 7. Out of Scope

- Auto-escalation, reminder timers tied to `end_date` (Group C).
- Billing group restrictions (Group D).
- Any change to category management (already exists, untouched).
- Attaching media to more than one resulting ticket from a split.
- Backfilling `issue_index`/sibling relationships for historical data beyond the automatic `default=0`.
- Any visual timeline/grouping of split messages beyond the simple sibling-link list in the modal.
