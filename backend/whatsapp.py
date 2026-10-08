import logging
import os

import httpx

logger = logging.getLogger(__name__)

OPENWA_URL = os.getenv("OPENWA_URL", "http://openwa:2785")
OPENWA_SESSION = os.getenv("OPENWA_SESSION", "opsgateway")
OPENWA_API_KEY = os.getenv("OPENWA_API_KEY", "")

_session_uuid: str | None = None


async def _resolve_session_uuid(client: httpx.AsyncClient) -> str:
    global _session_uuid
    if _session_uuid:
        return _session_uuid
    r = await client.get(
        f"{OPENWA_URL}/api/sessions",
        headers={"X-API-Key": OPENWA_API_KEY},
    )
    r.raise_for_status()
    for s in r.json():
        if s.get("name") == OPENWA_SESSION:
            _session_uuid = s["id"]
            return _session_uuid
    raise ValueError(f"OpenWA session {OPENWA_SESSION!r} not found")


async def _post_message(path: str, payload: dict) -> str:
    """POST to an OpenWA messages endpoint. Retries once if the session UUID has changed."""
    global _session_uuid
    async with httpx.AsyncClient(timeout=15.0) as client:
        session_id = await _resolve_session_uuid(client)
        response = await client.post(
            f"{OPENWA_URL}/api/sessions/{session_id}/{path}",
            headers={"X-API-Key": OPENWA_API_KEY, "Content-Type": "application/json"},
            json=payload,
        )
        if response.status_code in (400, 404):
            _session_uuid = None
            session_id = await _resolve_session_uuid(client)
            response = await client.post(
                f"{OPENWA_URL}/api/sessions/{session_id}/{path}",
                headers={"X-API-Key": OPENWA_API_KEY, "Content-Type": "application/json"},
                json=payload,
            )
        response.raise_for_status()
        return response.json()["messageId"]


async def list_groups() -> list[dict] | None:
    """Fetch the live list of WhatsApp groups the bot currently belongs to.

    Returns [{id, name}, ...] on success, or None (never raises) if the
    session can't be resolved or OpenWA is unreachable.
    """
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            session_id = await _resolve_session_uuid(client)
            response = await client.get(
                f"{OPENWA_URL}/api/sessions/{session_id}/groups",
                headers={"X-API-Key": OPENWA_API_KEY},
            )
            response.raise_for_status()
            return response.json()
    except Exception as exc:
        logger.warning("Failed to fetch WhatsApp groups: %s", exc)
        return None


async def list_contacts() -> list[dict] | None:
    """Contacts the WhatsApp session knows: [{id, number, name, pushName}, ...].

    Returns None (never raises) if the session can't be resolved or OpenWA
    is unreachable.
    """
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            session_id = await _resolve_session_uuid(client)
            response = await client.get(
                f"{OPENWA_URL}/api/sessions/{session_id}/contacts",
                headers={"X-API-Key": OPENWA_API_KEY},
            )
            response.raise_for_status()
            return response.json()
    except Exception as exc:
        logger.warning("Failed to fetch WhatsApp contacts: %s", exc)
        return None


async def send_group_message(chat_id: str, text: str) -> str:
    """Send a plain text message to a WhatsApp group."""
    return await _post_message("messages/send-text", {"chatId": chat_id, "text": text})


async def reply_to_message(
    chat_id: str,
    quoted_message_id: str,
    text: str,
    author_hint: str | None = None,
    timestamp_hint: int | None = None,
    context_snippet: str | None = None,
) -> str:
    """Send a quoted reply to a specific WhatsApp message, falling back to
    author+timestamp hints when the quoted message's WhatsApp ID isn't trustworthy."""
    payload = {"chatId": chat_id, "quotedMessageId": quoted_message_id, "text": text}
    if author_hint is not None:
        payload["authorHint"] = author_hint
    if timestamp_hint is not None:
        payload["timestampHint"] = timestamp_hint
    if context_snippet is not None:
        payload["contextSnippet"] = context_snippet
    return await _post_message("messages/reply", payload)


# ---------------------------------------------------------------------------
# Session control (Settings → WhatsApp Connection)
# ---------------------------------------------------------------------------

def _headers() -> dict:
    return {"X-API-Key": OPENWA_API_KEY}


async def _find_session(client: httpx.AsyncClient) -> dict | None:
    r = await client.get(f"{OPENWA_URL}/api/sessions", headers=_headers())
    r.raise_for_status()
    for s in r.json():
        if s.get("name") == OPENWA_SESSION:
            return s
    return None


async def get_session_state() -> dict:
    """Current connection state for the Settings page. Never raises.

    Returns {status, phone, qrCode, lastError, lastDisconnectReason, needsRelink}.
    status is OpenWA's session status upper-cased (READY, QR_READY, FAILED, ...),
    or NOT_FOUND / UNREACHABLE when the session or gateway can't be reached.
    """
    state = {
        "status": "UNREACHABLE", "phone": None, "qrCode": None,
        "lastError": None, "lastDisconnectReason": None, "needsRelink": False,
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            session = await _find_session(client)
            if session is None:
                state["status"] = "NOT_FOUND"
                return state
            state.update(
                status=(session.get("status") or "UNKNOWN").upper(),
                phone=session.get("phone"),
                lastError=session.get("lastError"),
                lastDisconnectReason=session.get("lastDisconnectReason"),
                needsRelink=bool(session.get("needsRelink")),
            )
            if state["status"] == "QR_READY":
                qr = await client.get(f"{OPENWA_URL}/api/sessions/{session['id']}/qr", headers=_headers())
                # Older OpenWA builds answer 400 when no QR is ready yet.
                if qr.status_code == 200:
                    state["qrCode"] = qr.json().get("qrCode")
    except Exception as exc:
        logger.warning("Failed to read WhatsApp session state: %s", exc)
        state["lastError"] = str(exc)
    return state


async def reconnect_session(mode: str) -> dict:
    """Restart ("restart": keep login) or re-link ("relink": wipe login, fresh QR)
    the WhatsApp session, creating it first if it doesn't exist yet.

    Returns {"ok": bool, "detail": str | None}. Never raises.
    """
    if mode not in ("restart", "relink"):
        raise ValueError(f"unknown reconnect mode {mode!r}")
    global _session_uuid
    _session_uuid = None  # the session may be recreated below
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            session = await _find_session(client)
            if session is None:
                created = await client.post(
                    f"{OPENWA_URL}/api/sessions",
                    headers={**_headers(), "Content-Type": "application/json"},
                    json={"name": OPENWA_SESSION},
                )
                if created.status_code == 409:
                    session = await _find_session(client)
                else:
                    created.raise_for_status()
                    session = created.json()
            base = f"{OPENWA_URL}/api/sessions/{session['id']}"

            r = await client.post(f"{base}/{mode}", headers=_headers())
            if r.status_code == 404 and mode == "restart":
                # OpenWA predates /restart -- fall back to the old stop + start.
                await client.post(f"{base}/stop", headers=_headers())
                r = await client.post(f"{base}/start", headers=_headers())
            elif r.status_code == 404:
                return {"ok": False, "detail": "This WhatsApp gateway is out of date and can't re-link yet. Contact support."}
            if r.status_code >= 400:
                return {"ok": False, "detail": f"Gateway returned {r.status_code}: {r.text[:200]}"}
            return {"ok": True, "detail": None}
    except Exception as exc:
        logger.warning("WhatsApp %s failed: %s", mode, exc)
        return {"ok": False, "detail": f"Could not reach the WhatsApp gateway: {exc}"}
