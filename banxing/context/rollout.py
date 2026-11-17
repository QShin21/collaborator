"""Compatibility source: Codex session files (`rollout-*.jsonl`), read-only.

Each line is one JSON event. The shapes handled here:

  {"type": "session_meta",  "payload": {"id": ..., "cwd": ...}}
  {"type": "event_msg",     "payload": {"type": "user_message", "message": ...}}
  {"type": "event_msg",     "payload": {"type": "agent_message", "message": ...}}
  {"type": "event_msg",     "payload": {"type": "task_complete", "last_agent_message": ...}}
  {"type": "event_msg",     "payload": {"type": "turn_aborted"}}
  {"type": "response_item", "payload": {"type": "message", "role": ..., "content": [...], "phase": ...}}

plus the older layout where a line is the response item itself. Unknown lines are
skipped, so a newer Codex adding event types does not break reading. Files can be
large; only the tail is read.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from ..textutil import clean
from .model import ThreadInfo, Turn, _content_text, _kind

_ID_IN_NAME = re.compile(r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\.jsonl$")
_WRAPPED = re.compile(r"^<([A-Za-z_][\w\-]*)[\s>].*</\1>\s*$", re.S)
_IDE_REQUEST = "## My request for Codex:"
_COMPLETE = ("taskcomplete", "turncomplete")
_ABORTED = ("turnaborted", "taskaborted")
TAIL_BYTES = 1_500_000
MAX_TAIL_BYTES = 12_000_000


def sessions_dir(explicit=""):
    if explicit:
        return Path(explicit)
    home = os.environ.get("CODEX_HOME")
    return (Path(home) if home else Path.home() / ".codex") / "sessions"


def user_text(text):
    """The user's own words from a stored user message, or "" for injected context."""
    text = clean(text)
    if _IDE_REQUEST in text:
        text = clean(text.split(_IDE_REQUEST, 1)[1])
    if not text or _WRAPPED.match(text) or text.startswith("# AGENTS.md"):
        return ""
    return text


def _records(lines):
    """Yield (kind, payload) from raw lines, tolerating both layouts and junk."""
    for line in lines:
        line = line.strip()
        if not line or line[0] != "{":
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        payload = data.get("payload")
        if isinstance(payload, dict):
            yield _kind(data.get("type")), payload
        elif "type" in data:
            yield "responseitem", data          # older layout: the item itself


def parse_turns(lines):
    """All turns found in `lines`, oldest first."""
    records = list(_records(lines))
    has_events = any(kind == "eventmsg" and _kind(p.get("type")) in ("usermessage", "agentmessage")
                     for kind, p in records)
    has_markers = any(kind == "eventmsg" and _kind(p.get("type")) in _COMPLETE for kind, p in records)
    turns, extras = [], []
    current = None

    def begin(text):
        nonlocal current
        current = {"user": text, "messages": [], "finals": [], "last": "", "done": False, "aborted": False}
        turns.append(current)

    for kind, payload in records:
        what = _kind(payload.get("type"))
        if kind == "eventmsg":
            if what == "usermessage":
                text = user_text(payload.get("message") or "")
                if text:
                    begin(text)
            elif current is None:
                continue
            elif what == "agentmessage":
                text = clean(payload.get("message") or "")
                if text:
                    current["messages"].append(text)
                    if _kind(payload.get("phase")) == "finalanswer":
                        current["finals"].append(text)
            elif what in _COMPLETE:
                current["done"] = True
                current["last"] = clean(payload.get("last_agent_message") or payload.get("lastAgentMessage") or "")
            elif what in _ABORTED:
                current["aborted"] = True
        elif kind == "responseitem" and what == "message":
            role = payload.get("role")
            text = clean(_content_text(payload.get("content")))
            if role == "user" and not has_events:
                text = user_text(text)
                if text:
                    begin(text)
            elif role == "assistant" and current is not None and text:
                if _kind(payload.get("phase")) == "finalanswer" and text not in current["finals"]:
                    current["finals"].append(text)
                if not has_events:
                    current["messages"].append(text)

    result = []
    for index, item in enumerate(turns):
        answer = "\n\n".join(item["finals"]) or item["last"] or (item["messages"][-1] if item["messages"] else "")
        if has_markers:
            completed = item["done"] and not item["aborted"]
        else:
            # Files without completion events: a turn that got an answer counts as finished.
            completed = bool(answer) and not item["aborted"]
        result.append(Turn(user=item["user"], answer=answer, completed=completed))
    return result


def _tail_lines(path, max_bytes):
    size = os.path.getsize(path)
    with open(path, "rb") as handle:
        if size > max_bytes:
            handle.seek(size - max_bytes)
            handle.readline()                      # drop the partial first line
        data = handle.read()
    return data.decode("utf-8", "replace").splitlines(), size > max_bytes


def read_turns(path, want=6):
    """The last turns of a session file, oldest first. Reads more of the file only
    when the tail does not hold `want` turns."""
    budget = TAIL_BYTES
    while True:
        lines, truncated = _tail_lines(path, budget)
        turns = parse_turns(lines)
        if len(turns) >= want or not truncated or budget >= MAX_TAIL_BYTES:
            return turns[-want:] if want else turns
        budget *= 4


def read_meta(path):
    """ThreadInfo for a session file: id and cwd from its first line (or its name)."""
    path = Path(path)
    info = ThreadInfo(id="", path=str(path), source="rollout")
    match = _ID_IN_NAME.search(path.name)
    if match:
        info.id = match.group(1)
    try:
        info.updated_at = path.stat().st_mtime
        with open(path, "rb") as handle:
            first = handle.readline(400_000).decode("utf-8", "replace")
        data = json.loads(first)
    except (OSError, ValueError):
        return info
    if isinstance(data, dict):
        meta = data.get("payload") if isinstance(data.get("payload"), dict) else data
        info.id = str(meta.get("id") or info.id)
        info.cwd = str(meta.get("cwd") or "")
    return info


def list_recent(root, limit=20, max_age_days=30, now=None, scan_limit=6000):
    """The most recently written session files under `root`, newest first."""
    root = Path(root)
    if not root.is_dir():
        return []
    cutoff = (now or time.time()) - max_age_days * 86400
    found, seen = [], 0
    stack = [root]
    while stack and seen < scan_limit:
        folder = stack.pop()
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(entry.path)
                elif entry.name.endswith(".jsonl") and entry.name.startswith("rollout-"):
                    seen += 1
                    mtime = entry.stat().st_mtime
                    if mtime >= cutoff:
                        found.append((mtime, entry.path))
            except OSError:
                continue
    found.sort(reverse=True)
    return [read_meta(path) for _, path in found[:limit]]
