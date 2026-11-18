# 证据台账

这份台账回答：**每一项现在有哪一级证据、依据是什么、还缺谁在什么环境下做什么。**
用语的规矩：总是带等级；不单独写「已验证」「测试通过」「可用」；离线替身模型的结果只写「流程走通」。

## 1. 证据等级

| 等级 | 含义 | 说明得了 | 说明不了 |
| --- | --- | --- | --- |
| E0 | 只有设计或代码，没有运行过 | 意图 | 任何行为 |
| E1 | Linux 上的自动化测试：合成对话、脚本写好的或离线替身的回复、假服务端 | 我们自己的逻辑自洽 | Windows、真实对话、真实模型的行为 |
| E2 | 真实 Windows（CI 机器或任意电脑） | 界面和系统接口这一层能工作 | 判断质量 |
| E3 | 真实的对话来源：一份真实导出、真实的 Codex / Claude Code 会话，人工核对，留有记录 | 这种来源读得对 | 其他版本；判断质量 |
| E4 | 真实的旁听模型输出，经带标注的对话或人工检查 | 判断质量（在该模型、该提示词版本下） | 用户是否受益 |
| E5 | 用户试用：记录、访谈、对比 | 产品是否真的帮到人 | — |

## 2. 现状（0.4.0a2 预览版，2026-11-19）

0.4.0a1 把产品推倒重做了（见 [`design.md`](design.md)）；0.4.0a2 在它上面改进了界面，并加了 Windows 安装程序。
开发环境是 **Linux**：没有 Windows、没有模型密钥、没有任何真实对话。

**一句话：应用自己的规则和流程有 E1；0.4.0a1 的代码在 Windows 运行器上有 E2（只到「测试通过、窗口建得出来、打包后起得来」）；
0.4.0a2 新增的界面和安装程序在提交时只在 Linux 上跑过，Windows 上是 E0，等 CI；判断质量、真实对话、真实桌面全部是 E0。**

- `python3 -m unittest discover -s tests`：Linux 上 219 项通过（3 项跳过：Windows 上的 DPAPI、需要原始方案在场的一项、
  有了证据记录之后改由人工核对措辞的一项）。
  `python3 run.py --selftest` 的必需项（`watch`、`prompts`、`secretbox`）通过，`tk` 一项在 Linux 的显示上通过
  （0.4.0a2 起它还会把四个对话框各开两次、按 Esc 关一个、全部关掉再开一遍）。
- **0.4.0a1 在 Windows 运行器上**：CI 与一次性发布工作流都成功，见 [EV-2026-012](evidence/EV-2026-012.md)（E2；只读到了运行结论，
  没有逐步核对日志）。
- **0.4.0a2 新增的部分**（使用说明、分色的账本窗口、悬停状态、对话框只开一个 / Esc / 居中、窗口图标）：Linux 上的自动化测试，
  加上在 Linux 的显示上截图看过（E1）。**在 Windows 上还没有运行过。**
- **安装程序**（`packaging/windows/banxing.iss`、`build.ps1`、`verify-installer.ps1`）：在 Linux 上写成，这里既没有 Inno Setup
  也没有 PowerShell，**一次也没有运行过（E0）**。它的第一次运行就是 CI 的 Windows 任务：静默安装、检查快捷方式和卸载项、
  运行安装后的程序、覆盖再装一次、静默卸载、检查用户数据还在。CI 通过后是 E2（运行器），**不等于有人在真实桌面上点过安装向导**。
- **v0.3 的 Windows 证据（EV-2026-001 至 011）不适用于这一版。** 那些记录说的是已归档的草稿路线代码，原样保存在
  [`legacy/v0.3/docs/`](../legacy/v0.3/docs/verification.md)。
- **没有任何真实模型读过那两份提示词。** 离线替身是关键词规则；它在回放案例上「全部答对」是因为规则就是对着这些案例写的。
- **没有任何真实对话被读过**：ChatGPT 导出、Codex 会话、Claude Code 会话、从网页复制的聊天页面，测试里用的全是合成的。
- **没有人在真实的 Windows 桌面上用过它**：气泡不抢焦点、托盘、高分屏、多显示器、SmartScreen 对未签名安装程序的提示，都没有验。

## 3. 台账

| 编号 | 内容 | 状态 | 证据 | 依据 / 备注 |
| --- | --- | --- | --- | --- |
| W-01 | 对话模型：一轮三部分（用户 / 主模型 / 记录）、引文回查、只留证据的渲染、全文搜索、缺口、分支指纹 | 已实现 | E1 | `tests/test_watch_core.py::TranscriptTests` |
| W-02 | 账本：目标、立足点及其依据（记录 / 用户 / 仅主模型）、提过的问题及其处理；出处规则 | 已实现 | E1 | `test_watch_core.py::LedgerTests`。核心规则「主模型的话不能给主模型的结论作证」在这里 |
| W-03 | 六类疑点及各自必须拿得出的出处；「正在影响方向」的判定；安全闸 | 已实现 | E1（回复是脚本写好的）/ 真实模型 E0 | `test_watch_core.py::FindingTests` |
| W-04 | 多步审查：回头重读、全文搜索、独立一读；步数上限；范围限制；失败也计数 | 已实现 | E1 / 真实模型 E0 | `test_watch_core.py::AgencyTests`。提示词 `audit-1.0`、`blind-1.0` 没有被任何真实模型读过 |
| W-05 | 气泡规则：有出处、正在影响方向、来得及、只说一次、听用户的 | 已实现 | E1 | `test_watch_core.py::NudgeTests` |
| W-06 | 一条对话随时间的推进：分段读历史、只提交新变化、用户的四种回应、后续是否被记录解决、分支核对 | 已实现 | E1 | `tests/test_watch_session.py` |
| W-07 | 读取：ChatGPT 导出 | 已实现 | E1（**合成的**导出文件） | `tests/test_watch_sources.py`。文件布局按这类导出常见的样子写，官方说明页没有给出格式。真实导出 E0 |
| W-08 | 读取：Codex 会话文件；App Server 分页历史 | 已实现 | E1（合成文件；自建的假服务端）/ 真实 Codex E0 | 同上。App Server 的方法、参数、条目类型对照了 2026-11-18 的官方文档页 |
| W-09 | 读取：Claude Code 会话文件；从剪贴板粘贴的聊天页面；文字稿 | 已实现 | E1（合成）/ 真实数据 E0 | 同上。行的形状和「… said:」标记是假设 |
| W-10 | 逐条同意、范围、使用范围计数；加密或不存；本机记录不含内容 | 已实现 | E1（回环地址上的假服务端；DPAPI 失败靠替换平台判断来模拟） | `tests/test_watch_shell.py::WireTests`、`StorageTests`；`tests/test_config.py` |
| W-11 | 密钥与线路：密钥绑定地址、不跟随重定向、一次尝试一个请求、https | 已实现（代码沿用 v0.3，未改） | E1 | `tests/test_security.py`：v0.3 的重定向、密钥绑定、不重发各用例原样保留，发请求的入口换成了审查 |
| W-12 | 桌面外壳：图标、气泡、账本窗口（分色、汇总、可滚动可缩放）、选对话、模型设置、使用说明（首次启动自动打开）、悬停状态、托盘；每种对话框只开一个，Esc 关闭，居中 | 已实现 | E1（在 Linux 的显示上真实渲染并截图看过）；0.4.0a1 的外壳在 Windows 运行器上 E2（[EV-2026-012](evidence/EV-2026-012.md)）；0.4.0a2 新增的部分在 Windows 上 E0 | `test_watch_shell.py::ScreenTests`（打开两次、取消、Esc、关窗、再打开）。不抢焦点的行为只在真实桌面上才有意义，未验 |
| W-13 | 命令行 `--watch`、`--stepwise`、`--replay`、`--selftest`、`--summary` | 已实现 | E1 | `test_watch_shell.py::CommandLineTests` |
| W-14 | 逐轮回放评测：成对案例、时机 / 准头 / 误打扰 / 主动性、一次性对照 | 已实现 | E1（流程走通，报告标注仅流程） | 11 段**合成**对话。**没有 E4** |
| W-15 | 离线替身（旁听与独立一读各一个分支） | 已实现 | —（它本身不是证据） | `watch/standin.py` |
| W-16 | Windows 外壳所需的系统调用 | 已取出 | E2（运行器：自检的 `win32` 项，[EV-2026-012](evidence/EV-2026-012.md)）；真实桌面 E0 | `banxing/winshell.py` |
| W-17 | CI 与发布工作流 | 已改写 | `ci.yml` 的测试 / 自检 / 回放各步 E2（[EV-2026-012](evidence/EV-2026-012.md)）；它新增的打包与安装检查两步、以及 `release.yml` 本身 E0 | `.github/workflows/`。0.4.0a1 是由一次性工作流发布的，`release.yml` 从未运行 |
| W-18 | Windows 安装程序：按用户安装（不需要管理员权限）、开始菜单与可选的桌面快捷方式、「应用」里的卸载项；升级和卸载都不动 `%APPDATA%\Banxing` | 已实现 | E0，等 CI（通过后 E2：运行器上的静默安装 / 运行 / 重装 / 卸载）；安装向导由人点一遍 E0 | `packaging/windows/banxing.iss`、`build.ps1`、`verify-installer.ps1`；`tests/test_repo.py::PackagingTests` 只核对脚本之间说的是同一回事 |

## 4. 写在代码里的假设

全部写在 `banxing/watch/sources.py` 和 `banxing/context/appserver.py` 的文件头：

- ChatGPT 导出里有一个对话列表，每条对话是一棵消息树（`mapping`、`parent`、`current_node`），工具的调用和输出是树上的节点；
- Codex 会话文件里，命令调用是 `function_call`、输出是带同一个调用号的 `function_call_output`，一轮结束有完成事件；
- App Server 的 `thread/turns/list` 支持 `itemsView: "full"` 和游标翻页，`thread/items/list` 的每一项带 `turnId`；
- Claude Code 会话文件每行一个对象，`message.content` 里有 `text`、`tool_use`、`tool_result` 块，工具结果出现在 `type: user` 的行里；
- 从网页复制的聊天页面带「You said: / ChatGPT said:」（或「您说：/ ChatGPT 说：」）这样的标记；
- 一轮结束后，会话文件或线程的更新时间会变。

这几条不成立时，后果是读不到、读不全或晚读到。读到的内容仍然要经过引文回查，对不上的不会上账。

## 5. 还缺的证据，以及谁来补

| 要补的 | 等级 | 谁 | 怎么做 |
| --- | --- | --- | --- |
| 0.4.0a2 的新界面和安装程序在 Windows 运行器上 | E2 | 协调方 | 推送后看 CI：Windows 任务跑全部测试、`--selftest --require tk,win32`、打包、安装检查；报告在运行的 artifacts 里（`installer-check.json`） |
| 安装向导、SmartScreen、从旧版升级、卸载后的残留 | E2/E3（真实桌面） | 有 Windows 的人 | [`acceptance.md`](acceptance.md) 第 2 部分 U0 |
| 一份真实的导出 / Codex 会话 / Claude Code 会话 / 复制的聊天页面读得对不对 | E3 | 有这些数据的人 | [`acceptance.md`](acceptance.md) 第 1 部分；读错的地方只报结构，做成合成夹具 |
| 旁听模型在带标注的对话上的表现 | E4（合成对话上的结构检查） | 持有密钥的人 | `python run.py --replay --agree 地址 --report replay.json`；记下服务、模型、提示词版本 |
| 拿真实的长对话逐轮回放 | E4（需对话的主人逐条判断） | 有自己的长对话、并愿意把它发给旁听服务的人 | `python run.py --watch 路径 --stepwise --agree 地址 --report steps.json` |
| 气泡不抢焦点、托盘、剪贴板复制 | E2/E3 | 有 Windows 的人 | [`acceptance.md`](acceptance.md) 第 2 部分 |
| 有没有用 | E5 | 试用者 | [`acceptance.md`](acceptance.md) 第 4 部分；`--summary` 的 `wrong_rate` |

## 6. 自动化测试怎么跑

```
python -m unittest discover -s tests -v          # 全部；离线，只用合成数据
python run.py --selftest                         # 当前机器上的自检，--require tk,win32 可要求对应项必须通过
BANXING_PROVIDER=mock python run.py --replay     # 带标注的对话逐轮回放；替身模型下只说明流程走通
BANXING_PROVIDER=mock python run.py --watch 文件 # 读一条对话，打印账本和疑点；加 --stepwise 逐轮回放
```

归档的 v0.3 自带它当时的 416 项测试，在归档目录里照旧能跑：`cd legacy/v0.3 && python -m unittest discover -s tests`。
