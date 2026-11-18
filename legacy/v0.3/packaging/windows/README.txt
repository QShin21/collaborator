伴行 Banxing  ——  你先走，我陪着
================================================

照常在 Codex 里打字，把想做的事写下来，停一下，伴行在旁边给出三条路线（其中一条是主线）；
选一条，提示词就填进输入框：你的原话在最前面，一个字不改。发不发、改不改由你决定。

【怎么用】
1. 双击 Banxing.exe（请保留同一文件夹里的 _internal 文件夹）。
   Windows 提示“已保护你的电脑”时：点“更多信息” → “仍要运行”（程序没有代码签名）。
2. 第一次启动会打开“模型配置”：选择模型服务，填入 API Key。
   窗口里写明了草稿会发往哪个地址，确认后点“同意并开启跟随”。
3. 打开 Codex，右键伴行 → “校准（开启自动跟随）”，照提示做九个小动作，大约三分钟。
   校准通过之前，伴行不会自己读取或填写 Codex。
4. 在 Codex 输入框里写下想做的事，停大约 1.5 秒，旁边会出现气泡。
5. 点一条路线 = 把“原话 + 这条路线”写进输入框；行尾“预览”可先看全文。
6. 右键伴行打开菜单：陪伴方式（带路 / 提点 / 同行）、暂停跟随、恢复原草稿、关于我、
   粘贴草稿、内置示例、模型配置、校准、兼容性自检、导出证据包、清除本机数据、退出。

【三种陪伴方式】
带路：每份草稿给三条路线。  提点：只说这稿还缺什么，点一下在末尾补一句。
同行：只在关键时出声。      草稿已经可以直接发时，伴行不出气泡。

【还没配置模型或还没校准】
右键伴行 → “内置示例”，可以先看两组完整的路线；“粘贴草稿”可以对任意一段话要三条路线并复制。

【校准没通过 / 读不到输入框】
右键伴行 → “导出证据包”，先在窗口里看一遍全部内容，再保存成文件发给开发者。
证据包不包含你的草稿和聊天内容。

【隐私】
- 草稿只在你停顿时发送给你配置的模型服务；不想让它看聊天，在菜单里取消勾选“参考当前聊天”。
- API Key 和“关于我”加密保存在本机；采用/忽略统计只保存在本机，不含草稿内容。
- 伴行不会替你发送消息，不上传任何东西。

【数据在哪】
%APPDATA%\Banxing（菜单“打开数据文件夹”）。菜单“清除本机数据”可一键恢复初始状态。

【说明】
这是预览版：还没有人在真实的 Codex 窗口上完成读草稿与回填的验收，行为可能因 Codex 版本而异。
证据等级见 VERIFICATION.md，验收步骤见 ACCEPTANCE.md。

------------------------------------------------

Banxing — a companion beside Codex

Write an idea in the Codex input box and pause: Banxing offers three routes, one marked
as the main line. Click one to put the prompt into the input box — your own words first,
unchanged. You decide what gets sent.

1. Double-click Banxing.exe (keep the _internal folder next to it). The app is
   unsigned: on the SmartScreen prompt choose "More info" -> "Run anyway".
2. First run opens Model settings: choose a service and enter an API key. The window
   states where drafts are sent.
3. Right-click Banxing -> "Calibrate". Until the calibration has passed, Banxing neither
   reads nor writes Codex by itself. Built-in examples and "Paste a draft" work without it.
4. If calibration fails, use "Export evidence bundle" and send the file (it contains no
   draft or chat text).

This is a preview: nobody has yet verified it against a real Codex window. See
VERIFICATION.md and ACCEPTANCE.md. Settings and local statistics live in %APPDATA%\Banxing.
