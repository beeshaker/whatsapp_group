import re
from typing import Optional

_PLATE_RE = re.compile(r"\bK[A-Z]{3}\s?\d{3}[A-Z]?\b", re.IGNORECASE)


def normalize_plate(raw: str) -> str:
    return re.sub(r"\s+", "", raw).upper()


def extract_plates(text: str) -> list[str]:
    if not text:
        return []
    return [normalize_plate(m) for m in _PLATE_RE.findall(text)]


def resolve_plate_for_issue(message_snippet: str, full_text: str) -> Optional[str]:
    snippet_plates = extract_plates(message_snippet)
    if snippet_plates:
        return snippet_plates[0]
    distinct = set(extract_plates(full_text))
    if len(distinct) == 1:
        return next(iter(distinct))
    return None


def is_valid_plate(value: str) -> bool:
    return bool(_PLATE_RE.fullmatch(value.strip()))
