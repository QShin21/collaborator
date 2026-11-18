"""Reading a conversation the user chose to have watched. Read-only, all of it.

  ChatGPT export     the official copy of an account's history: a .zip, the unpacked
                     folder, or the conversations file itself. One conversation is
                     picked on this machine; nothing else in the export is used.
  Codex session      a `rollout-*.jsonl` file — user messages, what the agent said,
                     and the commands it ran with their output
  Codex App Server   `thread/turns/list`, paged (see context/appserver.py)
  Claude Code        a session `.jsonl` — messages, tool calls and their results
  pasted text        a chat page copied to the clipboard ("You said: … ChatGPT said: …"):
                     the quickest way to hand over a conversation from any web chat
  transcript file    a plain JSON or marked-text transcript, for conversations from
                     anywhere else and for the replay cases

Nothing here looks for conversations by itself: every function is handed the path
or the thread the user pointed at. Tests pass synthetic files in a temp folder.

STATUS — what each reader was checked against:
  * ChatGPT export: written from the layout such exports are commonly seen to have
    (a list of conversations, each a `mapping` of message nodes with `parent` links
    and a `current_node`). OpenAI's help page describes the export but not this file
    format, so the reader is tolerant and exercised with SYNTHETIC files only (E1).
    Not run on a real export.
  * Codex session files: the event shapes already assumed by context/rollout.py, plus
    `function_call` / `function_call_output` response items for commands. Synthetic
    files only (E1). Not run on a real Codex session.
  * App Server: item types and paging as documented at
    https://learn.chatgpt.com/docs/app-server (read 2026-11-18); exercised against the
    synthetic server in tests/ (E1). Not run against a real Codex install.
  * Claude Code session files and pasted chat pages: the line shapes and the
    "… said:" marks are assumptions, exercised with synthetic text only (E1). A pasted
    page has no tool output: everything the model "ran" arrives as its own words, so
    nothing in it can count as a record — which is exactly how the ledger treats it.
"""
from __future__ import annotations

import io
import json
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path

from ..context import rollout
from ..context.model import _content_text, _kind
from ..textutil import clean, clip
from . import transcript as ts

MAX_FILE_BYTES = 600_000_000        # a single file larger than this is not read
EXPORT, SESSION, APPSERVER, FILE = "chatgpt_export", "codex_session", "codex_appserver", "transcript_file"
CLAUDE, PASTED = "claude_session", "pasted_text"
KINDS = (EXPORT, SESSION, APPSERVER, FILE, CLAUDE, PASTED)


class SourceError(Exception):
    """kind: missing | too_large | unreadable | format | empty | not_found | appserver"""

    def __init__(self, kind, message=""):
        super().__init__(message or kind)
        self.kind = kind


@dataclass(frozen=True)
class Listing:
    """One conversation on offer in a source. `title` is shown to the user who chose
    the file; it is never logged or stored."""
    id: str
    title: str = ""
    turns: int = 0
    updated_at: float = 0.0


@dataclass(frozen=True)
class SourceRef:
    """What is being watched: enough to read it again."""
    kind: str                       # chatgpt_export | codex_session | codex_appserver | transcript_file
    path: str = ""                  # the file or folder (not for the app server)
    conversation: str = ""          # which conversation in it / the thread id


# ================================================================== ChatGPT export
def _read_json_bytes(data, name):
    try:
        return json.loads(data.decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError) as error:
        raise SourceError("format", f"{name} is not JSON") from error


def _looks_like_conversations(value):
    return isinstance(value, list) and any(isinstance(item, dict) and isinstance(item.get("mapping"), dict)
                                           for item in value[:50])


def _export_documents(path):
    """Every conversations document in an export, as parsed JSON lists."""
    path = Path(path)
    if not path.exists():
        raise SourceError("missing", "no such file or folder")
    documents = []
    if path.is_dir():
        candidates = sorted(p for p in path.rglob("*.json") if "conversations" in p.name.casefold())
        for candidate in candidates:
            if candidate.stat().st_size > MAX_FILE_BYTES:
                raise SourceError("too_large", candidate.name)
            documents.append(_read_json_bytes(candidate.read_bytes(), candidate.name))
    elif zipfile.is_zipfile(path):
        try:
            with zipfile.ZipFile(path) as archive:
                # Members are read in memory, never extracted to disk: a crafted member
                # name cannot write anywhere.
                for info in sorted(archive.infolist(), key=lambda item: item.filename):
                    name = info.filename.rsplit("/", 1)[-1].casefold()
                    if info.is_dir() or not name.endswith(".json") or "conversations" not in name:
                        continue
                    if info.file_size > MAX_FILE_BYTES:
                        raise SourceError("too_large", name)
                    documents.append(_read_json_bytes(archive.read(info), name))
        except (zipfile.BadZipFile, OSError, RuntimeError) as error:
            raise SourceError("unreadable", type(error).__name__) from error
    else:
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                raise SourceError("too_large", path.name)
            documents.append(_read_json_bytes(path.read_bytes(), path.name))
        except OSError as error:
            raise SourceError("unreadable", type(error).__name__) from error
    found = [document for document in documents if _looks_like_conversations(document)]
    if not found:
        raise SourceError("format", "no conversations were found in this export")
    return found


def _conversation_id(conversation, index):
    return str(conversation.get("conversation_id") or conversation.get("id") or f"#{index}")


def _branch(conversation):
    """(the nodes of the current branch, oldest first; how many forks were passed)."""
    mapping = conversation.get("mapping") or {}
    node_id = conversation.get("current_node")
    if node_id not in mapping:
        # No pointer to the branch in use: follow the last child from the root.
        node_id = next((key for key, node in mapping.items()
                        if isinstance(node, dict) and node.get("parent") not in mapping), None)
        seen = set()
        while node_id in mapping and node_id not in seen:
            seen.add(node_id)
            children = [child for child in (mapping[node_id].get("children") or []) if child in mapping]
            if not children:
                break
            node_id = children[-1]
    path, forks, seen = [], 0, set()
    while node_id in mapping and node_id not in seen:
        seen.add(node_id)
        node = mapping[node_id] if isinstance(mapping[node_id], dict) else {}
        path.append(node)
        if len([child for child in (node.get("children") or []) if child in mapping]) > 1:
            forks += 1
        node_id = node.get("parent")
    path.reverse()
    return path, forks


def _message_text(content):
    """(text, things that could not be read) of one message's content."""
    if not isinstance(content, dict):
        return "", 0
    kind = _kind(content.get("content_type"))
    unread = 0
    if isinstance(content.get("parts"), list):
        parts = []
        for part in content["parts"]:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
            elif part:
                unread += 1                         # an image, an audio clip, a file pointer
        return "\n".join(p for p in parts if p), unread
    for name in ("text", "result", "summary"):
        if isinstance(content.get(name), str) and content[name]:
            return content[name], unread
    if kind in ("thoughts", "reasoningrecap"):
        return "", 0
    return "", unread


def _export_turns(conversation):
    nodes, forks = _branch(conversation)
    turns, gaps, current = [], [], None
    if forks:
        gaps.append(ts.Gap("branch", 0, forks))

    def close():
        nonlocal current
        if current is not None:
            answered = bool(current["model"] or current["records"])
            turns.append(ts.make_turn(len(turns) + 1, current["user"], "\n\n".join(current["model"]),
                                      current["records"], ts.DONE if answered else ts.RUNNING, current["id"]))
        current = None

    for node in nodes:
        message = node.get("message") if isinstance(node.get("message"), dict) else None
        if message is None:
            continue
        author = message.get("author") if isinstance(message.get("author"), dict) else {}
        role = _kind(author.get("role"))
        metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
        content = message.get("content") if isinstance(message.get("content"), dict) else {}
        kind = _kind(content.get("content_type"))
        if role == "system" or metadata.get("is_visually_hidden_from_conversation") or kind in (
                "usereditablecontext", "modeleditablecontext"):
            continue
        text, unread = _message_text(content)
        attachments = [a for a in metadata.get("attachments") or [] if isinstance(a, dict)]
        if role == "user":
            if not clean(text) and not attachments and not unread:
                continue
            close()
            current = {"user": text, "model": [], "records": [], "id": str(message.get("id") or node.get("id") or "")}
            number = len(turns) + 1
            for attachment in attachments:
                current["records"].append(ts.Record(ts.ATTACHMENT, clip(str(attachment.get("name") or ""), 120)))
            if attachments or unread:
                gaps.append(ts.Gap("attachment", number, len(attachments) + unread))
            continue
        if current is None:
            continue                                # something before the first user message
        number = len(turns) + 1
        if unread:
            gaps.append(ts.Gap("attachment", number, unread))
        if role == "assistant":
            recipient = str(message.get("recipient") or "all")
            if kind == "code" or recipient not in ("all", ""):
                current["records"].append(ts.Record(ts.COMMAND, recipient if recipient != "all" else "code", text))
            elif clean(text):
                current["model"].append(text)
        elif role == "tool":
            name = str(author.get("name") or "tool")
            if kind == "executionoutput":
                current["records"].append(ts.Record(ts.OUTPUT, name, text))
            elif clean(text):
                current["records"].append(ts.Record(ts.TOOL, name, text))
    close()
    return tuple(turns), ts.merge_gaps(gaps)


def list_export(path):
    """The conversations in an export, newest first."""
    listings = []
    for document in _export_documents(path):
        for index, conversation in enumerate(document):
            if not isinstance(conversation, dict) or not isinstance(conversation.get("mapping"), dict):
                continue
            asked = len(_export_turns(conversation)[0])
            stamp = conversation.get("update_time") or conversation.get("create_time") or 0
            listings.append(Listing(id=_conversation_id(conversation, len(listings)),
                                    title=clip(clean(str(conversation.get("title") or "")), 80), turns=asked,
                                    updated_at=float(stamp) if isinstance(stamp, (int, float)) else 0.0))
    listings.sort(key=lambda item: -item.updated_at)
    return listings


def read_export(path, conversation_id):
    position = 0
    for document in _export_documents(path):
        for conversation in document:
            if not isinstance(conversation, dict) or not isinstance(conversation.get("mapping"), dict):
                continue
            if _conversation_id(conversation, position) == conversation_id:
                turns, gaps = _export_turns(conversation)
                if not turns:
                    raise SourceError("empty", "this conversation has no messages")
                return ts.Transcript(turns=turns, gaps=gaps, source=EXPORT, conversation_id=conversation_id,
                                     title=clip(clean(str(conversation.get("title") or "")), 80))
            position += 1
    raise SourceError("not_found", "that conversation is not in this export")


# ================================================================== Codex session file
def _arguments(raw):
    """The command line of a function call, from its JSON arguments."""
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return clip(str(raw), 300)
    if isinstance(data, dict):
        for name in ("command", "cmd", "input", "code"):
            value = data.get(name)
            if isinstance(value, list):
                return " ".join(str(part) for part in value)
            if isinstance(value, str) and value:
                return value
        return clip(json.dumps(data, ensure_ascii=False), 300)
    return clip(str(data), 300)


def _call_output(raw):
    """(text, ok) of a function call's output: a string, or JSON with output and exit code."""
    data = raw
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except ValueError:
            return raw, None
    if isinstance(data, dict):
        text = data.get("output") if isinstance(data.get("output"), str) else data.get("content")
        meta = data.get("metadata") if isinstance(data.get("metadata"), dict) else data
        code = meta.get("exit_code", meta.get("exitCode"))
        ok = (code == 0) if isinstance(code, int) and not isinstance(code, bool) else None
        return (text if isinstance(text, str) else json.dumps(data, ensure_ascii=False)), ok
    return str(raw if isinstance(raw, str) else data), None


def session_turns(lines):
    """Turns of a Codex session file, with what was run and what came out."""
    records = list(rollout._records(lines))
    has_events = any(kind == "eventmsg" and _kind(p.get("type")) in ("usermessage", "agentmessage")
                     for kind, p in records)
    has_markers = any(kind == "eventmsg" and _kind(p.get("type")) in rollout._COMPLETE for kind, p in records)
    turns, gaps, current, calls = [], [], None, {}

    def begin(text):
        nonlocal current
        current = {"user": text, "said": [], "records": [], "done": False, "aborted": False, "missing": 0}
        turns.append(current)

    def say(text):
        text = clean(text)
        if text and (not current["said"] or current["said"][-1] != text):
            current["said"].append(text)

    for kind, payload in records:
        what = _kind(payload.get("type"))
        if kind == "compacted" or (kind == "eventmsg" and what in ("contextcompacted", "compacted")):
            gaps.append(ts.Gap("compacted", len(turns)))
            continue
        if kind == "eventmsg":
            if what == "usermessage":
                text = rollout.user_text(payload.get("message") or "")
                if text:
                    begin(text)
            elif current is None:
                continue
            elif what == "agentmessage":
                say(payload.get("message") or "")
            elif what in rollout._COMPLETE:
                current["done"] = True
                say(payload.get("last_agent_message") or payload.get("lastAgentMessage") or "")
            elif what in rollout._ABORTED:
                current["aborted"] = True
        elif kind == "responseitem":
            if what == "message":
                role = payload.get("role")
                text = clean(_content_text(payload.get("content")))
                if role == "user" and not has_events:
                    text = rollout.user_text(text)
                    if text:
                        begin(text)
                elif role == "assistant" and current is not None and text and not has_events:
                    say(text)
            elif current is None:
                continue
            elif what in ("functioncall", "localshellcall", "customtoolcall"):
                call_id = str(payload.get("call_id") or payload.get("id") or len(calls))
                title = _arguments(payload.get("arguments") or payload.get("action") or payload.get("input") or "")
                name = str(payload.get("name") or "")
                calls[call_id] = (current, len(current["records"]))
                current["records"].append(ts.Record(ts.COMMAND, title or name, ""))
                current["missing"] += 1
            elif what in ("functioncalloutput", "localshellcalloutput", "customtoolcalloutput"):
                text, ok = _call_output(payload.get("output"))
                owner, index = calls.pop(str(payload.get("call_id") or ""), (None, -1))
                if owner is not None:
                    owner["records"][index] = ts.Record(ts.COMMAND, owner["records"][index].title, text, ok)
                    owner["missing"] -= 1
                else:
                    current["records"].append(ts.Record(ts.OUTPUT, "", text, ok))

    result = []
    for number, item in enumerate(turns, 1):
        if has_markers:
            state = ts.ABORTED if item["aborted"] else ts.DONE if item["done"] else ts.RUNNING
        else:
            state = ts.ABORTED if item["aborted"] else ts.DONE if item["said"] else ts.RUNNING
        if item["missing"] > 0 and state != ts.RUNNING:
            gaps.append(ts.Gap("output", number, item["missing"]))
        result.append(ts.make_turn(number, item["user"], "\n\n".join(item["said"]), item["records"], state))
    return tuple(result), ts.merge_gaps(gaps)


def read_session(path):
    path = Path(path)
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            raise SourceError("too_large", path.name)
        with open(path, encoding="utf-8", errors="replace") as handle:
            turns, gaps = session_turns(handle)
    except OSError as error:
        raise SourceError("missing" if not path.exists() else "unreadable", type(error).__name__) from error
    if not turns:
        raise SourceError("empty", "no turns were found in this session file")
    meta = rollout.read_meta(path)
    return ts.Transcript(turns=turns, gaps=gaps, source=SESSION, conversation_id=meta.id, title="")


def list_sessions(sessions_dir, limit=20):
    """Recent Codex sessions under an explicit folder, newest first. The title is the
    opening of the first message — shown only in the dialog where the user picks one."""
    listings = []
    for info in rollout.list_recent(sessions_dir, limit):
        try:
            with open(info.path, encoding="utf-8", errors="replace") as handle:
                turns, _ = session_turns(handle)
        except OSError:
            continue
        if turns:
            listings.append(Listing(id=info.path, title=clip(turns[0].user.split("\n")[0], 60), turns=len(turns),
                                    updated_at=info.updated_at))
    return listings


# ================================================================== Codex App Server
def _item_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(filter(None, (_item_text(part) for part in value)))
    if isinstance(value, dict):
        for name in ("text", "output", "content", "result"):
            if value.get(name):
                return _item_text(value[name])
    return ""


def turn_from_items(number, data):
    """One turn object of the App Server (with full items) → (Turn, gaps)."""
    user, said, records, gaps = [], [], [], []
    for item in (data.get("items") or []) if isinstance(data, dict) else []:
        if not isinstance(item, dict):
            continue
        kind = _kind(item.get("type"))
        if kind == "usermessage":
            for part in item.get("content") if isinstance(item.get("content"), list) else [item.get("content")]:
                if isinstance(part, dict) and _kind(part.get("type")) in ("image", "localimage"):
                    gaps.append(ts.Gap("attachment", number))
                else:
                    text = _item_text(part)
                    if text:
                        user.append(text)
        elif kind in ("agentmessage", "plan"):
            text = _item_text(item.get("text") or item.get("content"))
            if text:
                said.append(text)
        elif kind == "commandexecution":
            code = item.get("exitCode")
            output = item.get("aggregatedOutput")
            records.append(ts.Record(ts.COMMAND, _item_text(item.get("command")),
                                     output if isinstance(output, str) else "",
                                     (code == 0) if isinstance(code, int) and not isinstance(code, bool) else None))
            if not isinstance(output, str) and _kind(item.get("status")) != "inprogress":
                gaps.append(ts.Gap("output", number))
        elif kind == "filechange":
            changes = [c for c in item.get("changes") or [] if isinstance(c, dict)]
            records.append(ts.Record(ts.FILE, "、".join(str(c.get("path") or "") for c in changes[:8]),
                                     "\n".join(str(c.get("diff") or "") for c in changes[:4])))
        elif kind in ("mcptoolcall", "dynamictoolcall", "collabtoolcall", "websearch"):
            records.append(ts.Record(ts.TOOL, str(item.get("tool") or item.get("query") or kind),
                                     _item_text(item.get("result") or item.get("contentItems") or "")))
        elif kind == "functioncalloutput":
            records.append(ts.Record(ts.OUTPUT, str(item.get("name") or ""), _item_text(item.get("output"))))
        elif kind == "imageview":
            gaps.append(ts.Gap("attachment", number))
        elif kind == "contextcompaction":
            gaps.append(ts.Gap("compacted", number))
    status = data.get("status") if isinstance(data, dict) else None
    status = _kind(status.get("type") if isinstance(status, dict) else status)
    state = ts.DONE if status == "completed" else ts.RUNNING if status == "inprogress" else ts.ABORTED
    turn = ts.make_turn(number, rollout.user_text("\n".join(user)), "\n\n".join(said), records, state,
                        str(data.get("id") or "") if isinstance(data, dict) else "")
    return turn, gaps


def thread_stamp(server, thread_id):
    """When the thread was last updated, from the thread list — or 0 when that cannot
    be told (then the caller reads the thread)."""
    try:
        for thread in server.list_threads(50):
            if thread.id == thread_id:
                return thread.updated_at
    except Exception:
        return 0
    return 0


def list_threads(server, limit=20):
    """The recent threads the App Server knows, newest first, for the user to pick one."""
    from ..context.appserver import AppServerError
    try:
        threads = server.list_threads(limit)
    except AppServerError as error:
        raise SourceError("appserver", str(error)[:120]) from error
    return [Listing(id=t.id, title=clip((t.name or t.preview).split("\n")[0], 60), updated_at=t.updated_at)
            for t in threads]


def read_appserver(server, thread_id):
    """The whole history of one thread, through the App Server's paged reads."""
    from ..context.appserver import AppServerError
    try:
        raw = server.read_history(thread_id)
    except AppServerError as error:
        raise SourceError("appserver", str(error)[:120]) from error
    turns, gaps = [], []
    for data in raw:
        turn, found = turn_from_items(len(turns) + 1, data)
        if turn.user or turn.model or turn.records:
            turns.append(turn)
            gaps += found
    if not turns:
        raise SourceError("empty", "the thread has no turns")
    return ts.Transcript(turns=tuple(turns), gaps=ts.merge_gaps(gaps), source=APPSERVER, conversation_id=str(thread_id))


# ================================================================== Claude Code session file
def claude_turns(lines):
    """Turns of a Claude Code session file (one JSON object per line: a `type` of
    "user" or "assistant" and a `message` whose content is text, `tool_use` and
    `tool_result` blocks). A tool result arrives in a "user" line; it is a record, not
    something the user said. Side chains (sub-agents) and meta lines are left out."""
    turns, gaps, current, calls = [], [], None, {}
    for line in lines:
        line = line.strip()
        if not line or line[0] != "{":
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if not isinstance(data, dict) or data.get("isSidechain") or data.get("isMeta"):
            continue
        if data.get("type") == "summary" or data.get("isCompactSummary"):
            gaps.append(ts.Gap("compacted", len(turns)))
            continue
        message = data.get("message") if isinstance(data.get("message"), dict) else None
        if message is None or data.get("type") not in ("user", "assistant"):
            continue
        content = message.get("content")
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else (
            [b for b in content if isinstance(b, dict)] if isinstance(content, list) else [])
        if data["type"] == "user":
            said = "\n".join(str(b.get("text") or "") for b in blocks if b.get("type") == "text")
            said = rollout.user_text(said)
            if said and not any(b.get("type") == "tool_result" for b in blocks):
                current = {"user": said, "said": [], "records": [], "missing": 0}
                turns.append(current)
            for block in blocks:
                if block.get("type") == "image" and current is not None:
                    gaps.append(ts.Gap("attachment", len(turns)))
                if block.get("type") != "tool_result" or current is None:
                    continue
                owner, index = calls.pop(str(block.get("tool_use_id") or ""), (None, -1))
                text = _item_text(block.get("content"))
                ok = None if block.get("is_error") is None else not block.get("is_error")
                if owner is not None:
                    owner["records"][index] = ts.Record(ts.COMMAND, owner["records"][index].title, text, ok)
                    owner["missing"] -= 1
                else:
                    current["records"].append(ts.Record(ts.OUTPUT, "", text, ok))
        elif current is not None:
            for block in blocks:
                if block.get("type") == "text" and clean(str(block.get("text") or "")):
                    current["said"].append(clean(str(block["text"])))
                elif block.get("type") == "tool_use":
                    title = f"{block.get('name') or 'tool'} {_arguments(block.get('input'))}".strip()
                    calls[str(block.get("id") or len(calls))] = (current, len(current["records"]))
                    current["records"].append(ts.Record(ts.COMMAND, title, ""))
                    current["missing"] += 1
    result = []
    for number, item in enumerate(turns, 1):
        last = number == len(turns)
        # The file has no "turn finished" line: the last turn counts as finished once the
        # assistant has said something after its last tool call.
        running = last and (not item["said"] or item["missing"] > 0)
        if item["missing"] > 0 and not running:
            gaps.append(ts.Gap("output", number, item["missing"]))
        result.append(ts.make_turn(number, item["user"], "\n\n".join(item["said"]), item["records"],
                                   ts.RUNNING if running else ts.DONE))
    return tuple(result), ts.merge_gaps(gaps)


def read_claude(path):
    path = Path(path)
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            raise SourceError("too_large", path.name)
        with open(path, encoding="utf-8", errors="replace") as handle:
            turns, gaps = claude_turns(handle)
    except OSError as error:
        raise SourceError("missing" if not path.exists() else "unreadable", type(error).__name__) from error
    if not turns:
        raise SourceError("empty", "no turns were found in this session file")
    return ts.Transcript(turns=turns, gaps=gaps, source=CLAUDE, conversation_id=path.stem)


# ================================================================== transcript file / pasted text
# Marks that start a part: our own ("## 用户", "助手："), and what a copied chat page
# carries ("You said:", "ChatGPT said:", "您说：").
_MARK = re.compile(r"^\s{0,3}(?:#{1,6}\s*|\*\*|【)?\s*(用户|我|您说|你说|you said|user|human|助手|主模型|chatgpt\s*说|"
                   r"chatgpt said|claude said|gemini said|assistant|ai|model|记录|结果|输出|result|output|log)"
                   r"\s*(?:\*\*|】)?\s*(?:[:：]\s*(.*))?$", re.I)
_ROLE = {"用户": ts.USER, "我": ts.USER, "您说": ts.USER, "你说": ts.USER, "you said": ts.USER, "user": ts.USER,
         "human": ts.USER, "助手": ts.MODEL, "主模型": ts.MODEL, "chatgpt说": ts.MODEL, "chatgpt said": ts.MODEL,
         "claude said": ts.MODEL, "gemini said": ts.MODEL, "assistant": ts.MODEL, "ai": ts.MODEL, "model": ts.MODEL}


def transcript_from_data(data):
    """Our own neutral shape: {"turns": [{"user", "assistant", "records": [...], "state"}], "gaps": [...]}."""
    if not isinstance(data, dict) or not isinstance(data.get("turns"), list):
        raise SourceError("format", "a transcript needs a list of turns")
    turns = []
    for item in data["turns"]:
        if not isinstance(item, dict):
            continue
        records = [ts.Record(kind=r.get("kind") if r.get("kind") in (ts.COMMAND, ts.OUTPUT, ts.FILE, ts.TOOL,
                                                                    ts.ATTACHMENT) else ts.OUTPUT,
                             title=str(r.get("title") or ""), text=str(r.get("text") or ""),
                             ok=r.get("ok") if isinstance(r.get("ok"), bool) else None)
                   for r in item.get("records") or [] if isinstance(r, dict)]
        turns.append(ts.make_turn(len(turns) + 1, str(item.get("user") or ""),
                                  str(item.get("assistant") or item.get("model") or ""), records,
                                  str(item.get("state") or ts.DONE)))
    gaps = [ts.Gap(kind=g.get("kind"), turn=g.get("turn") if isinstance(g.get("turn"), int) else 0,
                   count=g.get("count") if isinstance(g.get("count"), int) else 1)
            for g in data.get("gaps") or [] if isinstance(g, dict) and g.get("kind") in ts.GAP_KINDS]
    if not turns:
        raise SourceError("empty", "the transcript has no turns")
    return ts.Transcript(turns=tuple(turns), gaps=ts.merge_gaps(gaps), source=FILE,
                         conversation_id=str(data.get("id") or ""), title=clip(clean(str(data.get("title") or "")), 80))


def transcript_from_text(text):
    """A marked transcript: lines such as "## 用户", "助手：", "### 记录: python check.py"
    start a part; everything up to the next mark belongs to it."""
    turns, current, target = [], None, None
    for line in io.StringIO(text):
        match = _MARK.match(line.rstrip("\n"))
        if match:
            name, rest = " ".join(match.group(1).casefold().split()), (match.group(2) or "").strip()
            role = _ROLE.get(name, _ROLE.get(name.replace(" ", ""), ts.RECORD))
            if role == ts.USER:
                current = {"user": [rest] if rest else [], "model": [], "records": []}
                turns.append(current)
                target = current["user"]
            elif current is None:
                target = None
            elif role == ts.MODEL:
                if rest:
                    current["model"].append(rest)
                target = current["model"]
            else:
                current["records"].append({"title": rest, "lines": []})
                target = current["records"][-1]["lines"]
        elif target is not None:
            target.append(line.rstrip("\n"))
    if not turns:
        raise SourceError("format", "no “用户/User” marks were found")
    return ts.Transcript(source=FILE, turns=tuple(
        ts.make_turn(number, "\n".join(item["user"]), "\n".join(item["model"]),
                     [ts.Record(ts.OUTPUT, r["title"], "\n".join(r["lines"])) for r in item["records"]],
                     ts.DONE if (item["model"] or item["records"]) else ts.RUNNING)
        for number, item in enumerate(turns, 1)))


def read_pasted(text):
    """A conversation handed over as text (a copied chat page). It cannot be read
    again by itself: the user pastes the page again when it has grown."""
    from dataclasses import replace
    if len(text) > 20_000_000:
        raise SourceError("too_large", "pasted text")
    return replace(transcript_from_text(text), source=PASTED)


def read_transcript_file(path):
    path = Path(path)
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            raise SourceError("too_large", path.name)
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as error:
        raise SourceError("missing" if not path.exists() else "unreadable", type(error).__name__) from error
    if text.lstrip().startswith("{"):
        try:
            return transcript_from_data(json.loads(text))
        except ValueError as error:
            raise SourceError("format", "the file is not valid JSON") from error
    return transcript_from_text(text)


# ================================================================== one entry point
def sniff(path):
    """Which kind of source a path is, by its shape — not by trusting its name alone."""
    path = Path(path)
    if not path.exists():
        raise SourceError("missing", "no such file or folder")
    if path.is_dir() or zipfile.is_zipfile(path):
        return EXPORT
    try:
        with open(path, "rb") as handle:
            head = handle.read(8192).decode("utf-8-sig", "replace").lstrip()
    except OSError as error:
        raise SourceError("unreadable", type(error).__name__) from error
    if path.name.casefold().endswith(".jsonl"):
        # Codex wraps every line in {"type": …, "payload": …}; Claude Code lines carry a
        # "message" and a "sessionId".
        return CLAUDE if '"payload"' not in head and ('"sessionId"' in head or '"message"' in head) else SESSION
    if head.startswith("["):
        return EXPORT
    return FILE


def listing(path):
    """(kind, conversations on offer). A source that holds exactly one conversation
    lists that one."""
    kind = sniff(path)
    if kind == EXPORT:
        return kind, list_export(path)
    transcript = {SESSION: read_session, CLAUDE: read_claude}.get(kind, read_transcript_file)(path)
    return kind, [Listing(id=transcript.conversation_id, title=transcript.title or clip(
        transcript.turns[0].user.split("\n")[0], 60), turns=len(transcript.turns))]


def read(ref, server=None):
    """The conversation a SourceRef names, as it is now."""
    if ref.kind == EXPORT:
        return read_export(ref.path, ref.conversation)
    if ref.kind == SESSION:
        return read_session(ref.path)
    if ref.kind == CLAUDE:
        return read_claude(ref.path)
    if ref.kind == FILE:
        return read_transcript_file(ref.path)
    if ref.kind == PASTED:
        raise SourceError("pasted", "a pasted conversation is read when it is pasted")
    if ref.kind == APPSERVER:
        if server is None:
            raise SourceError("appserver", "the app server is not available")
        return read_appserver(server, ref.conversation)
    raise SourceError("format", f"unknown source {ref.kind!r}")
