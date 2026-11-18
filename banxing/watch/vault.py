"""The watched conversation's ledger on disk — encrypted, and only if the user asked.

By default watching lives in memory and is gone when the app closes. When the user
ticks "keep this ledger on this computer", one file is written: vault/watch.bin,
holding the ledger (which quotes short passages of the conversation), where the
conversation is read from, the range and the model service the user agreed to, and
the usage numbers. The conversation itself is never copied: it is read again from
where it is.

Encrypted or not stored: `secretbox.protect` raises on Windows when DPAPI fails, and
then nothing is written (the caller keeps working in memory and says so). A file
written by a newer version is neither read nor overwritten.
"""
from __future__ import annotations

import json
from pathlib import Path

from .. import secretbox
from . import ledger as lg
from .session import Session, Usage
from .sources import KINDS, SourceRef

WATCH_SCHEMA = 1
NAME = "watch.bin"


class VaultError(Exception):
    """kind: encrypt | newer | write"""

    def __init__(self, kind):
        super().__init__(kind)
        self.kind = kind


def path(folder):
    return Path(folder) / "vault" / NAME


def _freeze(value):
    return tuple(_freeze(item) for item in value) if isinstance(value, list) else value


def _thaw(value):
    return [_thaw(item) for item in value] if isinstance(value, tuple) else value


def _stored(folder):
    try:
        stored = path(folder).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    text = secretbox.reveal(stored) if stored else ""
    try:
        data = json.loads(text) if text else None
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def save(folder, ref, session):
    """Write the watched conversation's state. Raises VaultError; on any failure what
    was there before is left as it was."""
    existing = _stored(folder)
    if existing is not None and isinstance(existing.get("schema"), int) and existing["schema"] > WATCH_SCHEMA:
        raise VaultError("newer")
    data = {
        "schema": WATCH_SCHEMA,
        "source": {"kind": ref.kind, "path": ref.path, "conversation": ref.conversation},
        "first": session.first, "destination": session.destination, "paused": session.paused,
        "ledger": lg.to_json(session.ledger), "known": list(session.known),
        "usage": {"requests": session.usage.requests, "chars": session.usage.chars, "masked": session.usage.masked,
                  "turns": list(session.usage.turns)},
        "shown": [_thaw(key) for key in session.shown],
        "muted": {kind: _thaw(situation) for kind, situation in session.muted.items()},
    }
    try:
        stored = secretbox.protect(json.dumps(data, ensure_ascii=False))     # before anything is touched
    except secretbox.SecretError as error:
        raise VaultError("encrypt") from error
    target = path(folder)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(stored, encoding="utf-8")
    except OSError as error:
        raise VaultError("write") from error


def load(folder):
    """(SourceRef, Session) as stored, or None: nothing stored, unreadable, another
    Windows account, or written by a newer version."""
    data = _stored(folder)
    if data is None or data.get("schema") != WATCH_SCHEMA:
        return None
    source = data.get("source") if isinstance(data.get("source"), dict) else {}
    if source.get("kind") not in KINDS:
        return None
    ledger = lg.from_json(data.get("ledger"))
    if ledger is None:
        return None
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}

    def count(value):
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0

    shown = {_freeze(key) for key in data.get("shown") or [] if isinstance(key, list)} \
        if isinstance(data.get("shown"), list) else set()
    muted = {kind: _freeze(situation) for kind, situation in (data.get("muted") or {}).items()
             if isinstance(kind, str) and isinstance(situation, list)} if isinstance(data.get("muted"), dict) else {}
    session = Session(
        destination=str(data.get("destination") or ""), first=count(data.get("first")) or 1, ledger=ledger,
        known=[item for item in data.get("known") or [] if isinstance(item, str)],
        usage=Usage(requests=count(usage.get("requests")), chars=count(usage.get("chars")),
                    masked=count(usage.get("masked")),
                    turns=tuple(sorted({n for n in usage.get("turns") or [] if count(n)}))),
        shown=shown, muted=muted, paused=bool(data.get("paused")))
    ref = SourceRef(kind=source["kind"], path=str(source.get("path") or ""),
                    conversation=str(source.get("conversation") or ""))
    return ref, session


def forget(folder):
    """Remove the stored ledger. True if a file was there."""
    try:
        path(folder).unlink()
        return True
    except OSError:
        return False
