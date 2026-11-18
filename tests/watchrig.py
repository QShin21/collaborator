"""Test rig for watching a conversation: a synthetic conversation, a scripted
supervising model, and a loop that carries out a session's effects.

Everything here is synthetic. Passing these tests shows that the app's own rules do
what the design asks (E1); it says nothing about a real conversation or about the
judgement of a real supervising model."""
import json

import safety  # noqa: F401  (keeps tests away from real user data)

from banxing import llm
from banxing.watch import audit as au
from banxing.watch import session as ss
from banxing.watch import transcript as ts

MOCK = llm.ModelConfig(provider="mock")


def record(title, text, ok=None, kind=ts.COMMAND):
    return ts.Record(kind, title, text, ok)


def conversation(*turns, gaps=(), title=""):
    """Turns as (user, model) or (user, model, [records]) or (user, model, [records], state)."""
    made = []
    for number, item in enumerate(turns, 1):
        records = item[2] if len(item) > 2 else ()
        state = item[3] if len(item) > 3 else ts.DONE
        made.append(ts.make_turn(number, item[0], item[1], records, state))
    return ts.Transcript(turns=tuple(made), gaps=ts.merge_gaps(gaps), source="transcript_file", title=title)


TURNS = (
    ("我想判断新的检索方案（B）是否比现在的（A）更适合我们的客服知识库。", "好的，我们先定评测办法，再比较两套方案。"),
    ("评测只能用线下那份 300 条的标注集，不要动线上数据。", "明白：只用线下的 300 条标注集。"),
    ("先确认两套方案用的是同一份索引。", "确认过了，A 和 B 用的是同一份索引，都是 11 月 3 日构建的。"),
    ("跑一下 A 的基线。", "A 的基线跑完了，结果在下面。",
     [record("python eval.py --system A --set labeled_300", "queries: 300\nrecall@5 = 0.71 (n=300)\ndone", True)]),
    ("再跑 B。", "B 也跑完了。",
     [record("python eval.py --system B --set labeled_300", "queries: 300\nrecall@5 = 0.78 (n=300)\ndone", True)]),
    ("差别在哪类问题上？", "既然两者用的是同一份索引，差别只能来自重排模型；我按问题类型拆了一下，B 在长问题上提升最大。"),
    ("好，继续。", "所以可以确定 B 全面优于 A。我接着写上线方案，并把线上最近一周的查询日志也拉下来做了验证。"),
)


def research(upto=None):
    """Seven turns of comparing two retrieval set-ups. Turn 2: the user's limit. Turn
    3: the main model says the two share an index — nobody ever shows it. Turns 4–5:
    two real runs. Turn 6: the analysis is built on the unchecked remark. Turn 7: a
    conclusion wider than the runs, and the user's limit is ignored."""
    return conversation(*TURNS[:upto])


def extended(*more):
    return conversation(*TURNS, *more)


# Passages of `research()` that a careful supervisor would point at.
GOAL = {"turn": 1, "quote": "新的检索方案（B）是否比现在的（A）更适合我们的客服知识库"}
LIMIT = {"turn": 2, "quote": "只能用线下那份 300 条的标注集，不要动线上数据"}
CLAIM = {"turn": 3, "quote": "A 和 B 用的是同一份索引"}
BASE = {"turn": 4, "quote": "recall@5 = 0.71 (n=300)"}
BETTER = {"turn": 5, "quote": "recall@5 = 0.78 (n=300)"}
BUILT = {"turn": 6, "quote": "差别只能来自重排模型"}
SURE = {"turn": 7, "quote": "可以确定 B 全面优于 A"}
ONLINE = {"turn": 7, "quote": "把线上最近一周的查询日志也拉下来做了验证"}

ASK = "你在第 3 轮说 A 和 B 用的是同一份索引。请贴出两套方案各自的索引路径和构建时间；如果没有实际核对过，请现在核对并给出输出。"


def finding(kind="premise", **fields):
    data = {"kind": kind, "weight": "direction", "title": "「同一份索引」只是说过，后面的归因都靠它",
            "claim": "第 3 轮的说法没有任何记录支持，第 6 轮却据此把差别全部归给了重排模型。",
            "why": "两套方案的索引如果不同，提升可能来自索引而不是重排，结论要重做。",
            "ask": ASK, "probe": "打印两套方案的索引路径和构建时间。", "basis": [CLAIM], "now": [BUILT]}
    data.update(fields)
    return data


def conclusion(*findings, **fields):
    """A supervising model's conclusion, as a dict (see prompts/audit.md)."""
    data = {"v": 1, "goal": {"text": "方案 B 是否比 A 更适合客服知识库", "state": "confirmed", "refs": [GOAL]},
            "anchors": [
                {"text": "评测只用线下 300 条标注集，不动线上数据", "kind": "constraint", "refs": [LIMIT]},
                {"text": "A 和 B 用同一份索引", "kind": "premise", "carries": "第 6 轮的归因", "refs": [CLAIM]},
                {"text": "A 的 recall@5 为 0.71", "kind": "result", "refs": [BASE]},
                {"text": "B 的 recall@5 为 0.78", "kind": "result", "refs": [BETTER]}],
            "findings": list(findings), "resolved": [],
            "sound": "" if findings else "两次评测都有记录，目标没有变。"}
    data.update(fields)
    return data


def look(turns=(), search=(), independent=False):
    """The supervising model asks to see more before it says anything."""
    return {"look": {"turns": list(turns), "search": list(search), "independent": independent}, "why": "先查一下"}


BLIND = {"view": "两次运行的记录只支持「在这 300 条上 B 的 recall@5 高 0.07」。", "next": "先确认两套方案的索引是否相同。",
         "unknowns": ["两套方案的索引是不是同一份"]}


class Scripted:
    """A stand-in for llm.complete that answers from a script and records every call.
    Blind readings are answered separately (`blind`), so a script reads as the audit
    steps alone."""

    def __init__(self, *answers, default=None, blind=None):
        self.answers = list(answers)
        self.default = default if default is not None else conclusion()
        self.blind = BLIND if blind is None else blind
        self.calls = []

    def __call__(self, config, system, user, timeout=30.0, max_tokens=0, meta=None):
        meta = dict(meta or {})
        self.calls.append({"user": user, "system": system, "meta": meta, "config": config})
        if meta.get("task") == "blind":
            answer = self.blind
        else:
            answer = self.answers.pop(0) if self.answers else self.default
        if callable(answer):
            answer = answer(user, meta)
        if isinstance(answer, Exception):
            raise answer
        return answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False)

    def audits(self):
        return [call for call in self.calls if call["meta"].get("task") == "audit"]

    def blinds(self):
        return [call for call in self.calls if call["meta"].get("task") == "blind"]

    def sent_turns(self, index=-1):
        return [turn["number"] for turn in self.audits()[index]["meta"]["turns"]]


class Played:
    """What came of carrying out a batch of effects."""

    def __init__(self):
        self.bubbles, self.hidden, self.notices, self.logs, self.changed, self.audits = [], [], [], [], 0, []

    @property
    def bubble(self):
        return self.bubbles[-1] if self.bubbles else None

    def notice_kinds(self):
        return [notice.kind for notice in self.notices]

    def log(self, event):
        return [entry.data for entry in self.logs if entry.event == event]


def drain(session, auditor, effects, played=None, hold=None):
    """Carry out effects until none is left. With `hold` (a list), audits are not run
    but parked there as jobs — to play "the model is slow"."""
    played = played or Played()
    queue = list(effects)
    while queue:
        effect = queue.pop(0)
        if isinstance(effect, ss.Run):
            if hold is not None:
                hold.append(effect.job)
                continue
            audit = auditor.run(effect.job)
            played.audits.append(audit)
            queue += session.audited(effect.job.id, audit)
        elif isinstance(effect, ss.ShowBubble):
            played.bubbles.append(effect.bubble)
        elif isinstance(effect, ss.HideBubble):
            played.hidden.append(effect.reason)
        elif isinstance(effect, ss.Notice):
            played.notices.append(effect)
        elif isinstance(effect, ss.Log):
            played.logs.append(effect)
        elif isinstance(effect, ss.Changed):
            played.changed += 1
    return played


class Watching:
    """A session with a scripted supervising model, fed a conversation step by step."""

    def __init__(self, *answers, default=None, config=MOCK, first=1, destination=None, permit=None, blind=None):
        self.model = Scripted(*answers, default=default, blind=blind)
        self.config = config
        self.auditor = au.Auditor(lambda: self.config, complete=self.model, permit=permit)
        self.session = ss.Session(destination=config.destination if destination is None else destination, first=first)

    def see(self, transcript, hold=None):
        return drain(self.session, self.auditor, self.session.observe(transcript), hold=hold)

    def do(self, action, *args, hold=None):
        return drain(self.session, self.auditor, getattr(self.session, action)(*args), hold=hold)

    def finish(self, job):
        """Run a held audit now and hand its answer to the session."""
        audit = self.auditor.run(job)
        return drain(self.session, self.auditor, self.session.audited(job.id, audit))

    @property
    def ledger(self):
        return self.session.ledger
