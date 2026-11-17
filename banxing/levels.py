"""How much the companion speaks: three levels, and when it steps back. Pure functions.

  LEAD  带路   every draft that is not ready gets three routes
  HINT  提点   only what the draft still lacks, one tap adds a sentence
  WALK  同行   quiet unless there is a concrete problem to name

Rules:

  A  too many ignores   4 of the last 6 counted feedbacks at this level are
                        "ignore"  ->  one level quieter, automatically. The user
                        is told and can undo it.
  B  already able       5 of the last 6 analysed drafts were ready to send as they
                        were  ->  the companion ASKS whether to say less. Declined:
                        not asked again for 7 days.
  D  the user decides   picking a level in the menu takes effect at once and
                        clears both windows.

Quieter may happen by itself; louder only ever by the user. WALK has no automatic
exit. A message counts once: ignore may become adopt, undoing a fill takes the
adoption back. Prompts that ended without a decision are never passed in here.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Optional, Tuple

LEAD, HINT, WALK = "lead", "hint", "walk"
ORDER = (LEAD, HINT, WALK)
ADOPT, IGNORE = "adopt", "ignore"

WINDOW = 6
THRESHOLD = 4
READY_NEEDED = 5
ASK_AGAIN_AFTER = 7 * 86400.0


@dataclass(frozen=True)
class Feedback:
    message_id: str
    outcome: str
    before: Optional[str] = None


@dataclass(frozen=True)
class Levels:
    level: str = LEAD
    by: str = "default"                    # default | user | auto
    recent: Tuple[Feedback, ...] = ()
    ready: Tuple[bool, ...] = ()           # last analysed task drafts: ready to send as they were?
    declined_at: Optional[float] = None    # when rule B's question was last turned down
    stepped_from: Optional[str] = None     # the level rule A left, while "undo" is on offer
    stepped_at: Optional[float] = None

    @property
    def ignores(self):
        return sum(1 for f in self.recent if f.outcome == IGNORE)

    @property
    def adopts(self):
        return sum(1 for f in self.recent if f.outcome == ADOPT)


def quieter(level):
    index = ORDER.index(level)
    return ORDER[min(index + 1, len(ORDER) - 1)]


def _rule_a(state, now):
    if state.level != WALK and state.ignores >= THRESHOLD:
        return Levels(level=quieter(state.level), by="auto", declined_at=state.declined_at,
                      stepped_from=state.level, stepped_at=now), True
    return state, False


def record(state, message_id, outcome, now=None):
    """Count one feedback. Returns (state, stepped_down)."""
    if outcome not in (ADOPT, IGNORE):
        raise ValueError(f"unknown outcome: {outcome}")
    recent = list(state.recent)
    for index, item in enumerate(recent):
        if item.message_id != message_id:
            continue
        if item.outcome == outcome or item.outcome == ADOPT:
            return state, False
        recent[index] = Feedback(message_id, ADOPT, before=IGNORE)
        return replace(state, recent=tuple(recent)), False
    recent.append(Feedback(message_id, outcome))
    return _rule_a(replace(state, recent=tuple(recent[-WINDOW:])), now)


def cancel_adopt(state, message_id):
    recent = []
    for item in state.recent:
        if item.message_id == message_id and item.outcome == ADOPT:
            if item.before == IGNORE:
                recent.append(Feedback(message_id, IGNORE))
            continue
        recent.append(item)
    return replace(state, recent=tuple(recent))


def note_ready(state, ready, now):
    """One automatically analysed task draft was (not) ready. Returns (state, ask):
    ask is True when rule B says the companion should offer to say less."""
    window = (state.ready + (bool(ready),))[-WINDOW:]
    state = replace(state, ready=window)
    if state.level == WALK or len(window) < WINDOW or sum(window) < READY_NEEDED:
        return state, False
    if state.declined_at is not None and now - state.declined_at < ASK_AGAIN_AFTER:
        return state, False
    return state, True


def accept_offer(state):
    return Levels(level=quieter(state.level), by="user")


def decline_offer(state, now):
    return replace(state, declined_at=now, ready=())


def set_level(state, level):
    if level not in ORDER:
        raise ValueError(f"unknown level: {level}")
    return Levels(level=level, by="user", declined_at=state.declined_at)


def undo_step(state):
    """The user took back rule A's automatic step."""
    if state.stepped_from is None:
        return state
    return Levels(level=state.stepped_from, by="user", declined_at=state.declined_at)


def to_json(state):
    return {"level": state.level, "by": state.by, "ready": list(state.ready),
            "declined_at": state.declined_at, "stepped_from": state.stepped_from, "stepped_at": state.stepped_at,
            "recent": [{"id": f.message_id, "outcome": f.outcome, "before": f.before} for f in state.recent]}


def from_json(data):
    """Reads the current form, and the generation-1 form ({"mode": "every"|"critical"})."""
    if not isinstance(data, dict):
        return Levels()
    level = data.get("level")
    if level not in ORDER:
        level = {"every": LEAD, "critical": WALK}.get(data.get("mode"), LEAD)
        return Levels(level=level, by="user" if level == WALK else "default")
    recent = []
    for item in data.get("recent") or []:
        if isinstance(item, dict) and item.get("outcome") in (ADOPT, IGNORE) and item.get("id"):
            recent.append(Feedback(str(item["id"]), item["outcome"], IGNORE if item.get("before") == IGNORE else None))

    def number(name):
        value = data.get(name)
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    stepped = data.get("stepped_from") if data.get("stepped_from") in ORDER else None
    return Levels(level=level, by=data.get("by") if data.get("by") in ("default", "user", "auto") else "default",
                  recent=tuple(recent[-WINDOW:]),
                  ready=tuple(bool(x) for x in (data.get("ready") or []) if isinstance(x, bool))[-WINDOW:],
                  declined_at=number("declined_at"), stepped_from=stepped, stepped_at=number("stepped_at"))
