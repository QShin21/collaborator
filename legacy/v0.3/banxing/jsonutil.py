"""Tolerant JSON extraction from model output."""
from __future__ import annotations

import json
import re


def extract_json(text, default=None):
    """Return the first JSON object found in `text`, or `default`."""
    if not text:
        return default
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
    try:
        value = json.loads(cleaned)
        if isinstance(value, dict):
            return value
    except ValueError:
        pass
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            obj, _ = decoder.raw_decode(text[match.start():])
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    return default


def as_str(value):
    if value is None:
        return ""
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False)
    return value.strip()


def as_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        word = value.strip().casefold()
        if word in ("true", "yes", "1", "是", "提示"):
            return True
        if word in ("false", "no", "0", "否", "待机"):
            return False
    return default
