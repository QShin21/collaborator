"""The ledger: what a long conversation stands on, kept OUTSIDE that conversation.
Pure: no I/O, no clock.

A model that has said something tends to go on believing it; forty turns later an
unverified remark has become a foundation, and neither the model nor a tired user
re-reads turn seven. The ledger is the supervisor's own memory of the conversation,
written by a different model and checked by this module:

  goal      the question the USER wants answered — in the user's words, or marked as
            still exploring. A plan the main model wrote is not the user's goal.
  anchors   what the work rests on: decisions, premises, constraints, results. For
            each one the app records what backs it —
              record  something that actually ran or was observed
              user    the user said so
              model   only the main model's own word
            and from that its status: `backed`, or `unchecked`. The main model's
            account never backs the main model's claim. This is the rule the whole
            product turns on, and it is decided here, not by either model.
  marks     what became of each concern that was shown to the user: taken back to
            the main model, intended, rejected, or resolved by a later passage.

Every entry must lead back to a passage that is really in the conversation
(`transcript.locate`); where the passage was found is set here and never taken from
the supervising model. Entries are dropped or weakened, never strengthened.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Tuple

from ..jsonutil import as_bool, as_str
from ..textutil import clean, clip, fingerprint, loose
from . import transcript as ts

LEDGER_SCHEMA = 1

CONFIRMED, EXPLORING = "confirmed", "exploring"
DECISION, PREMISE, CONSTRAINT, RESULT = "decision", "premise", "constraint", "result"
ANCHOR_KINDS = (DECISION, PREMISE, CONSTRAINT, RESULT)
BY_RECORD, BY_USER, BY_MODEL = "record", "user", "model"           # what an entry rests on
BACKED, UNCHECKED, BROKEN, RETIRED = "backed", "unchecked", "broken", "retired"
STATUSES = (BACKED, UNCHECKED, BROKEN, RETIRED)
RAISED, ASKED, INTENDED, REJECTED, RESOLVED = "raised", "asked", "intended", "rejected", "resolved"
MARK_STATES = (RAISED, ASKED, INTENDED, REJECTED, RESOLVED)

GOAL_CHARS, ITEM_CHARS, SHORT_CHARS, QUOTE_CHARS = 200, 180, 100, ts.QUOTE_MAX
MAX_ANCHORS, MAX_MARKS, MAX_REFS, MAX_OPEN = 18, 24, 4, 4


@dataclass(frozen=True)
class Ref:
    turn: int
    quote: str
    where: str                     # transcript.USER | MODEL | RECORD — found by this app, not claimed


@dataclass(frozen=True)
class Goal:
    text: str = ""
    state: str = EXPLORING         # confirmed (in the user's own words) | exploring
    open: Tuple[str, ...] = ()     # what is still to be settled about it
    refs: Tuple[Ref, ...] = ()
    by: str = "model"              # "user" once the user rewrote it by hand
    since: int = 0                 # the turn the conversation was at when the user did


@dataclass(frozen=True)
class Anchor:
    text: str
    kind: str = PREMISE            # decision | premise | constraint | result
    backing: str = BY_MODEL        # record | user | model
    status: str = UNCHECKED        # backed | unchecked | broken | retired
    carries: str = ""              # what later work rests on it
    refs: Tuple[Ref, ...] = ()


@dataclass(frozen=True)
class Mark:
    """A concern that was shown, and what became of it."""
    id: str                        # "m3": how the supervising model refers to it later
    kind: str
    title: str
    turn: int                      # the conversation was at this turn when it was shown
    key: Tuple = ()                # the finding's identity (audit.finding_key)
    state: str = RAISED            # raised | asked | intended | rejected | resolved
    note: str = ""                 # the user's own words (optional), or how it was resolved
    refs: Tuple[Ref, ...] = ()     # the passage that resolved it


@dataclass(frozen=True)
class Ledger:
    goal: Goal = Goal()
    anchors: Tuple[Anchor, ...] = ()
    marks: Tuple[Mark, ...] = ()
    gaps: Tuple[ts.Gap, ...] = ()
    through: int = 0               # the last turn this ledger has taken in


# ------------------------------------------------------------------ references
def _text(value, limit):
    return clip(clean(as_str(value)), limit)


def _texts(values, limit, count):
    out = []
    for value in values if isinstance(values, (list, tuple)) else []:
        text = _text(value, limit)
        if text and text not in out:
            out.append(text)
    return tuple(out[:count])


def refs_from(raw, transcript, after=0):
    """(located references, how many were claimed but not found). `after`: only
    passages from later turns count."""
    found, missing = [], 0
    for item in raw if isinstance(raw, (list, tuple)) else []:
        if not isinstance(item, dict):
            continue
        number = item.get("turn")
        quote = " ".join(clean(as_str(item.get("quote"))).split())
        if isinstance(number, bool) or not isinstance(number, int) or not quote:
            missing += 1
            continue
        where = ts.locate(transcript.turn(number), quote) if number > after else ""
        if not where:
            missing += 1
            continue
        ref = Ref(turn=number, quote=clip(quote, QUOTE_CHARS), where=where)
        if ref not in found:
            found.append(ref)
    return tuple(found[:MAX_REFS]), missing


def backing_of(refs):
    places = {ref.where for ref in refs}
    if ts.RECORD in places:
        return BY_RECORD
    return BY_USER if ts.USER in places else BY_MODEL


# ------------------------------------------------------------------ from the supervisor's answer
def _goal(raw, transcript, previous, notes):
    raw = raw if isinstance(raw, dict) else {}
    text = _text(raw.get("text"), GOAL_CHARS)
    if previous.by == "user":
        # The user wrote this goal. Only the user changes it: a passage of their own,
        # later than the correction, and the supervisor saying outright that it moved.
        refs, missing = refs_from(raw.get("refs"), transcript, after=previous.since)
        notes["unlocated"] += missing
        if not (as_bool(raw.get("changed"), False) and text and any(r.where == ts.USER for r in refs)):
            return previous
    else:
        refs, missing = refs_from(raw.get("refs"), transcript)
        notes["unlocated"] += missing
        if not text:
            return previous
    confirmed = any(ref.where == ts.USER for ref in refs)
    wanted = as_str(raw.get("state")).casefold() == CONFIRMED
    if wanted and not confirmed:
        notes["weakened"] += 1
    return Goal(text=text, state=CONFIRMED if wanted and confirmed else EXPLORING,
                open=_texts(raw.get("open"), SHORT_CHARS, MAX_OPEN), refs=refs)


def _anchors(raw, transcript, notes):
    out = []
    for item in raw if isinstance(raw, (list, tuple)) else []:
        if not isinstance(item, dict):
            continue
        text = _text(item.get("text"), ITEM_CHARS)
        refs, missing = refs_from(item.get("refs"), transcript)
        notes["unlocated"] += missing
        if not text or not refs:
            notes["dropped"] += 1                   # an anchor nobody can look up is not on the ledger
            continue
        kind = as_str(item.get("kind")).casefold()
        backing = backing_of(refs)
        claimed = as_str(item.get("status")).casefold()
        against, missing = refs_from(item.get("against"), transcript)
        notes["unlocated"] += missing
        if claimed == BROKEN and any(r.where != ts.MODEL for r in against):
            status, refs = BROKEN, (refs + against)[:MAX_REFS]      # a record or the user says otherwise
        elif claimed == RETIRED and any(r.where == ts.USER for r in against):
            status, refs = RETIRED, (refs + against)[:MAX_REFS]     # only the user lets go of something
        else:
            status = BACKED if backing != BY_MODEL else UNCHECKED
            if claimed and claimed != status:
                notes["weakened"] += 1
        out.append(Anchor(text=text, kind=kind if kind in ANCHOR_KINDS else PREMISE, backing=backing,
                          status=status, carries=_text(item.get("carries"), ITEM_CHARS), refs=refs))
    return tuple(out[:MAX_ANCHORS])


def _resolve(marks, raw, transcript, notes):
    """A concern is resolved only when the conversation shows it — a passage from a
    turn AFTER it was raised, and not merely the main model saying it is fine."""
    marks = list(marks)
    for item in raw if isinstance(raw, (list, tuple)) else []:
        if not isinstance(item, dict):
            continue
        for index, mark in enumerate(marks):
            if mark.id != as_str(item.get("id")) or mark.state in (RESOLVED, REJECTED):
                continue
            refs, missing = refs_from(item.get("refs"), transcript, after=mark.turn)
            notes["unlocated"] += missing
            if refs and backing_of(refs) != BY_MODEL:
                marks[index] = replace(mark, state=RESOLVED, note=_text(item.get("how"), ITEM_CHARS), refs=refs)
            elif refs:
                notes["weakened"] += 1
    return tuple(marks)


def merge(previous, raw, transcript, through):
    """The ledger after one audit. `raw` is the supervising model's JSON object.

    Returns (ledger, notes); notes counts what was not accepted as claimed:
    unlocated (passages not found), weakened (entries lowered), dropped (removed)."""
    notes = {"unlocated": 0, "weakened": 0, "dropped": 0}
    raw = raw if isinstance(raw, dict) else {}
    ledger = Ledger(goal=_goal(raw.get("goal"), transcript, previous.goal, notes),
                    anchors=_anchors(raw.get("anchors"), transcript, notes),
                    marks=_resolve(previous.marks, raw.get("resolved"), transcript, notes),
                    gaps=tuple(g for g in transcript.gaps if g.turn <= through), through=through)
    return ledger, notes


# ------------------------------------------------------------------ the user's own changes
def correct_goal(ledger, text, through):
    """The user rewrote the question the conversation is answering."""
    text = clip(clean(text), GOAL_CHARS)
    if not text:
        return ledger
    return replace(ledger, goal=Goal(text=text, state=CONFIRMED, by="user", since=through))


def raise_mark(ledger, kind, title, turn, key):
    """Note that a concern was shown. Returns (ledger, mark)."""
    numbers = [int(m.id[1:]) for m in ledger.marks if m.id[1:].isdigit()]
    mark = Mark(id=f"m{max(numbers, default=0) + 1}", kind=kind, title=clip(clean(title), SHORT_CHARS),
                turn=turn, key=tuple(key))
    return replace(ledger, marks=(ledger.marks + (mark,))[-MAX_MARKS:]), mark


def set_mark(ledger, mark_id, state, note=""):
    marks = tuple(replace(m, state=state, note=clip(clean(note), ITEM_CHARS) or m.note) if m.id == mark_id else m
                  for m in ledger.marks)
    return replace(ledger, marks=marks)


def open_marks(ledger):
    """Concerns that were raised and are neither resolved nor rejected."""
    return tuple(m for m in ledger.marks if m.state in (RAISED, ASKED, INTENDED))


# ------------------------------------------------------------------ what a change is
def situation(ledger):
    """What "the same situation" means for the bubble: the goal and how the anchors
    stand. When none of this has changed, a concern the user turned away has nothing
    new to say."""
    statuses = [a.status for a in ledger.anchors]
    return (fingerprint(loose(ledger.goal.text)), ledger.goal.state,
            statuses.count(UNCHECKED), statuses.count(BROKEN), statuses.count(BACKED), statuses.count(RETIRED))


# ------------------------------------------------------------------ storage shape
def _refs_json(refs):
    return [{"turn": r.turn, "quote": r.quote, "where": r.where} for r in refs]


def _refs_back(raw):
    out = []
    for item in raw if isinstance(raw, list) else []:
        if (isinstance(item, dict) and isinstance(item.get("turn"), int) and not isinstance(item.get("turn"), bool)
                and item.get("where") in ts.PARTS and isinstance(item.get("quote"), str)):
            out.append(Ref(turn=item["turn"], quote=clip(item["quote"], QUOTE_CHARS), where=item["where"]))
    return tuple(out[:MAX_REFS])


def _key_json(key):
    return [list(part) if isinstance(part, tuple) else part for part in key]


def _key_back(raw):
    return tuple(tuple(part) if isinstance(part, list) else part for part in raw) if isinstance(raw, list) else ()


def to_json(ledger):
    goal = ledger.goal
    return {
        "schema": LEDGER_SCHEMA, "through": ledger.through,
        "goal": {"text": goal.text, "state": goal.state, "open": list(goal.open), "refs": _refs_json(goal.refs),
                 "by": goal.by, "since": goal.since},
        "anchors": [{"text": a.text, "kind": a.kind, "backing": a.backing, "status": a.status, "carries": a.carries,
                     "refs": _refs_json(a.refs)} for a in ledger.anchors],
        "marks": [{"id": m.id, "kind": m.kind, "title": m.title, "turn": m.turn, "key": _key_json(m.key),
                   "state": m.state, "note": m.note, "refs": _refs_json(m.refs)} for m in ledger.marks],
        "gaps": [{"kind": g.kind, "turn": g.turn, "count": g.count} for g in ledger.gaps],
    }


def from_json(data):
    """A stored ledger, or None when the data is not one this version understands. A
    ledger written by a newer version is not guessed at."""
    if not isinstance(data, dict) or data.get("schema") != LEDGER_SCHEMA:
        return None

    def text(value, limit=ITEM_CHARS):
        return clip(clean(value if isinstance(value, str) else ""), limit)

    def number(value):
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0

    def rows(name):
        return [row for row in data.get(name) or [] if isinstance(row, dict)] if isinstance(data.get(name), list) else []

    raw = data.get("goal") if isinstance(data.get("goal"), dict) else {}
    goal = Goal(text=text(raw.get("text"), GOAL_CHARS),
                state=raw.get("state") if raw.get("state") in (CONFIRMED, EXPLORING) else EXPLORING,
                open=_texts(raw.get("open"), SHORT_CHARS, MAX_OPEN), refs=_refs_back(raw.get("refs")),
                by="user" if raw.get("by") == "user" else "model", since=number(raw.get("since")))
    return Ledger(
        goal=goal, through=number(data.get("through")),
        anchors=tuple(Anchor(text=text(a.get("text")), kind=a.get("kind") if a.get("kind") in ANCHOR_KINDS else PREMISE,
                             backing=a.get("backing") if a.get("backing") in (BY_RECORD, BY_USER, BY_MODEL) else BY_MODEL,
                             status=a.get("status") if a.get("status") in STATUSES else UNCHECKED,
                             carries=text(a.get("carries")), refs=_refs_back(a.get("refs")))
                      for a in rows("anchors") if text(a.get("text")))[:MAX_ANCHORS],
        marks=tuple(Mark(id=text(m.get("id"), 8), kind=text(m.get("kind"), 20), title=text(m.get("title"), SHORT_CHARS),
                         turn=number(m.get("turn")), key=_key_back(m.get("key")),
                         state=m.get("state") if m.get("state") in MARK_STATES else RAISED,
                         note=text(m.get("note")), refs=_refs_back(m.get("refs")))
                    for m in rows("marks") if text(m.get("id"), 8))[:MAX_MARKS],
        gaps=ts.merge_gaps(ts.Gap(kind=g.get("kind"), turn=number(g.get("turn")), count=number(g.get("count")) or 1)
                           for g in rows("gaps") if g.get("kind") in ts.GAP_KINDS))
