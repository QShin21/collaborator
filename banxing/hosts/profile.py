"""How to recognise Codex and its input box — as data.

Everything the Windows client assumes about the Codex window is collected here so
it can be corrected after a real-device check without touching the UI Automation
code: which process is Codex, which editable element is the composer, what an
empty composer reads as, which windows mean "an input method is composing".

The defaults are informed guesses for the Codex desktop app (a Chromium-based
window). They are NOT verified against a real install; the compatibility
self-check in the pet menu reports what it actually finds. Overrides:

  settings.json  "process_names": ["codex.exe", ...]   extra executables
  BANXING_PROCESS_NAMES=a.exe,b.exe                    replaces the list (tests)
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, replace
from typing import Optional, Sequence, Tuple

from ..textutil import clean, strict

# UI Automation control type ids (UIAutomationClient.h)
EDIT, DOCUMENT, GROUP, CUSTOM, PANE, TEXT, BUTTON = 50004, 50030, 50026, 50025, 50033, 50020, 50000
_CONTAINER_TYPES = (DOCUMENT, GROUP, CUSTOM, PANE)
_ADDRESS = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]{1,20}:\S+$")


def page_address(value):
    """The value of a window's document element, if it is an address (app://…,
    file:///…, https://…) and not, say, the text of an editable document."""
    value = (value or "").strip()
    return value if len(value) <= 2000 and _ADDRESS.match(value) else ""


@dataclass(frozen=True)
class BoxCandidate:
    """What is known about one editable element."""
    control_type: int = 0
    framework: str = ""          # "Chrome", "Win32", "WPF", ...
    aria: str = ""               # UIA AriaProperties, e.g. "multiline=true;readonly=false"
    has_value: bool = False      # ValuePattern available
    read_only: bool = False
    has_text: bool = False       # TextPattern available
    focused: bool = False
    left: int = 0
    top: int = 0
    width: int = 0
    height: int = 0
    is_password: bool = False    # a password field: never read, never written, never a candidate

    def signature(self, window_width=0):
        """The structural facts a calibration remembers about the input box. No text."""
        aria = self.aria.casefold().replace(" ", "")
        return {"control_type": self.control_type, "framework": self.framework,
                "multiline": "multiline=true" in aria,
                "width_share": round(self.width / window_width, 2) if window_width else 0.0}


@dataclass(frozen=True)
class Profile:
    process_names: Tuple[str, ...] = ("codex.exe",)
    title_pattern: str = ""                      # optional regex the window title must match
    placeholders: Tuple[str, ...] = ()           # exact texts an empty composer shows
    min_box_width: int = 280                     # narrower editables are search / rename fields
    ime_classes: Tuple[str, ...] = (
        "Microsoft.IME.UIManager.CandidateWindow.Host",   # Microsoft Pinyin / Japanese IME
        "Microsoft.IME.CandidateWindow.Host",
        "SoPY_Comp",                                       # Sogou
        "QQPinyinCompWndTSF",                              # QQ Pinyin
        "BaiduPinyinCompWnd",
        "PalmInputUICand",
    )
    max_text_nodes: int = 600
    # What the calibrated input box looked like: (control_type, framework, multiline).
    # When set, only an element with the same three facts is taken for the input box.
    signature: Optional[Tuple[int, str, bool]] = None

    def with_signature(self, signature):
        if not isinstance(signature, dict) or "control_type" not in signature:
            return replace(self, signature=None)
        return replace(self, signature=(int(signature["control_type"]), str(signature.get("framework") or ""),
                                        bool(signature.get("multiline"))))

    @staticmethod
    def default(extra_process_names: Sequence[str] = (), placeholders: Sequence[str] = ()):
        names = [n.strip().casefold() for n in os.environ.get("BANXING_PROCESS_NAMES", "").split(",") if n.strip()]
        if not names:
            names = ["codex.exe"] + [str(n).strip().casefold() for n in extra_process_names if str(n).strip()]
        return Profile(process_names=tuple(dict.fromkeys(names)),
                       placeholders=tuple(strict(p) for p in placeholders if strict(p)))

    # ------------------------------------------------------------ the window
    def is_codex(self, exe_name, title=""):
        if (exe_name or "").casefold() not in self.process_names:
            return False
        return not self.title_pattern or bool(re.search(self.title_pattern, title or ""))

    # ------------------------------------------------------------ the input box
    def accepts(self, box: BoxCandidate, window_width=0):
        """Is this editable element plausibly the composer?"""
        if box.is_password or box.read_only or not (box.has_value or box.has_text):
            return False
        if self.signature is not None:
            known = box.signature()
            if (known["control_type"], known["framework"], known["multiline"]) != self.signature:
                return False
        if box.control_type != EDIT and not (box.control_type in _CONTAINER_TYPES and box.has_value):
            return False
        aria = box.aria.casefold().replace(" ", "")
        if "multiline=false" in aria:
            return False                           # a single-line <input>: search, rename, ...
        if "multiline=true" in aria:
            return True
        # No hint from the page: go by size. The composer is the wide one.
        return box.width >= max(self.min_box_width, int(0.3 * window_width))

    def pick(self, boxes: Sequence[BoxCandidate], window_width=0) -> Optional[int]:
        """Index of the composer among editable elements: a focused acceptable one,
        else the lowest acceptable one on screen (composers sit at the bottom)."""
        acceptable = [i for i, box in enumerate(boxes) if self.accepts(box, window_width)]
        if not acceptable:
            return None
        focused = [i for i in acceptable if boxes[i].focused]
        if focused:
            return focused[0]
        return max(acceptable, key=lambda i: (boxes[i].top + boxes[i].height, boxes[i].width))

    def fill_order(self, framework):
        """Which way of writing to try first.

        In a Chromium page the composer is a rich editor with its own state; pasting
        is what the editor itself handles, so its result can be trusted when read
        back. Native edit controls take a value directly, without the clipboard.
        """
        return ("paste", "value") if (framework or "").casefold() == "chrome" else ("value", "paste")

    def draft_from(self, text, name="", help_text=""):
        """The user's draft from what the box reads as. A placeholder is not a draft."""
        text = clean(text)
        probe = strict(text)
        if not probe:
            return ""
        if probe in self.placeholders or probe in (strict(name), strict(help_text)):
            return ""
        return text
