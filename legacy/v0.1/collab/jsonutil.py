"""Tolerant JSON extraction from model output."""
import json
import re


def extract_json(text, default=None):
    """Return the first JSON object found in `text`, or `default`."""
    if not text:
        return default
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
    try:
        return json.loads(cleaned)
    except ValueError:
        pass
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            obj, _ = decoder.raw_decode(text[match.start():])
            if isinstance(obj, dict):
                return obj
        except ValueError:
            continue
    return default


def as_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def as_str(value, limit=None):
    if value is None:
        return ""
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False)
    value = value.strip()
    return value[:limit] if limit else value


def clamp(value, low, high, default=None):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default if default is not None else low
    return max(low, min(high, number))
