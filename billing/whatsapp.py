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
            _log.warning("send_document_to_group %s status=%s body=%s", client.subdomain, r.status_code, r.text[:200])
    except Exception as exc:
        _log.warning("send_document_to_group failed for %s: %s", client.subdomain, exc)


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
