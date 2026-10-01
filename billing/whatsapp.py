import base64
import logging
import os

import httpx
from models import Client

_log = logging.getLogger(__name__)

_OPENWA_URL = os.getenv("OPENWA_URL", "")
_OPENWA_API_KEY = os.getenv("OPENWA_API_KEY", "")
_OPENWA_SESSION = os.getenv("OPENWA_SESSION", "")


async def send_to_group(client: Client, text: str) -> None:
    """Send a plain-text message to a client's superusers WhatsApp group."""
    if not client.openwa_url or not client.whatsapp_group_id:
        _log.warning(
            "send_to_group skipped for %s: openwa_url=%r group_id=%r",
            client.subdomain, client.openwa_url, client.whatsapp_group_id,
        )
        return

    try:
        async with httpx.AsyncClient(timeout=15.0) as http:
            sessions_r = await http.get(
                f"{client.openwa_url}/api/sessions",
                headers={"X-API-Key": client.openwa_api_key or ""},
            )
            sessions_r.raise_for_status()
            session_id = None
            session_status = None
            for s in sessions_r.json():
                if s.get("name") == client.openwa_session:
                    session_id = s["id"]
                    session_status = s.get("status")
                    break
            if not session_id:
                _log.warning(
                    "send_to_group: session %r not found in OpenWA for %s (available: %s)",
                    client.openwa_session, client.subdomain,
                    [s.get("name") for s in sessions_r.json()],
                )
                return
            if session_status and session_status.upper() not in ("WORKING", "CONNECTED", "READY", "AUTHENTICATED"):
                _log.warning(
                    "send_to_group: session %r is not active for %s (status=%s) — reconnect via OpenWA dashboard",
                    client.openwa_session, client.subdomain, session_status,
                )
                return

            send_r = await http.post(
                f"{client.openwa_url}/api/sessions/{session_id}/messages/send-text",
                headers={"X-API-Key": client.openwa_api_key or "", "Content-Type": "application/json"},
                json={"chatId": client.whatsapp_group_id, "text": text},
            )
            _log.warning(
                "send_to_group %s → %s status=%s body=%s",
                client.subdomain, client.whatsapp_group_id, send_r.status_code, send_r.text[:200],
            )
    except Exception as exc:
        _log.warning("send_to_group failed for %s: %s", client.subdomain, exc)


async def send_document_to_group(client: Client, pdf_bytes: bytes, filename: str, caption: str = "") -> None:
    """Send a PDF document to a client's WhatsApp group."""
    if not client.openwa_url or not client.whatsapp_group_id:
        _log.warning("send_document_to_group skipped for %s: missing openwa_url or group_id", client.subdomain)
        return
    try:
        async with httpx.AsyncClient(timeout=30.0) as http:
            sessions_r = await http.get(
                f"{client.openwa_url}/api/sessions",
                headers={"X-API-Key": client.openwa_api_key or ""},
            )
            sessions_r.raise_for_status()
            session_id = None
            for s in sessions_r.json():
                if s.get("name") == client.openwa_session:
                    status = (s.get("status") or "").upper()
                    if status in ("WORKING", "CONNECTED", "READY", "AUTHENTICATED"):
                        session_id = s["id"]
                    break
            if not session_id:
                _log.warning("send_document_to_group: session %r not active for %s", client.openwa_session, client.subdomain)
                return

            data_b64 = base64.b64encode(pdf_bytes).decode()
            r = await http.post(
                f"{client.openwa_url}/api/sessions/{session_id}/messages/send-document",
                headers={"X-API-Key": client.openwa_api_key or "", "Content-Type": "application/json"},
                json={
                    "chatId": client.whatsapp_group_id,
                    "base64": data_b64,
                    "mimetype": "application/pdf",
                    "filename": filename,
                    "caption": caption,
                },
            )
            r.raise_for_status()
    except Exception as exc:
        _log.warning("send_document_to_group failed for %s: %s", client.subdomain, exc)
        raise


async def send_dm_text(phone: str, text: str) -> None:
    """Send a plain-text DM to a phone number using the billing bot's OpenWA credentials."""
    url = _OPENWA_URL
    api_key = _OPENWA_API_KEY
    session_name = _OPENWA_SESSION
    if not url or not session_name:
        _log.warning("send_dm_text: OPENWA_URL or OPENWA_SESSION not configured")
        return
    chat_id = f"{phone}@c.us"
    try:
        async with httpx.AsyncClient(timeout=15.0) as http:
            sessions_r = await http.get(f"{url}/api/sessions", headers={"X-API-Key": api_key})
            sessions_r.raise_for_status()
            session_id = None
            for s in sessions_r.json():
                if s.get("name") == session_name:
                    session_id = s["id"]
                    break
            if not session_id:
                _log.warning("send_dm_text: session %r not found", session_name)
                return
            await http.post(
                f"{url}/api/sessions/{session_id}/messages/send-text",
                headers={"X-API-Key": api_key, "Content-Type": "application/json"},
                json={"chatId": chat_id, "text": text},
            )
    except Exception as exc:
        _log.warning("send_dm_text failed for %s: %s", phone, exc)


# ---------------------------------------------------------------------------
# Session control (admin client page → Restart / Link with new QR)
# Mirrors backend/whatsapp.py's get_session_state / reconnect_session so the
# tenant Settings page and this admin page always agree on state.
# ---------------------------------------------------------------------------

async def _find_session(http: httpx.AsyncClient, client: Client) -> dict | None:
    r = await http.get(f"{client.openwa_url}/api/sessions", headers={"X-API-Key": client.openwa_api_key or ""})
    r.raise_for_status()
    for s in r.json():
        if s.get("name") == client.openwa_session:
            return s
    return None


async def get_session_state(client: Client) -> dict:
    """Current WhatsApp connection state for a client. Never raises.

    Returns {status, phone, qrCode, lastError, lastDisconnectReason, needsRelink}.
    status is OpenWA's status upper-cased (READY, QR_READY, FAILED, ...), or
    NOT_CONFIGURED / NOT_FOUND / UNREACHABLE.
    """
    state = {
        "status": "UNREACHABLE", "phone": None, "qrCode": None,
        "lastError": None, "lastDisconnectReason": None, "needsRelink": False,
    }
    if not client.openwa_url or not client.openwa_session:
        state["status"] = "NOT_CONFIGURED"
        return state
    try:
        async with httpx.AsyncClient(timeout=10.0) as http:
            session = await _find_session(http, client)
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
                qr = await http.get(
                    f"{client.openwa_url}/api/sessions/{session['id']}/qr",
                    headers={"X-API-Key": client.openwa_api_key or ""},
                )
                # Older OpenWA builds answer 400 when no QR is ready yet.
                if qr.status_code == 200:
                    state["qrCode"] = qr.json().get("qrCode")
    except Exception as exc:
        _log.warning("get_session_state failed for %s: %s", client.subdomain, exc)
        state["lastError"] = str(exc)
    return state


async def reconnect_session(client: Client, mode: str) -> dict:
    """Restart ("restart": keep login) or re-link ("relink": wipe login, fresh QR)
    a client's WhatsApp session, creating it first if it doesn't exist yet.

    Returns {"ok": bool, "detail": str | None}. Never raises.
    """
    if mode not in ("restart", "relink"):
        raise ValueError(f"unknown reconnect mode {mode!r}")
    if not client.openwa_url or not client.openwa_session:
        return {"ok": False, "detail": "Set the OpenWA URL and session name first."}
    headers = {"X-API-Key": client.openwa_api_key or ""}
    try:
        async with httpx.AsyncClient(timeout=20.0) as http:
            session = await _find_session(http, client)
            if session is None:
                created = await http.post(
                    f"{client.openwa_url}/api/sessions",
                    headers={**headers, "Content-Type": "application/json"},
                    json={"name": client.openwa_session},
                )
                if created.status_code == 409:
                    session = await _find_session(http, client)
                else:
                    created.raise_for_status()
                    session = created.json()
            base = f"{client.openwa_url}/api/sessions/{session['id']}"

            r = await http.post(f"{base}/{mode}", headers=headers)
            if r.status_code == 404 and mode == "restart":
                # OpenWA predates /restart -- fall back to the old stop + start.
                await http.post(f"{base}/stop", headers=headers)
                r = await http.post(f"{base}/start", headers=headers)
            elif r.status_code == 404:
                return {"ok": False, "detail": "This client's OpenWA predates /relink. Deploy the latest openwa/ first."}
            if r.status_code >= 400:
                return {"ok": False, "detail": f"OpenWA returned {r.status_code}: {r.text[:200]}"}
            return {"ok": True, "detail": None}
    except Exception as exc:
        _log.warning("WhatsApp %s failed for %s: %s", mode, client.subdomain, exc)
        return {"ok": False, "detail": f"Could not reach OpenWA: {exc}"}
