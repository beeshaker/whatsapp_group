# Dunhill — Reaction-Triggered Status Updates + Reply-Quoting Fallback

**Date:** 2026-07-21
**Status:** Approved

## Overview

Two related bugs were reported for the Dunhill client (a `LEAD_MODE` deployment, see `docs/superpowers/specs/2026-07-17-dunhill-lead-capture-design.md`):

1. Replying to a lead from the dashboard sends a **plain, unquoted** WhatsApp message instead of a quoted reply to the original enquiry.
2. When an agent reacts to a lead's message in the WhatsApp group (e.g. 👍 to acknowledge it), **nothing happens on the dashboard** — the status never changes.

Root-cause investigation (see conversation history) traced both back to the same underlying gap: WhatsApp's newer `@lid` "linked identity" privacy scheme means senders in the Dunhill group frequently arrive at OpenWA with **no usable WhatsApp message ID** (`data.id` is empty — confirmed empirically during Dunhill onboarding, see commit `bf36a56`). This is a genuine, currently-unresolved limitation in `whatsapp-web.js` (confirmed on the latest pinned version, 1.34.7, via upstream issues #3618 and #5671) — not something fixable by a dependency bump.

- **Issue 1** breaks because `backend/main.py`'s reply endpoint only attempts a quoted reply when `incident.message_id` is a valid, matchable ID; otherwise it silently falls back to a plain send (`main.py:1586-1590`).
- **Issue 2** breaks for two independent reasons: OpenWA never listens for or forwards WhatsApp reaction events at all today, and even quoted text replies (whose `quotedMessage.id` OpenWA *does* capture) are discarded by the backend ingest handler. More fundamentally, there is **no code path anywhere** that changes ticket status except the manual dashboard `PATCH /incidents/{id}` — this was explicitly deferred as "Phase 3: LLM-parsed closure detection from agent WhatsApp replies" in the original lead-capture spec (§9), which left the hook that any future trigger should call that same status-change mechanism.

**Fix strategy:** both features fall back from "exact WhatsApp message ID" to "author + exact timestamp" whenever the ID can't be trusted. Both values (`author`, `timestamp`) are reliably captured by OpenWA even when `id` isn't, and both are already stored on every `Incident` (`reporter_phone`, `received_at`) with zero schema changes needed.

**Confirmed requirements:**
- Both features are gated behind the existing `LEAD_MODE` flag only — hardcoded for Dunhill and future lead-mode clients, not built as a generic configurable system for other clients (no other client has asked for this).
- 👍 reaction → `new → contacted` only. No other emoji is recognized in this phase; Closed (Won/Lost) still requires a deliberate dashboard action.
- The Dunhill WhatsApp group is internal-staff-only, so no sender restriction is needed — any reaction in the group is trusted.
- Reacting to a ticket that's already past `new` (i.e. `contacted` or `closed_won`/`closed_lost`) is a no-op — never overrides a director's dashboard decision.
- No confirmation message is posted back to the group when a reaction changes status — silent, dashboard-only update.
- When no confident quote-match can be found at all, the browser reply still sends (never a hard error), prefixed with a snippet of the original message for context.
- Today's `bf36a56` (`deliveryId` fallback for `message_id`) is unaffected and stays — it solves ticket-deduplication, which is orthogonal to quoting. The hint-based quoting fallback works regardless of what `message_id` ends up being.

---

## 1. Scope & Data Model

No new database columns. Both features reuse fields already captured on every `Incident`:

| Field | Used as |
|---|---|
| `group_id` | WhatsApp chat/group identity |
| `reporter_phone` | Original message author (works for both real phone numbers and `@lid` pseudo-identifiers — matching only needs consistency, not a "real" number) |
| `received_at` | Exact WhatsApp message timestamp (derived from `data.timestamp` at ingest, `main.py:1061` — not server-arrival time) |
| `message_id` | Exact-match key when a real WhatsApp ID is available |

Both features are read behind `LEAD_MODE` the same way `FLEET_PLATE_MODE`/`LEAD_MODE` are read elsewhere — gated off entirely for non-lead clients.

---

## 2. OpenWA: Capturing Reactions

`whatsapp-web-js.adapter.ts` adds a `message_reaction` listener, mirroring the existing `message_ack` listener:

```ts
this.client.on('message_reaction', async (reaction) => {
  const targetAuthor = reaction.msgId.participant || reaction.msgId.remote;
  let targetTimestamp: number | undefined;
  try {
    const msg = await this.client!.getMessageById(reaction.msgId._serialized);
    targetTimestamp = msg.timestamp;
  } catch {
    // getMessageById throws (not null) when the message isn't in the local
    // cache — documented upstream behavior, not @lid-specific. Non-fatal:
    // targetAuthor still lets the backend attempt a fallback match.
  }
  this.callbacks.onMessageReaction?.({
    chatId: reaction.msgId.remote,
    emoji: reaction.reaction,
    senderId: reaction.senderId,
    targetMessageId: reaction.msgId._serialized,
    targetAuthor,
    targetTimestamp,
  });
});
```

`reaction.msgId.participant` (the original message's sender) is available synchronously off the reaction event itself — it does not depend on the message cache, so it survives even when `getMessageById` fails.

`EngineEventCallbacks` (`whatsapp-engine.interface.ts`) gains `onMessageReaction?: (reaction: IncomingReaction) => void`, alongside a new `IncomingReaction` interface:

```ts
export interface IncomingReaction {
  chatId: string;
  emoji: string;
  senderId: string;
  targetMessageId?: string;
  targetAuthor?: string;
  targetTimestamp?: number;
}
```

`session.service.ts` gets a sibling `onMessageReaction` handler next to the existing `onMessage` handler, dispatching a new webhook event `message.reaction` through the same `hookManager.execute(...) → webhookService.dispatch(...)` path already used for `message.received`.

**Rollout note:** each client's webhook subscription is registered once at onboarding with an explicit `events` array (currently `["message.received"]` for every client — confirmed in `docs/onboarding-new-client.md`). Shipping this feature requires a one-time manual step to `PATCH` Dunhill's existing webhook to add `"message.reaction"` to its `events` list. This must be called out as an explicit deploy step (the same way `docs/vps-architecture.md` calls out the billing network-reconnect step), so it isn't silently forgotten.

---

## 3. Backend: Matching a Reaction to a Ticket

New branch in `/api/v1/ops/ingest` (`backend/main.py`), alongside the existing event-type check:

```python
if event_type == "message.reaction":
    return await _handle_reaction(data, db)
```

`_handle_reaction`, a no-op unless `LEAD_MODE` is on:

1. If `data["emoji"]` is not 👍 → `{"status": "ignored"}`.
2. Resolve `group_id` from `data["chatId"]`; apply the existing group-licensing check (`_get_allowed_ticket_groups`).
3. **Match attempt 1 (exact ID):** `Incident.group_id == group_id AND Incident.message_id == data["targetMessageId"]` (only if `targetMessageId` is present).
4. **Match attempt 2 (author + exact timestamp):** if attempt 1 finds nothing and both `targetAuthor` and `targetTimestamp` are present: `Incident.group_id == group_id AND Incident.reporter_phone == targetAuthor.split("@")[0] AND Incident.received_at == from_epoch(targetTimestamp)` — the same `.split("@")[0]` normalization already used to derive `reporter_phone` at ingest (`main.py:1059`), so the two values are directly comparable. This is an exact match, not a fuzzy window — both values derive from the same underlying WhatsApp timestamp field, observed at two different times (ingest vs. reaction lookup).
5. **Match attempt 3 (single-candidate fallback):** if attempts 1–2 find nothing and only `targetAuthor` is present (cache miss on `getMessageById`): look for `Incident`s in that group, from that author, with `status == 'new'`. If exactly one exists, use it. If zero or more than one, give up — log at debug and return `{"status": "ignored"}` rather than guessing.
6. If a matching Incident is found and its `status == 'new'`: transition to `'contacted'`, writing `IncidentStatusHistory(from_status='new', to_status='contacted', changed_by=f"whatsapp:{sender_phone}", changed_at=now)` and `AuditLog(action='auto_status_reaction', incident_id=..., detail=f"👍 reaction from {sender_phone}", created_at=now)` — the same history/audit mechanism the dashboard's own status-change endpoint uses today (the hook point `§9` of the lead-capture spec anticipated). If status is anything other than `'new'`, no-op.
7. No message is ever sent back to the group.

`changed_by` records the reactor's WhatsApp number (not a generic system label) so the ticket's history/audit trail shows who acknowledged it.

---

## 4. OpenWA: Quoting Fallback for Browser Replies

`/messages/reply`'s DTO (`message.controller.ts`, `message.service.ts`) gains three optional fields: `authorHint?: string`, `timestampHint?: number`, `contextSnippet?: string`. `replyToMessage()` in `whatsapp-web-js.adapter.ts` changes from hard match-or-throw to a tiered lookup:

```ts
async replyToMessage(chatId, quotedMsgId, text, authorHint?, timestampHint?, contextSnippet?) {
  this.ensureReady();
  const chat = await this.client!.getChatById(chatId);
  const messages = await chat.fetchMessages({ limit: 100 });

  let quotedMsg = messages.find(m => m.id._serialized === quotedMsgId);

  if (!quotedMsg && authorHint && timestampHint) {
    quotedMsg = messages.find(m => m.author === authorHint && m.timestamp === timestampHint);
  }

  if (!quotedMsg) {
    if (!authorHint && !timestampHint) {
      // No hints supplied at all — caller wants strict quote-or-fail
      // semantics (back-compat with any other consumer of this endpoint).
      throw new Error(`Message ${quotedMsgId} not found`);
    }
    // Hints were supplied but no confident match was found (e.g. the
    // original message aged out of the 100-message window). Send plain,
    // prefixed with the original snippet for context, rather than failing.
    const body = contextSnippet ? `> ${contextSnippet}\n\n${text}` : text;
    const msg = await chat.sendMessage(body);
    return { id: msg.id._serialized, timestamp: msg.timestamp };
  }

  const msg = await quotedMsg.reply(text);
  return { id: msg.id._serialized, timestamp: msg.timestamp };
}
```

**Backward compatibility:** when `authorHint`/`timestampHint` are omitted entirely (any caller besides the ticketing backend), behavior is unchanged from today — exact-ID-or-throw. The graceful plain-send fallback only activates when the caller opts in by supplying hints, keeping this change scoped to the one call site that needs it.

---

## 5. Backend: Reply Integration

`backend/whatsapp.py`'s `reply_to_message()` gains `author_hint`, `timestamp_hint`, and `context_snippet` params, threaded through to the `/messages/reply` payload.

`backend/main.py`'s `reply_to_incident` simplifies — the old `if incident.message_id: ... else: ...` branch is removed. It always calls:

```python
wa_message_id = await reply_to_message(
    incident.group_id,
    incident.message_id or "",
    text,
    author_hint=incident.reporter_phone,
    timestamp_hint=int(_aware(incident.received_at).timestamp()),
    context_snippet=incident.message_body[:200],
)
```

(`_aware(...)` normalizes SQLite's tzinfo-dropping on read-back to UTC, matching the existing pattern in `_check_ticket_reminders`.) Since hints are now always supplied, this call never throws — the try/except around it in `reply_to_incident` can be simplified accordingly, though it's kept as a safety net for genuine network/HTTP failures (OpenWA unreachable, etc.), which still should surface as a 502.

---

## 6. Testing

- **OpenWA (Jest):** `replyToMessage` — exact-ID match (unchanged), author+timestamp fallback match, no-match-at-all → plain send with snippet prefix, no-hints-provided → still throws (back-compat check). New `message_reaction` handling — cache-hit path (timestamp resolved), cache-miss path (`getMessageById` throws, falls back to author-only), dispatch payload shape.
- **Backend (pytest):** extend `test_reply.py` for the hint-passing change; new `test_reactions.py` covering the 3-tier match, the `new`-only guard (no-op on `contacted`/`closed_*`), the ambiguous-multiple-candidates no-op, and non-`LEAD_MODE` clients ignoring `message.reaction` entirely.

---

## 7. Rollout

1. Deploy OpenWA changes first — purely additive (new optional DTO fields, new event type nothing subscribes to yet), safe for every client.
2. Deploy backend changes.
3. One-time manual step: `PATCH` Dunhill's webhook to add `"message.reaction"` to its `events` list. Document this explicitly so it isn't forgotten (mirrors the billing network-reconnect gotcha in `docs/vps-architecture.md`).
4. Fold into the existing batch-deploy cadence rather than shipping standalone.

---

## 8. Out of Scope (not built now)

- Other emoji → status mappings (e.g. ✅ = Closed Won, ❌ = Closed Lost) — only 👍 → Contacted is built. Revisit if Dunhill asks for more.
- A generic, per-client-configurable emoji→status system — hardcoded to `LEAD_MODE` for now, per YAGNI.
- Text-reply keyword parsing / LLM-parsed closure detection (the original spec's "Phase 3") — still deferred; reactions are a separate, simpler mechanism addressing the same underlying need.
- Restricting which senders can trigger a reaction-based status change — not needed since Dunhill's group is internal-staff-only; would need revisiting if a future lead-mode client's group includes outsiders.
