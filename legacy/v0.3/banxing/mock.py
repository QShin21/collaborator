"""Offline stand-in for the model. Plumbing, not evidence.

It lets the tests, the self-test of the packaged build and demos run without a
network. Its routes are templates, its hints and its "ready" / "speak up" decisions
are keyword rules — just enough to play every outcome in the tests. Nothing about
quality may be concluded from it, and the app never falls back to it silently: it
is used only when the provider is set to "mock".
"""
from __future__ import annotations

import json

from . import examples
from .levels import HINT, WALK
from .textutil import cjk_ratio, clean, loose, similarity

# Words that suggest a step which is hard to take back.
_COMMIT_CUES = ("下单", "采购", "购买", "批量", "全部", "正式", "导入", "执行", "删除", "覆盖",
                "order", "purchase", "batch", "all files", "import", "delete", "overwrite")
# Words in the chat that suggest something is still open / has been settled.
_OPEN_CUES = [
    ("示例值", "上一轮的数值还是示例，没有换成实际的。"),
    ("示例数", "上一轮的数值还是示例，没有换成实际的。"),
    ("重复", "上一轮发现的重复记录还没处理。"),
    ("冲突", "上一轮发现的冲突还没决定怎么处理。"),
    ("待确认", "上一轮还有待确认的项目。"),
    ("尚未决定", "上一轮还有没定下来的选择。"),
    ("example value", "The numbers from the last round are still examples."),
    ("duplicate", "The duplicates found last round are still unresolved."),
    ("conflict", "The conflicts found last round are still unresolved."),
    ("to be confirmed", "Something from the last round is still unconfirmed."),
]
_SETTLED_CUES = ("已确认", "已量好", "已解决", "已选定", "已处理", "confirmed", "resolved", "measured")
# A small, specific change to something that already exists: ready to send as it is.
_EDIT_CUES = ("改成", "改为", "换成", "调大", "调小", "改一下", "change the", "rename the", "make the")
_CHAT_ONLY = ("谢谢", "辛苦了", "好的", "你好", "thanks", "thank you", "hello")
_FORM_CUES = ("网页", "页面", "表格", "脚本", "清单", "文件", "图纸", "html", "page", "spreadsheet", "script", "file")
_CHECK_CUES = ("检查", "核对", "示例", "怎么用", "怎么看", "验证", "check", "verify", "example", "how to")


def _example_for(draft):
    target = loose(draft)
    for name in examples.names():
        example = examples.get(name)
        if similarity(target, loose(example["draft"])) >= 0.85:
            return example
    return None


def _templates(english):
    if english:
        return [
            {"approach": "Sketch it first", "deliverable": "a draft you can open", "kind": "prototype",
             "main": True, "why": "nothing exists to look at yet",
             "fits": "You want to see the shape of it before deciding details.",
             "do": "Please start by making a first draft in this project that I can open and look at directly.",
             "deliver": "Mark every assumption you make as an example value and list what I still need to confirm.",
             "check": "Tell me what to check first once I have it.",
             "next": "Open the draft and tick off the list of things to confirm."},
            {"approach": "Try a small sample", "deliverable": "a sample and trial steps", "kind": "minimal",
             "main": False, "why": "",
             "fits": "You would rather test the idea on something small first.",
             "do": "Please start with a small sample in this project that covers one normal case and two awkward cases.",
             "deliver": "Make it something I can try from start to finish.",
             "check": "Tell me how to run or use the sample, what result to expect, and what to decide before going further.",
             "next": "Run the sample once and note what surprised you."},
            {"approach": "List what to check", "deliverable": "a checklist to confirm", "kind": "plan",
             "main": False, "why": "",
             "fits": "You are not sure which conditions matter yet.",
             "do": "Please start by writing a checklist in this project of the conditions that decide how this should be done.",
             "deliver": "For each item say how I can find or measure it and give an example answer.",
             "check": "Then tell me which three items to settle first and what you can build once I have them.",
             "next": "Settle the first three items on the list."},
        ]
    return [
        {"approach": "先做能看的样子", "deliverable": "可打开的示意稿", "kind": "prototype",
         "main": True, "why": "现在还没有可以看的东西",
         "fits": "想先看到大概的样子，再决定细节。",
         "do": "请先在这个项目里做一份我可以直接打开查看的示意稿。",
         "deliver": "把你做的每个假设标明为示例，列出还需要我确认的项目。",
         "check": "并告诉我拿到后先检查什么。",
         "next": "打开示意稿，对着待确认的项目逐条过一遍。"},
        {"approach": "先拿小样试一遍", "deliverable": "一份小样和试用步骤", "kind": "minimal",
         "main": False, "why": "",
         "fits": "想先在小范围里试通，再做完整的。",
         "do": "请先在这个项目里做一份小样，包含一个正常情况和两个容易出问题的情况。",
         "deliver": "让我能从头到尾试一遍。",
         "check": "告诉我怎样运行或使用这份小样、应该看到什么结果，以及继续之前我需要先决定什么。",
         "next": "照步骤把小样跑一遍，记下和预期不一样的地方。"},
        {"approach": "先列核对清单", "deliverable": "待确认清单", "kind": "plan",
         "main": False, "why": "",
         "fits": "还不清楚哪些条件会影响做法。",
         "do": "请先在这个项目里写一份核对清单，列出会决定做法的条件。",
         "deliver": "每一项写明我怎样查到或量到，并给一个示例答案。",
         "check": "再告诉我最该先确定哪三项，以及确定之后你可以先做出什么。",
         "next": "先把清单里最要紧的三项定下来。"},
    ]


def _critical(draft, chat):
    """(show, reason) for the quiet level."""
    draft_folded, chat_folded = clean(draft).casefold(), clean(chat).casefold()
    if not chat_folded or not any(cue in draft_folded for cue in _COMMIT_CUES):
        return False, "这份草稿原样发出去就行。"
    if any(cue in chat_folded for cue in _SETTLED_CUES):
        return False, "前文的条件已经确认。"
    for cue, reason in _OPEN_CUES:
        if cue in chat_folded:
            return True, reason
    return False, "这份草稿原样发出去就行。"


def _missing(draft, english):
    folded = clean(draft).casefold()
    found = []
    if not any(cue in folded for cue in _FORM_CUES):
        found.append({"kind": "deliverable", "label": "A form for the result" if english else "没说要什么样的成果",
                      "clause": "Please make it a file I can open directly." if english
                      else "请做成我能直接打开查看的文件。"})
    if not any(cue in folded for cue in _CHECK_CUES):
        found.append({"kind": "check", "label": "How to check it" if english else "没说拿到后怎么检查",
                      "clause": "Include example data and tell me what to check first." if english
                      else "请附一组示例数据，并告诉我拿到后先核对什么。"})
    if "我" not in folded and " i " not in f" {folded} ":
        found.append({"kind": "constraint", "label": "Your situation" if english else "没说你自己的情况",
                      "ask": "My situation: " if english else "我的情况是："})
    return found


def _line(data):
    return json.dumps(data, ensure_ascii=False)


def complete(meta):
    draft = meta.get("draft") or ""
    mode = meta.get("mode") or "lead"
    chat = meta.get("chat") or ""
    pull = bool(meta.get("pull"))
    given = tuple(meta.get("given") or ())
    folded = clean(draft).casefold()
    english = cjk_ratio(draft) < 0.3
    head = {"v": 2, "task": True, "ready": False, "show": True, "reason": "", "pack": "other", "assumes": ""}
    if not pull and any(folded.strip("。.！! ") == word for word in _CHAT_ONLY):
        return _line(dict(head, task=False, show=False))
    edit = any(cue in folded for cue in _EDIT_CUES) and not any(cue in folded for cue in _COMMIT_CUES)
    hold = False
    if mode == WALK and not pull:
        show, reason = _critical(draft, chat)
        if not show:
            return _line(dict(head, ready=edit, show=False, reason=reason))
        head.update(reason=reason)
        hold = True
    elif not pull:
        missing = _missing(draft, english)
        if (edit and chat) or (mode == HINT and not missing):
            return _line(dict(head, ready=True, show=False))
        if mode == HINT:
            return _line(head) + "\n" + _line({"missing": missing})
    example = _example_for(draft)
    if example and not given:
        routes = [dict(r) for r in example["routes"]]
        head.update(pack=example["pack"], assumes=example.get("assumes", ""))
    else:
        routes = _templates(english)
    if given:                       # "another three": do not repeat what was shown
        suffix = " again" if english else "·换个角度"
        routes = [dict(r, approach=(r["approach"] + suffix) if r["approach"] in given else r["approach"])
                  for r in routes]
        routes = routes[1:] + routes[:1]
    lines = [_line(head)]
    for index, r in enumerate(routes):
        lines.append(_line({"route": index, "approach": r["approach"], "deliverable": r["deliverable"],
                            "kind": r["kind"], "main": r["main"], "why": r["why"], "hold": hold}))
    for index, r in enumerate(routes):
        lines.append(_line({"route": index, "fits": r["fits"], "do": r["do"], "deliver": r["deliver"],
                            "check": r["check"], "next": r["next"]}))
    return "\n".join(lines)
