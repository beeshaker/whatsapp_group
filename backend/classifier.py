import json
import logging
import os

import httpx

logger = logging.getLogger(__name__)

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2")
try:
    OLLAMA_TIMEOUT = float(os.getenv("OLLAMA_TIMEOUT", "10"))
except ValueError:
    logger.warning("Invalid OLLAMA_TIMEOUT env var, using default of 10 seconds")
    OLLAMA_TIMEOUT = 10.0

_FALLBACK: dict = {
    "is_incident": False,
    "category": "other",
    "severity": "low",
    "confidence": 0.0,
}

_VALID_CATEGORIES = {"plumbing", "electrical", "lift", "security", "structural", "cleaning", "access", "other"}
_VALID_SEVERITIES = {"low", "medium", "high"}


def _build_prompt(message: str) -> str:
    safe_message = json.dumps(message)  # produces "..." with all control chars escaped
    return (
        "Classify this WhatsApp message from a property management group.\n"
        "Return ONLY valid JSON, no explanation:\n"
        "{\n"
        '  "is_incident": true or false,\n'
        '  "category": "plumbing|electrical|lift|security|structural|cleaning|access|other",\n'
        '  "severity": "low|medium|high",\n'
        '  "confidence": 0.0 to 1.0\n'
        "}\n\n"
        f"Message: {safe_message}"
    )


async def classify_message(message: str) -> dict:
    prompt = _build_prompt(message)  # no pre-escaping needed — _build_prompt handles it
    try:
        async with httpx.AsyncClient(timeout=OLLAMA_TIMEOUT) as client:
            response = await client.post(
                f"{OLLAMA_HOST}/api/generate",
                json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
            )
            response.raise_for_status()
            raw = response.json().get("response", "")
            parsed = json.loads(raw)
            raw_category = str(parsed.get("category", "other")).lower()
            raw_severity = str(parsed.get("severity", "low")).lower()
            raw_confidence = float(parsed.get("confidence", 0.0))
            return {
                "is_incident": bool(parsed.get("is_incident", False)),
                "category": raw_category if raw_category in _VALID_CATEGORIES else "other",
                "severity": raw_severity if raw_severity in _VALID_SEVERITIES else "low",
                "confidence": max(0.0, min(1.0, raw_confidence)),
            }
    except Exception as exc:
        logger.error("Ollama classification failed: %s", exc)
        return _FALLBACK.copy()
