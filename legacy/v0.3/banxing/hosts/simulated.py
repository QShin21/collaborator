"""An in-memory Codex: windows, chats, one input box per chat.

Used by the tests and by `--demo`. It behaves the way the real composer is
expected to behave (drafts kept per chat, box emptied on send) and can be told to
misbehave (unreadable, fill refused) to exercise the fallbacks. It is a model of
our assumptions, not evidence about the real application.
"""
from __future__ import annotations

from ..context.model import ChatSignals
from ..follow import Snapshot
from ..geometry import Rect
from ..textutil import strict
from .base import CodexClient, FillResult


class SimulatedCodex(CodexClient):
    name = "simulated"
    can_fill = True

    def __init__(self, chat="chat-a", window="w1"):
        self.foreground = True
        self.window = window
        self.chat = chat
        self.drafts = {}                 # (window, chat) -> text
        self.visible = {}                # chat -> texts on screen
        self.sent = []                   # [(chat, text)]
        self.composing = False
        self.readable = True
        self.fill_works = True
        self.fills = []                  # every text written by fill()
        self.focus_returned = 0
        self.reads = 0                   # how often the input box's text was fetched
        self.version = "sim-1"
        self.placeholder = ""            # what an "empty" box reads as, if the host shows one
        self.window_rect = Rect(100, 80, 1300, 880)
        self.box_rect = Rect(220, 780, 1180, 850)
        self.send_rect = Rect(1120, 800, 1170, 840)

    # ------------------------------------------------------------ the "user"
    @property
    def draft(self):
        return self.drafts.get((self.window, self.chat), "")

    def type(self, text):
        self.drafts[(self.window, self.chat)] = text

    def send(self):
        text = self.draft
        self.sent.append((self.chat, text))
        self.visible.setdefault(self.chat, []).append(text)
        self.type("")
        return text

    def switch_chat(self, chat):
        self.chat = chat

    def switch_window(self, window, chat):
        self.window, self.chat = window, chat

    # ------------------------------------------------------------ the client
    def _key(self):
        return (self.window, self.chat, f"box-{self.window}")

    def snapshot(self, now):
        if not self.foreground:
            return Snapshot(at=now, foreground=False)
        if not self.may_read():
            return Snapshot(at=now, foreground=True, window_id=self.window,
                            window_rect=self.window_rect, problem="uncalibrated")
        if not self.readable:
            return Snapshot(at=now, foreground=True, window_id=self.window, chat_key=self.chat,
                            window_rect=self.window_rect, problem="unreadable")
        self.reads += 1
        window, chat, box = self._key()
        return Snapshot(at=now, foreground=True, window_id=window, chat_key=chat, box_id=box,
                        draft=self.draft, composing=self.composing, window_rect=self.window_rect,
                        box_rect=self.box_rect, send_rect=self.send_rect)

    def signals(self):
        if not self.foreground or not self.may_read():
            return None
        return ChatSignals(title="Codex", selected=(self.chat,),
                           texts=tuple(self.visible.get(self.chat, ())))

    def fill(self, key, expect, text):
        if not (self.foreground and self.readable and tuple(key) == self._key()
                and strict(self.draft) == strict(expect)):
            return FillResult(False, "mismatch")
        if not self.fill_works:
            return FillResult(False, "failed")
        self.type(text)
        self.fills.append(text)
        self.focus_returned += 1
        return FillResult(True, method="simulated")

    def probe(self, try_fill=False):
        return {"client": self.name, "window": True, "box": self.readable, "fill": self.fill_works}

    def capabilities(self):
        from .base import CHAT_KEY, FILL, READ, SIGNALS
        return frozenset({READ, FILL, CHAT_KEY, SIGNALS})

    def host_version(self):
        return self.version

    def raw_box_text(self):
        if not self.may_read():
            return None
        self.reads += 1
        return (self.placeholder or self.draft) if self.readable else None

    def box_signature(self):
        return {"control_type": 50004, "framework": "Simulated", "multiline": True, "width_share": 0.8}

    def record_structure(self):
        return [{"role": 50004, "framework": "Simulated", "patterns": ["value", "text"], "password": False,
                 "focused": True, "box": [0.1, 0.87, 0.9, 0.96], "name_len": 0, "value_len": len(self.draft)}]
