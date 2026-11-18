"""The local trial log. It stays on this machine.

  events.jsonl   one line per thing that happened (an audit ran, a concern was shown,
                 the user took it back / called it intended / called it wrong, …)
                 with counts and timings, for looking at a trial afterwards

It never contains text of a conversation, of the ledger or of a finding — only
counts, durations and code-style names. The log is capped by rotating once.
"""
from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path

from . import __version__
from .config import home

MAX_LOG_BYTES = 2_000_000
_ALLOWED = (bool, int, float)
# What a logged string may look like: a code-style name such as "premise", "took_back"
# or "0.4.0". No spaces, no CJK, no punctuation of prose — so that a passage of a
# conversation cannot end up in the log even if a caller passes one by mistake,
# however short it is.
_NAME = re.compile(r"[A-Za-z0-9_.:\-]{1,40}")


def _safe(data):
    """Keep numbers, booleans and identifier-like strings only."""
    clean = {}
    for name, value in (data or {}).items():
        if isinstance(value, _ALLOWED) or value is None:
            clean[name] = value
        elif isinstance(value, str) and _NAME.fullmatch(value):
            clean[name] = value
    return clean


class Store:
    def __init__(self, folder=None, wall=time.time):
        self.folder = Path(folder) if folder else home()
        self.log_path = self.folder / "events.jsonl"
        self._wall = wall

    def log(self, event, data=None):
        line = {"t": round(self._wall(), 3), "v": __version__, "event": str(event)[:40]}
        line.update(_safe(data))
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            if self.log_path.exists() and self.log_path.stat().st_size > MAX_LOG_BYTES:
                self.log_path.replace(self.log_path.with_suffix(".1.jsonl"))
            with open(self.log_path, "a", encoding="utf-8") as stream:
                stream.write(json.dumps(line, ensure_ascii=False) + "\n")
        except OSError:
            pass                    # a full disk must not stop the app

    def events(self, limit=4000):
        try:
            with open(self.log_path, encoding="utf-8") as stream:
                lines = stream.readlines()[-limit:]
        except OSError:
            return []
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    def summary(self):
        """Numbers worth looking at after a trial: how often the supervisor spoke, and
        what the user made of it. The share the user called wrong is the one number
        that says whether it deserves to keep speaking."""
        events = self.events()
        count = lambda name: sum(1 for e in events if e.get("event") == name)   # noqa: E731
        audits = [e for e in events if e.get("event") == "watch_audit"]
        decided = [e for e in events if e.get("event") == "watch_decided"]
        shown = sum(1 for e in decided if e.get("action") == "show" and e.get("why") != "manual")
        took, intended, wrong = count("watch_took_back"), count("watch_intended"), count("watch_wrong")
        answered = took + intended + wrong
        seconds = sorted(e["seconds"] for e in audits if isinstance(e.get("seconds"), (int, float)))
        return {
            "audits": len(audits), "asked_for": sum(1 for e in audits if e.get("manual")),
            "failed": sum(1 for e in audits if not e.get("ok", True)),
            "requests": sum(e.get("requests", 0) for e in audits), "chars_sent": sum(e.get("chars", 0) for e in audits),
            "looked_back": sum(e.get("looks", 0) for e in audits),
            "searches": sum(e.get("searches", 0) for e in audits),
            "independent_readings": sum(1 for e in audits if e.get("independent")),
            "bubbles": shown, "taken_back": took, "intended": intended, "called_wrong": wrong,
            "closed": count("watch_dismissed"), "overtaken": count("watch_overtaken"),
            "resolved_later": count("watch_resolved"),
            "held_back": sum(1 for e in decided if e.get("why") in ("repeat", "muted", "too_late")),
            "wrong_rate": round(wrong / answered, 3) if answered else None,
            "median_audit_seconds": seconds[len(seconds) // 2] if seconds else None,
        }


def wipe(folder):
    """"清除本机数据": remove everything the app keeps in its data folder (files of the
    earlier versions included). Only files this app creates are touched; the folder
    itself stays."""
    folder = Path(folder)
    removed = 0
    for name in ("settings.json", "state.json", "events.jsonl", "events.1.jsonl", "calibration.json",
                 "banxing.log", "banxing.log.1"):
        try:
            (folder / name).unlink()
            removed += 1
        except OSError:
            pass
    for sub in ("vault",):
        if (folder / sub).is_dir():
            shutil.rmtree(folder / sub, ignore_errors=True)
            removed += 1
    return removed
