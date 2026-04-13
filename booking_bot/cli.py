"""Top-level orchestration. In this task, only normalize_phone is implemented —
the main() loop is added in Task 20."""
from __future__ import annotations

import re


def normalize_phone(raw: object) -> tuple[str, str | None]:
    """Coerce an Excel cell into a canonical 10-digit phone string.

    Returns (cleaned_phone, error_reason). error_reason is None on success and
    'invalid_phone_format' otherwise. Accepts:
      - 10-digit strings
      - +91 / 91 prefixed 12-digit strings
      - int cells (e.g. 9876543210)
      - whole-number float cells (e.g. 9876543210.0)
      - strings with spaces, dashes, parentheses
    """
    if isinstance(raw, bool):  # bool is a subclass of int; reject early
        return ("", "invalid_phone_format")
    if isinstance(raw, int):
        s = str(raw)
    elif isinstance(raw, float):
        if raw != int(raw):
            return ("", "invalid_phone_format")
        s = str(int(raw))
    elif isinstance(raw, str):
        s = re.sub(r"[^\d+]", "", raw.strip())
    else:
        return ("", "invalid_phone_format")

    m = re.fullmatch(r"(?:\+?91)?(\d{10})", s)
    if not m:
        return ("", "invalid_phone_format")
    return (m.group(1), None)
