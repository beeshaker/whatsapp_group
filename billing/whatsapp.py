import httpx
from models import Client


async def send_to_group(client: Client, text: str) -> None:
    """Send a plain-text message to a client's superusers WhatsApp group."""
    if not client.openwa_url or not client.whatsapp_group_id:
        return

    async with httpx.AsyncClient(timeout=15.0) as http:
        sessions_r = await http.get(
            f"{client.openwa_url}/api/sessions",
            headers={"X-API-Key": client.openwa_api_key or ""},
        )
        sessions_r.raise_for_status()
        session_id = None
        for s in sessions_r.json():
            if s.get("name") == client.openwa_session:
                session_id = s["id"]
                break
        if not session_id:
            return

        await http.post(
            f"{client.openwa_url}/api/sessions/{session_id}/messages/send-text",
            headers={"X-API-Key": client.openwa_api_key or "", "Content-Type": "application/json"},
            json={"chatId": client.whatsapp_group_id, "text": text},
        )
