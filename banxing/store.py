"""Local records: the companion's state and the trial log. Both stay on this machine.

  state.json     level, feedback windows, route-kind weights (numbers and names only)
  events.jsonl   one line per thing that happened (analysed, shown, adopted, ...)
                 with timings, for looking at a trial afterwards

Neither file ever contains draft text, prompts or chat content — only counts,
durations and code-style names. The log is capped by rotating once.

state.json carries a `schema` number. MIGRATIONS turn an older file into the current
shape, one step each; append to the list, never edit an entry.
"""
from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path

from . import __version__, levels as lv
from .config import home, read_json, write_json

STATE_SCHEMA = 2
MAX_LOG_BYTES = 2_000_000
_ALLOWED = (bool, int, float)
# What a logged string may look like: a code-style name such as "app_switch", "paste"
# or "0.3.0". No spaces, no CJK, no punctuation of prose — so that a draft, a prompt
# or a line of chat cannot end up in the log even if a caller passes one by mistake,
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


def _v1_to_v2(data):
    """Generation 1 kept {"version": 1, "frequency": {"mode": ...}}: two modes become
    two of the three levels; the feedback window starts over."""
    old = data.get("frequency") if isinstance(data.get("frequency"), dict) else {}
    return {"schema": 2, "levels": lv.to_json(lv.from_json({"mode": old.get("mode")})), "weights": {}}


MIGRATIONS = [(1, _v1_to_v2)]       # (from schema, step)


def migrate_state(data):
    """Any stored state → the current shape. Unknown or broken input → a fresh state."""
    if not isinstance(data, dict):
        return {"schema": STATE_SCHEMA, "levels": {}, "weights": {}}
    schema = data.get("schema", data.get("version", 1))
    if not isinstance(schema, int) or isinstance(schema, bool) or schema > STATE_SCHEMA or schema < 1:
        return {"schema": STATE_SCHEMA, "levels": {}, "weights": {}}
    for start, step in MIGRATIONS:
        if schema == start:
            data = step(data)
            schema = data["schema"]
    return data


class Store:
    def __init__(self, folder=None, wall=time.time):
        self.folder = Path(folder) if folder else home()
        self.state_path = self.folder / "state.json"
        self.log_path = self.folder / "events.jsonl"
        self._wall = wall

    # ------------------------------------------------------------ state
    def _state(self):
        return migrate_state(read_json(self.state_path, {}))

    def load_levels(self):
        return lv.from_json(self._state().get("levels"))

    def save_levels(self, state):
        data = self._state()
        data.update(schema=STATE_SCHEMA, levels=lv.to_json(state))
        write_json(self.state_path, data)

    def load_weights(self):
        from .aboutme import weights_from
        return weights_from(self._state())

    def save_weights(self, weights):
        data = self._state()
        data.update(schema=STATE_SCHEMA, weights={k: float(v) for k, v in weights.items()})
        write_json(self.state_path, data)

    # ------------------------------------------------------------ log
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
            pass                    # a full disk must not stop the companion

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
        """Numbers worth looking at after a trial."""
        events = self.events()
        count = lambda name: sum(1 for e in events if e.get("event") == name)   # noqa: E731
        waits = sorted(e["wait"] for e in events
                       if e.get("event") == "shown" and isinstance(e.get("wait"), (int, float)))
        shown, late = count("shown"), count("late")
        analysed_tasks = [e for e in events if e.get("event") in ("shown", "standby") and e.get("task", True)
                          and e.get("source") == "auto"]
        ready = sum(1 for e in analysed_tasks if e.get("ready"))
        return {
            "analysed": count("analyze"), "shown": shown, "standby": count("standby"),
            "adopted": count("adopted") + count("copied") + count("hint_applied") + count("hint_copied"),
            "dismissed": count("dismissed"), "sent_original": count("sent_original"),
            "incomplete": count("incomplete"), "late": late,
            "errors": count("error"), "restored": count("restored"), "more": count("more"),
            # Did help arrive before the draft was gone? The number generation 2 most needs.
            "in_time_rate": round(shown / (shown + late), 3) if shown + late else None,
            # How often a draft could be sent as it was: is the scaffold coming down?
            "ready_rate": round(ready / len(analysed_tasks), 3) if analysed_tasks else None,
            "median_wait_seconds": waits[len(waits) // 2] if waits else None,
        }


def wipe(folder):
    """"清除本机数据": remove everything the app keeps in its data folder. Only files
    this app creates are touched; the folder itself stays."""
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
