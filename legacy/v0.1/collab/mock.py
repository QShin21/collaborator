"""Deterministic offline model. It is not smart — it exists so the whole product
(UI, adaptation loop, evaluation harness, tests) can run with no API key."""
import json
import re

_WELL_HINTS = re.compile(
    r"\d|计算|求解|求出|证明|实现一个|写一个函数|报错|bug|solve|compute|calculate|prove|"
    r"implement|error|traceback", re.I)


def _short(text, n=24):
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[:n] + "…"


def _last_user(messages):
    for message in reversed(messages):
        if message["role"] == "user":
            return message["content"]
    return ""


def looks_well_structured(problem):
    return bool(_WELL_HINTS.search(problem or "")) and len(problem or "") < 400


def _frame(meta):
    problem = meta.get("problem", "")
    topic = _short(problem, 18)
    if looks_well_structured(problem) and not meta.get("force_map"):
        return {
            "title": topic, "structuredness": "well", "score": 0.85,
            "restatement": f"你要解决的是一个目标和条件都比较明确的问题：{_short(problem, 60)}",
            "knowns": ["目标已经给出", "有可以直接动手的起点"],
            "unknowns": ["有没有隐含的边界条件"],
            "clarifying_questions": [], "landscape": [], "threads": [],
            "recommendation": "问题已经足够清晰，可以直接拆任务开工。",
        }
    return {
        "title": topic, "structuredness": "ill", "score": 0.25,
        "restatement": f"你想推进「{topic}」，但目标、范围和评判标准还没有定下来。",
        "knowns": ["你有一个真实的动机和大致方向"],
        "unknowns": ["具体为谁解决、解决到什么程度", "有哪些资源与限制", "怎样算做成了"],
        "clarifying_questions": [
            "这件事做成之后，最直接受益的是谁？",
            "你手头有多少时间和资源？",
            "如果只能做成一小部分，你最想保住哪一部分？",
        ],
        "landscape": [
            {"topic": "问题界定", "why": "先分清现象、原因与真正想改变的东西"},
            {"topic": "相关方与场景", "why": "真实问题总是嵌在具体的人和情境里"},
            {"topic": "已有做法", "why": "别人怎么解决类似问题，能省掉很多弯路"},
            {"topic": "可行性与验证", "why": "尽早用小实验检验想法是否成立"},
        ],
        "threads": [
            {"id": "t1", "type": "main", "title": f"先摸清现状：{topic}",
             "summary": "收集事实和数据，把模糊的感觉变成可描述的现状。",
             "first_step": "写下你已经观察到的三个具体现象。", "learn": "调查与问题界定"},
            {"id": "t2", "type": "main", "title": "从目标倒推",
             "summary": "先定义理想结果，再反推需要哪些条件。",
             "first_step": "用一句话写出“做成了”的样子。", "learn": "目标分解与成功标准"},
            {"id": "t3", "type": "side", "title": "找一个最小可行的切入点",
             "summary": "挑一个一周内能验证的小实验。",
             "first_step": "列出两个成本最低的尝试。", "learn": "快速验证"},
            {"id": "t4", "type": "side", "title": "看看别人怎么做",
             "summary": "对照两三个现成案例，借鉴其结构。",
             "first_step": "找一个最接近的案例并记录它的做法。", "learn": "案例迁移"},
        ],
        "recommendation": "如果还不确定，建议以 t1 为主线、t3 为支线。",
    }


def _plan(meta):
    problem = meta.get("problem", "")
    chosen = meta.get("chosen_titles") or ["直接解决问题"]
    tasks = [{"title": f"明确范围：{_short(chosen[0], 16)}", "detail": "写下边界：包含什么，不包含什么。"},
             {"title": "列出已知与缺口", "detail": "分清哪些已知、哪些需要查证。"},
             {"title": "完成第一步并记录结果", "detail": "动手做最小的一步，把结果记到笔记里。"},
             {"title": "对照成功标准复盘", "detail": "看结果离目标还差什么，决定下一轮。"}]
    return {
        "card": {
            "goal": meta.get("goal") or f"围绕「{_short(problem, 30)}」得到一个可检验的结果",
            "givens": ["你当前掌握的信息与想法"] + [f"选定的方向：{t}" for t in chosen[:3]],
            "constraints": ["时间与资源有限", "需要自己能够独立说明每一步"],
            "success_criteria": ["能用自己的话讲清问题与方案", "至少完成一次真实验证"],
        },
        "tasks": tasks,
        "opening": f"我们把问题整理成了左边的问题卡，主线是「{chosen[0]}」。"
                   "你想先从第一项任务开始，还是先调整一下问题卡？",
    }


_BUBBLE_TEXT = {
    "question": "这里“做成了”的标准，你会怎么向别人解释？",
    "perspective": "换成受影响的人来看，这个问题会变成什么样？",
    "knowledge": "这类问题常用“现状—原因—对策”三段来梳理，也许用得上。",
    "next_step": "要不要先挑最小的一步，十分钟内试一下？",
    "risk": "有没有一个隐含假设，一旦不成立整个思路就要改？",
    "connection": "这和你之前解决过的哪件事有点像？",
}


def _bubbles(meta):
    kinds = meta.get("kinds") or ["question"]
    seen = set(meta.get("avoid") or [])
    out = []
    for kind in kinds:
        text = _BUBBLE_TEXT.get(kind, _BUBBLE_TEXT["question"])
        if text in seen:
            continue
        out.append({"kind": kind, "text": text, "why": "根据你当前的笔记与进展给出的提示"})
    return {"bubbles": out}


def _judge_answer(meta):
    answer = meta.get("answer", "")
    length = len(answer)
    def has(*words):
        return any(w in answer.lower() for w in words)
    base = 0 if length < 10 else (1 if length < 40 else (2 if length < 120 else 3))
    scores = {
        "clarity": min(4, base + (1 if has("目标", "解决", "goal", "问题") else 0)),
        "structure": min(4, base + (1 if has("首先", "然后", "步骤", "第一", "1.", "first", "then") else 0)),
        "criteria": min(4, max(0, base - 1) + (2 if has("标准", "成功", "指标", "criteria", "success") else 0)),
        "actionability": min(4, base + (1 if has("打算", "计划", "下一步", "plan", "next") else 0)),
    }
    return {"scores": scores, "comment": "（mock 评分：按长度与关键词估计，仅用于联调）"}


def _judge_transcript(meta):
    # Deliberately identical for every agent: the mock must never look like evidence.
    dims = ["scaffolding", "agency", "breadth", "progress", "efficiency"]
    return {"scores": {d: 2 for d in dims}, "comment": "（mock 评分：固定值，仅用于联调，不代表质量）"}


def mock_complete(task, system, messages, meta):
    user = _last_user(messages)
    if task == "frame":
        return json.dumps(_frame(meta), ensure_ascii=False)
    if task == "plan":
        return json.dumps(_plan(meta), ensure_ascii=False)
    if task == "bubbles":
        return json.dumps(_bubbles(meta), ensure_ascii=False)
    if task == "judge_answer":
        return json.dumps(_judge_answer(meta), ensure_ascii=False)
    if task == "judge_transcript":
        return json.dumps(_judge_transcript(meta), ensure_ascii=False)
    if task == "expand":
        return (f"我们展开聊聊这个想法：「{meta.get('bubble', '')}」。\n\n"
                "它之所以值得想一想，是因为它可能改变你对问题边界的判断。"
                "你觉得它和你现在的思路是互补，还是冲突？")
    if task == "sim_pre":
        return "我想解决这个问题，但还不太清楚具体要做什么。"
    if task == "sim_post":
        return "我的目标是先界定问题范围，然后选一个最小的切入点试一试。"
    if task == "sim_turn":
        bubbles = meta.get("bubbles") or []
        return json.dumps({"message": "好的，我想先从第一步开始，但不确定怎么衡量结果。",
                           "used_bubbles": [0] if bubbles else []}, ensure_ascii=False)
    if task == "sim_choose":
        return json.dumps({"thread_ids": ["t1", "t3"], "answers": "主要是为了我自己和同学，时间大约两周。"},
                          ensure_ascii=False)
    if task == "socratic":
        return f"在回答之前我想先问：关于「{_short(user, 20)}」，你自己目前最确定的是哪一点？"
    if task == "baseline":
        return (f"关于「{_short(user, 30)}」，可以这样做：\n\n1. 明确目标\n2. 收集资料\n"
                "3. 制定方案\n4. 执行并复盘\n\n需要我把某一步写得更详细吗？")
    # chat (collaborator / scaffold)
    return (f"我理解你现在关注的是：{_short(user, 40)}\n\n"
            "这里有两条可以走的路：**A.** 先把判断标准写下来再动手；"
            "**B.** 先做一个最小尝试，再回头修正标准。\n\n你更倾向哪一条？为什么？")
