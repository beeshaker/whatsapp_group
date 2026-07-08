# Billing Admin Activity Log

**Date:** 2026-07-08
**Status:** Approved

## Overview

Triggered by an incident investigation: a client's billing "WhatsApp Group ID" was misconfigured to point at one of their own ticket/support groups instead of a dedicated billing group. A payment reminder was pushed 3× (no confirmation feedback on the button led to repeat clicks) into that support group, and the client's group admin removed the bot in response — breaking ticket intake for that group too.

Reconstructing *what happened and when* took manual SSH/DB forensics because nothing in the billing admin records who changed a client's config, who clicked an action button, or when the scheduler fired an automated message. This spec adds a persistent activity log so that kind of investigation is a lookup, not an excavation.

Scope is the billing service only (`billing/`) — not the per-client ticketing backends, which are a separate concern.

---

## 1. Data Model (`billing/models.py`)

New table `ActivityLog`:
```python
class ActivityLog(Base):
    __tablename__ = "activity_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    client_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("clients.id"), nullable=True, index=True)
    actor: Mapped[str] = mapped_column(Text, nullable=False)       # admin username, or "system"
    action: Mapped[str] = mapped_column(String(40), nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
```
`client_id = NULL` marks a platform-wide (not client-scoped) event — currently only the two global price-setting actions. No new migration logic needed: this is a brand-new table, picked up automatically by the existing `Base.metadata.create_all` in `init_db()`.

---

## 2. Write Path (`billing/activity_log.py`, new file)

One helper, used everywhere a log entry is needed:
```python
async def record_activity(db, client_id: int | None, actor: str, action: str, detail: str = "") -> None:
    db.add(ActivityLog(
        client_id=client_id, actor=actor, action=action, detail=detail,
        created_at=datetime.now(timezone.utc),
    ))
```
It only calls `db.add(...)` — never `db.commit()`. Every call site already ends in a `db.commit()` for the change it's describing; the log row rides along in that same transaction, so a log entry and the event it describes are never out of sync.

---

## 3. Instrumented Call Sites (`billing/main.py`)

**Per-client, human-triggered** (`actor=username` from `require_login`):

| Handler | `action` | `detail` |
|---|---|---|
| `update_client` | `"update_client"` | Diff of changed tracked fields only, e.g. `"renewal_date: 2026-07-25 → 2026-07-27; whatsapp_group_id: (unset) → 120363...@g.us"`. Built by snapshotting tracked fields before mutation and comparing after. `openwa_api_key` is redacted to `"openwa_api_key: changed"` — never logs the credential value. If nothing changed, no entry is written. |
| `push_reminder` | `"push_reminder"` | `"Sent to {client.whatsapp_group_id}"` |
| `send_invite` | `"send_invite"` | `"Invite sent to {digits}"` |
| `manual_reactivate` | `"reactivate"` | `""` |
| `close_client` | `"close"` | `""` |
| `admin_add_ticket_group` | `"ticket_group_add"` | `"{group_id}"` |
| `admin_remove_ticket_group` | `"ticket_group_remove"` | `"{group_id}"` |
| `admin_reset_ticket_groups_unrestricted` | `"ticket_group_reset_unrestricted"` | `""` |

**Per-client, system-triggered** (`actor="system"`), all in `billing/scheduler.py::_check_client_status`:

| Branch | `action` |
|---|---|
| active → grace (expired) | `"scheduler_grace_start"` |
| 14-day pre-expiry warning | `"scheduler_pre_expiry_14"` |
| 2-day pre-expiry warning | `"scheduler_pre_expiry_2"` |
| grace 48h recurring warning | `"scheduler_grace_warning"` |
| grace → billing_only | `"scheduler_billing_only_start"` |
| billing_only 48h recurring warning | `"scheduler_billing_only_warning"` |

Each `detail` reuses the same human-readable summary already computed for the WhatsApp message in that branch (e.g. days overdue / days left), so the log doesn't need its own copy of that math.

**Global** (`client_id=None`, `actor=username`):

| Handler | `action` | `detail` |
|---|---|---|
| `set_prices` | `"set_plan_prices"` | `"monthly: 1500.00 → 1800.00; annual: ..."` (changed values only) |
| `set_group_tier_prices` | `"set_group_tier_prices"` | `"tier 1-5: 0 → 500; ..."` (changed values only) |

Both use the same before/after diff approach as `update_client`. If a save leaves every value unchanged, nothing is logged.

**Explicitly not instrumented:** payment/M-Pesa events (already visible via the existing `Payment` rows shown on the client detail page) and anything inside per-client ticketing backends.

---

## 4. UI

**`client_detail.html`** — new "Activity" card below the existing Actions card: a table of Time / Actor / Action / Detail, most recent first, capped to the last 50 rows for this client (`ActivityLog.client_id == client.id`, no pagination in this pass).

**`prices.html`** — new "Price change history" table in the same style, showing global rows (`ActivityLog.client_id IS NULL`), capped at 50, most recent first.

Both are read-only, server-rendered on page load — no polling/websocket needed since these are low-frequency admin actions.

---

## 5. Testing

Following the existing `billing/tests/test_clients.py` pattern (`auth_http` fixture, in-memory SQLite):
- One test per instrumented handler asserting a matching `ActivityLog` row appears with the expected `client_id`/`actor`/`action` after the call.
- `update_client` diff: assert unchanged fields produce no entry, and a multi-field change produces one entry listing only the changed fields.
- `openwa_api_key` redaction: assert the raw key value never appears in `ActivityLog.detail`.
- Scheduler: extend `billing/tests/test_scheduler_logic.py` to assert an `ActivityLog` row (`actor="system"`) is written alongside each existing `send_to_group` assertion.
- UI: assert the rendered `client_detail.html`/`prices.html` contain the expected log rows after triggering an action.
