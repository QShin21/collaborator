"""A conversation as the tracker sees it. Pure: no I/O, no clock.

A TURN is one message from the user and everything the main model did in answer to
it. Three kinds of text are kept apart on purpose, because they are not worth the
same as evidence:

  user     what the user wrote — goals, confirmations, changes of mind
  model    what the main model SAID: plans, promises, and reports ("cleaned it")
  record   what HAPPENED: a command and its output, a tool result, a changed file

"The model says the loader works" and "here is the loader's output" are different
things, and `locate` tells them apart: every passage quoted in the ledger is
looked up here, and where it was found decides how much it may prove.
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Optional, Tuple

from ..textutil import clean, fingerprint, redact

USER, MODEL, RECORD = "user", "model", "record"
PARTS = (USER, MODEL, RECORD)
DONE, RUNNING, ABORTED = "done", "running", "aborted"

# Kinds of record.
COMMAND, OUTPUT, FILE, TOOL, ATTACHMENT = "command", "output", "file", "tool", "attachment"
# Kinds of gap: what the watcher could NOT read, so that the ledger can say so.
#   head        the conversation starts earlier than what is available
#   scope       turns the user left out of the range to be read
#   attachment  a file or an image that was part of a message
#   output      a command or tool ran and its output is not in the history
#   branch      the conversation has other branches; only the current one is read
#   compacted   the host replaced older turns by a summary
GAP_KINDS = ("head", "scope", "attachment", "output", "branch", "compacted")

USER_CHARS, MODEL_CHARS, RECORD_CHARS, RECORDS_PER_TURN = 6000, 9000, 3600, 14
QUOTE_MIN, QUOTE_MAX = 6, 240


@dataclass(frozen=True)
class Record:
    kind: str                       # command | output | file | tool | attachment
    title: str = ""                 # the command line, the tool's name, the file's path
    text: str = ""                  # the output; "" when the history does not hold it
    ok: Optional[bool] = None       # exit status, when the source reports one


@dataclass(frozen=True)
class Turn:
    number: int                     # position on the watched branch, from 1
    user: str = ""
    model: str = ""                 # everything the main model said in this turn, in order
    records: Tuple[Record, ...] = ()
    state: str = DONE               # done | running | aborted
    id: str = ""                    # the source's own id for the turn, if it has one

    @property
    def settled(self):
        """The main model is no longer working on this turn (it finished or was stopped)."""
        return self.state != RUNNING

    def part(self, where):
        if where == USER:
            return self.user
        if where == MODEL:
            return self.model
        return "\n".join(f"{r.title}\n{r.text}" for r in self.records)


@dataclass(frozen=True)
class Gap:
    kind: str
    turn: int = 0                   # 0 = the conversation as a whole
    count: int = 1


@dataclass(frozen=True)
class Transcript:
    turns: Tuple[Turn, ...] = ()
    gaps: Tuple[Gap, ...] = ()
    source: str = ""                # chatgpt_export | codex_session | codex_appserver | transcript_file
    conversation_id: str = ""
    title: str = ""

    def turn(self, number):
        for item in self.turns:
            if item.number == number:
                return item
        return None

    @property
    def settled_through(self):
        """Number of the latest turn before which nothing is still running; 0 if none."""
        number = 0
        for turn in self.turns:
            if not turn.settled:
                break
            number = turn.number
        return number

    def upto(self, number):
        """The conversation as it stood when turn `number` had just finished — for
        replaying a conversation one turn at a time."""
        return Transcript(turns=tuple(t for t in self.turns if t.number <= number),
                          gaps=tuple(g for g in self.gaps if g.turn <= number), source=self.source,
                          conversation_id=self.conversation_id, title=self.title)


# ------------------------------------------------------------------ building
def shorten(text, limit):
    """Long text keeps its beginning and its end: a log says what ran at the top and
    how it went at the bottom."""
    text = clean(text)
    if len(text) <= limit:
        return text
    head = limit * 2 // 5
    return text[:head].rstrip() + "\n…〔中间略去 %d 字〕…\n" % (len(text) - limit) + text[-(limit - head):].lstrip()


def make_turn(number, user="", model="", records=(), state=DONE, turn_id=""):
    """A Turn with its texts cleaned and bounded. Too many records keep the first few
    and the latest ones."""
    records = [Record(kind=r.kind, title=shorten(r.title, 400), text=shorten(r.text, RECORD_CHARS), ok=r.ok)
               for r in records if r.title or r.text or r.kind == ATTACHMENT]
    if len(records) > RECORDS_PER_TURN:
        records = records[:4] + records[-(RECORDS_PER_TURN - 4):]
    return Turn(number=number, user=shorten(user, USER_CHARS), model=shorten(model, MODEL_CHARS),
                records=tuple(records), state=state if state in (DONE, RUNNING, ABORTED) else DONE,
                id=str(turn_id or ""))


def renumber(turns):
    return tuple(Turn(number=index, user=t.user, model=t.model, records=t.records, state=t.state, id=t.id)
                 for index, t in enumerate(turns, 1))


def merge_gaps(gaps):
    """One entry per (kind, turn), counts added, in conversation order."""
    totals = {}
    for gap in gaps:
        if gap.kind in GAP_KINDS:
            key = (max(0, int(gap.turn)), gap.kind)
            totals[key] = totals.get(key, 0) + max(1, int(gap.count))
    return tuple(Gap(kind=kind, turn=turn, count=count) for (turn, kind), count in sorted(totals.items()))


# ------------------------------------------------------------------ finding a passage
def _plain(text):
    """What a quotation is compared by: width- and case-folded, without whitespace."""
    text = unicodedata.normalize("NFKC", clean(text)).casefold()
    return "".join(text.split())


def locate(turn, quote):
    """Where in `turn` the passage `quote` really is: RECORD, USER or MODEL — or ""
    when it is not there, is too short to mean anything, or is too long to be a
    quotation. A record is looked at first: if the model repeats a line of a log, the
    log is the stronger source."""
    if turn is None:
        return ""
    wanted = _plain(quote)
    if not QUOTE_MIN <= len(wanted) <= QUOTE_MAX + 60:
        return ""
    for where in (RECORD, USER, MODEL):
        text = turn.part(where)
        if not text:
            continue
        if wanted in _plain(text) or wanted in _plain(redact(text)[0]):
            return where
    return ""


# ------------------------------------------------------------------ identity
def prints(transcript, through=None):
    """A short fingerprint per settled turn, in order. Stored with the ledger (it says
    nothing about the content) to notice that the history under it has changed — an
    edited earlier message, another branch."""
    out = []
    for turn in transcript.turns:
        if through is not None and turn.number > through:
            break
        if not turn.settled:
            break
        out.append(fingerprint(turn.user + "\x1f" + turn.model + "\x1f" + str(len(turn.records))))
    return tuple(out)


def diverged_at(known, transcript):
    """The first turn number whose content is no longer what it was when it was
    reviewed, or 0 when the reviewed history is still there. A conversation that has
    become SHORTER than what was reviewed has diverged as well."""
    now = prints(transcript, through=len(known))
    for index, old in enumerate(known):
        if index >= len(now) or now[index] != old:
            return index + 1
    return 0


# ------------------------------------------------------------------ text for the model
_PART_NAME = {USER: "用户", MODEL: "主模型"}
_RECORD_NAME = {COMMAND: "命令", OUTPUT: "输出", FILE: "文件改动", TOOL: "工具", ATTACHMENT: "附件（读不到内容）"}


def render_turn(turn, evidence_only=False):
    """One turn as the supervising model reads it. With `evidence_only` what the main
    model SAID is left out: only the user's words and what actually happened remain —
    the material for a reading that is not led by the main model's account."""
    parts = []
    if turn.user:
        parts.append(f"[第 {turn.number} 轮 · 用户]\n{turn.user}")
    if turn.model and not evidence_only:
        parts.append(f"[第 {turn.number} 轮 · 主模型]\n{turn.model}")
    for index, record in enumerate(turn.records, 1):
        head = f"[第 {turn.number} 轮 · 记录 {index} · {_RECORD_NAME.get(record.kind, '记录')}]"
        if record.title:
            head += " " + record.title
        if record.ok is not None:
            head += "（成功）" if record.ok else "（失败）"
        parts.append(head + ("\n" + record.text if record.text else "\n（历史里没有这次的输出）"
                             if record.kind in (COMMAND, TOOL) else ""))
    if turn.state == RUNNING:
        parts.append(f"[第 {turn.number} 轮 · 状态] 主模型还在进行中，本轮尚未结束。")
    elif turn.state == ABORTED:
        parts.append(f"[第 {turn.number} 轮 · 状态] 本轮被中断，没有完成。")
    return "\n".join(parts)


def render(turns, evidence_only=False):
    """(text, masked): the turns as the supervising model reads them, with anything
    that looks like a credential masked first."""
    pieces = [render_turn(turn, evidence_only) for turn in turns]
    text, masked = redact("\n\n".join(piece for piece in pieces if piece))
    return text, masked


# ------------------------------------------------------------------ looking things up
@dataclass(frozen=True)
class Hit:
    turn: int
    where: str                      # USER | MODEL | RECORD
    text: str                       # the passage around the match


def search(transcript, term, first=1, through=None, limit=4, width=70):
    """Where `term` occurs in the conversation: the supervising model asks "was this
    ever checked?" and the app looks — here, on this machine. Matching ignores case,
    width and spacing. The earliest and the latest occurrences are kept: where a
    thing was first said, and where it stands now."""
    wanted = _plain(term)
    if len(wanted) < 2:
        return ()
    hits = []
    for turn in transcript.turns:
        if turn.number < first or (through is not None and turn.number > through):
            continue
        for where in (USER, MODEL, RECORD):
            text = clean(turn.part(where))
            if not text or wanted not in _plain(text):
                continue
            folded = unicodedata.normalize("NFKC", text).casefold()
            at = folded.find(unicodedata.normalize("NFKC", clean(term)).casefold())
            at = max(0, at)                 # found only across a line break: show the beginning
            start = max(0, at - width)
            piece = " ".join(text[start:at + len(term) + width].split())
            hits.append(Hit(turn.number, where, ("…" if start else "") + piece))
    if len(hits) > limit:
        half = limit // 2
        hits = hits[:limit - half] + hits[-half:]
    return tuple(hits)


_GAP_TEXT = {
    "head": "这条对话更早的部分读不到（从第 {turn} 轮之前）。",
    "scope": "用户只授权了第 {turn} 轮及之后的内容，更早的没有提供。",
    "attachment": "第 {turn} 轮有 {count} 个附件或图片，读不到内容。",
    "output": "第 {turn} 轮有 {count} 次运行没有留下输出。",
    "branch": "这条对话有别的分支；这里只有当前分支。",
    "compacted": "第 {turn} 轮附近，宿主把更早的内容压缩成了摘要。",
}


def render_gaps(gaps):
    return "\n".join("- " + _GAP_TEXT[g.kind].format(turn=g.turn, count=g.count) for g in gaps if g.kind in _GAP_TEXT)
