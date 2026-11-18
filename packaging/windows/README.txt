伴行 Banxing  ——  你先走，我看着路
================================================

你和一个模型做一段长对话时，伴行请另一个模型在旁边读它：记下这段对话立在什么上面，
回头去查那些只是说过、没有查过的地方；思路走偏时提醒你一句，并给一句可以带回去问的话。
它只读，不往任何应用里写。目标和每一步都由你定。

【怎么用】
1. 双击 Banxing.exe（请保留同一文件夹里的 _internal 文件夹）。
   Windows 提示“已保护你的电脑”时：点“更多信息” → “仍要运行”（程序没有代码签名）。
2. 右键伴行 → “旁听用的模型”：选择模型服务，填入 API Key。
   这是在旁边读对话的那个模型，最好不是你正在对话的那一个。
3. 右键伴行 → “旁听一条对话”，选一条：
   - 在聊天网页里全选、复制，然后点“从剪贴板读取”；
   - 或一份 ChatGPT 导出（zip / 文件夹 / json）、Codex 或 Claude Code 的会话（jsonl）、文字稿。
   窗口里写明这条对话会从第几轮起发往哪个地址，确认后点“同意并开始旁听”。
4. 照常和主模型工作。思路走偏并且正在影响方向时，伴行旁边会出现一条气泡：
   哪里不对、它影响什么、依据是哪几轮的哪句话，以及一句可以原样带回去问主模型的追问。
   - “复制这句追问”：复制到剪贴板，由你粘贴给主模型。
   - “我知道，有意如此” / “这条不成立”：记下你的判断，这一类疑点不再出现。
5. 点一下伴行 = 现在看一眼。右键 → “账本”：这段对话的立足点里哪些有依据、哪些没查过，
   这次它回头查了什么，提交过哪些轮次、多少字、几次请求。
6. 网页聊天又长了：再全选复制一次，右键 → “对话有更新：从剪贴板再读一次”。

【隐私】
- 一条对话只在你为它点了“同意并开始旁听”之后才会发出，只发往窗口里写明的那个地址；地址变了就停，直到你重新确认。
- 你指定的起始轮次之前的内容不会被提交。看起来像密钥的文字会先被隐去。
- 对话本身不会被复制保存。账本默认只在内存里；勾选后才加密保存在本机，结束旁听即删除。
- API Key 加密保存在本机。统计只保存在本机，只有数字，不含对话内容。
- 伴行不读也不写任何应用的窗口，不替你发送任何东西。

【数据在哪】
%APPDATA%\Banxing（菜单“打开数据文件夹”）。菜单“清除本机数据”可一键恢复初始状态。

【说明】
这是预览版。旁听的模型判断得准不准，还没有用真实的模型和真实的对话评估过；
它的每一条提醒都附有出处，请以原文为准。证据等级见 VERIFICATION.md，验收步骤见 ACCEPTANCE.md。

------------------------------------------------

Banxing — an outside reader for a long conversation with an AI model

While you work with a model in a long conversation, Banxing has a different model read
alongside: it keeps a ledger of what the conversation rests on, goes back over what was only
said and never checked, and — when the thinking goes astray — shows one bubble with its
passages and a question you can take back to the main model. It only reads.

1. Double-click Banxing.exe (keep the _internal folder next to it). The app is unsigned: on
   the SmartScreen prompt choose "More info" -> "Run anyway".
2. Right-click Banxing -> "Supervising model": choose a service and enter an API key —
   preferably not the model you are talking to.
3. Right-click -> "Watch a conversation": copy a chat page and use "From the clipboard", or
   pick a ChatGPT export, a Codex or Claude Code session, or a transcript. The dialog states
   where the conversation is sent; nothing is sent before you agree.
4. A bubble appears only when something is off and bears on the direction. "Copy the
   question" puts it on the clipboard for you to paste; Banxing types nothing anywhere.

This is a preview: the supervising model's judgement has not been evaluated with a real
model or on real conversations. See VERIFICATION.md and ACCEPTANCE.md. Settings and local
statistics (numbers only) live in %APPDATA%\Banxing.
