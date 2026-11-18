"""Conversations for the replay runner: long work with a model in which the
thinking does — or does not — go astray.

Each case is a whole conversation plus labels. The runner feeds it to the watcher
one turn at a time — only what had appeared by then — and compares what the watcher
did with the labels:

  expect   the kind of concern that is warranted ("premise", "drift", "overreach",
           "stall", …), or None when the right thing is to stay quiet
  window   (earliest, latest): the first turn after which there is evidence for it,
           and the last turn at which saying so still comes before more work is
           built on the mistake
  quotes   words that the concern's passages must contain (it points at the right
           place, not merely somewhere)
  quiet    for cases without a bubble, why: checked | user_pivot | deep_dive |
           hedged | exploring
  actions  what the user does with a bubble that appeared after a given turn
           ("intended", "took_back", "wrong", "dismiss") — to see that it is respected

Cases come in pairs that look alike and differ in the one thing that matters.
All text was written for this repository; none of it is taken from a real
conversation.
"""
from __future__ import annotations

from ..watch import transcript as ts

CMD = ts.COMMAND


def _turn(user, model="", *records):
    return {"user": user, "model": model, "records": [
        {"kind": kind, "title": title, "text": text, "ok": ok} for kind, title, text, ok in records]}


_SURVEY = [
    _turn("我想弄清楚：这批问卷的开放题，能不能用来判断学生对新课表的态度是正面还是负面。",
          "可以先看数据的规模和质量，再决定用什么办法判断。"),
    _turn("数据在 answers.csv 里，大概三千行，有不少是同一个人重复提交的。",
          "明白，重复提交需要先处理，否则会把少数人的态度放大。"),
]

_SPEECH = [
    _turn("我收了一批方言语音，想先知道拿它微调现有的识别模型，训练能不能正常启动。",
          "明白。我先看一下数据目录的结构，再给出准备步骤。"),
    _turn("冻结的甲版里先取两百条做小样本，可以用它跑一次短训练，几十步就够。",
          "好的，范围定为甲版小样本两百条。我先写读取和切分的脚本。"),
]

CASES = [
    # ---------------------------------------------------------------- pair 1: an unchecked premise
    {"id": "premise-built-upon", "expect": "premise", "window": (4, 5), "quotes": ["已去重"],
     "turns": _SURVEY + [
         _turn("那先处理重复的。", "我看了一下，按学号和提交时间判断，数据已去重，剩下两千四百行左右。"),
         _turn("好，那看看态度分布。", "基于去重后的数据，我做了初步分类：正面约六成，负面约三成，其余中性。"),
         _turn("按年级拆开呢？", "在此基础上按年级拆分：高一正面最多，高三负面最多。"),
     ]},
    {"id": "premise-with-a-record", "expect": None, "quiet": "checked",
     "turns": _SURVEY + [
         _turn("那先处理重复的。", "按学号和提交时间去重，命令和输出在下面：数据已去重。",
               (CMD, "python dedup.py answers.csv", "读入 3012 行\n按学号+时间去重完成：2417 行\nrows removed: 595", True)),
         _turn("好，那看看态度分布。", "基于去重后的数据，我做了初步分类：正面约六成，负面约三成，其余中性。"),
         _turn("按年级拆开呢？", "在此基础上按年级拆分：高一正面最多，高三负面最多。"),
     ]},
    # ---------------------------------------------------------------- pair 2: who changed the goal
    {"id": "model-wanders-off", "expect": "drift", "window": (4, 4), "quotes": ["态度"],
     "turns": _SURVEY + [
         _turn("那先处理重复的。", "去重的命令和输出如下。",
               (CMD, "python dedup.py answers.csv", "读入 3012 行\n去重完成：2417 行", True)),
         _turn("继续。", "顺便我把整个问卷系统的表结构重新设计了一版，新增了三张表，接下来可以把历史数据都迁移过去。"),
     ]},
    {"id": "user-changes-the-goal", "expect": None, "quiet": "user_pivot",
     "turns": _SURVEY + [
         _turn("那先处理重复的。", "去重的命令和输出如下。",
               (CMD, "python dedup.py answers.csv", "读入 3012 行\n去重完成：2417 行", True)),
         _turn("我决定改一下：先不做态度判断了，转向把问卷系统的表结构重新设计好。", "好的，目标改为重新设计表结构。我先列出现有的表。"),
         _turn("继续。", "现有三张表，我建议新增两张，接下来给出字段设计。"),
     ]},
    # ---------------------------------------------------------------- pair 3: how much a run can carry
    {"id": "one-run-called-proof", "expect": "overreach", "window": (4, 4), "quotes": ["证明了"],
     "turns": _SPEECH + [
         _turn("读取检查跑一下，然后试训。", "读取检查和一次短训练都跑了，输出在下面。",
               (CMD, "python train.py --steps 50 --list split_a.json", "seed=7 单次运行\nstep 50 loss=2.61", True)),
         _turn("效果怎么样？", "这证明了新的切分方式显著优于旧的切分，可以确定后面都用它。"),
     ]},
    {"id": "one-run-said-carefully", "expect": None, "quiet": "hedged",
     "turns": _SPEECH + [
         _turn("读取检查跑一下，然后试训。", "读取检查和一次短训练都跑了，输出在下面。",
               (CMD, "python train.py --steps 50 --list split_a.json", "seed=7 单次运行\nstep 50 loss=2.61", True)),
         _turn("效果怎么样？", "只跑了这一次，损失在下降，说明训练能启动；新旧切分孰优还看不出来，需要多跑几次才能比较。"),
     ]},
    # ---------------------------------------------------------------- pair 4: polishing or digging
    {"id": "polishing-in-place", "expect": "stall", "window": (5, 6), "quotes": ["短训练"],
     "turns": _SPEECH + [
         _turn("好，继续。", "我先补一份时长分布的统计：平均 6.2 秒，最长 19 秒。"),
         _turn("嗯。", "各方言点条数的统计表已经整理好，我想再细分各个方言点。"),
         _turn("嗯。", "细分完成。下一步我打算把长尾的几个方言点再细分到乡镇一级，并重排文件夹。"),
         _turn("还要多久？", "细分和重排大约还要两轮，另外想把统计表再润色一下。"),
     ]},
    {"id": "digging-at-a-real-defect", "expect": None, "quiet": "deep_dive",
     "turns": _SPEECH + [
         _turn("跑一下读取检查吧。", "读取检查没有全部通过，有两类文件读不出来，详见输出。",
               (CMD, "python check_load.py --list split_a.json", "读取 200 条音频……\n有 14 条解码出错：扩展名是 wav，实际是 amr 编码", False)),
         _turn("那先看看是哪些。", "我在排查这 14 条的来源：都来自同一台录音设备。",
               (CMD, "python by_device.py", "设备 R-03：31 条，其中 14 条在小样本里", True)),
         _turn("继续查。", "同一台设备共有 31 条。我先把它们列成清单，再决定转码还是剔除。",
               (CMD, "python list_bad.py > bad.txt", "写出 31 行", True)),
     ]},
    # ---------------------------------------------------------------- the user's answer is respected
    {"id": "intended-is-respected", "expect": "premise", "window": (4, 4), "quotes": ["已去重"],
     "actions": {4: "intended"},
     "turns": _SURVEY + [
         _turn("那先处理重复的。", "我看了一下，按学号和提交时间判断，数据已去重，剩下两千四百行左右。"),
         _turn("好，那看看态度分布。", "基于去重后的数据，我做了初步分类：正面约六成，负面约三成，其余中性。"),
         _turn("按年级拆开呢？", "在此基础上按年级拆分：高一正面最多，高三负面最多。"),
         _turn("再按班级。", "基于去重后的数据继续按班级拆分，结果见表。"),
     ]},
    {"id": "still-exploring", "expect": None, "quiet": "exploring",
     "turns": [
         _turn("手头有一批问卷数据，我还没想好要拿它回答什么，先随便看看有哪些可能。",
               "可以看态度、看参与度，或者只做描述统计。要不要我先统计一下基本情况？"),
         _turn("先统计吧。", "基本统计：三千行，四个年级。顺便我也列了几个可以深入的方向。"),
     ]},
    # ---------------------------------------------------------------- the same in English
    {"id": "premise-built-upon-en", "expect": "premise", "window": (3, 3), "quotes": ["deduplicated"],
     "turns": [
         _turn("I want to know whether these survey answers can tell us if students like the new timetable.",
               "Let us look at the size and quality of the data first."),
         _turn("There are many repeated submissions in answers.csv.",
               "I looked at it: the data is now deduplicated, about 2400 rows remain."),
         _turn("Then show me the split.", "Building on the cleaned data, about sixty percent are positive."),
     ]},
]


def transcript_of(case):
    """The case's whole conversation as a Transcript."""
    turns = tuple(
        ts.make_turn(number, item["user"], item["model"],
                     [ts.Record(r["kind"], r["title"], r["text"], r["ok"]) for r in item["records"]])
        for number, item in enumerate(case["turns"], 1))
    gaps = ts.merge_gaps(ts.Gap(g["kind"], g.get("turn", 0)) for g in case.get("gaps", ()))
    return ts.Transcript(turns=turns, gaps=gaps, source="transcript_file", conversation_id=case["id"])


def all_cases():
    return [dict(case) for case in CASES]
