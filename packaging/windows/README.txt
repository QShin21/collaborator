AI Collaborator  ——  与你一起解决真实问题
================================================

【怎么用】
1. 双击 AI-Collaborator.exe。会出现一个黑色窗口，浏览器随后自动打开。
   （如果没有自动打开，把黑色窗口里显示的网址复制到浏览器。）
2. 使用期间请保持黑色窗口开着；关掉它程序就停止了。
3. 第一次使用：点击页面左下角的“⚙ 离线演示”标签，选择模型服务并填入 API Key。
   不填 Key 也能体验界面和流程，但 AI 的回复是固定的演示内容。

【常见问题】
- Windows 提示“已保护你的电脑”：本程序没有代码签名。点“更多信息” → “仍要运行”。
- 我的数据在哪：和 exe 同一文件夹下的 data\collab.db，设置在 .env 文件里。
  备份或换电脑时把整个文件夹带走即可。若该文件夹不可写，数据会放在
  %LOCALAPPDATA%\AICollaborator。
- 端口被占用：程序会自动换一个端口，以黑色窗口里显示的网址为准。
- 共享屏幕读屏：需要使用支持图片输入的模型，并在浏览器弹窗中允许共享。

------------------------------------------------

HOW TO USE
1. Double-click AI-Collaborator.exe. A console window appears and your browser opens.
2. Keep the console window open while you work; closing it stops the app.
3. First run: click the "⚙ Offline demo" label at the bottom left of the page and enter
   your API key. Without a key the app runs with canned demo replies.

Your data lives next to the exe (data\collab.db, .env). The executable is unsigned, so
Windows SmartScreen may ask for confirmation: "More info" -> "Run anyway".
