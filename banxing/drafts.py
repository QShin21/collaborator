"""What counts as a draft worth analysing, and what counts as a real edit."""
from __future__ import annotations

import math
import re

from .textutil import cjk_ratio, changed_chars, clean, loose

MAX_CHARS = 3000        # beyond this the user has already written a full brief (or pasted a log)
MIN_CJK_CHARS = 6       # "做个记账的小工具" is enough to work with; "继续" is not
MIN_LATIN_CHARS = 12
MIN_LATIN_WORDS = 3

_COMMAND = re.compile(r"^\s*[/!$][^\s/]")   # slash commands and shell escapes of the Codex composer


def analyzable(text):
    """Return (ok, reason). Reasons: empty, command, short, long."""
    text = clean(text)
    if not text:
        return False, "empty"
    if _COMMAND.match(text) and "\n" not in text:
        return False, "command"
    if len(text) > MAX_CHARS:
        return False, "long"
    core = loose(text)
    if cjk_ratio(core) >= 0.3:
        if len(core) < MIN_CJK_CHARS:
            return False, "short"
    elif len(core) < MIN_LATIN_CHARS or len(text.split()) < MIN_LATIN_WORDS:
        return False, "short"
    return True, ""


def substantial_change(old, new):
    """True when `new` is a different draft rather than a tidied-up `old`.

    Fixing a typo, swapping a word, changing punctuation or spacing is not
    substantial. Adding a condition ("家里没有电钻") or rewriting the request is.
    The bar is a tenth of the longer text, but never less than a short clause
    (6 CJK characters / 12 letters) and never more than a sentence (12 / 24), so
    that one new condition counts even in a long draft.
    """
    a, b = loose(old), loose(new)
    if a == b:
        return False
    if not a or not b:
        return True
    low, high = (6, 12) if cjk_ratio(a + b) >= 0.3 else (12, 24)
    bar = min(high, max(low, math.ceil(0.1 * max(len(a), len(b)))))
    return changed_chars(a, b) >= bar
