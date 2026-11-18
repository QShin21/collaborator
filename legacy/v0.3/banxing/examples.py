"""Two worked cases, usable without any model.

They serve three purposes: the "内置示例" entry in the menu (so the app is useful
before a model is configured), a reference for what a good set of three routes
looks like, and fixtures for the tests.

Both are generic illustrations written for this repository. Each route shows the
shape the model is asked for: a name, a first deliverable, its kind, whether it is
the main line (and why), when it fits, the three parts that follow the user's own
words in the prompt (do / deliver / check), and what to do once the result arrives.
"""
from __future__ import annotations

EXAMPLES = {
    "furniture": {
        "title": {"zh": "家具 DIY · 阳台三层花架", "en": "Furniture DIY · a three-tier plant stand"},
        "draft": "想在阳台做一个三层的花架，我没做过木工。",
        "pack": "furniture",
        "assumes": "还没量过阳台",
        "routes": [
            {
                "approach": "先做纸样比一比", "deliverable": "能打印的等大纸样", "kind": "verify",
                "main": True, "why": "你还没量过阳台",
                "fits": "还没量过阳台，想先看看摆不摆得下。",
                "do": "请先在这个项目里做一个双击就能打开的网页：我填花架的宽、高、进深和每层的间距，"
                      "它生成可以用 A4 纸分页打印的等大轮廓，拼起来就是花架的正面和侧面。",
                "deliver": "先填好一组写明“示例”的数字，保证一打开就能打印。",
                "check": "另外附一页说明，告诉我在阳台上要量哪几处，纸样立起来以后重点看什么。",
                "next": "把纸样拼好立在阳台上，看挡不挡窗、过道够不够宽。",
            },
            {
                "approach": "先做单层练手", "deliverable": "单层的图纸和材料单", "kind": "minimal",
                "main": False, "why": "",
                "fits": "想先用最少的材料练一次手，再决定要不要做完整的。",
                "do": "我想先只做一层练练手。请在这个项目里写一份单层花架的制作说明：一张标了尺寸的示意图，"
                      "一张材料和工具清单，再按先后顺序写出步骤，每一步点出新手容易出错的地方。",
                "deliver": "尺寸先用写明“示例”的数字，并单独列出动手之前必须由我自己量好或决定的几项。",
                "check": "最后说明这一层做完以后，怎样判断可以照样做出另外两层。",
                "next": "照材料单买一层的料，做完看牢不牢，再决定后面两层。",
            },
            {
                "approach": "买现成的来改", "deliverable": "改装步骤和采购清单", "kind": "adapt",
                "main": False, "why": "",
                "fits": "不想从木料做起，愿意买一个现成的架子再调整。",
                "do": "我打算买一个现成的置物架回来改。请在这个项目里做一张对照表，列出挑现成架子时要看的尺寸、"
                      "承重、材质和防水这几项，每一项写明怎样算合适。",
                "deliver": "再写一份改装步骤，说明加防水、加固和调整层高各需要什么材料。",
                "check": "阳台我还没量，所以先给一组写明“示例”的参考尺寸，并告诉我下单之前要先量哪里。",
                "next": "按清单比几款现成的架子，核对尺寸和承重再下单。",
            },
        ],
    },
    "software": {
        "title": {"zh": "个人软件 · 每天记几笔的记账工具", "en": "Personal software · a simple expense tracker"},
        "draft": "想做个记账的小工具，每天记几笔花销，月底能看汇总。",
        "pack": "software",
        "assumes": "还没定用什么形式来记",
        "routes": [
            {
                "approach": "先用表格搭一版", "deliverable": "带公式的记账表", "kind": "adapt",
                "main": True, "why": "今天就能开始记",
                "fits": "想今天就开始记，不介意先用表格。",
                "do": "请先在这个项目里生成一个能用表格软件直接打开的记账文件：一张按天填写的明细表，"
                      "有日期、金额、分类和备注；一张自动按月份和分类合计的汇总表，公式都写好。",
                "deliver": "先填上一个星期的数据并注明是“示例”，让我一打开就能看到汇总的样子。",
                "check": "再用几句话告诉我每天怎么填、月底看哪里。",
                "next": "打开表格记上三天，看分类和汇总合不合用。",
            },
            {
                "approach": "先做能点的页面", "deliverable": "离线可用的记账页", "kind": "prototype",
                "main": False, "why": "",
                "fits": "想要一个专门的界面，先试试顺不顺手。",
                "do": "请先在这个项目里做一个单文件网页，双击就能在浏览器里打开，不需要联网：可以添加一笔花销，"
                      "填金额、分类和备注；可以看到当天的列表和本月各分类的合计；数据先存在浏览器本地。",
                "deliver": "页面里先放几笔写明“示例”的记录。",
                "check": "请告诉我怎么打开它、数据存在哪里、换一台电脑会不会丢，以及试用时最该留意哪几处。",
                "next": "打开页面记几笔，把添加、修改和看汇总都试一遍。",
            },
            {
                "approach": "先想清要记什么", "deliverable": "分类表和三天样例", "kind": "plan",
                "main": False, "why": "",
                "fits": "还没想好分哪些类、每笔要记多细。",
                "do": "动手之前我想先把要记的内容定下来。请在这个项目里写一份说明文件：建议的花销分类，"
                      "不超过十个，每类举两个例子；每笔要记的栏目，以及哪些可以不填。",
                "deliver": "再按这个设计写出三天的示例流水，和对应的月底汇总是什么样子。",
                "check": "请标出哪些地方是你替我假设的，并列出需要我自己拿主意的三件事。",
                "next": "对着样例改分类，删掉用不上的栏，再决定做成表格还是页面。",
            },
        ],
    },
}


def names():
    return list(EXAMPLES)


def get(name):
    return EXAMPLES[name]
