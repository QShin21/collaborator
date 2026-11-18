"""The acceptance checklist (docs/verification.md, section 2), run against a
simulated Codex.

Each class names the row it covers. These prove the product logic end to end —
draft → pause → analysis → bubble → click → fill → feedback — with synthetic
input. Rows that can only be judged on a real Codex window or with a real model
(keyboard focus, IME, whether a beginner can choose, whether Codex delivers) are
listed in docs/verification.md and are NOT claimed here.
"""
import unittest

import safety  # noqa: F401  (keeps tests away from real user data)

from banxing import examples, levels as freq, llm, mock
from banxing.context import ContextProvider
from banxing.hosts.base import FillResult
from banxing.hosts.simulated import SimulatedCodex

from rig import RigCase, THREAD_A, THREAD_B, write_session

FURNITURE = examples.get("furniture")["draft"]
SOFTWARE = examples.get("software")["draft"]
OTHER = "帮我把读书笔记做成可以检索的网页，我不会编程"


class ClipboardRestorationWarning(RigCase):
    def test_a_successful_fill_still_warns_if_the_clipboard_could_not_be_restored(self):
        class Warns(SimulatedCodex):
            def fill(self, key, expect, text):
                result = super().fill(key, expect, text)
                return FillResult(result.ok, "clipboard_restore" if result.ok else result.reason,
                                  result.method)

        rig = self.rig(client=Warns())
        rig.pause_on(FURNITURE)
        rig.controller.pick(0)
        self.assertIn("filled", rig.view.notices)
        self.assertIn("clipboard_restore", rig.view.notices)


class AppearsByItself(RigCase):
    """Row "停顿后自己出现": write an idea, pause, three suggestions appear."""

    def test_bubble_appears_after_the_pause_and_not_before(self):
        rig = self.rig()
        rig.type(FURNITURE)
        rig.wait(1.25)
        self.assertIsNone(rig.view.bubble)
        self.assertEqual(rig.calls, [])
        rig.wait(0.5)
        self.assertEqual(rig.rows(), [("先做纸样比一比", "能打印的等大纸样"), ("先做单层练手", "单层的图纸和材料单"),
                                      ("买现成的来改", "改装步骤和采购清单")])
        self.assertEqual(len(rig.calls), 1)
        self.assertEqual(rig.codex.draft, FURNITURE)          # nothing was touched
        self.assertEqual(rig.view.bubble.reason, "")

    def test_pet_shows_generating_then_idle(self):
        rig = self.rig(hold=True)
        rig.pause_on(SOFTWARE)
        self.assertEqual(rig.view.state[0], "thinking")
        rig.executor.release()
        self.assertEqual(rig.view.state[0], "idle")
        self.assertEqual(rig.rows()[0], ("先用表格搭一版", "带公式的记账表"))


class TimingIsRight(RigCase):
    """Row "不该出声时不出声": standby while typing / composing; old suggestions go
    away; a closed or adopted suggestion is not repeated for the same text."""

    def test_continuous_typing_never_triggers(self):
        rig = self.rig()
        for length in range(4, len(FURNITURE), 2):
            rig.type(FURNITURE[:length])
            rig.wait(0.5)
        self.assertEqual(rig.calls, [])
        self.assertEqual(rig.view.state[0], "idle")

    def test_ime_composition_holds_the_timer(self):
        rig = self.rig()
        rig.codex.composing = True
        rig.type(FURNITURE, then_wait=4.0)
        self.assertEqual(rig.calls, [])
        rig.codex.composing = False
        rig.wait(1.0)
        self.assertEqual(rig.calls, [])                       # the pause starts when composing ends
        rig.wait(0.75)
        self.assertIsNotNone(rig.view.bubble)

    def test_empty_box_short_text_and_commands_stay_quiet(self):
        rig = self.rig()
        for text in ("", "继续", "好的谢谢", "/status 看看现在的状态怎么样"):
            rig.pause_on(text)
        self.assertEqual(rig.calls, [])

    def test_editing_closes_the_bubble_and_a_real_change_gets_a_new_one(self):
        rig = self.rig()
        rig.pause_on(OTHER)
        first = rig.view.bubble
        rig.type(OTHER + "，笔记")
        self.assertIsNone(rig.view.bubble)
        self.assertEqual(rig.view.hidden[-1], "edited")
        rig.type(OTHER + "，笔记都是 Markdown 文件", then_wait=2.0)
        self.assertIsNotNone(rig.view.bubble)
        self.assertNotEqual(rig.view.bubble.id, first.id)
        self.assertTrue(all("Markdown" in s.prompt for s in rig.view.bubble.routes))

    def test_tidying_a_prompted_draft_does_not_prompt_again(self):
        rig = self.rig()
        rig.pause_on(OTHER)
        rig.type(OTHER + "。", then_wait=3.0)
        self.assertIsNone(rig.view.bubble)
        self.assertEqual(len(rig.calls), 1)

    def test_switching_chat_or_app_closes_the_bubble(self):
        rig = self.rig()
        rig.pause_on(OTHER)
        rig.codex.switch_chat("chat-b")
        rig.tick()
        self.assertEqual(rig.view.hidden[-1], "chat_switch")
        rig.codex.switch_chat("chat-a")
        rig.wait(3.0)
        self.assertIsNone(rig.view.bubble)                    # same text: once only
        rig.type(OTHER + "，最好能离线用", then_wait=2.0)
        self.assertIsNotNone(rig.view.bubble)
        rig.codex.foreground = False
        rig.tick()
        self.assertEqual(rig.view.hidden[-1], "app_switch")
        self.assertEqual(rig.view.state[0], "away")
        rig.codex.foreground = True
        rig.wait(3.0)
        self.assertIsNone(rig.view.bubble)
        self.assertEqual(len(rig.calls), 2)

    def test_closed_bubble_is_not_shown_again_for_the_same_text(self):
        rig = self.rig()
        rig.pause_on(OTHER)
        rig.controller.dismiss()
        rig.wait(5.0)
        self.assertIsNone(rig.view.bubble)
        self.assertEqual(len(rig.calls), 1)

    def test_late_result_is_dropped_unless_everything_still_matches(self):
        rig = self.rig(hold=True)
        rig.pause_on(OTHER)
        rig.type(OTHER + "，而且要支持标签")                    # edited while the model works
        rig.executor.release(1)
        self.assertIsNone(rig.view.bubble)
        self.assertEqual(len(rig.events("late")), 1)
        self.assertEqual(rig.feedback(), [])                  # unfinished, not feedback

        rig.wait(2.0)                                         # the new text gets its own request
        rig.codex.switch_chat("chat-b")
        rig.tick()
        rig.executor.release()
        self.assertIsNone(rig.view.bubble)

    def test_result_for_a_draft_that_was_edited_and_restored_is_still_shown(self):
        rig = self.rig(hold=True)
        rig.pause_on(OTHER)
        rig.type(OTHER + "啊")
        rig.type(OTHER)
        rig.executor.release(1)
        self.assertIsNotNone(rig.view.bubble)

    def test_result_arriving_after_send_is_dropped(self):
        rig = self.rig(hold=True)
        rig.pause_on(OTHER)
        rig.send()
        rig.executor.release()
        self.assertIsNone(rig.view.bubble)
        self.assertEqual(rig.feedback(), [])


class FrequencyFollowsFeedback(RigCase):
    """Row "频率随反馈变化"."""

    def drafts(self, count):
        return [f"帮我做第 {n} 个小工具：把下载目录里的发票按月份归档" for n in range(count)]

    def test_adopting_keeps_every_draft(self):
        rig = self.rig()
        for draft in self.drafts(8):
            rig.pause_on(draft)
            rig.controller.pick(0)
            rig.send()
        self.assertEqual(rig.level, freq.LEAD)
        self.assertEqual(rig.feedback(), [freq.ADOPT] * 6)

    def test_four_ignores_in_six_switch_to_critical_and_tell_the_user(self):
        rig = self.rig()
        plan = ["x", 0, "x", 0, "x", "x"]
        for draft, action in zip(self.drafts(6), plan):
            rig.pause_on(draft)
            self.assertIsNotNone(rig.view.bubble, draft)
            if action == "x":
                rig.controller.dismiss()
            else:
                rig.controller.pick(action)
            rig.send()
        self.assertEqual(rig.level, freq.HINT)               # one level quieter, not straight to silence
        self.assertIn("stepped_down", rig.view.notices)
        self.assertEqual(rig.machine.levels.stepped_from, freq.LEAD)
        # The level survives a restart.
        self.assertEqual(rig.store.load_levels().level, freq.HINT)
        # "改回去" puts it back, and the count starts over.
        rig.controller.undo_step()
        self.assertEqual((rig.level, rig.feedback()), (freq.LEAD, []))

    def test_unfinished_prompts_are_not_feedback(self):
        rig = self.rig()
        for index, draft in enumerate(self.drafts(8)):
            rig.pause_on(draft)
            if index % 2:
                rig.type(draft + "，还要能导出表格")           # edited away
                rig.send()
            else:
                rig.codex.switch_chat(f"other-{index}")       # switched away
                rig.tick()
                rig.codex.switch_chat("chat-a")
                rig.tick()
                rig.send()
        self.assertEqual(rig.feedback(), [])
        self.assertEqual(rig.level, freq.LEAD)
        self.assertGreaterEqual(len(rig.events("incomplete")), 8)

    def test_sending_the_original_counts_only_after_two_seconds_on_screen(self):
        rig = self.rig()
        first, second = self.drafts(2)
        rig.type(first, then_wait=1.5)
        self.assertIsNotNone(rig.view.bubble)
        rig.wait(1.0)
        rig.send()                                            # ~1.25 s on screen
        self.assertEqual(rig.feedback(), [])
        rig.type(second, then_wait=1.5)
        rig.wait(2.5)
        rig.send()
        self.assertEqual(rig.feedback(), [freq.IGNORE])

    def test_one_message_edited_many_times_counts_once_and_can_become_adopt(self):
        rig = self.rig()
        draft = self.drafts(1)[0]
        rig.pause_on(draft)
        rig.controller.dismiss()
        rig.pause_on(draft + "，发票是 PDF")
        rig.controller.dismiss()
        self.assertEqual(rig.feedback(), [freq.IGNORE])
        rig.pause_on(draft + "，发票是 PDF，文件名里有日期")
        rig.controller.pick(1)
        self.assertEqual(rig.feedback(), [freq.ADOPT])

    def test_undo_cancels_the_adoption(self):
        rig = self.rig()
        draft = self.drafts(1)[0]
        rig.pause_on(draft)
        rig.controller.pick(0)
        self.assertEqual(rig.feedback(), [freq.ADOPT])
        rig.tick()
        self.assertTrue(rig.controller.menu()["can_undo"])
        rig.controller.undo()
        self.assertEqual(rig.codex.draft, draft)
        self.assertEqual(rig.feedback(), [])
        rig.wait(4.0)
        self.assertIsNone(rig.view.bubble)                    # the restored draft was already answered

    def test_user_can_return_to_every_draft_and_observation_restarts(self):
        rig = self.rig()
        for draft in self.drafts(4):
            rig.pause_on(draft)
            rig.controller.dismiss()
            rig.send()
        self.assertEqual(rig.level, freq.HINT)
        rig.controller.set_level(freq.LEAD)
        self.assertEqual((rig.level, rig.feedback()), (freq.LEAD, []))
        rig.pause_on("再来一个：把会议录音转成带时间戳的文字稿")
        self.assertIsNotNone(rig.view.bubble)


class CriticalCasesWithChatContext(RigCase):
    """Rows "关键情况判断" and "认对当前聊天", through the whole chain: session
    files → recognising the chat on screen → excerpt → request → decision.

    The decision itself comes from the offline stand-in (a keyword rule). What is
    verified is that the right chat's excerpt reaches the model and that the
    answer drives the bubble; a real model's judgement is a manual check."""

    ASK = "帮我做一个阳台花架的尺寸示意"
    MEASURED = "宽 76 进深 28 高 95，层板用 18 毫米的松木板"
    PLAN_EXAMPLE = ("我做了一个花架的示意页 rack.html。\n\n页面里的尺寸目前都是示例值：宽 80、进深 30、高 100，"
                    "等你量好阳台再替换。\n\n下一步可以先量阳台能用的宽度。")
    PLAN_CONFIRMED = ("已按你量的数字更新示意页：宽 76、进深 28、高 95，层板是 18 毫米的松木板。\n\n"
                      "尺寸和板材都已确认，rack.html 可以用来列材料清单了。")
    ORDER = "就按这个尺寸列材料清单，我今晚下单。"
    PAGE = [("帮我做一个可以离线用的记账页面", "已生成 ledger.html，可以直接双击打开。\n\n标题目前是黑色，汇总在页面底部。")]
    RESTYLE = "把记账页面的标题改成深蓝色。"

    def context_rig(self, sessions):
        rig = self.rig()
        root = rig.folder / "sessions"
        for thread_id, turns in sessions.items():
            write_session(root, thread_id, "C:\\proj\\balcony", turns)
        provider = ContextProvider(sessions_dir=str(root), use_appserver=False)
        rig.suggester._context = provider
        rig.controller.set_level(freq.WALK)
        return rig

    def show(self, rig, chat, turns):
        """Put a chat on screen the way the window would render it."""
        texts = []
        for user, answer in turns:
            texts.append(user)
            texts.extend(answer.split("\n\n"))
        rig.codex.visible[chat] = texts
        rig.codex.switch_chat(chat)

    def test_example_numbers_prompt_and_confirmed_numbers_stay_quiet(self):
        open_turns = [(self.ASK, self.PLAN_EXAMPLE)]
        rig = self.context_rig({THREAD_A: open_turns})
        self.show(rig, "chat-a", open_turns)
        rig.pause_on(self.ORDER)
        self.assertIsNotNone(rig.view.bubble)
        self.assertEqual(rig.view.bubble.reason, "上一轮的数值还是示例，没有换成实际的。")
        self.assertEqual(len(rig.view.bubble.routes), 3)
        self.assertIn("示例值", rig.calls[-1]["user"])
        self.assertIn("<mode>walk</mode>", rig.calls[-1]["user"])
        # The request itself should not be carried out as it stands: the draft is quoted.
        self.assertTrue(all(r.prompt.startswith("我本来想这样说：「" + self.ORDER + "」") for r in rig.view.bubble.routes))
        self.assertEqual(rig.view.bubble.basis, (True, 1, 0))

        done_turns = open_turns + [(self.MEASURED, self.PLAN_CONFIRMED)]
        rig = self.context_rig({THREAD_A: done_turns})
        self.show(rig, "chat-a", done_turns)
        rig.pause_on(self.ORDER)
        self.assertIsNone(rig.view.bubble)
        self.assertEqual(len(rig.calls), 1)
        self.assertIn("尺寸和板材都已确认", rig.calls[-1]["user"])
        self.assertEqual(len(rig.events("standby")), 1)

    def test_draft_that_is_ready_stays_quiet(self):
        rig = self.context_rig({THREAD_A: self.PAGE})
        self.show(rig, "chat-a", self.PAGE)
        rig.pause_on(self.RESTYLE)
        self.assertIn("ledger.html", rig.calls[-1]["user"])   # the chat was recognised and quoted
        self.assertIsNone(rig.view.bubble)
        self.assertEqual(rig.view.state[0], "idle")

    def test_two_chats_of_one_project_use_the_one_in_front(self):
        chat_a = [(self.ASK, self.PLAN_EXAMPLE)]
        chat_b = [(self.ASK, self.PLAN_EXAMPLE), (self.MEASURED, self.PLAN_CONFIRMED)]
        rig = self.context_rig({THREAD_A: chat_a, THREAD_B: chat_b})
        self.show(rig, "chat-b", chat_b)
        rig.pause_on(self.ORDER)
        self.assertIsNone(rig.view.bubble)                    # B is settled
        self.assertIn("尺寸和板材都已确认", rig.calls[-1]["user"])

        self.show(rig, "chat-a", chat_a)
        rig.pause_on(self.ORDER)
        self.assertIsNotNone(rig.view.bubble)                 # A still has example numbers
        self.assertNotIn("尺寸和板材都已确认", rig.calls[-1]["user"])
        self.assertEqual(rig.calls[-1]["meta"]["chat"].count("[第 1 轮 · 用户]"), 1)

    def test_unrecognised_chat_falls_back_to_the_draft_alone(self):
        turns = [(self.ASK, self.PLAN_EXAMPLE)]
        rig = self.context_rig({THREAD_A: turns})
        rig.codex.visible["chat-new"] = ["新聊天", "有什么可以帮你？"]
        rig.codex.switch_chat("chat-new")
        rig.pause_on(self.ORDER)
        self.assertEqual(rig.calls[-1]["meta"]["chat"], "")
        self.assertIn("没有可用的聊天片段", rig.calls[-1]["user"])
        self.assertIsNone(rig.view.bubble)                    # critical mode, nothing known: quiet

    def test_turning_context_off_sends_the_draft_only(self):
        turns = [(self.ASK, self.PLAN_EXAMPLE)]
        rig = self.context_rig({THREAD_A: turns})
        self.show(rig, "chat-a", turns)
        rig.controller.set_use_context(False)
        rig.pause_on(self.ORDER)
        self.assertEqual(rig.calls[-1]["meta"]["chat"], "")
        self.assertNotIn("示例值", rig.calls[-1]["user"])

    def test_clicking_the_pet_gives_suggestions_even_when_the_pet_chose_silence(self):
        rig = self.context_rig({THREAD_A: self.PAGE})
        self.show(rig, "chat-a", self.PAGE)
        rig.pause_on(self.RESTYLE)
        self.assertIsNone(rig.view.bubble)
        rig.controller.pet_clicked()
        self.assertEqual(len(rig.view.bubble.routes), 3)
        rig.controller.dismiss()
        self.assertEqual(rig.feedback(), [])                  # closing what you asked for is not an ignore


class ConditionsAreKept(RigCase):
    """Row "条件不丢" — as far as the app is responsible: the condition reaches
    the request intact and every suggestion row carries a full prompt.
    Whether a real model honours it is checked with a real model."""

    def test_conditions_reach_the_request_and_every_prompt(self):
        rig = self.rig()
        rig.pause_on("想给猫做一个爬架，家里没有电钻")
        self.assertIn("家里没有电钻", rig.calls[-1]["user"])
        self.assertIn("工具与加工", rig.calls[-1]["user"])     # the furniture reminders came along
        self.assertTrue(all("家里没有电钻" in s.prompt for s in rig.view.bubble.routes))
        rig.send()
        rig.pause_on("想做个小工具，把下载文件夹里的发票按月份归档，不要改动原文件")
        self.assertIn("撤销办法", rig.calls[-1]["user"])       # the file-handling reminders came along
        self.assertTrue(all("不要改动原文件" in s.prompt for s in rig.view.bubble.routes))


class OneClickFill(RigCase):
    """Row "点一下就回填": the click itself fills; the text stays unsent; chat, box
    and draft are checked first."""

    def test_click_replaces_the_draft_with_the_full_prompt_and_does_not_send(self):
        rig = self.rig()
        rig.pause_on(FURNITURE)
        prompt = rig.view.bubble.routes[0].prompt
        rig.controller.pick(0)
        self.assertEqual(rig.codex.draft, prompt)
        self.assertEqual(rig.codex.sent, [])
        self.assertIsNone(rig.view.bubble)
        self.assertEqual(rig.view.hidden[-1], "adopted")
        self.assertEqual(rig.codex.focus_returned, 1)
        self.assertIn("filled", rig.view.notices)
        rig.wait(5.0)
        self.assertIsNone(rig.view.bubble)                    # the filled prompt is not analysed
        self.assertEqual(len(rig.calls), 1)

    def test_user_may_keep_editing_the_filled_prompt_without_being_prompted(self):
        rig = self.rig()
        rig.pause_on(FURNITURE)
        prompt = rig.view.bubble.routes[0].prompt
        rig.controller.pick(0)
        rig.type(prompt + "桌下净宽大约 60 厘米。", then_wait=4.0)
        self.assertIsNone(rig.view.bubble)
        self.assertEqual(len(rig.calls), 1)

    def test_fill_is_refused_when_the_draft_changed_under_the_bubble(self):
        rig = self.rig()
        rig.pause_on(FURNITURE)
        prompt = rig.view.bubble.routes[0].prompt
        rig.codex.type(FURNITURE + "家里没有电钻和锯子。")      # changed, not yet observed
        rig.controller.pick(0)
        self.assertEqual(rig.codex.draft, FURNITURE + "家里没有电钻和锯子。")
        self.assertEqual(rig.codex.fills, [])
        self.assertEqual(rig.view.offers, [(prompt, "mismatch")])
        self.assertIsNone(rig.view.bubble)
        self.assertEqual(rig.feedback(), [])

    def test_fill_is_refused_in_another_chat(self):
        rig = self.rig()
        rig.pause_on(FURNITURE)
        rig.codex.switch_chat("chat-b")
        rig.codex.type(FURNITURE)                             # same text, different chat
        rig.controller.pick(0)
        self.assertEqual(rig.codex.fills, [])
        self.assertEqual(rig.codex.draft, FURNITURE)

    def test_when_fill_fails_the_chosen_prompt_can_be_copied_and_counts_as_adopted(self):
        rig = self.rig()
        rig.codex.fill_works = False
        rig.pause_on(FURNITURE)
        prompt = rig.view.bubble.routes[1].prompt
        rig.controller.pick(1)
        self.assertEqual(rig.view.offers, [(prompt, "failed")])
        self.assertEqual(rig.codex.draft, FURNITURE)          # the draft stays in Codex
        self.assertEqual(rig.feedback(), [])
        rig.controller.offer_copied()
        self.assertEqual(rig.feedback(), [freq.ADOPT])
        self.assertIsNone(rig.view.bubble)

    def test_copy_from_the_preview_takes_the_prompt_without_touching_the_box(self):
        rig = self.rig()
        rig.pause_on(SOFTWARE)
        prompt = rig.view.bubble.routes[2].prompt
        rig.controller.copy(2)
        self.assertEqual(rig.view.copied, [prompt])
        self.assertEqual(rig.codex.draft, SOFTWARE)
        self.assertEqual(rig.feedback(), [freq.ADOPT])

    def test_restore_still_works_when_the_editor_tidies_the_filled_text(self):
        from banxing.hosts.simulated import SimulatedCodex

        class TidyingCodex(SimulatedCodex):
            """An editor that normalises what is put into it (here: full-width commas)."""

            def fill(self, key, expect, text):
                result = super().fill(key, expect, text)
                if result.ok:
                    self.type(self.draft.replace("，", ", "))
                return result

        rig = self.rig(client=TidyingCodex())
        rig.pause_on(FURNITURE)
        prompt = rig.view.bubble.routes[0].prompt
        rig.controller.pick(0)
        rig.tick()
        self.assertNotEqual(rig.codex.draft, prompt)
        self.assertTrue(rig.controller.menu()["can_undo"])
        rig.wait(4.0)
        self.assertEqual(len(rig.calls), 1)                   # still the filled prompt: no new analysis
        rig.controller.undo()
        self.assertEqual(rig.codex.draft.replace(", ", "，"), FURNITURE)

    def test_restore_is_offered_only_while_the_filled_text_is_untouched(self):
        rig = self.rig()
        rig.pause_on(FURNITURE)
        prompt = rig.view.bubble.routes[0].prompt
        rig.controller.pick(0)
        rig.tick()
        self.assertTrue(rig.controller.menu()["can_undo"])
        rig.type(prompt + "另外……")
        self.assertFalse(rig.controller.menu()["can_undo"])
        rig.controller.undo()
        self.assertEqual(rig.codex.draft, prompt + "另外……")   # nothing was overwritten
        self.assertIn("restore_unavailable", rig.view.notices)
        rig.type(prompt)
        rig.controller.undo()
        self.assertEqual(rig.codex.draft, FURNITURE)
        self.assertIn("restored", rig.view.notices)
        rig.tick()
        self.assertFalse(rig.controller.menu()["can_undo"])


class FailuresAndFallbacks(RigCase):
    """Failures, a missing model and compatibility problems: what the pet does then."""

    def test_timeout_shows_a_status_keeps_the_draft_and_retry_works(self):
        state = {"fail": True}

        def flaky(config, system, user, timeout=30.0, meta=None):
            if state["fail"]:
                raise llm.LLMError("timeout", "too slow")
            return mock.complete(meta)
        rig = self.rig(complete=flaky)
        rig.pause_on(OTHER)
        self.assertEqual(rig.view.state, ("error", "timeout"))
        self.assertEqual(rig.codex.draft, OTHER)
        rig.wait(5.0)
        self.assertEqual(len(rig.calls), 1)                   # no silent retry loop
        self.assertTrue(rig.controller.menu()["can_retry"])
        state["fail"] = False
        rig.controller.retry()
        self.assertIsNotNone(rig.view.bubble)

    def test_malformed_reply_is_an_error_not_a_bubble(self):
        rig = self.rig(complete=lambda *a, **k: "当然可以！这里有几个建议：1. …")
        rig.pause_on(OTHER)
        self.assertEqual(rig.view.state, ("error", "invalid"))
        self.assertIsNone(rig.view.bubble)

    def test_without_a_model_the_pet_asks_for_setup_and_examples_still_work(self):
        rig = self.rig(environ={})
        self.assertEqual(rig.controller.setup_reason(), "model")
        rig.pause_on(OTHER)
        self.assertEqual(rig.view.state, ("setup", "model"))
        self.assertEqual(rig.calls, [])
        rig.controller.example("software")
        self.assertEqual(rig.rows()[0][0], "先用表格搭一版")
        prompt = rig.view.bubble.routes[0].prompt
        rig.controller.pick(0)
        self.assertEqual(rig.codex.draft, prompt)             # replaces the current draft...
        rig.tick()
        rig.controller.undo()
        self.assertEqual(rig.codex.draft, OTHER)              # ...and can be taken back
        self.assertEqual(rig.feedback(), [])                  # examples never count

    def test_consent_is_needed_before_anything_is_sent_and_again_for_a_new_destination(self):
        rig = self.rig(environ={})
        rig.controller.save_model(provider="openai", base_url="https://gateway.example.com/v1",
                                  api_key="test-key", model="some-model")
        self.assertEqual(rig.controller.setup_reason(), "consent")
        rig.pause_on(OTHER)
        self.assertEqual(rig.view.state, ("setup", "consent"))
        self.assertEqual(rig.calls, [])
        rig.controller.give_consent(use_context=False)
        self.assertEqual(rig.controller.setup_reason(), "")
        self.assertFalse(rig.settings.get("use_context"))
        rig.controller.save_model(provider="openai", base_url="https://other.example.net/v1",
                                  api_key="test-key-2")
        self.assertEqual(rig.controller.setup_reason(), "consent")

    def test_unreadable_input_box_offers_paste_and_copies_instead_of_filling(self):
        rig = self.rig()
        rig.codex.readable = False
        rig.wait(2.0)
        self.assertEqual(rig.view.state[0], "blind")
        rig.controller.pet_clicked()
        self.assertEqual(rig.view.paste_requests, 1)
        rig.controller.paste(FURNITURE)
        self.assertEqual(len(rig.view.bubble.routes), 3)
        prompt = rig.view.bubble.routes[0].prompt
        rig.wait(1.0)
        self.assertIsNotNone(rig.view.bubble)                 # a pasted-draft bubble stays put
        rig.controller.pick(0)
        self.assertEqual(rig.view.copied, [prompt])
        self.assertEqual(rig.codex.fills, [])

    def test_pause_stops_everything_and_resume_starts_clean(self):
        rig = self.rig()
        rig.pause_on(OTHER)
        rig.controller.set_paused(True)
        self.assertIsNone(rig.view.bubble)
        self.assertEqual(rig.view.state[0], "paused")
        rig.type(OTHER + "，支持全文搜索", then_wait=4.0)
        self.assertEqual(len(rig.calls), 1)
        self.assertFalse(rig.settings.get("follow"))
        rig.controller.set_paused(False)
        rig.wait(2.0)
        self.assertIsNotNone(rig.view.bubble)

    def test_trial_log_has_timings_but_no_text(self):
        rig = self.rig()
        rig.pause_on(FURNITURE)
        rig.controller.pick(0)
        rig.send()
        raw = rig.store.log_path.read_text(encoding="utf-8")
        self.assertNotIn("花架", raw)
        self.assertNotIn("木工", raw)
        self.assertNotIn("纸样", raw)                          # nor anything from the suggestions
        summary = rig.store.summary()
        self.assertEqual((summary["analysed"], summary["shown"], summary["adopted"]), (1, 1, 1))
        self.assertIsNotNone(summary["median_wait_seconds"])


if __name__ == "__main__":
    unittest.main()
