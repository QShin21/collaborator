"""The follow state machine: when to analyse, when to show, when to count.

Pure and single-threaded. It is fed observations and user intents and answers
with a list of effects for the shell to carry out:

    observe(snapshot)            a fresh look at the Codex input box (every poll)
    result(request_id, outcome)  a model answer arrived
    pick / copy / dismiss        the user acted on a routes bubble
    apply_hint / want_routes     the user acted on a hints bubble
    more                         "another three" for the same draft (once)
    fill_done(token, ok, ...)    the platform finished (or refused) a fill
    undo / request_now / retry / manual / show_example
    set_paused / set_level / undo_step / answer_offer

Three identities are checked before anything is shown or filled — the chat, the
input box and the draft — so a late answer or a stale click can never land on
the wrong text. Time is whatever the snapshots say; nothing here sleeps.

Vocabulary:
  key       (window, chat, input box) — where a draft lives
  message   everything typed into one box until it is sent or cleared; feedback
            is counted once per message
  bubble    kind "routes" (three routes) or "hints" (what the draft lacks).
            source "auto" (the companion spoke up) or "pull" (the user asked) —
            these follow the draft and vanish with it; "manual" (pasted draft),
            "example" (built-in) and "spare" (routes that arrived after the draft
            was sent) stay until closed and never count as feedback.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, replace
from typing import Dict, List, Optional, Tuple

from . import levels as lv
from . import suggest
from .drafts import analyzable, substantial_change
from .geometry import Rect
from .suggest import Outcome
from .textutil import fingerprint, strict

Key = Tuple[str, str, str]
FOLLOWING = ("auto", "pull")       # bubbles tied to the draft in the box
FILL_TIMEOUT = 8.0                 # seconds before an unanswered fill is given up
SPARE_SECONDS = 600.0              # how long routes that arrived too late stay on offer
HALT_AFTER = 3                     # consecutive automatic failures ...
HALT_WINDOW = 600.0                # ... within this many seconds stop automatic requests


@dataclass(frozen=True)
class Tuning:
    pause: float = 1.5             # stillness before a draft is analysed
    ignore_after: float = 2.0      # bubble on screen this long before "sent anyway" counts
    max_pending: int = 3


@dataclass(frozen=True)
class Snapshot:
    """One look at Codex. `draft is None` means the box could not be read."""
    at: float = 0.0
    foreground: bool = False       # a Codex window is in front (our own menus count as "still Codex")
    window_id: str = ""
    chat_key: str = ""             # changes when another chat is shown; "" if it cannot be told
    box_id: str = ""
    draft: Optional[str] = None
    composing: bool = False        # an input method is mid-composition
    window_rect: Optional[Rect] = None
    box_rect: Optional[Rect] = None
    send_rect: Optional[Rect] = None
    problem: str = ""              # why there is no draft: no_window | no_box | unreadable | error |
    #                                uncalibrated | stalled


# ------------------------------------------------------------------ effects
@dataclass(frozen=True)
class Analyze:
    request_id: int
    draft: str
    mode: str          # lead | hint | walk
    source: str
    pull: bool = False
    given: tuple = ()


@dataclass(frozen=True)
class Bubble:
    id: int
    source: str
    key: Optional[Key]
    draft: str
    kind: str                      # routes | hints
    routes: tuple = ()
    hints: tuple = ()
    reason: str = ""
    mode: str = lv.LEAD
    message_id: str = ""
    shown_at: float = 0.0
    assumes: str = ""
    basis: Tuple[bool, int, int] = (False, 0, 0)   # used the chat, how many turns, how many secrets masked
    request: str = ""              # what was sent to the model (memory only)
    more_left: bool = False        # "another three" is still available


@dataclass(frozen=True)
class ShowBubble:
    bubble: Bubble


@dataclass(frozen=True)
class HideBubble:
    reason: str


@dataclass(frozen=True)
class Fill:
    token: int
    key: Key
    expect: str        # what must be in the box for the fill to go ahead
    text: str
    purpose: str       # adopt | append | restore


@dataclass(frozen=True)
class CopyText:
    text: str
    why: str           # copy | no_fill


@dataclass(frozen=True)
class OfferCopy:
    text: str
    reason: str        # mismatch | failed | ...


@dataclass(frozen=True)
class NeedPreview:
    """A route carries a safety caution: show its preview; the fill needs a second click."""
    index: int


@dataclass(frozen=True)
class Ask:
    """A question to the user that changes nothing unless answered: "quieter"."""
    kind: str


@dataclass(frozen=True)
class Prefer:
    """A route kind was chosen (+1) or turned down (-1): for the preference weights."""
    kind: str
    delta: int


@dataclass(frozen=True)
class Status:
    state: str         # away | idle | thinking | error | paused | blind | setup
    detail: str = ""


@dataclass(frozen=True)
class Notice:
    kind: str
    detail: str = ""


@dataclass(frozen=True)
class Persist:
    pass


@dataclass(frozen=True)
class AskPaste:
    pass


@dataclass(frozen=True)
class Log:
    event: str
    data: dict = field(default_factory=dict)


# ------------------------------------------------------------------ internals
@dataclass
class _Pending:
    key: Optional[Key]
    draft: str
    fp: str
    started: float
    mode: str
    source: str
    pull: bool = False
    more: bool = False


@dataclass
class _FillRecord:
    prompt: str        # what the companion last wrote into the box
    original: str      # the user's own draft before any of it
    message_id: str
    counted: bool
    settled: bool = False


@dataclass
class _Filling:
    token: int
    key: Key
    expect: str
    text: str
    purpose: str
    started: float
    bubble_id: int = 0
    index: int = -1
    source: str = ""
    message_id: str = ""
    addition: str = ""


@dataclass
class _Offer:
    message_id: str
    counted: bool


class _Message:
    """One message being composed in one box."""

    def __init__(self):
        self.id = uuid.uuid4().hex[:12]
        self.handled: List[str] = []           # drafts already answered (shown, declined, filled)
        self.failed = set()                    # fingerprints whose analysis failed
        self.cache: Dict[str, Outcome] = {}
        self.fill: Optional[_FillRecord] = None
        self.more_used = False

    def remember(self, text):
        if strict(text) and not self.seen(text):
            self.handled.append(text)
            del self.handled[:-12]

    def seen(self, text):
        return any(not substantial_change(old, text) for old in self.handled)


class FollowMachine:
    def __init__(self, levels=None, tuning=None, model_ready=True, paused=False,
                 fill_supported=True, wall=time.time):
        self.levels = levels or lv.Levels()
        self.tuning = tuning or Tuning()
        self.model_ready = model_ready
        self.paused = paused
        self.fill_supported = fill_supported
        self._wall = wall
        self.snap: Optional[Snapshot] = None
        self.key: Optional[Key] = None
        self.bubble: Optional[Bubble] = None
        self.status = Status("away")
        self.halted = False                     # automatic requests stopped after repeated failures
        self._last_text = ""
        self._stable_since = 0.0
        self._messages: Dict[Key, _Message] = {}
        self._pending: Dict[int, _Pending] = {}
        self._filling: Optional[_Filling] = None
        self._offer: Optional[_Offer] = None
        self._sent: Dict[Key, Tuple[str, float]] = {}       # key -> (fingerprint of what was sent, when)
        self._spare = None                      # (key, draft, outcome, mode, expires)
        self._failures: List[float] = []
        self._asking = False
        self._counter = 0

    # ------------------------------------------------------------ helpers
    def _next(self):
        self._counter += 1
        return self._counter

    def _message(self, key, create=True):
        message = self._messages.get(key)
        if message is None and create:
            message = self._messages[key] = _Message()
            for old in list(self._messages)[:-8]:       # a handful of chats is plenty
                del self._messages[old]
        return message

    def _set_status(self, out, state, detail=""):
        if state in ("idle", "away", "blind", "setup") and any(
                p.source == "manual" for p in self._pending.values()):
            state, detail = "thinking", ""
        status = Status(state, detail)
        if status != self.status:
            self.status = status
            out.append(status)

    def _drop_bubble(self, out, reason):
        if self.bubble is not None:
            self.bubble = None
            out.append(HideBubble(reason))

    def _hide_following(self, out, reason):
        """The draft the bubble belonged to is gone from view: an unfinished prompt."""
        bubble = self.bubble
        if bubble is not None and bubble.source in FOLLOWING:
            out.append(Log("incomplete", {"reason": reason, "source": bubble.source, "bubble": bubble.kind,
                                          "shown_for": round(self._now() - bubble.shown_at, 2)}))
            self._drop_bubble(out, reason)

    def _now(self):
        return self.snap.at if self.snap is not None else 0.0

    def _feedback(self, out, message_id, outcome):
        before = self.levels
        state, stepped = lv.record(before, message_id, outcome, now=self._wall())
        if state != before:
            self.levels = state
            out.append(Persist())
            out.append(Log("feedback", {"outcome": outcome, "level": before.level,
                                        "ignores": state.ignores, "adopts": state.adopts}))
        if stepped:
            out.append(Log("level", {"level": state.level, "by": "auto"}))
            out.append(Notice("stepped_down", state.level))

    def _pending_for(self, key, fp):
        return any(p.key == key and p.fp == fp for p in self._pending.values())

    def _start(self, out, key, text, source, now, pull=False, given=(), more=False):
        request_id = self._next()
        mode = self.levels.level
        self._pending[request_id] = _Pending(key, text, fingerprint(text), now, mode, source, pull, more)
        for old in list(self._pending)[:-self.tuning.max_pending]:
            del self._pending[old]                  # its answer will be ignored
        # Status first: an executor may answer the Analyze effect on the spot.
        self._set_status(out, "thinking")
        out.append(Log("analyze", {"source": source, "level": mode, "pull": pull, "chars": len(text)}))
        out.append(Analyze(request_id, text, mode, source, pull, tuple(given)))

    def _readable(self):
        snap = self.snap
        return bool(snap and snap.foreground and snap.draft is not None and snap.box_id and self.key)

    # ------------------------------------------------------------ observation
    def observe(self, snap: Snapshot) -> list:
        out: list = []
        self.snap = snap
        if self._filling is not None:
            if snap.at - self._filling.started < FILL_TIMEOUT:
                return out                           # judge nothing while a fill is in flight
            out.extend(self.fill_done(self._filling.token, False, "timeout", snap.at))
        if self.paused:
            self._drop_bubble(out, "paused")
            self._set_status(out, "paused" if snap.foreground else "away")
            return out
        if not snap.foreground:
            self._hide_following(out, "app_switch")
            self._set_status(out, "away")
            return out
        if snap.draft is None or not snap.box_id:
            self._hide_following(out, "lost")
            self.key = None
            self._set_status(out, "blind", snap.problem)
            return out

        key = (snap.window_id, snap.chat_key, snap.box_id)
        text = snap.draft
        if key != self.key:
            self._hide_following(out, "chat_switch")
            self.key, self._last_text, self._stable_since = key, text, snap.at
        elif text != self._last_text:
            previous, self._last_text, self._stable_since = self._last_text, text, snap.at
            if not strict(text) and strict(previous):
                # The box emptied in one step: that draft was (most likely) sent.
                self._sent[key] = (fingerprint(previous), snap.at)
                for old in list(self._sent)[:-8]:
                    del self._sent[old]
            bubble = self.bubble
            if bubble is not None and bubble.source in FOLLOWING and strict(text) != strict(bubble.draft):
                if not strict(text) and strict(previous) == strict(bubble.draft):
                    self._sent_with_bubble(out, bubble, snap.at)
                else:
                    self._hide_following(out, "edited")

        if not strict(text):
            # Sent or cleared: this message is over. The next text starts a new one.
            self._messages.pop(key, None)
            self._set_status(out, "idle")
            return out
        self._settle_fill(key, text)
        thinking = any(p.key == key for p in self._pending.values())
        if snap.composing:
            self._stable_since = snap.at
            self._set_status(out, "thinking" if thinking else "idle")
            return out
        if self.bubble is not None and self.bubble.source in FOLLOWING:
            self._set_status(out, "idle")
            return out
        fp = fingerprint(text)
        if self._pending_for(key, fp):
            self._set_status(out, "thinking")
            return out
        message = self._message(key)
        if fp in message.failed:
            self._set_status(out, "error", self.status.detail if self.status.state == "error" else "")
            return out
        if snap.at - self._stable_since < self.tuning.pause or not analyzable(text)[0] or message.seen(text):
            self._set_status(out, "idle")
            return out
        if not self.model_ready:
            self._set_status(out, "setup")
            return out
        if self.halted:
            self._set_status(out, "error", "halted")
            return out
        self._start(out, key, text, "auto", snap.at)
        return out

    def expire(self, now) -> list:
        """Called by the shell's watchdog: give up a fill nobody answered, even when
        no snapshots arrive to move time forward."""
        if self._filling is not None and now - self._filling.started >= FILL_TIMEOUT:
            return self.fill_done(self._filling.token, False, "timeout", now)
        return []

    def _settle_fill(self, key, text):
        """The first look after a fill: remember the text the way the editor
        actually holds it (it may have tidied spacing or quotes), so that "restore"
        still recognises it. Later edits by the user are not adopted this way."""
        message = self._messages.get(key)
        record = message.fill if message is not None else None
        if record is not None and not record.settled:
            record.settled = True
            if not substantial_change(record.prompt, text):
                record.prompt = text

    def _sent_with_bubble(self, out, bubble, now):
        shown_for = now - bubble.shown_at
        counted = bubble.source == "auto" and shown_for >= self.tuning.ignore_after
        out.append(Log("sent_original", {"shown_for": round(shown_for, 2), "counted": counted,
                                         "bubble": bubble.kind}))
        if counted:
            self._feedback(out, bubble.message_id, lv.IGNORE)
        self._drop_bubble(out, "sent")

    # ------------------------------------------------------------ model answers
    def _failed(self, out, now):
        self._failures = [at for at in self._failures if now - at < HALT_WINDOW] + [now]
        if len(self._failures) >= HALT_AFTER and not self.halted:
            self.halted = True
            out.append(Log("halted", {"failures": len(self._failures)}))

    def result(self, request_id, outcome: Outcome, now) -> list:
        out: list = []
        pending = self._pending.pop(request_id, None)
        if pending is None:
            return out
        if pending.source in ("manual", "example"):
            return self._free_result(out, pending, outcome, now)
        snap = self.snap
        current = (not self.paused and self._readable() and self.key == pending.key
                   and strict(snap.draft) == strict(pending.draft))
        wait = round(now - pending.started, 2)
        if not current:
            # Edited, sent or switched while the model was thinking: an unfinished prompt.
            sent = self._sent.get(pending.key)
            spare = bool(sent and sent[0] == pending.fp and not outcome.error and outcome.show and outcome.routes)
            if spare:
                # The very draft was sent before the routes arrived. Keep them on offer:
                # "that could also have been asked like this".
                self._spare = (pending.key, pending.draft, outcome, pending.mode, now + SPARE_SECONDS)
                out.append(Notice("spare"))
            out.append(Log("late", {"source": pending.source, "wait": wait, "spare": spare}))
            self._set_status(out, "paused" if self.paused else "idle")
            return out
        message = self._message(pending.key)
        if outcome.error:
            message.failed.add(pending.fp)
            if pending.source == "auto":
                self._failed(out, now)
            self._set_status(out, "error", "halted" if self.halted else outcome.error)
            out.append(Log("error", {"kind": outcome.error, "wait": wait}))
            return out
        self._failures, self.halted = [], False
        message.remember(pending.draft)
        data = {"source": pending.source, "level": pending.mode, "wait": wait, "pull": pending.pull,
                "context": outcome.used_context, "turns": outcome.context_turns,
                "prompt": outcome.prompt_version}
        if pending.source == "auto" and outcome.task:
            self._note_ready(out, outcome.ready)
        if not outcome.show:
            self._set_status(out, "idle")
            out.append(Log("standby", dict(data, ready=outcome.ready, task=outcome.task)))
            if pending.source == "pull" or pending.pull:
                out.append(Notice("nothing", outcome.reason))
            elif outcome.task and outcome.ready:
                out.append(Notice("ready"))
            return out
        if outcome.routes:
            message.cache[pending.fp] = outcome
        self._show(out, pending.source, pending.key, pending.draft, outcome, pending.mode, message.id, now,
                   more_left=not pending.more and not message.more_used)
        out.append(Log("shown", dict(data, bubble=self.bubble.kind)))
        return out

    def _note_ready(self, out, ready):
        self.levels, ask = lv.note_ready(self.levels, ready, self._wall())
        out.append(Persist())
        if ask and not self._asking:
            self._asking = True
            out.append(Ask("quieter"))

    def _free_result(self, out, pending, outcome, now):
        self._set_status(out, self.status.state if self.status.state != "thinking" else "idle")
        if outcome.error:
            out.append(Notice("error", outcome.error))
        elif not outcome.show or not outcome.routes:
            out.append(Notice("nothing", outcome.reason))
        else:
            self._show(out, pending.source, None, pending.draft, outcome, pending.mode, "", now)
        return out

    def _show(self, out, source, key, draft, outcome, mode, message_id, now, more_left=False):
        self._drop_bubble(out, "replaced")
        self._spare = None
        self.bubble = Bubble(
            id=self._next(), source=source, key=key, draft=draft,
            kind="routes" if outcome.routes else "hints", routes=tuple(outcome.routes), hints=tuple(outcome.hints),
            reason=outcome.reason, mode=mode, message_id=message_id, shown_at=now, assumes=outcome.assumes,
            basis=(outcome.used_context, outcome.context_turns, outcome.redacted), request=outcome.request,
            more_left=more_left and source in FOLLOWING and bool(outcome.routes))
        out.append(ShowBubble(self.bubble))
        self._set_status(out, "idle")

    def bubble_shown(self, bubble_id, now):
        """The view reports the moment the bubble was fully on screen."""
        if self.bubble is not None and self.bubble.id == bubble_id:
            self.bubble = replace(self.bubble, shown_at=now)

    # ------------------------------------------------------------ user intents
    def dismiss(self, now) -> list:
        out: list = []
        bubble = self.bubble
        if bubble is None:
            return out
        out.append(Log("dismissed", {"source": bubble.source, "bubble": bubble.kind,
                                     "shown_for": round(now - bubble.shown_at, 2)}))
        if bubble.source == "auto":
            self._feedback(out, bubble.message_id, lv.IGNORE)
            for route in bubble.routes:
                out.append(Prefer(route.kind, -1))
        self._drop_bubble(out, "dismissed")
        return out

    def pick(self, index, now, confirmed=False) -> list:
        """A click on a route: put its prompt into the Codex input box."""
        bubble = self.bubble
        if bubble is None or self._filling is not None or not 0 <= index < len(bubble.routes):
            return []
        route = bubble.routes[index]
        if route.caution and not confirmed:
            return [NeedPreview(index)]
        if not (self.fill_supported and self._readable()):
            return self._take_by_copy(bubble, index, "no_fill")
        if bubble.source in FOLLOWING:
            key, expect = bubble.key, bubble.draft
        else:
            key, expect = self.key, self.snap.draft
        token = self._next()
        self._filling = _Filling(token, key, expect, route.prompt, "adopt", now, bubble.id, index,
                                 bubble.source, bubble.message_id or self._message(key).id)
        return [Fill(token, key, expect, route.prompt, "adopt")]

    def copy(self, index, now) -> list:
        """"复制" in the preview: the user takes the prompt without a fill."""
        bubble = self.bubble
        if bubble is None or not 0 <= index < len(bubble.routes):
            return []
        return self._take_by_copy(bubble, index, "copy")

    def _take_by_copy(self, bubble, index, why):
        route = bubble.routes[index]
        out: list = [CopyText(route.prompt, why),
                     Log("copied", {"index": index, "source": bubble.source, "why": why})]
        if bubble.source in FOLLOWING:
            message = self._message(bubble.key)
            message.remember(route.prompt)
            self._feedback(out, bubble.message_id, lv.ADOPT)
            out.append(Prefer(route.kind, 1))
        self._drop_bubble(out, "adopted")
        return out

    def apply_hint(self, index, now) -> list:
        """A click on "what is missing": add that one sentence to the end of the draft."""
        bubble = self.bubble
        if bubble is None or bubble.kind != "hints" or self._filling is not None \
                or not 0 <= index < len(bubble.hints):
            return []
        addition = bubble.hints[index].addition
        if not (self.fill_supported and self._readable()):
            out: list = [CopyText(addition, "no_fill"), Log("hint_copied", {"index": index})]
            self._feedback(out, bubble.message_id, lv.ADOPT)
            self._without_hint(out, bubble, index, bubble.draft)
            return out
        token = self._next()
        text = suggest.append(bubble.draft, addition)
        self._filling = _Filling(token, bubble.key, bubble.draft, text, "append", now, bubble.id, index,
                                 bubble.source, bubble.message_id, addition)
        return [Fill(token, bubble.key, bubble.draft, text, "append")]

    def _without_hint(self, out, bubble, index, draft):
        """The bubble after one of its hints was used."""
        left = bubble.hints[:index] + bubble.hints[index + 1:]
        if not left:
            self._drop_bubble(out, "adopted")
            return
        self.bubble = replace(bubble, hints=left, draft=draft)
        out.append(ShowBubble(self.bubble))

    def want_routes(self, now) -> list:
        """"要三条完整路线" at the bottom of a hints bubble."""
        out: list = []
        bubble = self.bubble
        if bubble is None or bubble.kind != "hints" or not self._readable():
            return out
        if bubble.source in FOLLOWING:
            self._feedback(out, bubble.message_id, lv.ADOPT)
        self._drop_bubble(out, "replaced")
        if not self.model_ready:
            return out + [Notice("setup")]
        self._start(out, self.key, self.snap.draft, "pull", now, pull=True)
        return out

    def more(self, now) -> list:
        """"换一批": once per message, three other routes for the same draft."""
        out: list = []
        bubble = self.bubble
        if bubble is None or not bubble.more_left or not self._readable() or not self.model_ready:
            return out
        message = self._message(bubble.key)
        message.more_used = True
        given = tuple(route.approach for route in bubble.routes)
        out.append(Log("more", {"source": bubble.source}))
        self._drop_bubble(out, "replaced")
        self._start(out, bubble.key, bubble.draft, bubble.source, now, pull=True, given=given, more=True)
        return out

    def fill_done(self, token, ok, reason="", now=None, method="") -> list:
        out: list = []
        filling = self._filling
        if filling is None or filling.token != token:
            return out
        self._filling = None
        now = self._now() if now is None else now
        message = self._message(filling.key)
        if filling.purpose == "restore":
            if ok:
                record, message.fill = message.fill, None
                self._last_text, self._stable_since = filling.text, now
                if record is not None and record.counted:
                    self.levels = lv.cancel_adopt(self.levels, record.message_id)
                    out.append(Persist())
                out.extend([Notice("restored"), Log("restored", {"method": method})])
            else:
                out.append(Notice("restore_failed", reason))
            return out
        bubble = self.bubble if self.bubble is not None and self.bubble.id == filling.bubble_id else None
        counted = filling.source in FOLLOWING
        if ok and filling.purpose == "append":
            if message.fill is None:
                message.fill = _FillRecord(filling.text, filling.expect, filling.message_id, counted)
            else:                                   # a second sentence: the original stays the first draft
                message.fill.prompt, message.fill.settled = filling.text, False
            message.remember(filling.expect)
            message.remember(filling.text)
            self._last_text, self._stable_since = filling.text, now
            out.append(Log("hint_applied", {"index": filling.index, "method": method}))
            if counted:
                self._feedback(out, filling.message_id, lv.ADOPT)
            if bubble is not None:
                self._without_hint(out, bubble, filling.index, filling.text)
            out.append(Notice("appended"))
            return out
        if ok:
            message.fill = _FillRecord(filling.text, filling.expect, filling.message_id, counted)
            message.remember(filling.expect)
            message.remember(filling.text)          # the filled prompt leaves this round of analysis
            self._last_text, self._stable_since = filling.text, now
            out.append(Log("adopted", {"index": filling.index, "source": filling.source, "method": method}))
            if counted:
                self._feedback(out, filling.message_id, lv.ADOPT)
                if bubble is not None and 0 <= filling.index < len(bubble.routes):
                    out.append(Prefer(bubble.routes[filling.index].kind, 1))
            if bubble is not None:
                self._drop_bubble(out, "adopted")
            out.append(Notice("filled"))
            return out
        out.append(Log("fill_failed", {"reason": reason, "source": filling.source, "purpose": filling.purpose}))
        if reason == "mismatch" and bubble is not None and bubble.source in FOLLOWING:
            # The draft, box or chat changed under the bubble: the suggestion is stale.
            self._drop_bubble(out, "stale")
        self._offer = _Offer(filling.message_id, counted)
        out.append(OfferCopy(filling.addition if filling.purpose == "append" else filling.text,
                             reason or "failed"))
        return out

    def offer_copied(self, now) -> list:
        """The user copied the text offered after a failed fill."""
        out: list = []
        offer, self._offer = self._offer, None
        if offer is None:
            return out
        out.append(Log("copied", {"why": "fill_failed"}))
        if offer.counted:
            self._feedback(out, offer.message_id, lv.ADOPT)
        if self.bubble is not None and self.bubble.source in FOLLOWING and self.bubble.kind == "routes":
            self._drop_bubble(out, "adopted")
        return out

    def can_undo(self):
        if not self._readable() or self._filling is not None:
            return False
        message = self._message(self.key, create=False)
        return bool(message and message.fill and strict(self.snap.draft) == strict(message.fill.prompt))

    def undo(self, now) -> list:
        """"恢复原草稿": only while what the companion wrote is still what is in the box."""
        if not self.can_undo():
            return [Notice("restore_unavailable")]
        out: list = []
        if self.bubble is not None and self.bubble.source in FOLLOWING:
            self._drop_bubble(out, "restored")
        record = self._message(self.key).fill
        token = self._next()
        self._filling = _Filling(token, self.key, record.prompt, record.original, "restore", now)
        return out + [Fill(token, self.key, record.prompt, record.original, "restore")]

    def has_spare(self, now=None):
        spare = self._spare
        if spare is None:
            return False
        if now is not None and now > spare[4]:
            self._spare = None
            return False
        return True

    def request_now(self, now) -> list:
        """The user clicked the companion: three routes for the current draft, whatever the level."""
        out: list = []
        if self.bubble is not None:
            return out
        if self.paused:
            return [Notice("paused")]
        if self.has_spare(now) and (not self._readable() or not strict(self.snap.draft)):
            _, draft, outcome, mode, _ = self._spare
            self._show(out, "spare", None, draft, outcome, mode, "", now)
            out.append(Log("spare_opened"))
            return out
        if not self._readable() or not strict(self.snap.draft):
            return [AskPaste()]
        text = self.snap.draft
        ok, why = analyzable(text)
        if not ok:
            return [Notice("too_" + why)]
        message, fp = self._message(self.key), fingerprint(text)
        cached = message.cache.get(fp)
        if cached is not None and cached.show and cached.routes:
            self._show(out, "pull", self.key, text, cached, self.levels.level, message.id, now,
                       more_left=not message.more_used)
            return out
        if not self.model_ready:
            return [Notice("setup")]
        if not self._pending_for(self.key, fp):
            message.failed.discard(fp)
            self._start(out, self.key, text, "pull", now, pull=True)
        return out

    def retry(self, now) -> list:
        """After a timeout or a bad reply: analyse the same draft again."""
        out: list = []
        self.halted, self._failures = False, []
        if self._readable():
            message = self._message(self.key, create=False)
            if message is not None:
                message.failed.discard(fingerprint(self.snap.draft))
            self._stable_since = min(self._stable_since, now - self.tuning.pause)
            out.extend(self.observe(self.snap))
        return out

    def manual(self, draft, now) -> list:
        """A draft pasted into the companion: same analysis, no input box involved."""
        out: list = []
        ok, why = analyzable(draft)
        if not ok:
            return [Notice("too_" + why)]
        if not self.model_ready:
            return [Notice("setup")]
        self._start(out, None, draft, "manual", now, pull=True)
        return out

    def show_example(self, draft, outcome: Outcome, now) -> list:
        out: list = []
        self._show(out, "example", None, draft, outcome, lv.LEAD, "", now)
        return out

    def set_paused(self, paused, now) -> list:
        out: list = []
        self.paused = bool(paused)
        if self.paused:
            self._pending.clear()
            self._drop_bubble(out, "paused")
            self._set_status(out, "paused" if self.snap and self.snap.foreground else "away")
        else:
            self._stable_since = now
            self._set_status(out, "idle" if self.snap and self.snap.foreground else "away")
        return out

    def set_level(self, level) -> list:
        if level == self.levels.level and self.levels.stepped_from is None:
            return []
        self.levels = lv.set_level(self.levels, level)
        return [Persist(), Log("level", {"level": level, "by": "user"})]

    def undo_step(self) -> list:
        """"改回去" after the companion stepped back by itself."""
        if self.levels.stepped_from is None:
            return []
        self.levels = lv.undo_step(self.levels)
        return [Persist(), Log("level", {"level": self.levels.level, "by": "undo"})]

    def answer_offer(self, accept) -> list:
        """The user's answer to "shall I say less?"."""
        if not self._asking:
            return []
        self._asking = False
        if accept:
            self.levels = lv.accept_offer(self.levels)
            return [Persist(), Log("level", {"level": self.levels.level, "by": "offer"})]
        self.levels = lv.decline_offer(self.levels, self._wall())
        return [Persist(), Log("offer_declined")]

    def set_model_ready(self, ready) -> list:
        out: list = []
        self.model_ready = bool(ready)
        if ready and self.status.state == "setup":
            self._set_status(out, "idle")
        return out

    def menu(self) -> dict:
        """What the menu needs to know."""
        return {"paused": self.paused, "level": self.levels.level,
                "ignores": self.levels.ignores, "adopts": self.levels.adopts,
                "stepped_from": self.levels.stepped_from,
                "can_undo": self.can_undo(), "can_retry": self.status.state == "error",
                "has_bubble": self.bubble is not None, "state": self.status.state,
                "model_ready": self.model_ready, "readable": self._readable(),
                "spare": self._spare is not None, "halted": self.halted}
