"""Every user-visible string, in Chinese and English. Use t(key, **values)."""
from __future__ import annotations

import locale
import os
import sys

STRINGS = {
    "app_name": ("伴行 · 你先走，我陪着", "Banxing · you lead, I walk along"),
    # ---- bubble
    "bubble_title_fill": ("三条路线 · 选一条填入 Codex", "Three routes · pick one to fill Codex"),
    "bubble_title_copy": ("三条路线 · 选一条复制提示词", "Three routes · pick one to copy the prompt"),
    "bubble_title_spare": ("刚才那句还可以这样问", "That last message could also have been asked like this"),
    "hints_title_fill": ("这稿还可以补一句 · 点一下加到末尾", "One more sentence would help · click to add it"),
    "hints_title_copy": ("这稿还可以补一句 · 点一下复制", "One more sentence would help · click to copy it"),
    "hints_routes": ("要三条完整路线", "Give me three full routes"),
    "tag_main": ("主线", "Main"),
    "tag_side": ("支线", "Side"),
    "row_why": ("因为：{why}", "Because: {why}"),
    "bubble_more": ("换一批", "Another three"),
    "basis_draft": ("依据：草稿", "Based on: your draft"),
    "basis_chat": ("依据：草稿 + 这个聊天最近 {turns} 轮", "Based on: your draft + the last {turns} turns of this chat"),
    "basis_masked": ("已隐去 {count} 处疑似密钥", "{count} likely secret(s) masked"),
    "basis_assumes": ("假设：{what}", "Assuming: {what}"),
    "basis_example": ("内置示例，没有发送任何内容", "Built-in example; nothing was sent"),
    "basis_sent": ("这次发给模型的内容", "What was sent to the model this time"),
    "part_yours": ("你的原话", "Your words"),
    "part_do": ("现在做什么", "Do now"),
    "part_deliver": ("交成什么样", "Hand back"),
    "part_check": ("怎么检查", "How to check"),
    "caution_privilege": ("这条会要求更高的权限或跳过确认", "asks for elevated rights or to skip a confirmation"),
    "caution_destructive": ("这条会删除或覆盖东西，却没有预览或备份", "deletes or overwrites without a preview or backup"),
    "caution_other": ("这条请先看全文再决定", "read this one in full first"),
    "caution_confirm": ("我看过了，仍然填入", "I have read it — fill anyway"),
    "bubble_get": ("拿到{deliverable}", "Get: {deliverable}"),
    "bubble_preview": ("预览", "Preview"),
    "bubble_collapse": ("收起", "Hide"),
    "bubble_close": ("关闭", "Close"),
    "preview_fits": ("适用：{fits}", "Fits when: {fits}"),
    "preview_fill": ("填入 Codex", "Fill into Codex"),
    "preview_copy": ("复制", "Copy"),
    "bubble_example_note": ("内置示例 · 不经过模型", "Built-in example · no model involved"),
    # ---- status (pet tooltip / first menu line)
    "status_away": ("等待 Codex 窗口", "Waiting for a Codex window"),
    "status_idle": ("跟随中", "Following"),
    "status_thinking": ("正在想…", "Thinking…"),
    "status_uncalibrated": ("还没校准：不会自动读取或填写 Codex · 可粘贴草稿",
                            "Not calibrated: Codex is neither read nor written automatically · paste a draft instead"),
    "status_stalled": ("Codex 窗口暂时没有响应", "The Codex window is not responding"),
    "status_error_unsafe": ("这次的建议没通过安全检查 · 可重试", "The suggestions did not pass the safety check · retry"),
    "status_error_halted": ("模型连续失败，已暂停自动请求 · 点重试恢复",
                            "The model failed repeatedly; automatic requests are paused · retry to resume"),
    "level_lead": ("带路", "Lead"),
    "level_hint": ("提点", "Hint"),
    "level_walk": ("同行", "Walk along"),
    "level_lead_about": ("每稿给三条路线", "three routes for each draft"),
    "level_hint_about": ("只提醒缺什么", "only what is missing"),
    "level_walk_about": ("只在关键时出声", "only when it matters"),
    "status_paused": ("已暂停跟随", "Following is paused"),
    "status_blind": ("读不到 Codex 输入框 · 可粘贴草稿", "Cannot read the Codex input box · paste a draft instead"),
    "status_setup_model": ("还没配置模型", "No model configured yet"),
    "status_setup_consent": ("需要先确认草稿发往哪里", "Confirm where drafts are sent first"),
    "status_error_timeout": ("模型超时 · 草稿仍在 Codex，可重试", "The model timed out · your draft is untouched, retry from the menu"),
    "status_error_invalid": ("模型返回内容异常 · 可重试", "The model's reply was unusable · retry from the menu"),
    "status_error_auth": ("模型拒绝了 API Key · 请检查模型配置", "The model rejected the API key · check the model settings"),
    "status_error_network": ("连不上模型服务 · 可重试", "Cannot reach the model service · retry from the menu"),
    "status_error_redirect": ("模型服务要求跳转到别的地址，已拒绝 · 请在模型配置里填写最终地址",
                              "The model service tried to redirect; refused · enter the final address in Model settings"),
    "status_error_effort": ("模型服务不接受「思考强度」参数 · 请在模型配置里设为 none",
                            "The model service rejects the reasoning-effort field · set it to none in Model settings"),
    "status_error_other": ("生成失败 · 可重试", "Could not generate · retry from the menu"),
    # ---- menu
    "menu_pause": ("暂停跟随", "Pause following"),
    "menu_resume": ("继续跟随", "Resume following"),
    "menu_levels": ("陪伴方式", "How much I speak"),
    "menu_undo_step": ("改回「{level}」", "Go back to “{level}”"),
    "menu_spare": ("看看刚才那句还能怎么问", "See other ways to ask that last message"),
    "menu_aboutme": ("关于我…", "About me…"),
    "menu_calibrate": ("重新校准…", "Calibrate again…"),
    "menu_calibrate_first": ("校准（开启自动跟随）…", "Calibrate (turns on following)…"),
    "menu_evidence": ("导出证据包…", "Export evidence bundle…"),
    "menu_clear": ("清除本机数据…", "Clear local data…"),
    "menu_recent": ("最近反馈：采用 {adopts} · 忽略 {ignores}", "Recent feedback: {adopts} adopted · {ignores} ignored"),
    "menu_context": ("参考当前聊天", "Use the current chat as context"),
    "menu_now": ("现在给这份草稿三条路线", "Three routes for this draft now"),
    "menu_retry": ("重试", "Retry"),
    "menu_undo": ("恢复原草稿", "Restore my original draft"),
    "menu_paste": ("粘贴草稿…", "Paste a draft…"),
    "menu_examples": ("内置示例", "Built-in examples"),
    "menu_model": ("模型配置…", "Model settings…"),
    "menu_probe": ("兼容性自检…", "Compatibility self-check…"),
    "menu_folder": ("打开数据文件夹", "Open the data folder"),
    "menu_about": ("关于", "About"),
    "menu_hide": ("隐藏桌宠（托盘图标可找回）", "Hide the pet (find it in the tray)"),
    "menu_show": ("显示桌宠", "Show the pet"),
    "menu_quit": ("退出", "Quit"),
    # ---- notices
    "notice_filled": ("已填入 Codex，可继续编辑，由你决定何时发送。菜单里可恢复原草稿。",
                      "Filled into Codex. Edit it and send when you are ready. The menu can restore your draft."),
    "notice_copied": ("提示词已复制，可粘贴到 Codex。", "Prompt copied. Paste it into Codex."),
    "notice_started": ("伴行已启动。到 Codex 的输入框里写下想做的事，停一下，我会出现在窗口旁边。右键我可以打开菜单。",
                       "Banxing is running. Write an idea in the Codex input box and pause — I will appear next "
                       "to the window. Right-click me for the menu."),
    "notice_started_uncalibrated": ("伴行已启动，但还没校准，所以不会自动读取 Codex。右键我 → 校准；或者先用「粘贴草稿」。",
                                    "Banxing is running but not calibrated yet, so it will not read Codex by itself. "
                                    "Right-click me → Calibrate, or use “Paste a draft” for now."),
    "notice_ready": ("✓ 这稿可以直接发", "✓ Ready to send as it is"),
    "notice_spare": ("那句已经发出去了。我想到的三条路线先留着，点我可以看。",
                     "That message is already sent. I kept the three routes — click me to see them."),
    "notice_appended": ("已补到草稿末尾。菜单里可恢复原草稿。", "Added to the end of your draft. The menu can restore it."),
    "notice_stepped_down": ("最近的提示多被忽略，我改为「{level}」，少说一些。",
                            "Recent prompts were mostly ignored, so I switched to “{level}” and will say less."),
    "notice_cleared": ("本机数据已清除。", "Local data cleared."),
    "step_undo": ("改回去", "Undo"),
    "ask_quieter": ("最近的草稿都挺完整。要不要我少提示一些？", "Your recent drafts were complete. Shall I say less?"),
    "ask_yes": ("好，少一些", "Yes, less"),
    "ask_no": ("保持现在这样", "Keep as it is"),
    "notice_restored": ("已恢复原草稿。", "Your original draft is back."),
    "notice_restore_unavailable": ("输入框里的内容已经变了，没有改动它。", "The input box has changed, so nothing was touched."),
    "notice_restore_failed": ("没能恢复原草稿，输入框未被改动。", "Could not restore the draft. The input box was left alone."),
    "notice_nothing": ("这份草稿原样发给 Codex 就可以。", "This draft is ready to send to Codex as it is."),
    "notice_paused": ("跟随已暂停，可在菜单继续。", "Following is paused. Resume from the menu."),
    "notice_setup_model": ("还没配置模型。可先看内置示例，或打开「模型配置」。",
                           "No model yet. Try a built-in example or open Model settings."),
    "notice_setup_consent": ("请先在「模型配置」里确认草稿发往哪里。", "Confirm in Model settings where drafts are sent."),
    "notice_too_short": ("草稿太短，再多写一点想做什么。", "The draft is too short. Say a bit more about what you want."),
    "notice_too_long": ("草稿已经很完整了，直接发给 Codex 吧。", "The draft is already detailed. Send it to Codex."),
    "notice_too_command": ("这是一条命令，不需要建议。", "That is a command; no suggestions needed."),
    "notice_too_empty": ("先写一句想做的东西。", "Write a sentence about what you want first."),
    "notice_error": ("生成失败（{detail}），可重试。", "Could not generate ({detail}). Try again."),
    "offer_mismatch": ("草稿或聊天已经变了，没有回填。需要的话可以复制这条提示词。",
                       "The draft or chat changed, so nothing was filled. You can still copy this prompt."),
    "offer_failed": ("没能填入 Codex。可以复制这条提示词自己粘贴。",
                     "Could not fill Codex. Copy the prompt and paste it yourself."),
    "offer_clipboard_busy": ("剪贴板里有无法保留的内容，没有回填。可以复制这条提示词（会替换剪贴板）。",
                             "The clipboard holds something that could not be kept, so nothing was filled. "
                             "Copy the prompt instead (this replaces the clipboard)."),
    "offer_keys_held": ("检测到正按着修饰键，没有回填。松开后可再点一次，或复制提示词。",
                        "A modifier key was held down, so nothing was filled. Release it and click again, or copy."),
    "offer_copy": ("复制提示词", "Copy the prompt"),
    # ---- dialogs
    "ok": ("确定", "OK"),
    "cancel": ("取消", "Cancel"),
    "close": ("关闭", "Close"),
    "model_title": ("模型配置", "Model settings"),
    "model_provider": ("模型服务", "Service"),
    "provider_anthropic": ("Anthropic (Claude)", "Anthropic (Claude)"),
    "provider_openai": ("OpenAI 兼容接口", "OpenAI-compatible endpoint"),
    "provider_mock": ("离线演示（固定模板，不联网）", "Offline demo (templates, no network)"),
    "model_name": ("模型名称（留空用默认）", "Model name (blank = default)"),
    "model_url": ("接口地址（留空用官方地址）", "Base URL (blank = official)"),
    "model_key": ("API Key", "API key"),
    "model_key_saved": ("已保存（留空则沿用；更换接口地址必须重新输入）", "saved (blank keeps it; a new endpoint needs it again)"),
    "model_effort": ("思考强度", "Reasoning effort"),
    "model_disclosure": (
        "开启跟随后，你在 Codex 输入框里停顿的草稿，以及（可选）当前聊天最近三轮问答的相关片段，"
        "会发送到：{host}。只发往这个地址，不跟随跳转。\n采用/忽略的统计只保存在本机，且不含草稿内容。看起来像密钥的文字会先被隐去。",
        "While following, the draft you pause on in the Codex input box and (optionally) relevant excerpts of "
        "the last three turns of the current chat are sent to: {host}. Only there — redirects are not followed."
        "\nAdopt/ignore statistics stay on this "
        "computer and contain no draft text. Things that look like credentials are masked first."),
    "model_disclosure_local": ("离线演示不会发送任何内容。", "The offline demo sends nothing anywhere."),
    "model_use_context": ("参考当前聊天（发送相关片段）", "Use the current chat (send relevant excerpts)"),
    "model_save": ("同意并开启跟随", "Agree and start following"),
    "model_env_locked": ("模型服务和接口地址由环境变量 BANXING_PROVIDER / BANXING_BASE_URL 指定，这里不能修改。",
                         "The service and base URL come from BANXING_PROVIDER / BANXING_BASE_URL and cannot be "
                         "changed here."),
    "model_error_env": ("环境变量指定的模型配置不完整：更换了服务或接口地址时必须同时提供 BANXING_API_KEY，"
                        "且地址必须是 https（本机除外）。已保存的 Key 不会用于其他地址。",
                        "The model set by environment variables is incomplete: a changed service or base URL "
                        "needs its own BANXING_API_KEY, and the URL must be https (except localhost). The saved "
                        "key is never used for another address."),
    "model_error_destination": ("实际发送地址与刚才显示的不同（环境变量覆盖了设置）。上面已改为实际地址，确认无误后请再点一次。",
                                "Drafts would go somewhere other than what was shown (an environment variable "
                                "overrides the settings). The real destination is shown above now — press again "
                                "if you agree."),
    "model_error_provider": ("请选择模型服务。", "Choose a service."),
    "model_error_base_url": ("接口地址需要是 https://…（本机地址可用 http）。", "The base URL must be https://… (http only for localhost)."),
    "model_error_api_key": ("请填写 API Key。", "Enter an API key."),
    "model_error_encrypt": ("Windows 没能加密 API Key，所以什么都没有保存（Key 不会以明文写入）。请重新登录 Windows 后再试；"
                            "也可以改用环境变量 BANXING_API_KEY 提供 Key。",
                            "Windows could not encrypt the API key, so nothing was saved (the key is never written in "
                            "the clear). Sign in to Windows again and retry, or supply the key through the "
                            "BANXING_API_KEY environment variable instead."),
    "model_error_api_key_again": ("更换了服务或接口地址，请重新输入 API Key。", "The service or endpoint changed: enter the API key again."),
    "paste_title": ("粘贴草稿", "Paste a draft"),
    "paste_hint": ("把想交给 Codex 的那句话贴在这里：", "Paste what you were going to ask Codex:"),
    "paste_go": ("给我三条路线", "Give me three routes"),
    # ---- about me
    "aboutme_title": ("关于我", "About me"),
    "aboutme_hint": ("写几句关于你自己的事，一行一条，比如「我没有编程基础」「家里没有电钻」。每次请求都会带上，路线都会照办。"
                     "只加密保存在这台电脑上；清空即删除。",
                     "A few lines about yourself, one per line, e.g. “I cannot program”. They go with every request "
                     "and every route respects them. Kept encrypted on this computer only; clearing deletes them."),
    "aboutme_kinds": ("我注意到你选路线的习惯（只用于推荐主线，不会减少选择）：",
                      "What I noticed about the routes you pick (used only to choose the main line):"),
    "aboutme_reset": ("清零这些习惯", "Forget these habits"),
    "aboutme_save": ("保存", "Save"),
    "aboutme_error_encrypt": ("Windows 没能加密这段内容，所以没有保存（也没有以明文写入）。请重新登录 Windows 后再试；"
                              "问题持续时请导出证据包。",
                              "Windows could not encrypt this, so nothing was saved (and nothing was written in the "
                              "clear). Sign in to Windows again and retry; if it persists, export an evidence bundle."),
    "kind_verify": ("先验证", "verify first"),
    "kind_minimal": ("先做最小版", "smallest version first"),
    "kind_adapt": ("从现成的改", "adapt something ready-made"),
    "kind_delegate": ("交给别人或现成工具", "hand a step to someone else"),
    "kind_prototype": ("先做看得见的样子", "something to look at first"),
    "kind_plan": ("先定规则", "settle the rules first"),
    "kind_other": ("其他", "other"),
    "band_often": ("常选", "often"),
    "band_sometimes": ("一般", "sometimes"),
    "band_rarely": ("少选", "rarely"),
    # ---- calibration
    "cal_title": ("校准", "Calibration"),
    "cal_intro": ("校准用两三分钟，在你自己的 Codex 上做几个小动作，确认伴行读得对、写得对。通过之前，伴行不会自动读取或填写 Codex。"
                  "校准只记录对错、数量和耗时，不保存你输入的任何文字。",
                  "Calibration takes two or three minutes: a few small actions in your own Codex confirm that Banxing "
                  "reads and writes correctly. Until it passes, Banxing neither reads nor fills Codex by itself. "
                  "Only yes/no answers, counts and timings are recorded — nothing you type."),
    "cal_check": ("检查这一步", "Check this step"),
    "cal_skip": ("跳过", "Skip"),
    "cal_working": ("检查中…", "Checking…"),
    "cal_failed": ("这一步没通过。可以照说明再试一次，或者跳过。", "This step did not pass. Try again as described, or skip it."),
    "cal_failed_required": ("这一步没通过，后面的步骤依赖它。可以再试一次，或者结束校准。",
                            "This step did not pass and the rest depends on it. Try again, or finish."),
    "cal_finish": ("结束校准", "Finish"),
    "cal_do_window": ("点一下 Codex 的窗口，让它在最前面，然后回来点「检查这一步」。",
                      "Click the Codex window so it is in front, then come back and press Check."),
    "cal_do_read": ("点进 Codex 的输入框，输入下面这句话（不要发送），然后点「检查这一步」：\n\n{phrase}",
                    "Click into the Codex input box, type the phrase below (do not send it), then press Check:\n\n{phrase}"),
    "cal_do_empty": ("把输入框清空，然后点「检查这一步」。", "Clear the input box, then press Check."),
    "cal_do_ime": ("点「检查这一步」后，马上用中文输入法在输入框里打几个字，选词时停两秒。打完后请清空输入框。不用输入法可以跳过。",
                   "Press Check, then at once type a few words with an input method, pausing on the candidates. "
                   "Clear the box afterwards. Skip this if you do not use an input method."),
    "cal_do_fill": ("保持输入框是空的。点「检查这一步」后，伴行会写入一小段测试文字再删掉。",
                    "Keep the input box empty. After you press Check, Banxing writes a short test text and removes it."),
    "cal_do_focus": ("先点一下屏幕上的伴行图标，然后直接在键盘上打一个字母，再点「检查这一步」。完成后请清空输入框。",
                     "Click the Banxing figure on screen, then type one letter straight away, then press Check. "
                     "Clear the box afterwards."),
    "cal_do_chat": ("在 Codex 里切换到另一个聊天，然后点「检查这一步」。", "Switch to another chat in Codex, then press Check."),
    "cal_do_sessions": ("这一步不需要你做什么，点「检查这一步」。", "Nothing for you to do: press Check."),
    "cal_do_speed": ("这一步不需要你做什么，点「检查这一步」。", "Nothing for you to do: press Check."),
    "cal_step_window": ("找到 Codex 窗口", "Codex window found"),
    "cal_step_read": ("读到输入框里的文字", "Input box read correctly"),
    "cal_step_empty": ("认得出空的输入框", "Empty input box recognised"),
    "cal_step_ime": ("输入法（仅记录）", "Input method (recorded only)"),
    "cal_step_fill": ("写入并恢复", "Write and restore"),
    "cal_step_focus": ("点击后键盘仍在 Codex", "Keyboard stays in Codex after a click"),
    "cal_step_chat": ("能区分不同聊天", "Chats can be told apart"),
    "cal_step_sessions": ("能看到窗口里的文字", "Window text available"),
    "cal_step_speed": ("读取够快", "Reading is fast enough"),
    "cal_on": ("已开启：{what}", "Switched on: {what}"),
    "cal_none": ("没有开启任何自动能力。仍可使用粘贴草稿和内置示例。",
                 "No automatic ability was switched on. Paste mode and the examples still work."),
    "cal_no_fill": ("写入没有通过：选一条路线时会复制提示词，由你自己粘贴。",
                    "Writing did not pass: picking a route copies the prompt for you to paste."),
    "cal_focus_warning": ("注意：点击伴行后键盘可能离开了 Codex。请把这一点连同证据包反馈给开发者。",
                          "Note: the keyboard may leave Codex after a click on Banxing. Please report it with the evidence bundle."),
    "cal_marker_left": ("注意：写入测试中途输入框变了（切换了聊天或改了内容），伴行没有再碰它。原来的输入框里可能还留着"
                        "「〔伴行写入测试〕」这几个字，请手动删掉，然后重新校准。",
                        "Note: the input box changed during the write test (another chat, or an edit), so it was left "
                        "alone. The words “〔伴行写入测试〕” may still be in the original input box — delete them by "
                        "hand, then calibrate again."),
    "cal_slow_warning": ("注意：读取 Codex 比预期慢，可能会让 Codex 变卡。", "Note: reading Codex is slower than expected and may slow Codex down."),
    "cap_read": ("自动读草稿", "reading drafts"),
    "cap_fill": ("一键填入", "one-click fill"),
    "cap_chat_key": ("区分聊天", "telling chats apart"),
    "cap_signals": ("参考当前聊天", "using the current chat"),
    "cap_turn_events": ("知道一轮结束", "knowing when a turn ends"),
    "cap_selection": ("读取选区", "reading a selection"),
    # ---- evidence / clear
    "evidence_title": ("证据包", "Evidence bundle"),
    "evidence_intro": ("下面是将要保存的全部内容：校准结果、兼容性报告、界面结构（只有类型、位置和文字长度）和版本信息。"
                       "不含草稿、聊天和提示词。伴行不会上传它，保存到哪里、发不发给开发者由你决定。",
                       "Below is everything that will be saved: calibration results, the compatibility report, the "
                       "interface structure (types, positions and text lengths only) and versions. No draft, chat or "
                       "prompt text. Banxing never uploads it; where it goes is up to you."),
    "evidence_save": ("保存为文件…", "Save to a file…"),
    "evidence_saved": ("已保存。", "Saved."),
    "clear_confirm": ("这会删除伴行在这台电脑上保存的全部内容：模型配置和密钥、校准结果、陪伴方式、关于我、试用记录。要继续吗？",
                      "This deletes everything Banxing keeps on this computer: model settings and key, calibration, "
                      "level, about-me and the trial log. Continue?"),
    "probe_title": ("兼容性自检", "Compatibility self-check"),
    "probe_intro": ("请先切到 Codex，在输入框里随便写几个字，再回来点「开始检查」。报告不包含草稿和聊天内容。",
                    "Switch to Codex, type a few characters in its input box, then come back and press Check. "
                    "The report contains no draft or chat text."),
    "probe_run": ("开始检查", "Check"),
    "probe_fill": ("同时测试回填（会短暂改写输入框再恢复）", "Also test filling (briefly rewrites the input box, then restores it)"),
    "probe_learn": ("输入框为空时仍读到文字？清空输入框后点这里记录", "Empty box still reads as text? Clear it, then click here"),
    "probe_learned": ("已记录：今后把这段文字视为空输入框。", "Recorded: that text now counts as an empty box."),
    "probe_nothing_to_learn": ("输入框现在读到的是空的，无需记录。", "The box already reads as empty; nothing to record."),
    "probe_copy": ("复制报告", "Copy report"),
    "probe_running": ("检查中…", "Checking…"),
    "about_text": ("{name}\n版本 {version}\n\n数据文件夹：\n{folder}\n\n草稿只在你停顿时发送给你配置的模型服务；采用/忽略统计只保存在本机。",
                   "{name}\nVersion {version}\n\nData folder:\n{folder}\n\nDrafts go only to the model service you "
                   "configured, when you pause; adopt/ignore statistics stay on this computer."),
    "already_running": ("伴行已经在运行了（看看托盘图标）。", "Banxing is already running (look for the tray icon)."),
    "example_furniture": ("家具 DIY · 阳台三层花架", "Furniture DIY · a three-tier plant stand"),
    "example_software": ("个人软件 · 每天记几笔的记账工具", "Personal software · a simple expense tracker"),
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
