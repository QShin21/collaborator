"""Text helpers shared by the core: normalisation, comparison, redaction.

Three levels of "the same text" are used on purpose:

  strict()  whitespace-insensitive. Decides whether a late result or a click still
            belongs to the draft that is in the input box right now.
  loose()   also ignores case and punctuation. Decides whether an edit is a real
            change or just tidying up.
  fingerprint()  short hash of strict(); used as a key, never shown.
"""
from __future__ import annotations

import difflib
import hashlib
import re
import unicodedata

# Characters editors put into the text that the user never typed: object replacement,
# zero-width spaces/joiners, BOM, bidi marks.
_INVISIBLE = dict.fromkeys(map(ord, "￼​‌‍⁠﻿‎‏"))
_COMPARE_LIMIT = 4000


def clean(text):
    """The text as the user would recognise it: no invisible characters, unified
    line breaks, no trailing blank lines."""
    if not text:
        return ""
    text = str(text).translate(_INVISIBLE).replace("\r\n", "\n").replace("\r", "\n")
    return text.replace(" ", " ").strip()


def strict(text):
    """Whitespace-insensitive form used for "is this still the same draft"."""
    return " ".join(clean(text).split())


def loose(text):
    """Case-, width- and punctuation-insensitive form used to judge how much changed."""
    text = unicodedata.normalize("NFKC", clean(text)).casefold()
    return "".join(ch for ch in text if ch.isalnum())


def fingerprint(text, salt=""):
    return hashlib.sha256((salt + strict(text)).encode("utf-8")).hexdigest()[:16]


def similarity(a, b):
    a, b = a[:_COMPARE_LIMIT], b[:_COMPARE_LIMIT]
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def changed_chars(a, b):
    """How many characters were removed plus added going from `a` to `b`."""
    a, b = a[:_COMPARE_LIMIT], b[:_COMPARE_LIMIT]
    if a == b:
        return 0
    total = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag != "equal":
            total += (i2 - i1) + (j2 - j1)
    return total


def clip(text, limit, ellipsis="…"):
    text = clean(text)
    if limit is None or len(text) <= limit:
        return text
    return text[: max(0, limit - len(ellipsis))].rstrip() + ellipsis


def is_cjk(ch):
    code = ord(ch)
    return (0x4E00 <= code <= 0x9FFF or 0x3400 <= code <= 0x4DBF or 0x3040 <= code <= 0x30FF
            or 0xAC00 <= code <= 0xD7AF or 0xF900 <= code <= 0xFAFF)


def cjk_ratio(text):
    letters = [ch for ch in text if ch.isalnum()]
    if not letters:
        return 0.0
    return sum(1 for ch in letters if is_cjk(ch)) / len(letters)


def keywords(text):
    """Cheap bag of terms for overlap scoring: latin words and CJK bigrams."""
    text = unicodedata.normalize("NFKC", clean(text)).casefold()
    terms = set(re.findall(r"[a-z0-9_][a-z0-9_.\-]{2,}", text))
    run = []
    for ch in text + " ":
        if is_cjk(ch):
            run.append(ch)
            continue
        for i in range(len(run) - 1):
            terms.add(run[i] + run[i + 1])
        run = []
    return terms


# --------------------------------------------------------------------- redaction
# Passages of a conversation are sent to a model service. Things that look like
# credentials are masked first; this is a safety net, not a guarantee.
_SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{6,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/\-]{20,}=*"),
]
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)((?:\bapi[_-]?key|\bsecret|\btoken|\bpassword|\bpasswd|\bpwd|密码|口令|密钥)\s*(?:[:=：]|是|为|\bis\b)\s*)"
    r"([^\s,;，；\"']{6,})")
MASK = "[已隐去]"


def redact(text):
    """Mask credential-looking substrings. Returns (text, number_masked)."""
    if not text:
        return "", 0
    count = 0
    for pattern in _SECRET_PATTERNS:
        text, n = pattern.subn(MASK, text)
        count += n
    text, n = _SECRET_ASSIGNMENT.subn(lambda m: m.group(1) + MASK, text)
    return text, count + n
