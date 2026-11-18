"""One audit of the conversation by the supervising model — a different model from
the one doing the work, with no stake in what was said.

It is not handed a context and asked for a verdict. It works in steps, and decides
itself what to look at (prompts/audit.md):

  look         it may ask to re-read older turns and to SEARCH the whole conversation
               for a term. The search runs here, on this machine; "nothing found" is
               an answer too — that is how "nobody ever checked this" is established.
  independent  it may ask for a blind reading: a separate call that sees only the
               user's words and the records, NOT what the main model said, and says
               what the evidence supports (prompts/blind.md). A second model that
               has read forty turns of confident prose is led by it as well; this is
               the one reading that is not.
  conclude     the ledger as it now stands, and findings — or none.

The number of steps is bounded (MAX_LOOKS); every request is counted in `Sent`.

What comes back is checked before anything is shown — silence over a generic bubble:

  * the ledger is rebuilt through `ledger.merge`, which keeps only what can be looked
    up and decides by itself what is backed and what is not;
  * a finding needs a title, what is wrong, what it affects and a question to take
    back — all four — and passages found in the conversation;
  * each kind must show what it claims: drift quotes the USER's goal; a contradiction
    quotes two different turns; an unchecked premise or an overreach quotes what the
    main model said;
  * a finding counts as bearing on the direction only if it can quote the step that
    rests on it NOW (a recent turn); otherwise it is a note in the ledger;
  * every text passes the safety gate (lint.py) against what was in the conversation:
    a foreign address, "send your key to…", download-and-run, "ignore previous
    instructions" → the whole answer is dropped. Chat content is data.

Privacy: the conversation goes only to the configured model service, and only if the
user agreed to have THIS conversation read by THIS destination (`Job.destination`),
checked here for the configuration actually used. An optional `permit` adds the
general consent (used by the replay runner).
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Optional, Tuple

from .. import lint, llm
from ..jsonutil import as_bool, as_str, extract_json
from ..textutil import clean, clip, redact
from . import ledger as lg
from . import transcript as ts

log = logging.getLogger(__name__)

PROMPTS = Path(__file__).resolve().parent.parent / "prompts"
_VERSION = re.compile(r"<!--\s*version:\s*([\w.\-]+)\s*-->")

DRIFT, PREMISE, OVERREACH, CONTRADICTION, NARROWING, STALL = (
    "drift", "premise", "overreach", "contradiction", "narrowing", "stall")
KINDS = (DRIFT, PREMISE, OVERREACH, CONTRADICTION, NARROWING, STALL)
DIRECTION, NOTE = "direction", "note"
AUTO, MANUAL = "auto", "manual"

MAX_LOOKS = 3               # how many times one audit may go back and look
MAX_TURNS, MAX_TERMS = 6, 4
RECENT = 3                  # "now" = within this many turns of the latest one
BLIND_CHARS = 24_000
TITLE_CHARS, PART_CHARS, ASK_CHARS, VIEW_CHARS = 48, 260, 360, 500
MAX_FINDINGS = 5


@dataclass(frozen=True)
class Finding:
    kind: str
    title: str
    claim: str                              # what is off
    why: str                                # what it would change
    ask: str                                # a question the user can take back to the main model
    probe: str = ""                         # the cheapest way to find out
    basis: Tuple[lg.Ref, ...] = ()
    now: Tuple[lg.Ref, ...] = ()            # the step that rests on it at present
    weight: str = NOTE                      # direction | note
    caution: Tuple[str, ...] = ()

    @property
    def text(self):
        return "\n".join((self.title, self.claim, self.why, self.ask, self.probe))


def finding_key(finding):
    """What "the same concern on the same evidence" is compared by."""
    return (finding.kind, tuple(sorted({ref.turn for ref in finding.basis})))


@dataclass(frozen=True)
class Sent:
    """What one audit handed to the model service — numbers only."""
    requests: int = 0
    turns: Tuple[int, ...] = ()
    chars: int = 0
    masked: int = 0

    def plus(self, other):
        return Sent(self.requests + other.requests, tuple(sorted(set(self.turns) | set(other.turns))),
                    self.chars + other.chars, self.masked + other.masked)


@dataclass(frozen=True)
class Trace:
    """What the supervisor went and looked at during one audit — numbers only."""
    looks: int = 0
    turns: Tuple[int, ...] = ()
    searches: int = 0
    found: int = 0                          # how many of the searches found something
    independent: bool = False


@dataclass(frozen=True)
class Job:
    """One audit to run. Carries everything the call needs, so that it can run on
    another thread without touching the session."""
    id: int
    mode: str                               # auto | manual
    transcript: ts.Transcript               # for looking passages up; never sent whole
    ledger: lg.Ledger
    new: Tuple[int, ...]                    # the turns to add this time
    history: Tuple[int, ...]                # older turns sent along for reference
    through: int
    destination: str                        # what the user agreed to for this conversation
    first: int = 1                          # the first turn the user allowed to be read
    final: bool = True                      # False while catching up on a long history


@dataclass(frozen=True)
class Audit:
    ledger: Optional[lg.Ledger] = None      # None = this audit brought no usable ledger
    findings: Tuple[Finding, ...] = ()
    sound: str = ""                         # why the direction holds, when nothing was found
    notes: dict = field(default_factory=dict)
    error: str = ""                         # "", or an llm.LLMError kind / "unsafe" / "invalid" / "consent"
    detail: str = ""
    prompt_version: str = ""
    seconds: float = 0.0
    sent: Sent = Sent()
    trace: Trace = Trace()
    independent: str = ""                   # the blind reading, if one was made (memory only)
    request: str = ""                       # the last request, for "what did you use" — memory only

    @property
    def ok(self):
        return not self.error


def failure(kind, detail="", **more):
    return Audit(error=kind, detail=clip(detail, 300), **more)


def system_prompt(name="audit"):
    text = (PROMPTS / f"{name}.md").read_text(encoding="utf-8")
    match = _VERSION.match(text)
    return text, (match.group(1) if match else "")


# ------------------------------------------------------------------ requests
_MARK_STATE = {lg.RAISED: "用户已看到，尚未处理", lg.ASKED: "用户已带回去问主模型", lg.INTENDED: "用户说这是有意如此",
               lg.REJECTED: "用户认为不成立", lg.RESOLVED: "已解决"}


def _marks_text(ledger):
    lines = []
    if ledger.goal.by == "user":
        lines.append(f"- 用户亲手把目标改写为：{ledger.goal.text}（在第 {ledger.goal.since} 轮之后）")
    for mark in ledger.marks:
        lines.append(f"- {mark.id} [{mark.kind}] 第 {mark.turn} 轮后提出：{mark.title} —— {_MARK_STATE[mark.state]}"
                     + (f"；{mark.note}" if mark.note else ""))
    return "\n".join(lines)


def _ledger_text(ledger):
    if not ledger.through:
        return "（还没有账本：这是第一次读这条对话。）"
    data = lg.to_json(ledger)
    for name in ("schema", "marks", "gaps"):
        data.pop(name, None)
    return json.dumps(data, ensure_ascii=False)


def looked_text(transcript, turns, searches, first, through):
    """(text, masked, turn numbers used, how many searches found something): what the
    supervisor asked to see, looked up on this machine."""
    parts, used, found = [], set(), 0
    wanted = [t for t in (transcript.turn(n) for n in turns) if t is not None]
    if wanted:
        parts.append("\n\n".join(ts.render_turn(turn) for turn in wanted))
        used.update(turn.number for turn in wanted)
    for term in searches:
        hits = ts.search(transcript, term, first=first, through=through)
        if not hits:
            parts.append(f"[搜「{term}」] 在允许读的范围内没有找到。")
            continue
        found += 1
        who = {ts.USER: "用户", ts.MODEL: "主模型", ts.RECORD: "记录"}
        parts.append("\n".join(f"[搜「{term}」→ 第 {hit.turn} 轮 · {who[hit.where]}] {hit.text}" for hit in hits))
        used.update(hit.turn for hit in hits)
    text, masked = redact("\n\n".join(parts))
    return text, masked, tuple(sorted(used)), found


def build_user_message(job, looked="", independent="", looks_left=0):
    """(text, masked). Only the turns named in the job are included."""
    transcript = job.transcript
    history, masked_old = ts.render([t for t in (transcript.turn(n) for n in job.history) if t is not None])
    fresh, masked_new = ts.render([t for t in (transcript.turn(n) for n in job.new) if t is not None])
    gaps = ts.render_gaps(g for g in transcript.gaps if g.turn <= job.through)
    parts = [f"<mode>{job.mode}</mode>", f"<ledger>\n{_ledger_text(job.ledger)}\n</ledger>"]
    marks = _marks_text(job.ledger)
    if marks:
        parts.append(f"<marks>\n{marks}\n</marks>")
    if gaps:
        parts.append(f"<gaps>\n{gaps}\n</gaps>")
    if history:
        parts.append(f"<history>\n{history}\n</history>")
    parts.append(f"<new>\n{fresh or '（没有新增的轮次：用户要求再看一遍。）'}\n</new>")
    if looked:
        parts.append(f"<looked>\n{looked}\n</looked>")
    if independent:
        parts.append(f"<independent>\n{independent}\n</independent>")
    span = f"第 {job.new[0]}–{job.new[-1]} 轮" if job.new else "无"
    parts.append(f"<range>已读到第 {job.through} 轮；本次新增：{span}；允许读的范围从第 {job.first} 轮开始。</range>")
    if not job.final:
        parts.append("<steps>后面还有更多轮次。这一步只更新账本：不要回头查，findings 给空数组。</steps>")
    elif looks_left > 0:
        parts.append(f"<steps>还可以回头查 {looks_left} 次。</steps>")
    else:
        parts.append("<steps>不能再查了：这一步必须给出结论。</steps>")
    return "\n\n".join(parts), masked_old + masked_new


def build_blind_message(job):
    """(text, masked, turn numbers): the user's words and the records — nothing the
    main model said."""
    question = job.ledger.goal.text
    turns = [t for t in job.transcript.turns if job.first <= t.number <= job.through]
    if not question:
        question = next((clip(t.user, 300) for t in turns if t.user), "")
    kept, used = [], 0
    for turn in reversed(turns):                    # the latest evidence first; older until it no longer fits
        piece = ts.render_turn(turn, evidence_only=True)
        if kept and used + len(piece) > BLIND_CHARS:
            break
        if piece:
            kept.append(turn)
            used += len(piece)
    kept.reverse()
    evidence, masked = ts.render(kept, evidence_only=True)
    settled = "\n".join(f"- {a.text}" for a in job.ledger.anchors if a.status == lg.BACKED)
    parts = [f"<question>\n{redact(question)[0]}\n</question>"]
    if settled:
        parts.append(f"<settled>\n{redact(settled)[0]}\n</settled>")
    parts.append(f"<evidence>\n{evidence}\n</evidence>")
    return "\n\n".join(parts), masked, tuple(t.number for t in kept)


def parse_blind(text):
    """The blind reading as a short text for the auditor, or "" when it is unusable."""
    raw = extract_json(text)
    if not isinstance(raw, dict):
        return ""
    view = clip(clean(as_str(raw.get("view"))), VIEW_CHARS)
    following = clip(clean(as_str(raw.get("next"))), VIEW_CHARS)
    if not view and not following:
        return ""
    unknowns = [clip(clean(as_str(item)), 160) for item in (raw.get("unknowns") or [])[:4]
                if isinstance(raw.get("unknowns"), list) and clean(as_str(item))]
    lines = [f"证据支持的结论：{view}", f"它会选的下一步：{following}"]
    lines += [f"还缺的事实：{item}" for item in unknowns]
    return "\n".join(lines)


# ------------------------------------------------------------------ the answer
def parse_look(raw, job):
    """What the supervisor asks to see, or None. Turn numbers outside the range the
    user allowed are not honoured."""
    look = raw.get("look") if isinstance(raw, dict) else None
    if not isinstance(look, dict):
        return None
    turns = tuple(sorted({n for n in look.get("turns") or [] if isinstance(n, int) and not isinstance(n, bool)
                          and job.first <= n <= job.through})[:MAX_TURNS]) if isinstance(look.get("turns"), list) else ()
    terms = []
    for term in look.get("search") if isinstance(look.get("search"), list) else []:
        term = clip(clean(as_str(term)), 40)
        if len(term) >= 2 and term not in terms:
            terms.append(term)
    return {"turns": turns, "search": tuple(terms[:MAX_TERMS]), "independent": as_bool(look.get("independent"), False)}


def _finding(raw, transcript, through, notes, haystack):
    """(Finding | None, problem). problem "unsafe" = the safety gate dropped it."""
    raw = raw if isinstance(raw, dict) else {}
    kind = as_str(raw.get("kind")).casefold()
    title = clip(clean(as_str(raw.get("title"))).rstrip("。."), TITLE_CHARS)
    claim, why = (clip(clean(as_str(raw.get(name))), PART_CHARS) for name in ("claim", "why"))
    ask = clip(clean(as_str(raw.get("ask"))), ASK_CHARS)
    if kind not in KINDS or not (title and claim and why and ask):
        return None, "incomplete"
    basis, missing = lg.refs_from(raw.get("basis"), transcript)
    notes["unlocated"] += missing
    now, missing = lg.refs_from(raw.get("now"), transcript)
    notes["unlocated"] += missing
    if not basis:
        return None, "no passage"
    every = basis + now
    places = {ref.where for ref in every}
    if kind == DRIFT and ts.USER not in places:
        return None, "drift without the user's own words"
    if kind == CONTRADICTION and len({ref.turn for ref in every}) < 2 and len({ref.where for ref in every}) < 2:
        return None, "a contradiction needs two passages"
    if kind in (PREMISE, OVERREACH) and ts.MODEL not in places:
        return None, "nothing the main model said is quoted"
    current = tuple(ref for ref in now if ref.turn >= through - RECENT)
    weight = DIRECTION if as_str(raw.get("weight")).casefold() == DIRECTION and current else NOTE
    finding = Finding(kind=kind, title=title, claim=claim, why=why, ask=ask,
                      probe=clip(clean(as_str(raw.get("probe"))), PART_CHARS), basis=basis, now=now, weight=weight)
    gate = lint.check(finding.text, "", haystack)
    if gate.verdict == lint.DROP:
        return None, "unsafe"
    return replace(finding, caution=gate.codes if gate.verdict == lint.CAUTION else ()), ""


def parse(raw, job, haystack=""):
    """The supervisor's conclusion (already a dict) → Audit. Never raises."""
    if not isinstance(raw, dict) or not ("findings" in raw or "anchors" in raw or "goal" in raw):
        return failure("invalid", "the reply was not a conclusion")
    ledger, notes = lg.merge(job.ledger, raw, job.transcript, job.through)
    audit = Audit(ledger=ledger, notes=notes, sound=clip(clean(as_str(raw.get("sound"))), PART_CHARS))
    if not job.final:
        return audit                                # still catching up: the ledger only
    findings = []
    for item in raw.get("findings") if isinstance(raw.get("findings"), list) else []:
        finding, problem = _finding(item, job.transcript, job.through, notes, haystack)
        if problem == "unsafe":
            return failure("unsafe", "a finding did not pass the safety gate")
        if finding is None:
            notes["dropped"] += 1
        elif finding_key(finding) not in {finding_key(f) for f in findings}:
            findings.append(finding)
    findings.sort(key=lambda f: f.weight != DIRECTION)       # what bears on the direction first; order kept otherwise
    return replace(audit, findings=tuple(findings[:MAX_FINDINGS]))


# ------------------------------------------------------------------ the call
def _turns_meta(transcript, numbers):
    out = []
    for number in numbers:
        turn = transcript.turn(number)
        if turn is not None:
            out.append({"number": turn.number, "user": turn.user, "model": turn.model, "state": turn.state,
                        "records": [{"kind": r.kind, "title": r.title, "text": r.text, "ok": r.ok}
                                    for r in turn.records]})
    return out


class Auditor:
    """Runs one audit. Thread-safe as long as its collaborators are."""

    def __init__(self, model_config: Callable[[], llm.ModelConfig], complete=llm.complete, timeout=120.0,
                 clock=time.monotonic, permit=None):
        self._model_config = model_config
        self._complete = complete
        self._timeout = timeout
        self._clock = clock
        self._permit = permit

    def _blind(self, config, job):
        """(text, Sent). A reading that has not seen what the main model said."""
        user, masked, numbers = build_blind_message(job)
        sent = Sent(1, numbers, len(user), masked)
        system, _ = system_prompt("blind")
        try:
            reply = self._complete(config, system, user, timeout=self._timeout, max_tokens=900, meta={
                "task": "blind", "turns": [dict(turn, model="") for turn in _turns_meta(job.transcript, numbers)]})
        except llm.LLMError:
            return "", sent
        return parse_blind(reply), sent

    def run(self, job) -> Audit:
        started = self._clock()
        config = self._model_config()
        if not config.ready:
            return failure("config", "no model is configured")
        if self._permit is not None and not self._permit(config):
            return failure("consent", "sending text to this destination has not been agreed to")
        if config.destination != job.destination:
            # The user agreed to have this conversation read by one named service. It is
            # not the one that would be used now: nothing is sent until they agree again.
            return failure("consent", "the supervising service is not the one this conversation was agreed for")
        system, version = system_prompt("audit")
        sent, looked, independent = Sent(), [], ""
        seen_turns, searches, found, looks, asked_terms = set(), 0, 0, 0, set()
        masked_looked = 0
        result = None
        while result is None:
            left = MAX_LOOKS - looks if job.final else 0
            user, masked = build_user_message(job, "\n\n".join(looked), independent, left)
            numbers = tuple(sorted(set(job.history) | set(job.new) | seen_turns))
            sent = sent.plus(Sent(1, numbers, len(user), masked + masked_looked))
            try:
                reply = self._complete(config, system, user, timeout=self._timeout, max_tokens=4200, meta={
                    "task": "audit", "mode": job.mode, "final": job.final, "looks_left": left,
                    "turns": _turns_meta(job.transcript, numbers), "new": list(job.new),
                    "ledger": lg.to_json(job.ledger) if job.ledger.through else None,
                    "looked": "\n\n".join(looked), "independent": independent,
                    "gaps": [g.kind for g in job.transcript.gaps if g.turn <= job.through]})
            except llm.LLMError as error:
                result = failure(error.kind, str(error))
                break
            raw = extract_json(reply)
            look = parse_look(raw, job) if left > 0 else None
            if look is None:
                result = replace(parse(raw, job, haystack=user), request=user)
                break
            # The supervisor wants to see more before it says anything. Fetch it here.
            looks += 1
            turns = tuple(n for n in look["turns"] if n not in job.history and n not in job.new and n not in seen_turns)
            terms = tuple(t for t in look["search"] if t not in asked_terms)
            asked_terms.update(terms)
            if turns or terms:
                text, masked_more, used, hits = looked_text(job.transcript, turns, terms, job.first, job.through)
                looked.append(text)
                masked_looked += masked_more
                seen_turns.update(used)
                searches += len(terms)
                found += hits
            if look["independent"] and not independent:
                independent, cost = self._blind(config, job)
                sent = sent.plus(cost)
        trace = Trace(looks=looks, turns=tuple(sorted(seen_turns)), searches=searches, found=found,
                      independent=bool(independent))
        return replace(result, sent=sent, trace=trace, independent=independent, prompt_version=version,
                       seconds=self._clock() - started)
