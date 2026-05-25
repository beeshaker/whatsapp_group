import json
import logging
import os

import httpx

logger = logging.getLogger(__name__)

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2")
OLLAMA_TIMEOUT = float(os.getenv("OLLAMA_TIMEOUT", "10"))

_FALLBACK: dict = {
    "is_incident": False,
    "category": "other",
    "severity": "low",
    "confidence": 0.0,
}


def _build_prompt(message: str) -> str:
    return (
        "Classify this WhatsApp message from a property management group.\n"
        "Return ONLY valid JSON, no explanation:\n"
        "{\n"
        '  "is_incident": true or false,\n'
        '  "category": "plumbing|electrical|lift|security|structural|cleaning|access|other",\n'
        '  "severity": "low|medium|high",\n'
        '  "confidence": 0.0 to 1.0\n'
        "}\n\n"
        f'Message: "{message}"'
    )


async def classify_message(message: str) -> dict:
    prompt = _build_prompt(message.replace('"', '\\"'))
    try:
        async with httpx.AsyncClient(timeout=OLLAMA_TIMEOUT) as client:
            response = await client.post(
                f"{OLLAMA_HOST}/api/generate",
                json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
            )
            response.raise_for_status()
            raw = response.json().get("response", "")
            parsed = json.loads(raw)
            return {
                "is_incident": bool(parsed.get("is_incident", False)),
                "category": str(parsed.get("category", "other")),
                "severity": str(parsed.get("severity", "low")),
                "confidence": float(parsed.get("confidence", 0.0)),
            }
    except Exception as exc:
        logger.error("Ollama classification failed: %s", exc)
        return _FALLBACK.copy()
