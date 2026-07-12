import json
import logging
import os

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2")
CLASSIFIER_CONTEXT = os.getenv(
    "CLASSIFIER_CONTEXT",
    "You are classifying WhatsApp messages from a property management company.\n"
    "Properties include residential blocks, lifts, water systems, electrical infrastructure.",
)
try:
    OLLAMA_TIMEOUT = float(os.getenv("OLLAMA_TIMEOUT", "10"))
except ValueError:
    logger.warning("Invalid OLLAMA_TIMEOUT env var, using default of 10 seconds")
    OLLAMA_TIMEOUT = 10.0

_FALLBACK: dict = {"issues": []}

_VALID_PRIORITIES = {"low", "medium", "high", "urgent"}
_MAX_ISSUES = 5


def _build_prompt(message: str, categories: list[str]) -> str:
    safe_message = json.dumps(message)
    pipe_cats = "|".join(categories)
    return (
        f"{CLASSIFIER_CONTEXT}\n"
        "A message may describe ONE or MULTIPLE distinct, actionable operational problems.\n"
        "An ISSUE is a concrete, actionable operational problem requiring maintenance or emergency response.\n"
        "NOT an issue: general chat, greetings, scheduling discussions, complaints without a specific fault.\n\n"
        "Return ONLY valid JSON, no explanation, no markdown — a JSON array, one entry per distinct issue "
        "(empty array if there are no issues):\n"
        "[\n"
        "  {\n"
        '    "message_snippet": "<the relevant portion of the message for this issue>",\n'
        f'    "category": "{pipe_cats}",\n'
        '    "priority": "low|medium|high|urgent",\n'
        '    "confidence": 0.0 to 1.0\n'
        "  }\n"
        "]\n\n"
        f"Message: {safe_message}"
    )


async def classify_message(message: str, db: AsyncSession) -> dict:
    try:
        from models import IncidentCategory
        result = await db.execute(select(IncidentCategory))
        slugs = [row.slug for row in result.scalars().all()]
        if not slugs:
            slugs = ["other"]
        valid_set = set(slugs)
        prompt = _build_prompt(message, slugs)
        async with httpx.AsyncClient(timeout=OLLAMA_TIMEOUT) as client:
            response = await client.post(
                f"{OLLAMA_HOST}/api/generate",
                json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
            )
            response.raise_for_status()
            raw = response.json().get("response", "")
            parsed = json.loads(raw)
            if not isinstance(parsed, list):
                return _FALLBACK.copy()

            issues = []
            for item in parsed:
                if not isinstance(item, dict):
                    continue
                raw_category = str(item.get("category", "other")).lower()
                raw_priority = str(item.get("priority", "medium")).lower()
                raw_confidence = float(item.get("confidence", 0.0))
                issues.append({
                    "category": raw_category if raw_category in valid_set else "other",
                    "priority": raw_priority if raw_priority in _VALID_PRIORITIES else "medium",
                    "confidence": max(0.0, min(1.0, raw_confidence)),
                    "message_snippet": str(item.get("message_snippet", message)),
                })

            if len(issues) > _MAX_ISSUES:
                kept = sorted(issues, key=lambda i: i["confidence"], reverse=True)[:_MAX_ISSUES]
                kept_ids = {id(i) for i in kept}
                issues = [i for i in issues if id(i) in kept_ids]

            return {"issues": issues}
    except Exception as exc:
        logger.error("Ollama classification failed: %s", exc)
        return _FALLBACK.copy()


def _build_routing_prompt(message: str, open_tickets: list[dict]) -> str:
    ticket_lines = "\n".join(
        f"- ticket_id={t['id']}: [{t['category']}] {t['message_body'][:200]}"
        for t in open_tickets
    )
    safe_message = json.dumps(message)
    return (
        "You are deciding whether a WhatsApp message is an update to an existing open ticket "
        "or a brand new issue.\n\n"
        "Open tickets in this group:\n"
        f"{ticket_lines}\n\n"
        "Return ONLY valid JSON, no explanation, no markdown:\n"
        '{"routing": "new"}\n'
        "or\n"
        '{"routing": "update", "ticket_id": <integer id from the list above>}\n\n'
        f"New message: {safe_message}"
    )


async def classify_update_or_new(message: str, open_tickets: list[dict]) -> dict:
    if not open_tickets:
        return {"routing": "new"}
    prompt = _build_routing_prompt(message, open_tickets)
    try:
        async with httpx.AsyncClient(timeout=OLLAMA_TIMEOUT) as client:
            response = await client.post(
                f"{OLLAMA_HOST}/api/generate",
                json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
            )
            response.raise_for_status()
            raw = response.json().get("response", "")
            parsed = json.loads(raw)
            routing = str(parsed.get("routing", "new")).lower()
            if routing == "update":
                ticket_id = int(parsed["ticket_id"])
                valid_ids = {t["id"] for t in open_tickets}
                if ticket_id in valid_ids:
                    return {"routing": "update", "ticket_id": ticket_id}
            return {"routing": "new"}
    except Exception as exc:
        logger.error("classify_update_or_new failed: %s", exc)
        return {"routing": "new"}
