import logging
import os

import httpx

logger = logging.getLogger(__name__)

OPENWA_URL = os.getenv("OPENWA_URL", "http://openwa:2785")
OPENWA_SESSION = os.getenv("OPENWA_SESSION", "opsgateway")
OPENWA_API_KEY = os.getenv("OPENWA_API_KEY", "")


async def send_group_message(chat_id: str, text: str) -> str:
    """Send text to a WhatsApp group. Returns the WhatsApp message ID."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(
            f"{OPENWA_URL}/api/sessions/{OPENWA_SESSION}/messages/text",
            headers={"X-API-Key": OPENWA_API_KEY, "Content-Type": "application/json"},
            json={"chatId": chat_id, "text": text},
        )
        response.raise_for_status()
        return response.json()["messageId"]
