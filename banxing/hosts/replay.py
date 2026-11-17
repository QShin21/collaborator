"""Replay a structure recording made on a real machine.

`HostClient.record_structure()` writes down what the host's window looked like to
UI Automation — types, patterns, relative positions, text LENGTHS, never text. Such
a recording, added to tests/fixtures/, lets the rules in profile.py be run against
it on any platform: the one way a real-device observation turns into a regression
test.

The recordings in the repository so far are synthetic (written by hand to exercise
this code); they say nothing about what Codex really exposes. A real one is produced
with the evidence export.
"""
from __future__ import annotations

from .profile import BoxCandidate

ALLOWED_FIELDS = {"role", "framework", "multiline", "patterns", "read_only", "password", "focused",
                  "box", "name_len", "value_len"}


def is_clean(nodes):
    """A recording may only contain the structural fields — numbers, booleans, short
    technical names. Anything else (a stray text field) makes it unfit to share."""
    for node in nodes:
        if not isinstance(node, dict) or set(node) - ALLOWED_FIELDS:
            return False
        if not isinstance(node.get("framework", ""), str) or len(node.get("framework", "")) > 24:
            return False
        if any(not isinstance(p, str) or p not in ("value", "text") for p in node.get("patterns", [])):
            return False
        for name in ("name_len", "value_len"):
            if node.get(name) is not None and not isinstance(node.get(name), int):
                return False
    return True


def candidates(nodes, window_width=1200, window_height=800):
    """Recording → the BoxCandidate list profile.pick() works on."""
    found = []
    for node in nodes:
        left, top, right, bottom = (list(node.get("box") or []) + [0, 0, 0, 0])[:4]
        patterns = node.get("patterns") or []
        found.append(BoxCandidate(
            control_type=int(node.get("role") or 0), framework=str(node.get("framework") or ""),
            aria="multiline=true" if node.get("multiline") else "",
            has_value="value" in patterns, has_text="text" in patterns,
            read_only=bool(node.get("read_only")), focused=bool(node.get("focused")),
            left=int(left * window_width), top=int(top * window_height),
            width=int((right - left) * window_width), height=int((bottom - top) * window_height),
            is_password=bool(node.get("password"))))
    return found


def pick(profile, nodes, window_width=1200, window_height=800):
    """Which recorded node the profile would take for the input box (index or None)."""
    return profile.pick(candidates(nodes, window_width, window_height), window_width)
