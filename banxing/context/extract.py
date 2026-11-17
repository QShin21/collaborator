"""Choose what to quote from the chat: the last three finished turns, cut down to the
parts that bear on the draft.

Only the user's messages and Codex's final answers are used — no tool output, no
reasoning. Long answers are reduced to the paragraphs most likely to matter:
those sharing terms with the draft, and those that state confirmed constraints,
delivered results or things still to verify. The model does the real judging;
this step only keeps the request small and on topic.
"""
from __future__ import annotations

import re

from ..textutil import clip, keywords

TURNS = 3
USER_CHARS = 500
ANSWER_CHARS = 1100
TOTAL_CHARS = 4200

_FENCE = re.compile(r"```.*?(?:```|\Z)", re.S)
_FILE = re.compile(r"[\w\-./\\]+\.(?:html?|py|js|ts|css|md|csv|json|xlsx?|txt|svg|png|jpe?g|pdf|ps1|bat)\b", re.I)
_CUES = ("确认", "已", "示例", "假设", "待", "需要", "注意", "冲突", "尺寸", "只有", "不要", "必须",
         "生成", "创建", "保存", "下一步", "验证", "核对", "example", "assum", "confirm", "todo",
         "created", "saved", "next", "verify", "conflict", "placeholder", "must", "only")


def _paragraphs(text):
    text = _FENCE.sub("\n[代码略]\n", text or "")
    blocks = [block.strip() for block in re.split(r"\n\s*\n", text)]
    return [block for block in blocks if block]


def condense(answer, draft_terms, limit=ANSWER_CHARS):
    """Keep the opening paragraph and the most relevant others, in order."""
    blocks = _paragraphs(answer)
    if not blocks:
        return ""
    if sum(len(b) for b in blocks) + 2 * len(blocks) <= limit:
        return "\n".join(blocks)

    def worth(index, block):
        folded = block.casefold()
        value = 3 * len(draft_terms & keywords(block))
        value += sum(1 for cue in _CUES if cue in folded)
        value += 2 * len(_FILE.findall(block))
        if index == len(blocks) - 1:
            value += 2                        # closing paragraphs tend to say what is next
        return value

    ranked = sorted(range(1, len(blocks)), key=lambda i: (-worth(i, blocks[i]), i))
    keep, used = {0}, min(len(blocks[0]), limit // 2)
    for index in ranked:
        cost = min(len(blocks[index]), limit // 3) + 2
        if used + cost > limit:
            continue
        keep.add(index)
        used += cost
    parts, previous = [], None
    for index in sorted(keep):
        if previous is not None and index != previous + 1:
            parts.append("…")
        parts.append(clip(blocks[index], limit // 2 if index == 0 else limit // 3))
        previous = index
    if previous != len(blocks) - 1:
        parts.append("…")
    return "\n".join(parts)


def excerpt(turns, draft, turns_wanted=TURNS, total=TOTAL_CHARS):
    """Text block for the model, or "" when there is no finished turn."""
    finished = [t for t in turns if t.completed and (t.user or t.answer)][-turns_wanted:]
    if not finished:
        return "", 0
    terms = keywords(draft)
    parts = []
    for number, turn in enumerate(finished, 1):
        if turn.user:
            parts.append(f"[第 {number} 轮 · 用户]\n{clip(turn.user, USER_CHARS)}")
        if turn.answer:
            parts.append(f"[第 {number} 轮 · Codex 的回答]\n{condense(turn.answer, terms)}")
    text = "\n\n".join(parts)
    if len(text) > total:
        # Drop from the oldest end; the latest turn is the one the draft continues.
        text = "…" + text[-(total - 1):]
    return text, len(finished)
