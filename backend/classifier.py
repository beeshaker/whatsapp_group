import json
import logging
import os
from typing import Optional

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

from lead_fields import extract_agent_tag, extract_contact_name, extract_source_tag, is_valid_phone, normalize_phone, resolve_phone_for_issue

LEAD_MODE = os.getenv("LEAD_MODE", "false").lower() == "true"
LEAD_DEFAULT_PRIORITY = "low"
_VALID_TRANSACTION_TYPES = {"sale", "rent", "unknown"}

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


def _clean_optional_str(value) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _build_lead_prompt(message: str, categories: list[str]) -> str:
    safe_message = json.dumps(message)
    pipe_cats = "|".join(categories)
    return (
        f"{CLASSIFIER_CONTEXT}\n"
        "A message may describe ONE or MULTIPLE distinct property enquiries, each usually "
        "tagging a different agent with @~Name.\n"
        "If an enquiry is tagged with an @~Name agent mention, include that exact @~Name tag "
        "verbatim inside that enquiry's own message_snippet.\n"
        "An ENQUIRY is a request from a prospective client to view, rent, or buy a property.\n"
        "NOT an enquiry: general chat, greetings, scheduling discussions, acknowledgements with "
        "no new property request.\n\n"
        "Return ONLY valid JSON, no explanation, no markdown — a JSON array, one entry per "
        "distinct enquiry (empty array if there is no enquiry):\n"
        "[\n"
        "  {\n"
        '    "message_snippet": "<the relevant portion of the message for this enquiry>",\n'
        f'    "category": "{pipe_cats}",\n'
        "    \"contact_name\": \"<the prospective client's name, or empty string if not stated>\",\n"
        '    "lead_location": "<desired area, or empty string if not stated>",\n'
        '    "lead_budget": "<budget exactly as written, or empty string if not stated>",\n'
        '    "transaction_type": "sale|rent|unknown",\n'
        '    "confidence": 0.0 to 1.0\n'
        "  }\n"
        "]\n\n"
        f"Message: {safe_message}"
    )


async def classify_lead_message(message: str, db: AsyncSession) -> dict:
    try:
        from models import IncidentCategory
        result = await db.execute(select(IncidentCategory))
        slugs = [row.slug for row in result.scalars().all()]
        if not slugs:
            slugs = ["other"]
        valid_set = set(slugs)
        prompt = _build_lead_prompt(message, slugs)
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
                raw_confidence = float(item.get("confidence") or 0.0)
                raw_txn = str(item.get("transaction_type", "unknown")).lower()
                snippet = str(item.get("message_snippet", message))

                phone = resolve_phone_for_issue(snippet, message)
                if phone is None:
                    llm_phone = str(item.get("contact_phone", "")).strip()
                    if llm_phone and is_valid_phone(llm_phone):
                        phone = normalize_phone(llm_phone)

                # Multi-issue messages: only trust a name found in `snippet` when it's
                # genuinely this issue's own text (snippet != message). If the LLM omitted
                # or echoed the full message as the snippet, searching it here — or falling
                # back to the full message below — would leak another issue's name into
                # this one. Simplifying this back to an unconditional search reopens that bug.
                name = None
                if len(parsed) == 1 or snippet != message:
                    name = extract_contact_name(snippet)
                if name is None and len(parsed) == 1:
                    name = extract_contact_name(message)
                if name is None:
                    name = _clean_optional_str(item.get("contact_name"))

                issues.append({
                    "category": raw_category if raw_category in valid_set else "other",
                    "priority": LEAD_DEFAULT_PRIORITY,
                    "confidence": max(0.0, min(1.0, raw_confidence)),
                    "message_snippet": snippet,
                    "contact_name": name,
                    "lead_location": _clean_optional_str(item.get("lead_location")),
                    "lead_budget": _clean_optional_str(item.get("lead_budget")),
                    "transaction_type": raw_txn if raw_txn in _VALID_TRANSACTION_TYPES else "unknown",
                    "contact_phone": phone,
                    "lead_agent": extract_agent_tag(snippet) or extract_agent_tag(message),
                    "lead_source": extract_source_tag(message),
                })

            if len(issues) > _MAX_ISSUES:
                kept = sorted(issues, key=lambda i: i["confidence"], reverse=True)[:_MAX_ISSUES]
                kept_ids = {id(i) for i in kept}
                issues = [i for i in issues if id(i) in kept_ids]

            return {"issues": issues}
    except Exception as exc:
        logger.error("Ollama lead classification failed: %s", exc)
        return _FALLBACK.copy()


async def classify_message(message: str, db: AsyncSession) -> dict:
    if LEAD_MODE:
        return await classify_lead_message(message, db)
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
