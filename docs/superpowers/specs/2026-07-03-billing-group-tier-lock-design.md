# Super-Admin Billing: Group Selection & Tiered Pricing

**Date:** 2026-07-03
**Status:** Approved

## Overview

This is Group D of a 6-item ticket request (Groups A, B, C already specced/A shipped). Today, any WhatsApp group that OpenWA forwards messages from can raise tickets in a client's ticketing backend — there is no restriction and no billing tie-in to *which* groups, or *how many*. This spec introduces a per-client "allowed ticket-raising groups" list, capped by a paid tier (more groups = higher tier), managed exclusively by the billing service's admin (a completely separate, separately-authenticated system the client never logs into) — with one carve-out: the client can self-service *add* a group (paying to upgrade if it exceeds their current tier), but can never remove or swap an existing one.

Groups B (multi-ticket splitting) and C (reminder timers) are unrelated and specced separately.

**Important distinction:** this is unrelated to `Client.whatsapp_group_id` / `SUPERUSERS_GROUP_ID` — that's the single billing/payment-command group (where `/payment` is typed), untouched by this spec. "Ticket-raising groups" is a new, separate, plural concept — the WhatsApp groups (e.g. one per building/block) whose messages become maintenance tickets.

---

## 1. Data Model (billing service, `billing/models.py`)

New table `GroupTierPrice` (mirrors the existing `PlanPrice` shape):
```python
class GroupTierPrice(Base):
    __tablename__ = "group_tier_prices"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    min_groups: Mapped[int] = mapped_column(Integer, nullable=False)
    max_groups: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # None = no upper bound
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(5), nullable=False, default="KES")
    set_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    set_by: Mapped[str] = mapped_column(Text, nullable=False)
```
Seeded with three tiers: `(1, 5)`, `(6, 10)`, `(11, None)` — non-overlapping, so every group count maps to exactly one tier (the "1-5, 5-10, 10+" boundaries from discussion are closed here to avoid double-counting at 5 and 10).

`Client` gains two nullable columns:
- `allowed_ticket_groups: Mapped[Optional[str]]` (`Text`, nullable) — a JSON-encoded array of group_id strings. `None` = unrestricted (today's behavior).
- `ticket_group_tier_id: Mapped[Optional[int]]` (`Integer`, `ForeignKey("group_tier_prices.id")`, nullable) — which tier the client is currently paying for. `None` alongside `allowed_ticket_groups = None` means "not opted into this feature at all."

Both default to `None` on the existing `Client.__init__` pattern, so **existing clients are entirely unaffected** — this feature only applies once a client has an `allowed_ticket_groups` list configured (opt-in only, per the rollout decision below).

---

## 2. Billing-Admin Management (`billing/main.py`, `client_detail.html`)

- `client_detail.html` gains a "Ticket-raising groups" section listing current groups with individual remove buttons, plus an "add group" field — all backed by an extended `update_client` handler (or a new dedicated `POST /clients/{client_id}/ticket-groups` endpoint) that can freely add, remove, or swap any group, with no restriction. This is the **only** place removal or swapping is possible — the client-facing UI (§4) never gets a remove control.
- A new admin page (or a section added to the existing `/prices` page) to view/edit the three `GroupTierPrice` rows, reusing the existing `set_prices` pattern (parse amounts, upsert by identifying tier, stamp `set_at`/`set_by`).
- Since billing has no role model at all (a single flat `require_login`), anyone who can reach these pages is, by definition, "the billing admin" — no new permission logic needed.

---

## 3. Cross-Service Enforcement (`billing/main.py` ↔ `backend/main.py`)

New billing endpoint, same auth pattern as the existing `/api/clients/{subdomain}/status`:
```python
@app.get("/api/clients/{subdomain}/ticket-groups")
async def client_ticket_groups(subdomain: str, request: Request, db=Depends(get_db)):
    # same X-Billing-Secret header check as client_billing_status
    ...
    return {
        "allowed_groups": json.loads(client.allowed_ticket_groups) if client.allowed_ticket_groups else None,
        "tier_limit": tier.max_groups if (tier := ...) else None,
    }
```

Backend gets a new cached fetch function, `_get_allowed_ticket_groups()`, structurally identical to the existing `_get_client_billing_status()`: same 60-second TTL cache, same fail-open behavior (`BILLING_SERVICE_URL`/`CLIENT_SUBDOMAIN` not configured, or the HTTP call fails → returns `None`/unrestricted, logged) — consistent with the existing fail-open default for the billing-status check.

`ingest()` gains one check near the top, after the existing group/message-type filtering: if `allowed_groups is not None and group_id not in allowed_groups`, respond `{"status": "group_not_licensed"}` and create nothing (no `Incident`, no `IncidentUpdate`) — a new status value alongside today's `"noise"`/`"ignored"`, easy to distinguish in logs from an AI-classified non-incident.

---

## 4. Client Self-Service (`backend/main.py`, `/settings`)

- `/settings` gains a "Ticket-raising groups" section: the current list (read-only — no remove/edit control exists here at all) plus an "Add group" form (paste a group ID/JID, matching today's manual support-group-registration pattern from onboarding).
- On submit, the backend calls a new billing endpoint, `POST /api/clients/{subdomain}/ticket-groups/add` (body: `{group_id}`, same `X-Billing-Secret` auth):
  - **Under the current tier's limit:** billing adds the group to `allowed_ticket_groups` immediately and returns success; the backend's next cache refresh (≤60s) picks it up.
  - **Would exceed the limit:** billing does **not** add the group. It returns `{"status": "limit_reached", "next_tier_amount": ..., "next_tier_max": ...}`. The `/settings` page shows "You've reached your plan's limit (N groups) — upgrade to add more" with an upgrade button.
  - The client triggers the upgrade via the existing `initiate_stk_push(phone, amount, account_ref, callback_url)` M-Pesa mechanism (same flow already used for renewals). The group is **not added yet** at this point.
  - Only when the M-Pesa callback (`/webhook/mpesa`, existing handler) confirms payment does billing bump `ticket_group_tier_id` to the new tier **and** add the originally-requested group to `allowed_ticket_groups` in the same transaction.
  - If the client never completes payment, nothing changes — no group was added, so there's nothing to roll back and no grace-period logic is needed for this flow (distinct from the existing renewal grace/billing_only state machine, which this spec does not touch or reuse).
- Duplicate-add (group already in the list) is a no-op, not an error.

---

## 5. Rollout

Per the opt-in-only decision: this feature applies **only** to clients where `allowed_ticket_groups` has been explicitly set (new clients onboarded after this ships, or any existing client the billing admin manually opts in via §2). No migration/backfill of existing clients' currently-active groups — they keep today's fully unrestricted behavior indefinitely unless a billing admin opts them in.

---

## 6. Error Handling

| Scenario | Behavior |
|---|---|
| Billing service unreachable when checking allowed groups | Fail open — treat as unrestricted (`None`), logged, matches existing `_get_client_billing_status()` fail-open pattern |
| Client adds a group already in their list | No-op, no error |
| Client adds a group while under an existing pending (unpaid) upgrade | Show the pending upgrade's status instead of starting a second STK push |
| M-Pesa payment for a tier upgrade fails or times out | Group is not added; client can retry the upgrade, same as a failed renewal payment today |
| Billing admin removes a group that has existing tickets | Historical tickets/dashboard data for that group are untouched — only future incoming messages from it are blocked |
| Malformed group ID submitted (doesn't look like a WhatsApp JID) | Rejected client-side/server-side with a validation message, no billing call made |

---

## 7. Testing

- **Billing model/migration tests**: `GroupTierPrice` seeded correctly with non-overlapping tiers; `Client.allowed_ticket_groups`/`ticket_group_tier_id` default to `None`.
- **Billing endpoint tests**: `GET .../ticket-groups` returns correct shape for an unrestricted vs. a configured client; `POST .../ticket-groups/add` adds immediately when under limit, returns `limit_reached` when not (without adding), rejects duplicate group with a no-op, and — on a mocked M-Pesa payment confirmation — adds the group and bumps the tier atomically.
- **Backend tests**: `_get_allowed_ticket_groups()` cache and fail-open behavior (mirroring existing billing-status tests); `ingest()` allows a message through unchanged when unrestricted (regression test — existing clients must see zero behavior change), blocks and returns `group_not_licensed` for a non-allowed group, allows an explicitly-allowed group through normally.
- **Manual verification**: billing admin adds/removes a group in `client_detail.html`; `/settings` reflects the current list within a cache cycle; client attempts to add a group beyond their limit, sees the upgrade prompt, completes a (sandboxed) M-Pesa payment, and the group appears afterward.

---

## 8. Out of Scope

- Auto-grandfathering existing clients' currently-active groups (explicitly declined — new clients / manually opted-in clients only).
- Removing a group's historical ticket data from the dashboard.
- Any change to `Client.whatsapp_group_id` / `SUPERUSERS_GROUP_ID` (the separate billing/payment-command group concept).
- Client self-service downgrade or removal — billing-admin-only, by design, forever.
- Proration or refunds when a tier changes.
- Reusing or extending the existing renewal grace/billing_only state machine for unpaid tier upgrades (deliberately a separate, simpler "not added until paid" flow instead).
