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
