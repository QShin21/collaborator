"""From a draft to help: build the request, call the model, validate the answer.

One request answers everything at once: is this a task at all, is the draft ready to
send as it is, should the companion speak, and — depending on the level — three
routes or up to three things the draft still lacks.

The answer is a few lines, one JSON object per line (see prompts/routes.md):

    head                     task / ready / show / reason / pack / assumes
    {"missing": [...]}       hint mode only
    three route heads        approach, deliverable, kind, main, why, hold
    three route bodies       fits, do, deliver, check, next

The complete prompt is put together HERE, not by the model: the user's draft, word
for word, followed by the model's three parts. With `hold` the draft is quoted and
marked "do not do this yet" (the request itself should not be carried out as is).

Validation is strict on purpose. Two near-identical routes, a missing part, no
stated reason at the quiet level, or a route the safety gate drops — all are worse
than no bubble, so they become an error (retry) or silence, never a degraded bubble.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Tuple

from . import checklists, examples, lint, llm
from .jsonutil import as_bool, as_str
from .levels import HINT, LEAD, WALK
from .textutil import cjk_ratio, clean, clip, loose, redact, similarity

log = logging.getLogger(__name__)

PROMPTS = Path(__file__).resolve().parent / "prompts"
PROMPT_FILES = {LEAD: "routes.md", WALK: "routes.md", HINT: "hint.md"}

KINDS = ("verify", "minimal", "adapt", "delegate", "prototype", "plan", "other")
HINT_KINDS = ("deliverable", "check", "constraint", "scope", "data")
PACKS = ("furniture", "files", "software", "other")

APPROACH_MAX = 24          # characters; the prompt asks for far fewer
DELIVERABLE_MAX = 40
FITS_MAX = 120
WHY_MAX = 40
REASON_MAX = 60
LABEL_MAX = 20
CLAUSE_MAX = 120
PART_MAX = 700
TAIL_MIN = 24
TOO_SIMILAR = 0.92

HOLD_TEMPLATE = {"zh": "我本来想这样说：「{draft}」先不要照这句话做。{tail}",
                 "en": "I was going to say: \"{draft}\" Please do not do that yet. {tail}"}


@dataclass(frozen=True)
class Route:
    approach: str            # the name of the route — the bold part of a bubble row
    deliverable: str         # the first thing Codex hands back
    kind: str                # one of KINDS: what makes this route different
    main: bool               # the recommended one (exactly one of three)
    why: str                 # why it is recommended, a statement about the situation
    fits: str                # when to choose this one (preview)
    do: str                  # what Codex should do now
    deliver: str             # what form the result takes
    check: str               # how the user checks or uses it
    next: str                # what to do once the result is there
    hold: bool = False       # the draft should not be carried out as it stands
    caution: Tuple[str, ...] = ()   # safety-gate codes: needs a look at the preview before a fill
    prompt: str = ""         # the complete prompt: the user's words + do + deliver + check

    @property
    def tail(self):
        return join_parts(self.do, self.deliver, self.check)


@dataclass(frozen=True)
class Hint:
    kind: str                # one of HINT_KINDS
    label: str               # what is missing, a few words
    clause: str = ""         # a sentence that can be appended to the draft as it is
    ask: str = ""            # or: something only the user knows — a stub to complete

    @property
    def addition(self):
        return self.clause or self.ask


@dataclass(frozen=True)
class Outcome:
    task: bool = True
    ready: bool = False
    show: bool = False
    reason: str = ""
    routes: Tuple[Route, ...] = ()
    hints: Tuple[Hint, ...] = ()
    assumes: str = ""
    pack: str = "other"
    error: str = ""              # "", or an llm.LLMError kind (config, consent, auth, timeout, unsafe, ...)
    detail: str = ""
    mode: str = LEAD
    prompt_version: str = ""
    used_context: bool = False
    context_turns: int = 0
    redacted: int = 0
    dropped: int = 0             # routes removed by the safety gate
    seconds: float = 0.0
    request: str = ""            # what was sent, for "show me what you used" — memory only

    @property
    def ok(self):
        return not self.error


def failure(kind, detail=""):
    return Outcome(error=kind, detail=clip(detail, 300))


# ----------------------------------------------------------------- text assembly
def is_english(text):
    return cjk_ratio(text) < 0.3


def join_parts(*parts):
    parts = [clean(p) for p in parts if clean(p)]
    if not parts:
        return ""
    return (" " if is_english(parts[0]) else "").join(parts)


def with_stop(text):
    """The draft with sentence-ending punctuation, added only if it has none."""
    text = clean(text)
    if not text or text.endswith(("。", "！", "？", ".", "!", "?", "…", "：", ":")):
        return text
    return text + ("." if is_english(text) else "。")


def compose(draft, tail, hold=False):
    """The complete prompt. The draft is kept character for character."""
    draft = clean(draft)
    english = is_english(draft)
    if hold:
        return HOLD_TEMPLATE["en" if english else "zh"].format(draft=draft, tail=tail)
    return with_stop(draft) + (" " if english else "") + tail


def append(draft, addition):
    """A draft with one more sentence at its end (the hint level's "补一句")."""
    draft = with_stop(draft)
    return draft + (" " if is_english(draft) else "") + clean(addition)


# ----------------------------------------------------------------- request
def prompt_file(mode):
    return PROMPTS / PROMPT_FILES.get(mode, "routes.md")


_VERSION = re.compile(r"<!--\s*version:\s*([\w.\-]+)\s*-->")


def system_prompt(mode=LEAD):
    """(text, version). The first line of a prompt file names its version."""
    text = prompt_file(mode).read_text(encoding="utf-8")
    match = _VERSION.match(text)
    return text, (match.group(1) if match else "")


def build_user_message(draft, mode, chat="", checklist=(), about=(), prefers=(), given=(), pull=False):
    parts = [f"<mode>{mode}</mode>"]
    if pull:
        parts.append("<pull>true</pull>")
    parts.append(f"<draft>\n{clean(draft)}\n</draft>")
    if chat:
        parts.append(f"<chat>\n{chat}\n</chat>")
    else:
        parts.append("<chat>（没有可用的聊天片段：新聊天，或暂时无法确认当前聊天。只根据草稿判断。）</chat>")
    if about:
        parts.append("<about>\n" + "\n".join(f"- {line}" for line in about) + "\n</about>")
    if prefers:
        parts.append(f"<prefers>{', '.join(prefers)}</prefers>")
    if given:
        parts.append("<given>\n" + "\n".join(f"- {name}" for name in given) + "\n</given>")
    if checklist:
        parts.append("<checklist>\n" + "\n".join(f"- {line}" for line in checklist) + "\n</checklist>")
    return "\n\n".join(parts)


# ------------------------------------------------------------------- reply
def _objects(text):
    """The JSON objects of a line-per-object reply. Fences and blank lines are tolerated."""
    found = []
    for line in (text or "").splitlines():
        line = line.strip().rstrip(",")
        if not line or line.startswith("```"):
            continue
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            found.append(value)
    return found


def _hint(item):
    if not isinstance(item, dict):
        return None
    kind = as_str(item.get("kind")).casefold()
    label = clip(clean(as_str(item.get("label"))), LABEL_MAX)
    clause = clean(as_str(item.get("clause")))
    ask = clean(as_str(item.get("ask")))
    if not label or not (clause or ask) or len(clause or ask) > CLAUSE_MAX:
        return None
    return Hint(kind=kind if kind in HINT_KINDS else "scope", label=label,
                clause=clause, ask="" if clause else ask)


def _route(head, body, draft, chat):
    """(Route | None, dropped_by_gate)."""
    approach = clean(as_str(head.get("approach"))).rstrip("。.")
    deliverable = clean(as_str(head.get("deliverable"))).rstrip("。.")
    for lead in ("拿到", "得到", "Get ", "get "):
        if deliverable.startswith(lead):
            deliverable = deliverable[len(lead):].lstrip("：: ")
    do, deliver, check = (clean(as_str(body.get(name))) for name in ("do", "deliver", "check"))
    if not approach or not deliverable or not do or not (deliver or check):
        return None, False
    if max(len(do), len(deliver), len(check)) > PART_MAX:
        return None, False
    tail = join_parts(do, deliver, check)
    if len(tail) < TAIL_MIN:
        return None, False
    finding = lint.check(tail, draft, chat)
    if finding.verdict == lint.DROP:
        return None, True
    kind = as_str(head.get("kind")).casefold()
    hold = as_bool(head.get("hold"), False)
    return Route(
        approach=clip(approach, APPROACH_MAX), deliverable=clip(deliverable, DELIVERABLE_MAX),
        kind=kind if kind in KINDS else "other", main=as_bool(head.get("main"), False),
        why=clip(clean(as_str(head.get("why"))), WHY_MAX), fits=clip(clean(as_str(body.get("fits"))), FITS_MAX),
        do=do, deliver=deliver, check=check, next=clip(clean(as_str(body.get("next"))), FITS_MAX), hold=hold,
        caution=finding.codes if finding.verdict == lint.CAUTION else (),
        prompt=compose(draft, tail, hold)), False


def _valid_set(routes):
    if len(routes) != 3:
        return "expected three complete routes, got %d" % len(routes)
    for i, a in enumerate(routes):
        for b in routes[i + 1:]:
            if loose(a.approach) == loose(b.approach):
                return "two routes share a name"
            if similarity(loose(a.tail), loose(b.tail)) >= TOO_SIMILAR:
                return "two routes say the same thing"
    if len({r.kind for r in routes}) < 2:
        return "the routes are all of one kind"
    mains = [r for r in routes if r.main]
    if len(mains) != 1:
        return "exactly one route must be the main line"
    if not mains[0].why:
        return "the main line has no reason"
    return ""


def parse(text, mode, draft="", chat="", pull=False):
    """Model reply → Outcome. Never raises."""
    objects = _objects(text)
    head = next((o for o in objects if "ready" in o or "show" in o or "task" in o), None)
    if head is None:
        return failure("invalid", "the reply had no head line")
    pack = as_str(head.get("pack")).casefold()
    base = Outcome(task=as_bool(head.get("task"), True), ready=as_bool(head.get("ready"), False),
                   reason=clip(as_str(head.get("reason")), REASON_MAX), mode=mode,
                   assumes=clip(clean(as_str(head.get("assumes"))), WHY_MAX),
                   pack=pack if pack in PACKS else "other")
    if not base.task and not pull:
        return replace(base, ready=False)
    if base.ready and not pull:
        return base
    if mode == HINT and not pull:
        raw = next((o.get("missing") for o in objects if isinstance(o.get("missing"), list)), [])
        hints = tuple(h for h in (_hint(item) for item in raw) if h is not None)[:3]
        # Nothing usable to point at: stay quiet rather than show an empty bubble.
        return replace(base, show=bool(hints), hints=hints)
    heads = {o.get("route"): o for o in objects if "approach" in o and isinstance(o.get("route"), int)}
    bodies = {o.get("route"): o for o in objects if "do" in o and isinstance(o.get("route"), int)}
    wants = as_bool(head.get("show"), default=bool(heads))
    if not wants and not pull:
        return base
    routes, dropped = [], 0
    for index in sorted(heads):
        if index not in bodies:
            continue
        route, gated = _route(heads[index], bodies[index], draft, chat)
        dropped += int(gated)
        if route is not None:
            routes.append(route)
    if dropped and len(routes) < 3:
        return replace(failure("unsafe", f"{dropped} route(s) did not pass the safety gate"), dropped=dropped)
    problem = _valid_set(routes[:3] if len(routes) >= 3 else routes)
    if problem:
        return failure("invalid", problem)
    if mode == WALK and not pull and not base.reason:
        # No concrete problem named: not a critical case. Silence over a generic bubble.
        return base
    return replace(base, show=True, routes=tuple(routes[:3]), dropped=dropped,
                   reason=base.reason if mode == WALK and not pull else "")


def from_example(name):
    """The built-in example as an Outcome (no model involved)."""
    example = examples.get(name)
    routes = tuple(
        Route(approach=r["approach"], deliverable=r["deliverable"], kind=r["kind"], main=r["main"], why=r["why"],
              fits=r["fits"], do=r["do"], deliver=r["deliver"], check=r["check"], next=r["next"],
              prompt=compose(example["draft"], join_parts(r["do"], r["deliver"], r["check"])))
        for r in example["routes"])
    return Outcome(show=True, routes=routes, pack=example["pack"], assumes=example.get("assumes", ""),
                   prompt_version="example")


# ---------------------------------------------------------------- suggester
class Suggester:
    """Runs one analysis. Thread-safe as long as its collaborators are."""

    def __init__(self, model_config: Callable[[], llm.ModelConfig], context=None,
                 complete=llm.complete, timeout=30.0, clock=time.monotonic, permit=None, about=None):
        """`permit(config) -> bool` says whether the user has agreed to send drafts to
        where `config` sends them. It is asked here, for the very configuration the
        request is about to use, so no caller can forget it. None = no gate (tests
        of other things, and callers that already hold the user's explicit yes).
        `about() -> (facts, preferred_kinds)` supplies the user's standing facts."""
        self._model_config = model_config
        self._permit = permit
        self._context = context
        self._complete = complete
        self._timeout = timeout
        self._clock = clock
        self._about = about

    def analyze(self, draft, mode=LEAD, signals=None, use_context=True, pull=False, given=()) -> Outcome:
        started = self._clock()
        config = self._model_config()
        if not config.ready:
            return failure("config", "no model is configured")
        if self._permit is not None and not self._permit(config):
            # Before the chat is even looked at: nothing is gathered for a destination
            # the user has not agreed to.
            return failure("consent", "sending drafts to this destination has not been agreed to")
        chat, turns = "", 0
        if use_context and self._context is not None and signals is not None:
            try:
                found = self._context.excerpt(signals, draft)
            except Exception as error:      # context is optional; never let it block a suggestion
                found = None
                log.warning("chat context failed: %s", error)
            if found is not None:
                chat, turns = found.text, found.turns
        safe_draft, masked_draft = redact(clean(draft))
        safe_chat, masked_chat = redact(chat)
        facts, prefers = self._about() if self._about is not None else ((), ())
        facts = tuple(redact(fact)[0] for fact in facts)
        checklist = checklists.for_text(safe_draft + "\n" + safe_chat)
        system, version = system_prompt(HINT if mode == HINT and not pull else LEAD)
        user = build_user_message(safe_draft, mode, safe_chat, checklist, facts, prefers, given, pull)
        try:
            reply = self._complete(
                config, system, user, timeout=self._timeout,
                max_tokens=700 if mode == HINT and not pull else 2600,
                meta={"draft": safe_draft, "mode": mode, "chat": safe_chat, "pull": pull, "given": tuple(given)})
        except llm.LLMError as error:
            return replace(failure(error.kind, str(error)), seconds=self._clock() - started, mode=mode)
        outcome = parse(reply, mode, safe_draft, safe_chat, pull)
        return replace(outcome, mode=mode, prompt_version=version, used_context=bool(chat), context_turns=turns,
                       redacted=masked_draft + masked_chat, seconds=self._clock() - started, request=user)
