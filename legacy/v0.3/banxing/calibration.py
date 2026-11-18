"""Calibration: a few small actions on the user's own Codex that decide which
abilities are switched on for this machine. "未校准不跟随、不回填."

Until a calibration has passed, the companion neither reads the input box by itself
nor writes into it; paste mode and the built-in examples still work. A calibration
is valid for one host version and one version of this app — when either changes it
has to be done again.

Every step knows what it should see (a fixed phrase, a marker it wrote itself), so
it can judge right or wrong without keeping anything the user typed. The results
hold yes/no answers, counts and timings only.

The step logic uses nothing but the HostClient interface, so it runs unchanged
against the simulator in tests. Whether the steps pass on a real Codex window is
exactly what this module exists to find out — it has not been run there yet.
"""
from __future__ import annotations

import time
from pathlib import Path

from . import __version__
from .config import read_json, write_json
from .hosts.base import CHAT_KEY, FILL, READ, SIGNALS
from .textutil import strict

PHRASE = "伴行校准 123 abc"
MARKER = "〔伴行写入测试〕"
SCHEMA = 1

# (step, the user has to do something, may be skipped)
STEPS = (
    ("window", True, False),     # C1  click into the Codex window
    ("read", True, False),       # C2  type the phrase into the input box
    ("empty", True, False),      # C3  clear the input box
    ("ime", True, True),         # C4  compose a sentence with an input method, pausing on the candidates
    ("fill", False, False),      # C5  write a marker and take it away again
    ("focus", True, True),       # C6  click the test bubble, then type one character
    ("chat", True, True),        # C7  switch to another chat
    ("sessions", False, True),   # C8  can the chat's history be read (structure only)
    ("speed", False, True),      # C9  how long one look at the window takes
)
STEP_NAMES = tuple(step for step, _, _ in STEPS)
SLOW_MS = 250.0


def _snap(client, clock):
    return client.snapshot(clock())


def prepare(client, step, memo, clock=time.monotonic):
    """Called when a step is put in front of the user: remember what to compare with."""
    if step == "chat":
        with client.authorized():
            memo["chat_before"] = _snap(client, clock).chat_key
    if step == "ime":
        memo["ime_seen"] = False
        memo["ime_changes"] = 0


def check(client, step, memo, clock=time.monotonic, sleep=time.sleep, context=None):
    """Run one step's check. Returns a dict with at least {"ok": bool}; no text in it.

    This is the one place the input box is read before a calibration exists: the user
    opened the wizard and pressed the button for this step. The permission lasts for
    this call only (`client.authorized()`); the watcher's routine looks stay blind."""
    with client.authorized():
        return _check(client, step, memo, clock, sleep, context)


def _check(client, step, memo, clock, sleep, context):
    snap = _snap(client, clock)
    if step == "window":
        return {"ok": bool(snap.foreground), "problem": snap.problem if not snap.foreground else ""}
    if step == "read":
        ok = snap.draft is not None and strict(snap.draft) == strict(PHRASE)
        result = {"ok": ok, "readable": snap.draft is not None,
                  "chars_seen": len(snap.draft) if snap.draft is not None else None, "chars_expected": len(PHRASE)}
        if ok:
            signature = client.box_signature()
            if signature:
                result["signature"] = signature
                memo["signature"] = signature
        return result
    if step == "empty":
        if snap.draft is None:
            return {"ok": False, "readable": False}
        if not strict(snap.draft):
            return {"ok": True, "placeholder": False}
        raw = client.raw_box_text()
        # Text that is still there after the user cleared the box is the application's own
        # placeholder — interface text, not something the user wrote. It is learned as "empty".
        if raw and len(raw) <= 120:
            known = list(memo.get("placeholders") or []) + [raw]
            memo["placeholders"] = known
            client.set_placeholders(known)
            return {"ok": not strict(_snap(client, clock).draft or ""), "placeholder": True,
                    "placeholder_chars": len(raw)}
        return {"ok": False, "placeholder": False, "chars_seen": len(snap.draft)}
    if step == "ime":
        seen, changes, last = False, 0, snap.draft
        for _ in range(10):
            sleep(0.25)
            look = _snap(client, clock)
            seen = seen or bool(look.composing)
            if look.draft != last:
                changes, last = changes + 1, look.draft
        return {"ok": True, "composing_reported": seen, "text_changes": changes}
    if step == "fill":
        return _check_fill(client, snap, clock)
    if step == "focus":
        return {"ok": snap.draft is not None and bool(strict(snap.draft)), "foreground": bool(snap.foreground)}
    if step == "chat":
        before = memo.get("chat_before", "")
        changed = bool(snap.chat_key) and snap.chat_key != before
        return {"ok": changed, "has_key": bool(snap.chat_key)}
    if step == "sessions":
        signals = client.signals()
        nodes = len(signals.texts) if signals is not None else 0
        result = {"ok": nodes > 0, "text_nodes": nodes,
                  "address": bool(signals is not None and signals.url)}
        if context is not None:
            result.update(context.probe())
        return result
    if step == "speed":
        times = []
        for _ in range(30):
            started = clock()
            client.snapshot(started)
            times.append((clock() - started) * 1000)
        times.sort()
        p50, p95 = times[len(times) // 2], times[int(len(times) * 0.95) - 1]
        return {"ok": p95 < SLOW_MS, "p50_ms": round(p50, 2), "p95_ms": round(p95, 2)}
    raise ValueError(f"unknown step: {step}")


def _check_fill(client, snap, clock):
    """Write our marker into the (empty) input box and take it away again.

    The only thing this ever removes is its own marker, from the very box it wrote
    it into. Both writes go through `client.fill`, which re-checks chat, box and
    text at the moment of writing: the clean-up names the ORIGINAL identity and
    expects exactly MARKER. If the user switched chats or typed in the meantime, the
    clean-up is refused and nothing of theirs is touched — whatever is in front now
    is never used as the thing to erase."""
    if snap.draft is None or strict(snap.draft):
        return {"ok": False, "reason": "box_not_empty"}
    key = (snap.window_id, snap.chat_key, snap.box_id)
    started = clock()
    wrote = client.fill(key, "", MARKER)
    after = _snap(client, clock)
    same_box = (after.window_id, after.chat_key, after.box_id) == key
    landed = bool(wrote.ok and same_box and after.draft is not None and strict(after.draft) == strict(MARKER))
    # Take the marker away — also when the write was reported as failed but the marker
    # is in fact there. The host refuses unless it is still that box holding that text.
    back = client.fill(key, MARKER, "")
    final = _snap(client, clock)
    still_there = (final.window_id, final.chat_key, final.box_id) == key
    readable = still_there and final.draft is not None
    # Restored = the host took our marker out of that very box (it checks before it
    # writes), or that box is in front and empty anyway.
    restored = bool(back.ok or (readable and not strict(final.draft)))
    if restored:
        marker_left = False                          # it is gone (whatever the user typed since stays)
    else:
        # Not removable safely: another chat is shown, the text is no longer exactly the
        # marker, or the box cannot be read. It is left alone and the user is told —
        # unless we can see that box and the marker is not in it.
        marker_left = not (readable and MARKER not in final.draft)
    # Somebody else changed things in between: the box in front is a different one, or
    # the marker is there with other text around it.
    interrupted = bool(not same_box or not still_there
                       or (readable and MARKER in final.draft and strict(final.draft) != strict(MARKER)))
    return {"ok": bool(landed and restored), "wrote": bool(wrote.ok), "read_back": landed,
            "restored": restored, "marker_left": marker_left, "interrupted": interrupted,
            "method": wrote.method, "reason": wrote.reason or (back.reason if not restored else ""),
            "ms": round((clock() - started) * 1000, 1),
            # Fatal = our own write left the box in a state we could not undo, with nobody
            # interfering. Interference by the user is "try again", not a broken host.
            "fatal": bool(not restored and not interrupted)}


def capabilities_from(results, platform):
    """The abilities to switch on: what the steps proved, limited to what the client can do."""
    def ok(step):
        return bool(results.get(step, {}).get("ok"))

    caps = set()
    if ok("window") and ok("read") and ok("empty"):
        caps.add(READ)
        if ok("fill") and not results.get("fill", {}).get("fatal"):
            caps.add(FILL)
        if ok("chat"):
            caps.add(CHAT_KEY)
        if ok("sessions"):
            caps.add(SIGNALS)
    return frozenset(caps) & frozenset(platform)


def verdict(results):
    """passed | partial | failed — for the result page and the evidence record."""
    if results.get("fill", {}).get("fatal"):
        return "failed"
    if all(results.get(step, {}).get("ok") for step in ("window", "read", "empty", "fill")):
        return "passed"
    if all(results.get(step, {}).get("ok") for step in ("window", "read", "empty")):
        return "partial"
    return "failed"


# ------------------------------------------------------------------ storage
def _entry_key(client):
    app = ".".join(__version__.split(".")[:2])
    return f"{client.name}|{client.host_version() or 'unknown'}|{app}"


def path_in(folder):
    return Path(folder) / "calibration.json"


def save(folder, client, results, memo=None, wall=time.time):
    data = read_json(path_in(folder), {})
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        data = {"schema": SCHEMA, "entries": {}}
    memo = memo or {}
    entry = {"at": round(wall(), 1), "app": __version__, "host": client.name,
             "host_version": client.host_version(), "verdict": verdict(results), "results": results,
             "capabilities": sorted(capabilities_from(results, client.capabilities())),
             "signature": memo.get("signature"), "placeholders": list(memo.get("placeholders") or [])}
    data["entries"] = {_entry_key(client): entry}       # only the current host version is kept
    write_json(path_in(folder), data)
    return entry


def load(folder, client):
    """The stored calibration for this host version and app version, or None."""
    data = read_json(path_in(folder), {})
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        return None
    entry = (data.get("entries") or {}).get(_entry_key(client))
    return entry if isinstance(entry, dict) else None


def effective(folder, client):
    """(capabilities, entry): what may be used right now."""
    entry = load(folder, client)
    if entry is None:
        return frozenset(), None
    return frozenset(entry.get("capabilities") or ()) & frozenset(client.capabilities()), entry
