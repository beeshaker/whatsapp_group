# Billing Enforcement Lifecycle Design

**Date:** 2026-06-27  
**Status:** Approved

## Overview

Implement a graduated payment enforcement system that keeps the billing WhatsApp group functional at all times while progressively restricting service access for clients with outstanding payments. Manual client creation via the dashboard remains unchanged.

## Status Lifecycle

| Status | Trigger | Dashboard | Ticketing groups | Billing group | Containers |
|---|---|---|---|---|---|
| `active` | Payment confirmed | ✅ | ✅ | ✅ | Running |
| `grace` | Past `renewal_date` | ❌ (nginx gate) | ✅ | ✅ | Running |
| `billing_only` | 14 days into grace | ❌ (nginx gate) | ❌ (silent drop) | ✅ | Running |
| `closed` | `/close` command or admin button | ❌ | ❌ | ❌ | Stopped |

Payment confirmation at any non-closed state returns client to `active`.

## Database Changes

New and changed columns on the `clients` table:

| Column | Type | Description |
|---|---|---|
| `grace_started_at` | DateTime (existing) | When grace period began |
| `billing_only_started_at` | DateTime (new) | When billing-only mode began |
| `last_warning_sent_at` | DateTime (replaces `warning_sent_at`) | Tracks 2-day warning cadence |
| `data_retention_days` | Integer (new, default 90) | How long data is retained after close; editable per client |
| `pre_expiry_14_warned` | Boolean (new, default false) | Whether 14-day pre-expiry warning was sent |
| `pre_expiry_2_warned` | Boolean (new, default false) | Whether 2-day pre-expiry warning was sent |

Status values: `active`, `grace`, `billing_only`, `closed`. Old values `warning` and `suspended` are removed.

## Scheduler Logic (`billing/scheduler.py`)

Runs daily at 08:00 Nairobi time. Skips clients with status `closed`.

### Active clients
- `renewal_date - today == 14` and not `pre_expiry_14_warned` → send 14-day warning, set flag
- `renewal_date - today == 2` and not `pre_expiry_2_warned` → send 2-day warning, set flag
- `today > renewal_date` → set `status = "grace"`, set `grace_started_at`, send grace entry message, update `last_warning_sent_at`

### Grace clients
- `now - grace_started_at >= 14 days` → set `status = "billing_only"`, set `billing_only_started_at`, send billing-only entry message, update `last_warning_sent_at`
- else if `now - last_warning_sent_at >= 2 days` → send grace reminder (includes days overdue and days remaining), update `last_warning_sent_at`

### Billing-only clients
- `now - last_warning_sent_at >= 2 days` → send data-loss warning (includes days remaining based on `data_retention_days`), update `last_warning_sent_at`

**Note:** The scheduler no longer calls `stop_client()`. Containers are only stopped on explicit close.

## Warning Messages (all sent to billing WhatsApp group)

| Trigger | Message |
|---|---|
| 14 days before renewal | `🔔 Reminder: Your subscription renews in 14 days on {renewal_date}. Type /payment to pay early and avoid any interruption.` |
| 2 days before renewal | `⚠️ Urgent: Your subscription renews in 2 days on {renewal_date}. Type /payment now to keep your service active.` |
| Enters grace | `🔒 Your subscription expired on {renewal_date}. Your dashboard has been locked. Type /payment to restore access. You have 14 days before ticketing is also suspended.` |
| Grace reminder (every 2 days) | `⚠️ Your subscription is unpaid ({N} days overdue). Dashboard locked. Type /payment now — ticketing will be suspended in {days_left} days.` |
| Enters billing_only | `🚨 Your ticketing system has been suspended due to non-payment. Your dashboard and ticketing groups are now offline. Type /payment to reactivate. Your data is retained for {data_retention_days} days.` |
| Billing-only reminder (every 2 days) | `🚨 Urgent: Your service remains suspended ({N} days overdue). Type /payment now. Data will be retained for {days_remaining} more days.` |
| Close (command or admin) | `👋 Your account has been closed. All services have been stopped. Thank you for using our service.` |

## Billing Service Changes (`billing/main.py`)

### New endpoint: `GET /api/clients/{subdomain}/status`
- Auth: `X-Billing-Secret` header (same as existing statement endpoint)
- Returns: `{"status": "active" | "grace" | "billing_only" | "closed"}`
- Used by backend service to gate ticket processing

### New `/close` command in billing group
- Handled in `_process_client_message` alongside `/payment` and `/statement`
- Sets `status = "closed"`, calls `stop_client(client)`, sends close confirmation message

### New `POST /clients/{client_id}/close` endpoint (admin dashboard)
- Requires login
- Sets `status = "closed"`, calls `stop_client(client)`
- Redirects to client detail page

### `auth_check` gate update
- Now blocks: `grace`, `billing_only`, `closed`
- Removes old references to `warning`, `suspended`

### Payment confirmation reset (existing mpesa callback)
- On success: clear `grace_started_at`, `billing_only_started_at`, `last_warning_sent_at`, `pre_expiry_14_warned`, `pre_expiry_2_warned`; set `status = "active"`; call `start_client()`

## Backend Service Changes (`backend/main.py`)

### Status cache
```python
_billing_status_cache: dict | None = None  # {"status": str, "fetched_at": datetime}
_CACHE_TTL_SECONDS = 60
```

Helper `_get_client_billing_status() -> str` calls `GET /api/clients/{CLIENT_SUBDOMAIN}/status` on `BILLING_SERVICE_URL`. On any failure defaults to `"active"` (fail open — never punish client for billing service downtime).

### Gate in webhook handler
Inserted after the billing forward (so billing group messages are always forwarded) and before the `/` command early-return (so commands are also gated):

```python
# existing — unchanged
if BILLING_SERVICE_URL:
    asyncio.create_task(_forward_to_billing_by_group(group_id, data))

# new gate — sits here, before the "/" command check
billing_status = await _get_client_billing_status()
if billing_status in ("billing_only", "closed"):
    return {"status": "billing_only_drop"}  # silent drop, no reply to users

# existing — unchanged
if message_body.startswith("/"):
    return {"status": "forwarded_to_billing"}

# existing ticket processing continues
```

This ensures:
- All group messages (including billing group) are still forwarded to billing service
- No tickets created in `billing_only` or `closed` state
- No reply sent to users in ticketing groups (silent drop per requirements)
- Commands starting with `/` are also silently dropped

## Dashboard Changes

### Client detail page
- Show `billing_only_started_at` field when in billing_only state
- Editable `data_retention_days` field (shown always)
- "Close Account" button → `POST /clients/{client_id}/close` (with confirmation prompt)
- Status badge updated to handle `billing_only` and `closed` values

## Migration

Add columns via the existing `_migrate_db()` pattern in `billing/main.py`. All additions are safe — existing rows get NULL/default:
- `billing_only_started_at DATETIME`
- `last_warning_sent_at DATETIME` (new column; existing `warning_sent_at` column left in place and ignored — Python model maps to the new column name)
- `data_retention_days INTEGER DEFAULT 90`
- `pre_expiry_14_warned BOOLEAN DEFAULT 0`
- `pre_expiry_2_warned BOOLEAN DEFAULT 0`

## Out of Scope

- Actual data deletion (data_retention_days is informational for now; auto-deletion is a future feature)
- Changing how clients are created (dashboard manual creation unchanged)
- Self-service client registration
