"""Data shapes for chat context, and tolerant conversion from the app-server JSON."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from ..textutil import clean


@dataclass
class Turn:
    user: str = ""            # what the user sent
    answer: str = ""          # Codex's final answer text (not commentary, not tool output)
    completed: bool = False   # finished normally; only these are quoted to the model


@dataclass
class ThreadInfo:
    id: str
    name: str = ""
    preview: str = ""         # usually the first user message
    cwd: str = ""
    updated_at: float = 0.0
    path: str = ""            # the session .jsonl, when known
    source: str = ""          # "appserver" | "rollout"
    probes: Optional[List[Tuple[str, int, bool]]] = None   # see identify.probes_for


@dataclass(frozen=True)
class ChatSignals:
    """What can be seen in the Codex window, used only to recognise the chat."""
    title: str = ""                   # window title
    selected: Tuple[str, ...] = ()    # names of selected items (e.g. the chat in the sidebar)
    texts: Tuple[str, ...] = ()       # visible text, in document order
    url: str = ""                     # address of the page shown, if the window exposes one


@dataclass(frozen=True)
class ContextExcerpt:
    text: str
    thread_id: str
    turns: int
    source: str               # "appserver" | "rollout"


# ------------------------------------------------------------ app-server JSON
def _kind(value):
    return re.sub(r"[^a-z]", "", str(value or "").casefold())


def _content_text(content):
    """Text of a message whose content is a string or a list of typed parts."""
    if isinstance(content, str):
        return content
    parts = []
    for part in content or []:
        if isinstance(part, str):
            parts.append(part)
        elif isinstance(part, dict) and _kind(part.get("type")) in ("text", "inputtext", "outputtext"):
            parts.append(str(part.get("text") or ""))
    return "\n".join(p for p in parts if p)


def turn_from_api(data):
    """One turn object from thread/turns/list or thread/read → Turn."""
    user_parts, finals, others = [], [], []
    for item in (data.get("items") or []) if isinstance(data, dict) else []:
        if not isinstance(item, dict):
            continue
        kind = _kind(item.get("type"))
        if kind == "usermessage":
            text = _content_text(item.get("content")) or str(item.get("text") or "")
            if text:
                user_parts.append(text)
        elif kind == "agentmessage":
            text = str(item.get("text") or "") or _content_text(item.get("content"))
            if not text:
                continue
            (finals if _kind(item.get("phase")) == "finalanswer" else others).append(text)
    status = data.get("status") if isinstance(data, dict) else None
    if isinstance(status, dict):
        status = status.get("type")
    # Without phases the last agent message of a turn is its answer.
    answer = "\n\n".join(finals) if finals else (others[-1] if others else "")
    return Turn(user=clean("\n".join(user_parts)), answer=clean(answer),
                completed=_kind(status) == "completed")


def turns_from_api(items):
    return [turn_from_api(item) for item in items or [] if isinstance(item, dict)]


def thread_from_api(data):
    def number(*names):
        for name in names:
            value = data.get(name)
            if isinstance(value, (int, float)):
                # seconds, or milliseconds from newer builds
                return float(value) / 1000.0 if value > 1e11 else float(value)
        return 0.0

    return ThreadInfo(
        id=str(data.get("id") or ""), name=clean(data.get("name") or data.get("title") or ""),
        preview=clean(data.get("preview") or ""), cwd=str(data.get("cwd") or ""),
        updated_at=number("updatedAt", "updated_at", "createdAt", "created_at"),
        path=str(data.get("path") or ""), source="appserver")
