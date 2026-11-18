"""Short reminders of what beginners tend to miss, per kind of project.

They are handed to the model as thinking aids ("has anyone measured the space?"),
never shown to the user as a list. Adding a kind of project means adding one entry
here; the cue words only decide which reminders are worth their tokens.
"""
from __future__ import annotations

from .textutil import clean

CHECKLISTS = {
    "furniture": {
        "cues": ["家具", "柜", "抽屉", "桌", "椅", "书架", "架子", "花架", "置物架", "鞋架", "床", "木工",
                 "板材", "木板", "木料", "电钻", "螺丝",
                 "封边", "打孔", "五金", "滑轨", "铰链", "合页", "榫", "diy", "furniture", "cabinet",
                 "drawer", "shelf", "desk", "plywood", "woodwork"],
        "items": [
            "放的地方：摆放位置的宽、高、进深有没有实际量过，踢脚线、插座、窗台和栏杆会不会挡。",
            "活动空间：门和抽屉打开、人走动、东西拿进拿出的时候会不会互相碰到。",
            "配件匹配：滑轨、铰链、支架这类配件的型号会反过来决定板件的尺寸和要留的间隙；先选配件，再定尺寸。",
            "工具与加工：手头有哪些工具，锯、钻、封边这些步骤由谁来做，代加工能做到什么精度。",
            "材料：板材的厚度、处理之后的实际尺寸、防水和纹理方向；正式买料之前尺寸要量过。",
            "承重与固定：每层的跨度、连接方式，要不要固定到墙上或地上。",
        ],
    },
    "files": {
        "cues": ["整理", "归档", "改名", "重命名", "批量", "文件夹", "照片", "图片", "备份", "移动文件",
                 "去重", "rename", "organize", "organise", "folder", "photos", "backup", "dedupe"],
        "items": [
            "先看预览：真正移动或改名之前，先列出“现在在哪 → 要放去哪”的对照给人看。",
            "重名处理：目标位置已经有同名文件时怎么办（跳过、加序号、比较内容）。",
            "认不出的文件：缺日期、缺元数据或者格式特殊的文件归到哪里。",
            "撤销办法：留一份能把每个文件放回原处的记录，先在副本或小样本上试。",
            "范围：包不包含子文件夹、隐藏文件和其他类型；中文路径和超长路径是否正常。",
        ],
    },
    "software": {
        "cues": ["小工具", "工具", "软件", "程序", "脚本", "网页", "网站", "界面", "app", "应用", "插件",
                 "自动", "windows", "exe", "python", "tool", "script", "website", "plugin", "bot"],
        "items": [
            "第一份成果要能直接打开或运行，并说明怎么启动。",
            "先用内置示例数据跑通，再接真实数据；示例里要包含正常情况和几种异常情况。",
            "规则先验证：把判断规则（怎么识别、怎么分类）做成能单独试的小样。",
            "破坏性操作（覆盖、删除、批量修改）要有预览、确认和恢复办法。",
            "运行环境：用户的系统、是否需要安装依赖、零基础用户怎样运行。",
        ],
    },
    "other": {
        "cues": [],
        "items": [
            "第一份成果应当是用户能打开、能检查或能拿去用的东西，而不是一段说明。",
            "缺少的关键数据先用标明为示例的数值代替，并列出需要用户确认的项目。",
            "不易回头的步骤（花钱、批量修改）之前，先有一步可以核对的预览或清单。",
        ],
    },
}


def detect(text):
    """Kinds of project the text touches, most likely first. Always ends with a result."""
    folded = clean(text).casefold()
    scored = []
    for name, entry in CHECKLISTS.items():
        hits = sum(1 for cue in entry["cues"] if cue in folded)
        if hits:
            scored.append((hits, name))
    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    return [name for _, name in scored[:2]] or ["other"]


def for_text(text):
    """The reminder lines worth sending for this draft (plus its chat excerpt)."""
    lines = []
    for name in detect(text):
        lines.extend(CHECKLISTS[name]["items"])
    return lines
