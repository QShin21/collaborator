"""One watched conversation over time. Pure and single-threaded: no I/O, no clock,
no model call.

It is fed looks at the conversation and the user's intents, and answers with a list
of effects for the caller to carry out:

    observe(transcript)        a fresh read of the conversation the user chose
    look_now()                 the user asked for an audit now
    audited(job_id, audit)     an audit came back
    took_back / intended / wrong / dismiss      the user answered a bubble
    correct_goal(text)         the user rewrote the question in the ledger
    pause / resume / accept_branch / agree

What is decided here:

  when an audit is due   only when the main model has finished a turn and nothing is
                         running — that is when the user is deciding. A first pass
                         reads the whole range the user allowed, in pieces if it is
                         long; later passes send only the new turns plus the few old
                         ones the ledger's entries point at (the supervisor asks for
                         more itself when it wants to — audit.py).
  what an audit may use  never a turn before `first` (the range the user chose).
  what happens to it     the ledger is replaced only by an audit of this conversation
                         as it still is; a bubble follows nudge.decide.
  when a bubble goes     the user answered it, watching was paused, the conversation
                         moved on, or its history changed under it.
  a changed history      if a turn that was already read is no longer what it was (an
                         edited message, another branch), nothing more is sent until
                         the user confirms what is being watched.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Optional, Tuple

from . import audit as au
from . import ledger as lg
from . import nudge
from . import transcript as ts

CHUNK_CHARS = 48_000        # how much new conversation one request carries at most
HISTORY_TURNS = 8           # how many older turns go along for reference ...
RECENT_TURNS = 3            # ... of which the latest ones, always

IDLE, AUDITING, DIVERGED, NEEDS_CONSENT = "idle", "auditing", "diverged", "consent"


# ------------------------------------------------------------------ effects
@dataclass(frozen=True)
class Run:
    job: au.Job


@dataclass(frozen=True)
class Bubble:
    id: int
    finding: au.Finding
    mark: str                       # the id of the ledger mark that stands for it
    through: int                    # the turn the conversation was at ...
    size: int                       # ... and how many turns it had: either changes → the bubble goes
    manual: bool
    more: int = 0                   # how many other findings are in the ledger


@dataclass(frozen=True)
class ShowBubble:
    bubble: Bubble


@dataclass(frozen=True)
class HideBubble:
    reason: str     # took_back | intended | wrong | dismissed | paused | moved_on | branch | replaced | stopped


@dataclass(frozen=True)
class Changed:
    """The ledger, the usage or the state changed: redraw, and store if asked to."""


@dataclass(frozen=True)
class Notice:
    kind: str                       # watch_clear | watch_error | watch_branch | watch_consent | watch_empty
    detail: str = ""


@dataclass(frozen=True)
class Log:
    event: str
    data: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Usage:
    """What has left this machine for this conversation — numbers only."""
    requests: int = 0
    chars: int = 0
    masked: int = 0
    turns: Tuple[int, ...] = ()     # every turn number whose text was sent at least once

    def plus(self, sent):
        return Usage(requests=self.requests + sent.requests, chars=self.chars + sent.chars,
                     masked=self.masked + sent.masked, turns=tuple(sorted(set(self.turns) | set(sent.turns))))


# ------------------------------------------------------------------ helpers
def reference_turns(ledger, first, limit=HISTORY_TURNS):
    """The older turns worth sending again: the ones the ledger leads back to, the
    shakiest first — the goal, what is broken or unchecked, then the rest — and the
    few turns just before the new ones."""
    ordered = []

    def take(refs):
        for ref in refs:
            if first <= ref.turn <= ledger.through and ref.turn not in ordered:
                ordered.append(ref.turn)

    take(ledger.goal.refs)
    for wanted in (lg.BROKEN, lg.UNCHECKED, lg.BACKED, lg.RETIRED):
        for anchor in ledger.anchors:
            if anchor.status == wanted:
                take(anchor.refs)
    # The last few turns always go along: "these last turns brought nothing new" and
    # "this is no longer the question" can only be seen across several of them.
    recent = [n for n in range(ledger.through, ledger.through - RECENT_TURNS, -1) if n >= first]
    chosen = [n for n in ordered if n not in recent][:max(0, limit - len(recent))] + recent
    return tuple(sorted(chosen))


def chunk(transcript, start, target, budget=None):
    """Turn numbers start..n (n ≤ target) whose text fits one request; at least one."""
    budget = CHUNK_CHARS if budget is None else budget
    numbers, used = [], 0
    for number in range(start, target + 1):
        turn = transcript.turn(number)
        if turn is None:
            continue
        size = len(ts.render_turn(turn))
        if numbers and used + size > budget:
            break
        numbers.append(number)
        used += size
    return tuple(numbers)


# ------------------------------------------------------------------ the session
class Session:
    def __init__(self, destination, first=1, ledger=None, known=(), usage=None, shown=(), muted=None, paused=False):
        """`destination`: the model service the user agreed to for THIS conversation
        (llm.ModelConfig.destination; "" = nothing leaves the machine).
        `first`: the first turn the user allowed to be read."""
        self.destination = destination
        self.first = max(1, int(first))
        self.ledger = ledger or lg.Ledger()
        self.known = tuple(known)               # fingerprints of the turns the ledger has taken in
        self.usage = usage or Usage()
        self.shown = set(shown)                 # keys of findings already shown
        self.muted = dict(muted or {})          # kind -> situation it was turned away in
        self.paused = bool(paused)
        self.state = IDLE
        self.transcript = ts.Transcript()
        self.bubble = None
        self.findings = ()                      # what the latest audit found (also the notes)
        self.last = None                        # the latest usable audit, for the ledger window
        self.error = ""                         # why the last audit failed, if it did
        self._job = None
        self._ids = 0
        self._tried = 0                         # the turn an automatic audit last failed at: not retried by itself

    # ------------------------------------------------------------ reading state
    @property
    def auditing(self):
        return self._job is not None

    def _timely(self, through):
        """Is the decision after turn `through` still open?"""
        turns = self.transcript.turns
        return bool(turns) and turns[-1].settled and self.transcript.settled_through == through

    def _next_id(self):
        self._ids += 1
        return self._ids

    # ------------------------------------------------------------ what is due
    def _job_for(self, mode):
        transcript, target = self.transcript, self.transcript.settled_through
        start = max(self.ledger.through + 1, self.first)
        if target < start:
            if mode != au.MANUAL or target < self.first or not self.ledger.through:
                return None
            new = ()                            # asked again with nothing new: judge from the ledger and its turns
        else:
            new = chunk(transcript, start, target)
            if not new:
                return None
        through = new[-1] if new else self.ledger.through
        history = reference_turns(self.ledger, self.first) if self.ledger.through else ()
        history = tuple(n for n in history if n not in new)
        return au.Job(id=self._next_id(), mode=mode, transcript=transcript, ledger=self.ledger, new=new,
                      history=history, through=through, destination=self.destination, first=self.first,
                      final=through >= target)

    def _start(self, mode):
        job = self._job_for(mode)
        if job is None:
            return []
        self._job, self.state = job, AUDITING
        return [Run(job), Changed()]

    def _hide(self, reason):
        if self.bubble is None:
            return []
        self.bubble = None
        return [HideBubble(reason)]

    def _mute(self, bubble):
        self.muted[bubble.finding.kind] = lg.situation(self.ledger)

    # ------------------------------------------------------------ the world
    def observe(self, transcript):
        """A fresh read of the conversation."""
        if self.first > 1:
            # What the user left out is a gap like any other: the ledger says so.
            transcript = replace(transcript, gaps=ts.merge_gaps(
                tuple(g for g in transcript.gaps if g.kind != "scope") + (ts.Gap("scope", self.first),)))
        self.transcript = transcript
        effects = []
        if self.state == DIVERGED:
            if ts.diverged_at(self.known, transcript):
                return effects
            self.state = IDLE                   # back on the history that was read
            effects.append(Changed())
        elif self.known and ts.diverged_at(self.known, transcript):
            # What was read is no longer there. Do not send the new history, do not
            # judge it by the old ledger: ask the user what is being watched.
            effects += self._hide("branch")
            self._job, self.state = None, DIVERGED
            return effects + [Notice("watch_branch"), Log("watch_branch"), Changed()]
        bubble = self.bubble
        if bubble is not None and (len(transcript.turns) != bubble.size
                                   or transcript.settled_through != bubble.through):
            # The user sent the next message. The old bubble is no longer about an open
            # decision; going on without it counts as an answer.
            if not bubble.manual:
                self._mute(bubble)
            effects += self._hide("moved_on") + [Log("watch_overtaken", {"kind": bubble.finding.kind})]
        if self.paused or self.state == NEEDS_CONSENT or self._job is not None:
            return effects
        turns = transcript.turns
        if not turns or not turns[-1].settled:
            return effects                      # the main model is working: the user has decided; wait
        target = transcript.settled_through
        if target > self.ledger.through and target >= self.first and target != self._tried:
            effects += self._start(au.AUTO)
        return effects

    def look_now(self):
        """The user asked for an audit."""
        if self.state == DIVERGED:
            return [Notice("watch_branch")]
        if self.state == NEEDS_CONSENT:
            return [Notice("watch_consent")]
        if self._job is not None:
            if self._job.mode == au.AUTO:
                self._job = replace(self._job, mode=au.MANUAL)   # the answer will be shown whatever it is
            return []
        effects = self._start(au.MANUAL)
        return effects or [Notice("watch_empty")]

    def audited(self, job_id, audit):
        """An audit came back."""
        # What was handed over is counted whatever becomes of the answer: the usage
        # numbers say what left this machine, not what was useful.
        self.usage = self.usage.plus(audit.sent)
        job = self._job
        if job is None or job.id != job_id:
            # Asked for a state of things that is gone (paused, another branch accepted).
            return [Changed()] if audit.sent.requests else []
        self._job, self.state = None, IDLE
        manual = job.mode == au.MANUAL
        notes, trace = audit.notes or {}, audit.trace
        effects = [Log("watch_audit", {
            "ok": audit.ok, "error": audit.error or "none", "manual": manual, "through": job.through,
            "new": len(job.new), "history": len(job.history), "findings": len(audit.findings),
            "direction": sum(1 for f in audit.findings if f.weight == au.DIRECTION),
            "requests": audit.sent.requests, "chars": audit.sent.chars, "seconds": round(audit.seconds, 2),
            "looks": trace.looks, "looked_turns": len(trace.turns), "searches": trace.searches,
            "searches_found": trace.found, "independent": trace.independent,
            "unlocated": int(notes.get("unlocated", 0)), "weakened": int(notes.get("weakened", 0)),
            "dropped": int(notes.get("dropped", 0)), "prompt": audit.prompt_version or "none"})]
        if ts.diverged_at(self.known, self.transcript):
            return effects + self.observe(self.transcript)       # the history changed meanwhile
        if audit.ledger is not None:
            before = {m.id: m.state for m in self.ledger.marks}
            # What the USER did while the audit ran is not undone by it: their marks and
            # a goal they rewrote stand; the audit only adds what it resolved.
            resolved = {m.id: m for m in audit.ledger.marks if m.state == lg.RESOLVED}
            marks = tuple(resolved.get(m.id, m) if m.state != lg.REJECTED else m for m in self.ledger.marks)
            goal = self.ledger.goal if self.ledger.goal != job.ledger.goal else audit.ledger.goal
            self.ledger = replace(audit.ledger, marks=marks, goal=goal)
            self.known = ts.prints(job.transcript, through=job.through)
            for mark in self.ledger.marks:
                if mark.state == lg.RESOLVED and before.get(mark.id) != lg.RESOLVED:
                    effects.append(Log("watch_resolved", {"kind": mark.kind}))
        if not audit.ok:
            self.error = audit.error
            if audit.error == "consent":
                self.state = NEEDS_CONSENT
                return effects + self._hide("stopped") + [Notice("watch_consent"), Changed()]
            self._tried = self.transcript.settled_through        # not again by itself for this turn
            if manual:
                effects.append(Notice("watch_error", audit.error))
            return effects + [Changed()]
        self.error, self._tried = "", 0
        self.last = audit
        if not job.final:
            return effects + [Changed()] + self._start(job.mode)   # a long history: read on
        self.findings = audit.findings
        action, finding, why = nudge.decide(audit.findings, manual, self._timely(job.through), self.shown,
                                            self.muted, lg.situation(self.ledger))
        effects.append(Log("watch_decided", {"action": action, "why": why}))
        if action == nudge.SHOW:
            effects += self._hide("replaced")
            key = au.finding_key(finding)
            mark = next((m for m in self.ledger.marks if m.key == key and m.state != lg.RESOLVED), None)
            if mark is None:
                self.ledger, mark = lg.raise_mark(self.ledger, finding.kind, finding.title, job.through, key)
            self.bubble = Bubble(id=self._next_id(), finding=finding, mark=mark.id, through=job.through,
                                 size=len(self.transcript.turns), manual=manual, more=len(audit.findings) - 1)
            if not manual:
                self.shown.add(key)
            effects.append(ShowBubble(self.bubble))
        elif action == nudge.NOTE:
            effects.append(Notice("watch_clear", audit.sound))
        effects.append(Changed())
        if self.transcript.settled_through > job.through:
            effects += self.observe(self.transcript)             # something newer arrived while it ran
        return effects

    # ------------------------------------------------------------ the user's answers
    def _answer(self, state, reason, note=""):
        bubble = self.bubble
        if bubble is None:
            return []
        self.ledger = lg.set_mark(self.ledger, bubble.mark, state, note)
        self.shown.add(au.finding_key(bubble.finding))           # answered: not again on the same evidence
        self._mute(bubble)
        return self._hide(reason) + [Log("watch_" + reason, {"kind": bubble.finding.kind, "manual": bubble.manual}),
                                     Changed()]

    def took_back(self):
        """The user copied the question to put it to the main model. Whether it was
        answered shows in the conversation; the supervisor will look for it."""
        return self._answer(lg.ASKED, "took_back")

    def intended(self, note=""):
        """"I know, and I mean it": the user's call. Noted, with their reason if given."""
        return self._answer(lg.INTENDED, "intended", note)

    def wrong(self, note=""):
        """"This does not hold": the supervisor was wrong. Noted, and it is told so."""
        return self._answer(lg.REJECTED, "wrong", note)

    def add_note(self, note):
        """A line of the user's own on the concern they answered last."""
        for mark in reversed(self.ledger.marks):
            if mark.state in (lg.INTENDED, lg.REJECTED, lg.ASKED):
                self.ledger = lg.set_mark(self.ledger, mark.id, mark.state, note)
                return [Changed()]
        return []

    def dismiss(self):
        """Closed without an answer."""
        bubble = self.bubble
        if bubble is None:
            return []
        if not bubble.manual:
            self._mute(bubble)
        return self._hide("dismissed") + [Log("watch_dismissed", {"kind": bubble.finding.kind,
                                                                  "manual": bubble.manual})]

    def correct_goal(self, text):
        """The user rewrote the question. What was said against the old one lapses."""
        before = self.ledger.goal
        self.ledger = lg.correct_goal(self.ledger, text, self.ledger.through)
        if self.ledger.goal == before:
            return []
        self.shown.clear()
        self.muted.clear()
        effects = self._hide("replaced") + [Log("watch_goal_corrected"), Changed()]
        if self.state == IDLE and self._job is None and not self.paused:
            effects += self._start(au.MANUAL)   # look again, under the goal the user just wrote
        return effects

    def pause(self):
        if self.paused:
            return []
        self.paused = True
        self._job = None
        if self.state == AUDITING:
            self.state = IDLE
        return self._hide("paused") + [Log("watch_paused"), Changed()]

    def resume(self):
        if not self.paused:
            return []
        self.paused = False
        return [Log("watch_resumed"), Changed()] + self.observe(self.transcript)

    def accept_branch(self):
        """The user confirmed: watch the conversation as it is now. The ledger starts
        over (a goal the user wrote by hand is kept); nothing of the old branch is
        carried along as if it had been said here."""
        if self.state != DIVERGED:
            return []
        goal = self.ledger.goal if self.ledger.goal.by == "user" else lg.Goal()
        self.ledger = lg.Ledger(goal=replace(goal, since=0))
        self.known, self.shown, self.muted, self.last, self.findings, self._tried = (), set(), {}, None, (), 0
        self.state = IDLE
        return [Log("watch_branch_accepted"), Changed()] + self.observe(self.transcript)

    def agree(self, destination):
        """The user agreed again, to `destination`, for this conversation."""
        self.destination = destination
        if self.state == NEEDS_CONSENT:
            self.state, self.error, self._tried = IDLE, "", 0
        return [Changed()] + self.observe(self.transcript)
