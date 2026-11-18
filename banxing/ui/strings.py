"""Every user-visible string, in Chinese and English. Use t(key, **values)."""
from __future__ import annotations

import locale
import os
import sys

STRINGS = {
    "app_name": ("伴行 · 你先走，我看着路", "Banxing · you lead, I watch the road"),
    "ok": ("确定", "OK"),
    "cancel": ("取消", "Cancel"),
    "close": ("关闭", "Close"),
    # ---- menu
    "menu_watch": ("旁听一条对话…", "Watch a conversation…"),
    "menu_look": ("现在看一眼", "Look now"),
    "menu_ledger": ("账本…", "Ledger…"),
    "menu_paste": ("对话有更新：从剪贴板再读一次", "The conversation grew: read it from the clipboard again"),
    "menu_pause": ("暂缓旁听", "Pause watching"),
    "menu_resume": ("继续旁听", "Resume watching"),
    "menu_stop": ("结束旁听", "Stop watching"),
    "menu_model": ("旁听用的模型…", "Supervising model…"),
    "menu_folder": ("打开数据文件夹", "Open the data folder"),
    "menu_clear": ("清除本机数据…", "Clear local data…"),
    "menu_about": ("关于", "About"),
    "menu_hide": ("隐藏伴行（托盘图标可找回）", "Hide Banxing (find it in the tray)"),
    "menu_show": ("显示伴行", "Show Banxing"),
    "menu_quit": ("退出", "Quit"),
    # ---- status
    "status_setup": ("还没配置旁听用的模型", "No supervising model configured yet"),
    "status_none": ("还没有在旁听的对话", "No conversation is being watched"),
    "status_on": ("旁听中 · 已读到第 {through} 轮", "Watching · read through turn {through}"),
    "status_auditing": ("旁听中 · 正在回查原文…", "Watching · going back over the conversation…"),
    "status_paused": ("旁听已暂缓", "Watching is paused"),
    "status_diverged": ("对话的历史变了 · 等你确认旁听哪一条", "The history changed · confirm what to watch"),
    "status_consent": ("旁听用的模型服务变了 · 需要重新同意", "The supervising service changed · agree again"),
    "status_unreadable": ("读不到这条对话了", "The conversation cannot be read"),
    "status_findings": ("账上记着 {count} 处疑点", "{count} concern(s) on the ledger"),
    # ---- choosing a conversation
    "watch_title": ("旁听一条对话", "Watch a conversation"),
    "watch_intro": ("选一条你正在和模型进行的长对话。伴行请另一个模型在旁边读它：记下这段对话立在什么上面，"
                    "回头查那些只是说过、没有查过的地方；思路走偏时提醒你一句，并给一句可以带回去问的话。"
                    "它只读，不往任何地方写。目标和每一步都由你定。",
                    "Pick a long conversation you are having with a model. Banxing has a different model read "
                    "alongside: it notes what the conversation rests on and goes back over what was only said, never "
                    "checked. When the thinking goes astray it tells you, with a question to take back. It only "
                    "reads. The goal and every step stay yours."),
    "watch_file": ("对话文件：ChatGPT 导出（zip / 文件夹 / json）、Codex 或 Claude Code 的会话（jsonl）、文字稿",
                   "Conversation file: a ChatGPT export (zip / folder / json), a Codex or Claude Code session "
                   "(jsonl), a transcript"),
    "watch_browse": ("浏览…", "Browse…"),
    "watch_load": ("读取", "Read"),
    "watch_sessions": ("Codex 最近的会话", "Recent Codex sessions"),
    "watch_clipboard": ("从剪贴板读取（先在聊天页面全选、复制）", "From the clipboard (select all on the chat page, copy)"),
    "watch_pick": ("选择其中一条：", "Choose one:"),
    "watch_listing": ("{title}  ·  {turns} 轮", "{title}  ·  {turns} turns"),
    "watch_untitled": ("（无标题）", "(untitled)"),
    "watch_pasted": ("剪贴板里的对话", "The conversation on the clipboard"),
    "watch_first": ("从第几轮开始读（更早的不提交）", "First turn to read (earlier ones are not sent)"),
    "watch_persist": ("在这台电脑上加密保存账本（不勾选：退出即失）",
                      "Keep the ledger on this computer, encrypted (unticked: gone when the app closes)"),
    "watch_disclosure": (
        "同意后，这条对话第 {first} 轮起的原文会发送到：{host}（模型 {model}）。第一次回查可取得的历史；之后只提交新增的轮次、"
        "账本条目指向的少数几轮，以及旁听的模型主动要求重读或搜索到的段落。只发往这个地址，不跟随跳转；看起来像密钥的文字会先被隐去。"
        "账本里随时能看到提交过哪些轮次、多少字、几次请求。伴行不往对话里写任何东西。",
        "If you agree, the text of this conversation from turn {first} on is sent to: {host} (model {model}). The "
        "first pass reads the history that is available; after that only new turns, the few turns the ledger points "
        "at, and passages the supervising model asks to re-read or finds by searching. Only there — redirects are not "
        "followed; things that look like credentials are masked first. The ledger always shows which turns, how many "
        "characters and how many requests were sent. Banxing writes nothing into the conversation."),
    "watch_disclosure_local": ("离线演示不会发送任何内容；它的判断只是关键词规则，不代表真实效果。",
                               "The offline demo sends nothing; its judgement is keyword rules and says nothing about "
                               "real quality."),
    "watch_agree": ("同意并开始旁听", "Agree and start watching"),
    "watch_agree_again": ("同意并继续旁听", "Agree and continue"),
    "watch_error_model": ("还没配置旁听用的模型。请先在菜单里打开「旁听用的模型」。",
                          "No supervising model is configured yet. Open “Supervising model” in the menu first."),
    "watch_error_destination": ("实际发送地址与刚才显示的不同。上面已改为实际地址，确认无误后请再点一次。",
                                "The conversation would go somewhere other than what was shown. The real destination "
                                "is shown above now — press again if you agree."),
    "watch_error_choose": ("请先读取一个文件或剪贴板，并选中一条对话。", "Read a file or the clipboard and choose a conversation first."),
    "watch_error_source": ("读不了（{why}）。", "It cannot be read ({why})."),
    "watch_error_first": ("起始轮次超出了这条对话的长度。", "The first turn is beyond the end of this conversation."),
    "source_error_missing": ("找不到这个文件或文件夹", "no such file or folder"),
    "source_error_too_large": ("内容太大", "it is too large"),
    "source_error_unreadable": ("打不开", "it cannot be opened"),
    "source_error_format": ("不是认识的对话格式", "not a conversation format this app knows"),
    "source_error_empty": ("里面没有对话内容", "there is no conversation in it"),
    "source_error_not_found": ("里面没有这条对话", "that conversation is not in it"),
    "source_error_appserver": ("Codex App Server 没有应答", "the Codex App Server did not answer"),
    "source_error_clipboard": ("剪贴板里没有文字", "there is no text on the clipboard"),
    # ---- the bubble
    "kind_drift": ("目标漂移", "Drift from the question"),
    "kind_premise": ("前提没查过", "An unchecked premise"),
    "kind_overreach": ("结论超出证据", "A conclusion beyond its evidence"),
    "kind_contradiction": ("前后矛盾", "A contradiction"),
    "kind_narrowing": ("过早收窄", "Narrowed too early"),
    "kind_stall": ("原地打转", "Polishing in place"),
    "nudge_why": ("它影响：{text}", "It bears on: {text}"),
    "nudge_probe": ("最省事的核验：{text}", "Cheapest check: {text}"),
    "nudge_ask": ("可以原样带回去问主模型：", "A question you can take back to the main model as it is:"),
    "nudge_sources": ("查看依据", "Show the sources"),
    "nudge_sources_hide": ("收起依据", "Hide the sources"),
    "nudge_copy": ("复制这句追问", "Copy the question"),
    "nudge_intended": ("我知道，有意如此", "I know — it is intended"),
    "nudge_wrong": ("这条不成立", "This does not hold"),
    "nudge_more": ("账本里还有 {count} 处", "{count} more on the ledger"),
    "nudge_footer": ("读到第 {through} 轮 · {mode}", "Read through turn {through} · {mode}"),
    "nudge_mode_auto": ("主模型刚结束一轮", "the main model just finished a turn"),
    "nudge_mode_manual": ("你让我看的", "you asked"),
    "nudge_now": ("眼下正靠它往前走的一步", "The step that rests on it right now"),
    "nudge_note_title": ("补一句", "Add a line"),
    "nudge_note_hint": ("已记下。想写一句理由的话（可留空）——下次回查时旁听的模型会看到：",
                        "Noted. A line on why, if you like (optional) — the supervising model sees it next time:"),
    "caution_privilege": ("这句追问会要求更高的权限或跳过确认", "the question asks for elevated rights or to skip a confirmation"),
    "caution_destructive": ("这句追问涉及删除或覆盖", "the question involves deleting or overwriting"),
    "caution_other": ("这条请先看全文再决定", "read this one in full first"),
    "ref_line": ("第 {turn} 轮 · {who}：「{quote}」", "Turn {turn} · {who}: “{quote}”"),
    "ref_user": ("你说", "you"),
    "ref_model": ("主模型说", "the main model said"),
    "ref_record": ("记录", "record"),
    # ---- the ledger
    "ledger_title": ("账本", "Ledger"),
    "ledger_empty": ("还没有读完这条对话。", "The conversation has not been read yet."),
    "ledger_goal": ("你想回答的问题", "The question you want answered"),
    "goal_confirmed": ("你说过", "in your words"),
    "goal_exploring": ("还在摸索，或只是主模型的提法", "still exploring, or only the main model's wording"),
    "goal_user": ("你亲手改写", "rewritten by you"),
    "goal_none": ("（还没看出来）", "(not clear yet)"),
    "ledger_open": ("待明确：{text}", "Still open: {text}"),
    "ledger_anchors": ("这段对话立在什么上面", "What this conversation rests on"),
    "anchor_decision": ("定下的做法", "decision"),
    "anchor_premise": ("前提", "premise"),
    "anchor_constraint": ("你的限制", "your constraint"),
    "anchor_result": ("结果", "result"),
    "status_backed": ("有依据", "backed"),
    "status_unchecked": ("没查过", "unchecked"),
    "status_broken": ("被推翻", "broken"),
    "status_retired": ("已放下", "let go"),
    "backing_record": ("有记录", "a record"),
    "backing_user": ("你说的", "your word"),
    "backing_model": ("只有主模型自己说过", "only the main model's word"),
    "ledger_carries": ("靠它的：{text}", "Rests on it: {text}"),
    "ledger_findings": ("这次看到的疑点", "Concerns from the latest look"),
    "weight_direction": ("正在影响方向", "bears on the direction"),
    "weight_note": ("记一笔", "a note"),
    "ledger_marks": ("提过的问题，和你的处理", "What was raised, and what you did with it"),
    "mark_line": ("第 {turn} 轮后 · {kind}：{title} —— {state}", "After turn {turn} · {kind}: {title} — {state}"),
    "mark_raised": ("你看到了，还没处理", "seen, not answered"),
    "mark_asked": ("你带回去问了", "you took it back"),
    "mark_intended": ("你说有意如此", "you said it is intended"),
    "mark_rejected": ("你认为不成立", "you said it does not hold"),
    "mark_resolved": ("后来解决了", "resolved later"),
    "ledger_gaps": ("读不到的部分（相关判断待确认）", "What could not be read (judgements resting on it are open)"),
    "ledger_none": ("（暂无）", "(none)"),
    "ledger_turns": ("第 {turns} 轮", "turn {turns}"),
    "ledger_last": ("最近一次回查", "The latest look"),
    "ledger_sound": ("没有发现走偏", "nothing is off"),
    "ledger_trace": ("它回头查了 {looks} 次：看了 {turns}，搜了 {searches} 个词（{found} 个有结果）",
                     "It went back {looks} time(s): looked at {turns}, searched {searches} term(s) ({found} found)"),
    "ledger_trace_none": ("它没有回头查", "It did not go back to look"),
    "ledger_trace_independent": ("做了一次不看主模型说法的独立一读", "made one blind reading that never saw the main model's words"),
    "ledger_usage": ("使用范围：已读到第 {through} 轮；提交过 {turns}（共 {chars} 字，请求 {requests} 次）→ {host}",
                     "Use: read through turn {through}; sent {turns} ({chars} characters, {requests} requests) → {host}"),
    "ledger_usage_none": ("使用范围：还没有提交过任何内容。", "Use: nothing has been sent yet."),
    "ledger_usage_local": ("这台电脑（离线演示）", "this computer (offline demo)"),
    "ledger_usage_masked": ("已隐去 {count} 处疑似密钥", "{count} likely secret(s) masked"),
    "ledger_correct": ("更正目标…", "Correct the goal…"),
    "ledger_correct_hint": ("用一两句话写下你想让这段对话回答什么：", "In a sentence or two: what should this conversation answer?"),
    "ledger_correct_save": ("以这个为准", "Use this"),
    "ledger_accept_branch": ("就旁听现在这条", "Watch the conversation as it is now"),
    "ledger_persist_on": ("加密保存这本账", "Keep this ledger (encrypted)"),
    "ledger_sent": ("上次提交了什么", "What was last sent"),
    "ledger_blind": ("独立一读说了什么", "What the blind reading said"),
    "gap_head": ("这条对话更早的部分读不到", "the earlier part of the conversation cannot be read"),
    "gap_scope": ("第 {turn} 轮之前的内容不在你授权的范围内", "turns before {turn} are outside the range you allowed"),
    "gap_attachment": ("第 {turn} 轮有 {count} 个附件或图片读不到", "turn {turn}: {count} attachment(s) or image(s) cannot be read"),
    "gap_output": ("第 {turn} 轮有 {count} 次运行没有留下输出", "turn {turn}: {count} run(s) left no output"),
    "gap_branch": ("这条对话有别的分支，这里只读了当前分支", "the conversation has other branches; only the current one is read"),
    "gap_compacted": ("第 {turn} 轮附近更早的内容被压缩成了摘要", "around turn {turn} earlier content was replaced by a summary"),
    # ---- notices
    "notice_started": ("伴行已启动。右键我 → 旁听一条对话。", "Banxing is running. Right-click me → Watch a conversation."),
    "notice_setup": ("还没配置旁听用的模型。右键我 → 旁听用的模型。", "No supervising model yet. Right-click me → Supervising model."),
    "notice_watch_started": ("开始旁听。我先把可取得的历史读一遍。", "Watching. I am reading the history that is available first."),
    "notice_watch_clear": ("看过了：没有发现走偏。{detail}", "Looked: nothing is off. {detail}"),
    "notice_watch_error": ("这次没看成（{detail}），可以再点一次「现在看一眼」。", "The look did not succeed ({detail}). Use “Look now” to try again."),
    "notice_watch_branch": ("这条对话的历史变了（改过更早的消息，或换了分支）。我先停下，等你在账本里确认旁听哪一条。",
                            "The history of this conversation changed (an earlier message was edited, or another "
                            "branch). I stop until you confirm in the ledger what to watch."),
    "notice_watch_consent": ("旁听用的模型服务和开始时不一样了。没有再提交任何内容；请在账本里重新确认。",
                             "The supervising service is not the one you agreed to. Nothing more was sent — confirm "
                             "again in the ledger."),
    "notice_watch_empty": ("这条对话里还没有主模型完成的轮次。", "No finished turn in this conversation yet."),
    "notice_watch_none": ("还没有在旁听的对话。右键我 → 旁听一条对话。", "Nothing is being watched. Right-click me → Watch a conversation."),
    "notice_watch_unreadable": ("读不到正在旁听的对话了（{detail}）。文件还在吗？", "The watched conversation cannot be read ({detail}). Is the file still there?"),
    "notice_watch_error_encrypt": ("Windows 没能加密账本，所以没有保存（也没有以明文写入）。这次旁听只在内存里，退出即失。",
                                   "Windows could not encrypt the ledger, so it was not saved (and nothing was written "
                                   "in the clear). This watch lives in memory only."),
    "notice_watch_error_newer": ("这台电脑上有一本由更新版本保存的账，没有覆盖它。这次旁听只在内存里。",
                                 "A ledger saved by a newer version is on this computer and was left alone. This watch "
                                 "lives in memory only."),
    "notice_watch_error_write": ("账本没能写入磁盘。这次旁听只在内存里。", "The ledger could not be written. This watch lives in memory only."),
    "notice_copied": ("已复制。粘贴给主模型；它怎么回答，我接着看。", "Copied. Paste it to the main model; I will read what it answers."),
    "notice_pasted": ("已从剪贴板读入更新后的对话。", "The updated conversation was read from the clipboard."),
    "notice_cleared": ("本机数据已清除。", "Local data cleared."),
    # ---- model dialog
    "model_title": ("旁听用的模型", "Supervising model"),
    "model_note": ("这里配置的是在旁边读对话的那个模型。它最好**不是**你正在对话的那一个：换一个模型，才不会对同一套说法照单全收。",
                   "This is the model that reads alongside. It should preferably NOT be the one you are talking to: a "
                   "different model does not take the same account at face value."),
    "model_provider": ("模型服务", "Service"),
    "provider_anthropic": ("Anthropic (Claude)", "Anthropic (Claude)"),
    "provider_openai": ("OpenAI 兼容接口", "OpenAI-compatible endpoint"),
    "provider_mock": ("离线演示（关键词规则，不联网）", "Offline demo (keyword rules, no network)"),
    "model_name": ("模型名称（留空用默认）", "Model name (blank = default)"),
    "model_url": ("接口地址（留空用官方地址）", "Base URL (blank = official)"),
    "model_key": ("API Key", "API key"),
    "model_key_saved": ("已保存（留空则沿用；更换接口地址必须重新输入）", "saved (blank keeps it; a new endpoint needs it again)"),
    "model_effort": ("思考强度", "Reasoning effort"),
    "model_where": ("对话只在你为某一条对话点了「同意并开始旁听」之后，才会发往：{host}。只发往这个地址，不跟随跳转。",
                    "A conversation is sent only after you press “Agree and start watching” for it, and then to: {host}. "
                    "Only there — redirects are not followed."),
    "model_where_local": ("离线演示不会发送任何内容。", "The offline demo sends nothing anywhere."),
    "model_save": ("保存", "Save"),
    "model_env_locked": ("模型服务和接口地址由环境变量 BANXING_PROVIDER / BANXING_BASE_URL 指定，这里不能修改。",
                         "The service and base URL come from BANXING_PROVIDER / BANXING_BASE_URL and cannot be "
                         "changed here."),
    "model_error_provider": ("请选择模型服务。", "Choose a service."),
    "model_error_base_url": ("接口地址需要是 https://…（本机地址可用 http）。", "The base URL must be https://… (http only for localhost)."),
    "model_error_api_key": ("请填写 API Key。", "Enter an API key."),
    "model_error_api_key_again": ("更换了服务或接口地址，请重新输入 API Key。", "The service or endpoint changed: enter the API key again."),
    "model_error_encrypt": ("Windows 没能加密 API Key，所以什么都没有保存（Key 不会以明文写入）。请重新登录 Windows 后再试；"
                            "也可以改用环境变量 BANXING_API_KEY 提供 Key。",
                            "Windows could not encrypt the API key, so nothing was saved (the key is never written in "
                            "the clear). Sign in to Windows again and retry, or supply the key through the "
                            "BANXING_API_KEY environment variable instead."),
    # ---- other
    "clear_confirm": ("这会删除伴行在这台电脑上保存的全部内容：模型配置和密钥、保存的账本、试用记录，并结束正在进行的旁听。要继续吗？",
                      "This deletes everything Banxing keeps on this computer: model settings and key, a kept ledger "
                      "and the trial log — and stops watching. Continue?"),
    "about_text": ("{name}\n版本 {version}\n\n数据文件夹：\n{folder}\n\n被旁听的对话只发往你为它同意过的那个地址；统计只保存在本机，不含内容。"
                   "伴行只读，不往任何应用里写。",
                   "{name}\nVersion {version}\n\nData folder:\n{folder}\n\nA watched conversation goes only to the "
                   "destination you agreed to for it; statistics stay on this computer and hold no content. Banxing "
                   "only reads; it writes into no application."),
    "already_running": ("伴行已经在运行了（看看托盘图标）。", "Banxing is already running (look for the tray icon)."),
}

_LANG = "zh"


def detect_language():
    if sys.platform == "win32":
        try:
            import ctypes
            primary = ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF
            return "zh" if primary == 0x04 else "en"
        except (OSError, AttributeError):
            pass
    name = os.environ.get("LC_ALL") or os.environ.get("LANG") or (locale.getlocale()[0] or "")
    return "zh" if name.casefold().startswith("zh") else "en"


def set_language(language):
    global _LANG
    _LANG = language if language in ("zh", "en") else detect_language()
    return _LANG


def language():
    return _LANG


def t(key, **values):
    pair = STRINGS.get(key)
    if pair is None:
        return key
    text = pair[0 if _LANG == "zh" else 1]
    return text.format(**values) if values else text
