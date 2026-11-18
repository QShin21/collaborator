"""Offline stand-in for the supervising model. Plumbing, not evidence.

Keyword rules over the turns it is handed — just enough to play every step of an
audit (looking back, searching, asking for a blind reading, concluding with or
without findings) in the tests, the self-test and the replay runner without a
network. It quotes real passages, so the ledger's look-ups have something to find,
but it understands nothing: a conversation phrased differently gets a different,
meaningless answer. Nothing about the quality of supervision may be concluded from
it. Reached only through mock.complete, i.e. only when the provider is "mock".
"""
from __future__ import annotations

import json
import re

from ..textutil import clean

_PIVOT = ("改成先", "转向", "换个目标", "我决定改", "不做这个了", "instead, let", "change the goal", "pivot to")
_EXPLORING = ("还没想好", "先随便看看", "先探索", "not sure yet", "just exploring")
_CONSTRAINT = ("必须", "不要", "不能", "只能", "不许", "must ", "never ", "do not ", "only ")
# (what the main model says, the word to search the conversation for)
_CLAIMS = (("已去重", "去重"), ("已经去重", "去重"), ("已验证", "验证"), ("已经验证", "验证"), ("已经确认", "确认"),
           ("没有泄漏", "泄漏"), ("deduplicated", "dedup"), ("already verified", "verif"), ("has been validated", "valid"))
_PASSED = ("检查通过", "核对通过", "读取成功", "全部通过", "无错误", "去重完成", "no errors", "passed", "rows removed")
_BUILDS_ON = ("基于", "在此基础上", "既然已经", "既然数据", "接着用", "building on", "given that", "on top of that")
_WANDERS = ("顺便", "另外我还", "我转而", "不如先做", "我先去做", "while we are at it", "i went ahead and", "i also started")
_STRONG = ("证明了", "显著优于", "毫无疑问", "可以确定", "明显更好", "proves", "clearly better", "definitely", "conclusively")
_THIN = ("单次", "一次运行", "只跑了一次", "seed=", "1 run", "single run", "n=1")
_ALLOWED = ("短训练", "试跑", "小试", "跑一下", "smoke", "dry run", "short run", "quick run")
_CONFIRM = ("可以", "就用", "同意", "先用", "go ahead", "fine to", "ok to")
_POLISH = ("统计", "细分", "清洗", "重排", "润色", "再补", "分布", "statistics", "breakdown", "polish", "reorganiz")
_SPLIT = re.compile(r"[。！？!?\n]|(?<=[a-z0-9\"')])\.\s")


def _has(text, cues):
    folded = (text or "").casefold()
    return next((cue for cue in cues if cue in folded), "")


def _long_enough(piece):
    return len("".join(piece.split())) >= 6


def _passage(text, cue=""):
    """A short verbatim piece of `text` around `cue` (or its first sentence)."""
    text = clean(text)
    if not text:
        return ""
    pieces = [piece.strip() for piece in _SPLIT.split(text) if piece and piece.strip()]
    if cue:
        for index, piece in enumerate(pieces):
            if cue not in piece.casefold():
                continue
            if _long_enough(piece):
                return piece[:80].strip()
            start = text.find(pieces[index - 1]) if index else text.find(piece)
            end = text.find(piece, start) + len(piece)
            if start >= 0 and end > start and _long_enough(text[start:end]):
                return text[start:end][-80:].strip()
    for piece in pieces:
        if _long_enough(piece):
            return piece[:80].strip()
    return text[:80].strip()


def _ref(turn, text, cue=""):
    quote = _passage(text, cue)
    return {"turn": turn["number"], "quote": quote} if quote else None


def _records(turn):
    return "\n".join(f"{r.get('title') or ''}\n{r.get('text') or ''}" for r in turn.get("records") or [])


def blind(meta):
    """A reading from the user's words and the records only (the main model's words
    were removed before this was called)."""
    turns = meta.get("turns") or []
    passed = next((_passage(_records(t), _has(_records(t), _PASSED)) for t in reversed(turns)
                   if _has(_records(t), _PASSED)), "")
    if passed:
        view = f"记录里有一处通过的检查（{passed}）；除此之外的结论没有记录支持。"
    else:
        view = "材料里没有任何运行记录，现有结论都还只是说法。"
    return json.dumps({"view": view, "next": "先把最关键的那个说法实际跑一次，贴出输出。",
                       "unknowns": ["关键前提有没有被实际检查过"]}, ensure_ascii=False)


def audit(meta):
    turns = meta.get("turns") or []
    new = set(meta.get("new") or [])
    previous = meta.get("ledger") or {}
    looked = meta.get("looked") or ""
    final = meta.get("final", True)
    out = {"v": 1, "anchors": [], "findings": [], "resolved": [], "sound": ""}

    # ---- goal: the user's opening statement, until the USER changes it
    goal = previous.get("goal") if isinstance(previous.get("goal"), dict) and previous["goal"].get("text") else None
    opening = next((t for t in turns if t.get("user")), None)
    pivot = next((t for t in reversed(turns) if t["number"] in new and _has(t["user"], _PIVOT)
                  and (goal is not None or t is not opening)), None)
    if goal is None or pivot is not None:
        source = pivot or opening
        if source is not None:
            ref = _ref(source, source["user"], _has(source["user"], _PIVOT) if pivot else "")
            goal = {"text": ref["quote"] if ref else "", "refs": [ref] if ref else [], "changed": pivot is not None,
                    "state": "exploring" if _has(source["user"], _EXPLORING) else "confirmed"}
    out["goal"] = goal or {}

    # ---- anchors: what the user required, what the main model claimed, what a record shows
    anchors, claims, allowed = {}, [], None
    for item in (previous.get("anchors") or []) if pivot is None else []:
        if item.get("refs"):
            anchors[item["text"]] = dict(item, refs=[dict(turn=r["turn"], quote=r["quote"]) for r in item["refs"]])
            if item.get("kind") == "premise" and item.get("status") == "unchecked":
                term = next((term for cue, term in _CLAIMS if cue in item["text"].casefold()), "")
                claims.append((item["refs"][0], term))
    for turn in turns:
        user, model, records = turn.get("user") or "", turn.get("model") or "", _records(turn)
        cue = _has(user, _CONSTRAINT)
        if cue and turn["number"] in new:
            ref = _ref(turn, user, cue)
            anchors.setdefault(ref["quote"], {"text": ref["quote"], "kind": "constraint", "refs": [ref]})
        if _has(user, _ALLOWED) and _has(user, _CONFIRM):
            allowed = _ref(turn, user, _has(user, _ALLOWED))
        passed = _has(records, _PASSED)
        if passed and turn["number"] in new:
            ref = _ref(turn, records, passed)
            anchors.setdefault(ref["quote"], {"text": ref["quote"], "kind": "result", "refs": [ref]})
        for cue, term in _CLAIMS:
            if cue in model.casefold() and turn["number"] in new:
                ref = _ref(turn, model, cue)
                if passed:                          # said AND shown in the same turn: backed by the record
                    anchors[ref["quote"]] = {"text": ref["quote"], "kind": "result",
                                             "refs": [ref, _ref(turn, records, passed)]}
                else:
                    anchors.setdefault(ref["quote"], {"text": ref["quote"], "kind": "premise", "refs": [ref],
                                                      "carries": "后面的分析"})
                    claims.append((ref, term))
                break
    out["anchors"] = list(anchors.values())
    last = turns[-1] if turns else None
    latest = (last.get("model") or "") if last else ""
    if not final or last is None:
        return json.dumps(out, ensure_ascii=False)

    # ---- an earlier concern that a record has since settled
    for mark in previous.get("marks") or []:
        if mark.get("state") in ("raised", "asked") and mark.get("kind") == "premise":
            settled = next((t for t in turns if t["number"] > mark.get("turn", 0) and _has(_records(t), _PASSED)), None)
            if settled is not None:
                out["resolved"].append({"id": mark["id"], "how": "后来有了实际的检查记录",
                                        "refs": [_ref(settled, _records(settled), _has(_records(settled), _PASSED))]})
                claims = []

    # ---- 1. a premise nobody checked, with new work resting on it
    builds = _has(latest, _BUILDS_ON)
    if claims and builds:
        claim, term = claims[-1]
        if term and not looked and meta.get("looks_left", 0) > 0:
            # Before saying anything: was it ever checked? Search, and ask for a blind reading.
            return json.dumps({"look": {"turns": [claim["turn"]], "search": [term], "independent": True},
                               "why": "想确认这个说法后来有没有被实际检查过"}, ensure_ascii=False)
        if "· 记录]" not in looked:
            now = _ref(last, latest, builds)
            out["findings"].append({
                "kind": "premise", "weight": "direction", "title": "一个没查过的说法成了后面工作的地基",
                "claim": f"第 {claim['turn']} 轮的这个说法只有主模型自己说过，全文里找不到实际检查它的记录。",
                "why": "后面的分析都建立在它上面；它不成立的话，这几轮的结论要重做。",
                "ask": f"你在第 {claim['turn']} 轮说「{claim['quote']}」。请给出当时实际运行的命令和输出；如果没有运行过，请现在做一次并贴出结果。",
                "probe": "实际跑一次对应的检查，看输出。", "basis": [claim], "now": [now] if now else []})

    # ---- 2. the main model wandered off the user's question
    wanders = _has(latest, _WANDERS)
    if wanders and pivot is None and out["goal"].get("refs") and out["goal"].get("state") == "confirmed":
        now = _ref(last, latest, wanders)
        out["findings"].append({
            "kind": "drift", "weight": "direction", "title": "现在做的事不在你要回答的问题里",
            "claim": "主模型把工作转到了另一件事上，而你没有改过目标。", "why": "继续下去，原来的问题不会得到回答。",
            "ask": "请先停一下：现在这一步和我最初要回答的问题是什么关系？如果没有直接关系，请回到原来的问题上。",
            "probe": "", "basis": out["goal"]["refs"][:1] + ([now] if now else []), "now": [now] if now else []})

    # ---- 3. a conclusion stronger than what was run
    strong = _has(latest, _STRONG)
    thin = next((t for t in turns if _has(_records(t), _THIN)), None)
    if strong and (thin is not None or not any(_records(t).strip() for t in turns)):
        now = _ref(last, latest, strong)
        basis = [now] + ([_ref(thin, _records(thin), _has(_records(thin), _THIN))] if thin is not None else [])
        out["findings"].append({
            "kind": "overreach", "weight": "direction", "title": "结论说得比跑出来的结果更满",
            "claim": "这个结论的措辞很强，而支持它的只有一次运行（或者没有任何记录）。",
            "why": "按这个结论往下走，可能是在一次偶然的结果上加码。",
            "ask": "这个结论依据的是哪几次运行？请列出每次的设置和结果；如果只有一次，请换两个随机种子再跑，并说明差异有多大。",
            "probe": "换种子重复两次。", "basis": [b for b in basis if b], "now": [now] if now else []})

    # ---- 4. polishing in place while a trial has been possible for a while
    recent = [t for t in turns if t.get("model")][-3:]
    if (allowed and len(recent) == 3 and all(_has(t["model"], _POLISH) and not _records(t).strip() for t in recent)
            and not _has(latest, _BUILDS_ON) and not wanders):
        now = _ref(last, latest, _has(latest, _POLISH))
        out["findings"].append({
            "kind": "stall", "weight": "direction", "title": "一直在加工，该试的那一步还没做",
            "claim": "最近几轮都在同一处补充和整理，没有带来会改变判断的新东西。",
            "why": "你同意过的那次小试早就可以做了；它的反馈比继续整理更能说明问题。",
            "ask": "先不要继续整理了。请按我之前同意的范围做一次小规模试跑，并贴出实际输出。",
            "probe": "按已同意的范围试跑一次。", "basis": [allowed], "now": [now] if now else []})

    if not out["findings"]:
        out["sound"] = "没有发现走偏：现在做的事还在回答你的问题，关键的说法有记录或有你的确认。"
    return json.dumps(out, ensure_ascii=False)
