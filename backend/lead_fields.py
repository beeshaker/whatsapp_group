import re
from typing import Optional

_CANDIDATE_RE = re.compile(r"\+?\d[\d\s]{7,13}\d")
_KENYAN_MOBILE_RE = re.compile(r"254(7|1)\d{8}")
_AGENT_TAG_RE = re.compile(r"@~\s*([A-Za-z][A-Za-z.]*)")
_SOURCE_TAG_RE = re.compile(r"\(([^)]+)\)\s*$")
_CONTACT_NAME_RE = re.compile(r"(?i:contact)\s+((?:[A-Z][a-zA-Z'\-]*\s*){1,3})")
_CONTACT_NAME_STOPWORDS = {"us", "agent", "office", "team", "support", "admin"}


def normalize_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("0") and len(digits) == 10:
        return "254" + digits[1:]
    return digits


def extract_phones(text: str) -> list[str]:
    if not text:
        return []
    found = []
    for match in _CANDIDATE_RE.finditer(text):
        normalized = normalize_phone(match.group())
        if _KENYAN_MOBILE_RE.fullmatch(normalized) and normalized not in found:
            found.append(normalized)
    return found


def resolve_phone_for_issue(message_snippet: str, full_text: str) -> Optional[str]:
    snippet_phones = extract_phones(message_snippet)
    if snippet_phones:
        return snippet_phones[0]
    distinct = set(extract_phones(full_text))
    if len(distinct) == 1:
        return next(iter(distinct))
    return None


def is_valid_phone(value: str) -> bool:
    return bool(_KENYAN_MOBILE_RE.fullmatch(normalize_phone(value.strip())))


def extract_agent_tag(text: str) -> Optional[str]:
    if not text:
        return None
    m = _AGENT_TAG_RE.search(text)
    return m.group(1).strip() if m else None


def extract_source_tag(text: str) -> Optional[str]:
    if not text:
        return None
    m = _SOURCE_TAG_RE.search(text.strip())
    return m.group(1).strip() if m else None


def extract_contact_name(text: str) -> Optional[str]:
    if not text:
        return None
    m = _CONTACT_NAME_RE.search(text)
    if not m:
        return None
    words = m.group(1).split()
    while words and words[-1].lower() in _CONTACT_NAME_STOPWORDS:
        words.pop()
    return " ".join(words) if words else None
