"""The contract between the app and a host — the application whose input box the
companion follows. In generation 2 the only followed host is the Codex desktop app.

What a host client CAN do (`capabilities()`) is not yet what the companion WILL do:
reading and writing are switched on only for capabilities that also passed the
calibration on this machine (see calibration.py).
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Optional

from ..context.model import ChatSignals
from ..follow import Snapshot

READ, FILL, CHAT_KEY, SIGNALS, TURN_EVENTS, SELECTION = "read", "fill", "chat_key", "signals", "turn_events", "selection"
ALL_CAPABILITIES = frozenset({READ, FILL, CHAT_KEY, SIGNALS, TURN_EVENTS, SELECTION})


@dataclass(frozen=True)
class FillResult:
    ok: bool
    reason: str = ""      # mismatch | failed | unsupported | clipboard_busy | keys_held | focus | error
    method: str = ""      # value | paste | simulated


class HostClient:
    """All methods are called from one thread (the watcher)."""

    name = "none"
    can_fill = False

    # ---- may the input box be read at all? -------------------------------------
    # "Not calibrated, not following" is kept HERE, in front of the host, not only
    # where results are used: until reading has been switched on, `snapshot()` reports
    # where the window is and nothing else — the input box is not looked for and its
    # text is never fetched. The default is off.
    _reading = False
    _reading_version = None     # reading was granted for this host version only
    _authorized = 0             # depth of user-started checks in progress (watcher thread only)

    def set_reading(self, allowed, host_version=None):
        """Switch routine reading of the input box on or off. With `host_version`,
        the permission holds only while the host still is that version."""
        self._reading_version = host_version if allowed else None
        self._reading = bool(allowed)

    @contextmanager
    def authorized(self):
        """Reading for the length of one check the user started (a calibration step,
        the self-check). Used on the watcher thread, around that check only."""
        self._authorized += 1
        try:
            yield self
        finally:
            self._authorized -= 1

    def may_read(self):
        if self._authorized > 0:
            return True
        if not self._reading:
            return False
        return self._reading_version is None or self._reading_version == self.host_version()

    def open(self):
        """Called once on the thread that will use the client."""

    def close(self):
        pass

    def capabilities(self) -> frozenset:
        """What this client is able to do at all, before calibration."""
        return frozenset()

    def host_version(self) -> str:
        """Something that changes when the host application is updated. A calibration
        is only valid for the version it was made on. "" = unknown."""
        return ""

    def snapshot(self, now) -> Snapshot:
        """One look at the host. Implementations must not look for, or read, the
        input box unless `may_read()`; they then answer with the window only and
        problem="uncalibrated"."""
        raise NotImplementedError

    def signals(self) -> Optional[ChatSignals]:
        """What is visible in the current host window, for recognising the chat."""
        return None

    def fill(self, key, expect, text) -> FillResult:
        """Replace the draft with `text` — only if the chat, the box and the draft
        (`expect`) are still the ones in `key`. Must never send the message."""
        return FillResult(False, "unsupported")

    def focus_codex(self):
        """Give the keyboard back to the host's input box."""

    def raw_box_text(self):
        """What the input box reads as right now, placeholder included — used once,
        on request, to learn what an empty box looks like. None if unreadable."""
        return None

    def set_placeholders(self, texts):
        """Texts that mean "the box is empty"."""

    def box_signature(self):
        """A few structural facts about the input box in use (control type, framework,
        multi-line hint, share of the window width) — never its text. None if unknown."""
        return None

    def set_signature(self, signature):
        """Only treat an editable element that matches this signature as the input box."""

    def probe(self, try_fill=False) -> dict:
        """Compatibility self-check; contains no draft or chat text."""
        return {"client": self.name}

    def record_structure(self) -> list:
        """The interface structure around the input box: types, patterns, positions
        and text LENGTHS only. For turning a real machine into a test fixture."""
        return []


CodexClient = HostClient        # the generation-1 name


class NullClient(HostClient):
    """No way to see a host here. The companion stays reachable for paste mode and examples."""

    name = "none"

    def __init__(self, reason="unsupported"):
        self.reason = reason

    def snapshot(self, now):
        return Snapshot(at=now, foreground=False, problem=self.reason)

    def probe(self, try_fill=False):
        return {"client": self.name, "reason": self.reason}
