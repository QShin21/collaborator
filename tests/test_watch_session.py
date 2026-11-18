"""One watched conversation over time: when an audit is asked for, what it is
given, what happens to its answer, and what the user's own choices do. A scripted
supervising model plays every answer; nothing here says anything about a real one (E1)."""
import unittest
from dataclasses import replace
from unittest import mock

import safety  # noqa: F401  (keeps tests away from real user data)

from banxing import llm, store
from banxing.watch import audit as au
from banxing.watch import ledger as lg
from banxing.watch import session as ss
from banxing.watch import transcript as ts

from watchrig import (BASE, BUILT, CLAIM, GOAL, LIMIT, ONLINE, SURE, Watching, conclusion, conversation, extended,
                      finding, look, record, research)

REMOTE = llm.ModelConfig(provider="openai", base_url="https://models.example.test/v1", api_key="k-test-0123456789")
DRIFT = finding("drift", title="已经在写上线方案了", basis=[GOAL], now=[ONLINE])


def long_talk(turns=20):
    return conversation(*[(f"第 {n} 步的要求，编号 Q{n}。", f"第 {n} 步做完了，编号 A{n}。") for n in range(1, turns + 1)])


LONG = conclusion(goal={"text": "按步骤做完", "state": "confirmed", "refs": [{"turn": 1, "quote": "第 1 步的要求，编号 Q1"}]},
                  anchors=[{"text": "第 5 步的结果", "kind": "result", "refs": [{"turn": 5, "quote": "第 5 步做完了，编号 A5"}]}])


class TimingTests(unittest.TestCase):
    def test_a_premise_that_new_work_rests_on_ends_in_one_bubble_with_its_sources(self):
        watching = Watching(conclusion(finding()))
        played = watching.see(research())
        bubble = played.bubble
        self.assertEqual((len(played.bubbles), bubble.finding.kind, bubble.through, bubble.manual), (1, au.PREMISE, 7, False))
        self.assertEqual([ref.turn for ref in bubble.finding.basis + bubble.finding.now], [3, 6])
        self.assertEqual(watching.model.sent_turns(), [1, 2, 3, 4, 5, 6, 7])              # first pass: the whole history
        self.assertEqual((watching.session.usage.requests, watching.session.usage.turns), (1, (1, 2, 3, 4, 5, 6, 7)))
        mark = watching.ledger.marks[0]
        self.assertEqual((mark.id, mark.state, mark.turn, bubble.mark), ("m1", lg.RAISED, 7, "m1"))
        self.assertEqual(watching.ledger.anchors[1].status, lg.UNCHECKED)

    def test_nothing_is_asked_while_the_main_model_is_working(self):
        watching = Watching(conclusion(finding()))
        played = watching.see(extended(("写吧。", "", (), ts.RUNNING)))
        self.assertEqual((watching.model.calls, played.bubbles), ([], []))                # the user has decided: wait
        self.assertEqual(len(watching.see(extended(("写吧。", "上线方案写好了。"))).bubbles), 1)
        self.assertEqual(watching.see(ts.Transcript()).bubbles, [])
        self.assertEqual(Watching().do("look_now").notice_kinds(), ["watch_empty"])

    def test_a_later_pass_sends_the_new_turns_the_recent_ones_and_what_the_ledger_points_at(self):
        watching = Watching(LONG, LONG)
        watching.see(long_talk(18))
        self.assertEqual(watching.model.sent_turns(), list(range(1, 19)))
        watching.see(long_talk(19))
        self.assertEqual(watching.model.audits()[1]["meta"]["new"], [19])
        self.assertEqual(watching.model.sent_turns(), [1, 5, 16, 17, 18, 19])             # goal, anchor, the last three
        self.assertNotIn("编号 Q11", watching.model.audits()[1]["user"])
        self.assertEqual(watching.session.usage.turns, tuple(range(1, 20)))
        self.assertEqual(ss.reference_turns(watching.ledger, first=1), (1, 5, 17, 18, 19))
        self.assertEqual(ss.reference_turns(watching.ledger, first=18), (18, 19))

    def test_turns_outside_the_range_the_user_allowed_are_never_sent(self):
        watching = Watching(look(turns=[1, 2, 3], search=["同一份索引"]), conclusion(), first=4)
        watching.see(research())
        for call in watching.model.calls:
            self.assertTrue(all(turn["number"] >= 4 for turn in call["meta"]["turns"]))
            self.assertNotIn("确认过了", call["user"])                                     # turn 3 stays on this machine
        self.assertEqual(min(watching.session.usage.turns), 4)
        self.assertIn(ts.Gap("scope", 4), watching.ledger.gaps)                           # and the ledger says what it lacks

    def test_a_long_history_is_read_in_pieces_and_only_the_last_piece_may_speak(self):
        with mock.patch.object(ss, "CHUNK_CHARS", 320):
            watching = Watching(default=conclusion(finding()))
            played = watching.see(research())
        audits = watching.model.audits()
        self.assertGreater(len(audits), 2)
        self.assertEqual([n for call in audits for n in call["meta"]["new"]], [1, 2, 3, 4, 5, 6, 7])
        self.assertEqual([call["meta"]["final"] for call in audits], [False] * (len(audits) - 1) + [True])
        self.assertIn("后面还有更多轮次", audits[0]["user"])
        self.assertEqual((len(played.bubbles), played.bubble.through, watching.ledger.through), (1, 7, 7))
        self.assertEqual(ss.chunk(research(), 1, 7, budget=10), (1,))                      # an oversized turn goes alone

    def test_an_answer_for_a_moment_that_has_passed_updates_the_ledger_but_does_not_interrupt(self):
        watching, held = Watching(conclusion(finding())), []
        watching.see(research(), hold=held)
        self.assertTrue(watching.session.auditing)
        watching.see(extended(("写吧。", "", (), ts.RUNNING)), hold=held)                 # the user went on meanwhile
        played = watching.finish(held[0])
        self.assertEqual((played.bubbles, played.log("watch_decided")[0]["why"]), ([], "too_late"))
        self.assertEqual((watching.ledger.through, len(watching.session.findings)), (7, 1))   # still on the ledger

    def test_a_turn_that_finished_meanwhile_is_audited_next(self):
        watching, held = Watching(conclusion(), conclusion()), []
        watching.see(research(6), hold=held)
        watching.see(research(), hold=held)
        watching.finish(held[0])
        self.assertEqual([call["meta"]["new"] for call in watching.model.audits()], [[1, 2, 3, 4, 5, 6], [7]])


class BubbleLifeTests(unittest.TestCase):
    def shown(self, *more):
        watching = Watching(conclusion(finding()), *more)
        watching.see(research())
        self.assertIsNotNone(watching.session.bubble)
        return watching

    def test_taking_the_question_back_and_seeing_it_resolved_by_a_record(self):
        watching = self.shown()
        played = watching.do("took_back")
        self.assertEqual((played.hidden, watching.ledger.marks[0].state), (["took_back"], lg.ASKED))
        answered = extended(("请贴出两套方案的索引路径。", "核对了，输出如下。",
                             [record("python show_index.py", "A: idx_1103\nB: idx_1103")]))
        watching.model.answers = [conclusion(resolved=[{"id": "m1", "how": "输出显示两者是同一份索引",
                                                         "refs": [{"turn": 8, "quote": "B: idx_1103"}]}])]
        played = watching.see(answered)
        self.assertIn("m1 [premise] 第 7 轮后提出", watching.model.audits()[-1]["user"])
        self.assertIn("用户已带回去问主模型", watching.model.audits()[-1]["user"])
        mark = watching.ledger.marks[0]
        self.assertEqual((mark.state, mark.note, mark.refs[0].where), (lg.RESOLVED, "输出显示两者是同一份索引", ts.RECORD))
        self.assertEqual((played.bubbles, played.log("watch_resolved")), ([], [{"kind": "premise"}]))

    def test_intended_is_noted_and_that_kind_of_concern_stays_away(self):
        watching = self.shown()
        self.assertEqual(watching.do("intended").hidden, ["intended"])
        watching.do("add_note", "索引我自己核对过")
        mark = watching.ledger.marks[0]
        self.assertEqual((mark.state, mark.note), (lg.INTENDED, "索引我自己核对过"))
        # The next turn brings the same kind of concern on wider evidence: nothing has changed.
        wider = finding(basis=[CLAIM, BASE], now=[{"turn": 8, "quote": "上线方案写好了"}])
        watching.model.answers = [conclusion(wider)]
        played = watching.see(extended(("写吧。", "上线方案写好了。")))
        self.assertEqual((played.bubbles, played.log("watch_decided")[0]["why"]), ([], "muted"))
        self.assertIn("用户说这是有意如此；索引我自己核对过", watching.model.audits()[-1]["user"])

    def test_wrong_is_told_to_the_supervisor_and_not_raised_again(self):
        watching = self.shown(conclusion(finding()))
        self.assertEqual(watching.do("wrong", "这一点我确认过").hidden, ["wrong"])
        self.assertEqual(watching.ledger.marks[0].state, lg.REJECTED)
        played = watching.see(extended(("写吧。", "上线方案写好了。")))
        self.assertEqual(played.bubbles, [])
        self.assertIn(played.log("watch_decided")[0]["why"], ("repeat", "muted"))
        self.assertIn("用户认为不成立；这一点我确认过", watching.model.audits()[-1]["user"])

    def test_closing_mutes_until_the_footing_really_changes(self):
        watching = self.shown()
        self.assertEqual(watching.do("dismiss").hidden, ["dismissed"])
        self.assertEqual(watching.ledger.marks[0].state, lg.RAISED)                       # closed is not an answer
        wider = finding(basis=[CLAIM, BASE], now=[{"turn": 8, "quote": "上线方案写好了"}])
        watching.model.answers = [conclusion(wider)]
        self.assertEqual(watching.see(extended(("写吧。", "上线方案写好了。"))).bubbles, [])
        # Turn 9: a record now contradicts the premise — the footing is no longer what it was.
        broken = conclusion(finding(basis=[CLAIM, {"turn": 9, "quote": "B: idx_1117 (rebuilt)"}],
                                    now=[{"turn": 9, "quote": "看来不是同一份"}]))
        broken["anchors"][1].update(status="broken", against=[{"turn": 9, "quote": "B: idx_1117 (rebuilt)"}])
        watching.model.answers = [broken]
        again = watching.see(extended(("写吧。", "上线方案写好了。"), ("查一下索引。", "看来不是同一份。", [
            record("python show_index.py", "A: idx_1103\nB: idx_1117 (rebuilt)")])))
        self.assertEqual(len(again.bubbles), 1)
        self.assertEqual(watching.ledger.anchors[1].status, lg.BROKEN)

    def test_a_muted_kind_does_not_hide_another_kind(self):
        watching = self.shown(conclusion(finding(), DRIFT))
        watching.do("intended")
        played = watching.see(extended(("写吧。", "上线方案写好了。")))
        self.assertEqual(played.bubble.finding.kind, au.DRIFT)
        self.assertEqual([m.kind for m in watching.ledger.marks], [au.PREMISE, au.DRIFT])

    def test_notes_stay_on_the_ledger_without_interrupting(self):
        watching = Watching(conclusion(finding("narrowing", weight="note", basis=[BUILT], now=[])))
        played = watching.see(research())
        self.assertEqual((played.bubbles, played.log("watch_decided")[0]["why"]), ([], "notes_only"))
        self.assertEqual([f.kind for f in watching.session.findings], [au.NARROWING])
        self.assertEqual(watching.ledger.marks, ())

    def test_the_old_bubble_goes_when_the_user_sends_the_next_message(self):
        watching = self.shown()
        played = watching.see(extended(("写吧。", "", (), ts.RUNNING)))
        self.assertEqual((played.hidden, watching.session.bubble, len(watching.model.calls)), (["moved_on"], None, 1))

    def test_pausing_hides_the_bubble_stops_audits_and_drops_what_is_in_flight(self):
        watching = self.shown()
        self.assertEqual(watching.do("pause").hidden, ["paused"])
        watching.see(extended(("写吧。", "上线方案写好了。")))
        self.assertEqual(len(watching.model.calls), 1)
        watching.do("resume")
        self.assertEqual(watching.model.audits()[1]["meta"]["new"], [8])
        other, held = Watching(conclusion(finding())), []
        other.see(research(), hold=held)
        other.do("pause")
        played = other.finish(held[0])
        self.assertEqual((played.bubbles, played.notices, other.ledger.through), ([], [], 0))
        self.assertEqual(other.session.usage.requests, 1)                                 # it was sent all the same: counted

    def test_asking_always_gets_an_answer_and_is_not_counted(self):
        watching = Watching(conclusion(sound="两次评测都有记录。"), conclusion(finding()))
        self.assertEqual(watching.see(research()).bubbles, [])
        played = watching.do("look_now")                                                  # nothing new since
        call = watching.model.audits()[1]
        self.assertEqual((call["meta"]["new"], call["meta"]["mode"]), ([], "manual"))
        self.assertIn("没有新增的轮次", call["user"])
        self.assertTrue(played.bubble.manual)
        self.assertEqual(watching.session.shown, set())                                   # asked for: not "shown once"
        self.assertEqual(watching.do("dismiss").hidden, ["dismissed"])
        self.assertEqual(watching.session.muted, {})                                      # closing what you asked for mutes nothing
        clear = Watching(default=conclusion(sound="两次评测都有记录。"))
        clear.see(research())
        self.assertEqual([(n.kind, n.detail) for n in clear.do("look_now").notices], [("watch_clear", "两次评测都有记录。")])

    def test_asking_while_an_audit_is_already_running_turns_it_into_an_answer(self):
        watching, held = Watching(conclusion()), []
        watching.see(research(), hold=held)
        self.assertEqual(watching.do("look_now", hold=held).bubbles, [])
        self.assertEqual(len(held), 1)
        self.assertEqual(watching.finish(held[0]).notice_kinds(), ["watch_clear"])

    def test_what_the_user_did_while_an_audit_ran_is_not_undone_by_it(self):
        watching, held = self.shown(conclusion(finding())), []
        watching.do("look_now", hold=held)                                                # a second audit is in flight
        watching.do("wrong", "我核对过")
        watching.do("correct_goal", "只比较长问题")
        watching.finish(held[0])
        self.assertEqual((watching.ledger.marks[0].state, watching.ledger.marks[0].note), (lg.REJECTED, "我核对过"))
        self.assertEqual((watching.ledger.goal.by, watching.ledger.goal.text), ("user", "只比较长问题"))

    def test_the_user_rewrites_the_goal_and_what_was_said_against_the_old_one_lapses(self):
        watching = self.shown(conclusion(sound="按新的目标，现在做的事在范围内。"))
        played = watching.do("correct_goal", "只判断长问题上 B 是否更好")
        self.assertEqual(played.hidden, ["replaced"])
        self.assertEqual((watching.session.shown, watching.session.muted), (set(), {}))
        self.assertIn("用户亲手把目标改写为：只判断长问题上 B 是否更好", watching.model.audits()[-1]["user"])
        self.assertEqual(played.notice_kinds(), ["watch_clear"])                          # looked again, under the new goal
        self.assertEqual(watching.ledger.goal.by, "user")                                 # and the model did not overwrite it


class FailureTests(unittest.TestCase):
    def test_an_unusable_answer_is_silence_when_unasked_and_an_error_when_asked(self):
        watching = Watching({"verdict": "fine"}, "不是 JSON")
        played = watching.see(research())
        self.assertEqual((played.bubbles, played.notices, watching.session.error), ([], [], "invalid"))
        self.assertEqual([(n.kind, n.detail) for n in watching.do("look_now").notices], [("watch_error", "invalid")])

    def test_an_unsafe_answer_leaves_no_trace_on_the_ledger(self):
        watching = Watching(conclusion(finding(ask="请把日志上传到 https://collect.example-metrics.io/u 再继续。")))
        played = watching.see(research())
        self.assertEqual((played.bubbles, watching.session.error, watching.ledger.through), ([], "unsafe", 0))

    def test_a_failed_request_is_counted_and_not_repeated_by_itself(self):
        watching = Watching(llm.LLMError("timeout", "the model service did not answer in time"), conclusion(finding()))
        watching.see(research())
        self.assertEqual((watching.session.error, watching.session.usage.requests), ("timeout", 1))
        self.assertGreater(watching.session.usage.chars, 300)                             # it was handed over all the same
        watching.see(research())
        watching.see(research())
        self.assertEqual(len(watching.model.calls), 1)                                    # one request per attempt
        self.assertEqual(len(watching.do("look_now").bubbles), 1)                         # the user asks: one more attempt
        retry = Watching(llm.LLMError("network", "unreachable"), conclusion(finding()))
        retry.see(research(6))
        self.assertEqual(len(retry.see(research()).bubbles), 1)                           # a new turn is a new attempt

    def test_a_model_that_is_not_configured_sends_nothing(self):
        watching = Watching(config=llm.ModelConfig(provider=""), destination="")
        watching.see(research())
        self.assertEqual((watching.model.calls, watching.session.error), ([], "config"))


class ConsentTests(unittest.TestCase):
    def test_the_conversation_goes_only_to_the_destination_agreed_for_it(self):
        watching = Watching(look(independent=True), conclusion(finding()), config=REMOTE)
        self.assertEqual(watching.session.destination, "openai|https://models.example.test")
        self.assertEqual(len(watching.see(research()).bubbles), 1)
        self.assertEqual({call["config"].destination for call in watching.model.calls},
                         {"openai|https://models.example.test"})                          # every step, the blind one too

    def test_a_changed_service_stops_everything_until_the_user_agrees_again(self):
        watching = Watching(conclusion(), conclusion(finding()), config=REMOTE)
        watching.see(research(6))
        watching.config = replace(REMOTE, base_url="https://elsewhere.example.test/v1")
        played = watching.see(research())
        self.assertEqual(len(watching.model.calls), 1)                                    # nothing went to the new place
        self.assertEqual((watching.session.state, played.notice_kinds()), (ss.NEEDS_CONSENT, ["watch_consent"]))
        self.assertEqual(watching.do("look_now").notice_kinds(), ["watch_consent"])
        self.assertEqual(len(watching.model.calls), 1)
        self.assertEqual(len(watching.do("agree", watching.config.destination).bubbles), 1)
        self.assertEqual(watching.model.calls[1]["config"].origin, "https://elsewhere.example.test")

    def test_another_port_host_or_scheme_is_another_destination(self):
        for other in ("https://models.example.test:8443/v1", "https://models.example.test.evil.example/v1",
                      "https://user@models.example.test/v1", "http://models.example.test/v1"):
            watching = Watching(conclusion(finding()), config=REMOTE)
            watching.config = replace(REMOTE, base_url=other)
            watching.see(research())
            self.assertEqual(watching.model.calls, [], other)
            self.assertIn(watching.session.error, ("consent", "config"), other)

    def test_a_general_permit_is_asked_for_the_configuration_in_use(self):
        asked = []
        watching = Watching(conclusion(), config=REMOTE, permit=lambda config: asked.append(config.destination) or False)
        watching.see(research())
        self.assertEqual((watching.model.calls, asked), ([], ["openai|https://models.example.test"]))


class BranchTests(unittest.TestCase):
    def edited(self):
        turns = [(t.user, t.model, t.records) for t in research().turns]
        turns[1] = ("评测可以用线上最近一周的真实查询。", "明白：用线上查询。")
        return conversation(*turns)

    def test_a_changed_history_stops_watching_until_the_user_confirms(self):
        watching = Watching(conclusion(finding()), conclusion())
        watching.see(research())
        played = watching.see(self.edited())
        self.assertEqual((played.hidden, played.notice_kinds(), watching.session.state),
                         (["branch"], ["watch_branch"], ss.DIVERGED))
        self.assertEqual(len(watching.model.calls), 1)                                    # the other branch was not sent
        self.assertEqual(watching.do("look_now").notice_kinds(), ["watch_branch"])
        self.assertEqual(watching.see(self.edited()).notices, [])                         # said once
        watching.see(extended(("写吧。", "写好了。")))                                     # back on the history that was read
        self.assertEqual((watching.session.state, watching.model.audits()[-1]["meta"]["new"]), (ss.IDLE, [8]))

    def test_accepting_the_branch_starts_the_ledger_over(self):
        other = conclusion(anchors=[{"text": "评测用线上查询", "kind": "constraint",
                                     "refs": [{"turn": 2, "quote": "评测可以用线上最近一周的真实查询"}]}])
        watching = Watching(conclusion(finding()), other)
        watching.see(research())
        watching.do("took_back")
        watching.see(self.edited())
        watching.do("accept_branch")
        self.assertEqual(watching.model.sent_turns(), [1, 2, 3, 4, 5, 6, 7])              # read again from the start
        self.assertIn("还没有账本", watching.model.audits()[-1]["user"])
        self.assertEqual(([a.text for a in watching.ledger.anchors], watching.ledger.marks), (["评测用线上查询"], ()))

    def test_a_history_that_changes_while_an_audit_runs_is_caught_when_it_returns(self):
        watching, held = Watching(conclusion(), conclusion(finding())), []
        watching.see(research(6))
        watching.see(research(), hold=held)
        watching.session.transcript = self.edited()                                       # read again meanwhile
        played = watching.finish(held[0])
        self.assertEqual((played.bubbles, watching.session.state, watching.ledger.through), ([], ss.DIVERGED, 6))


class RecordsTests(unittest.TestCase):
    def test_what_is_logged_is_numbers_and_names_only(self):
        """Everything a session asks to have logged passes the event log's own filter
        unchanged — so none of it is prose — and holds no Chinese text."""
        watching = Watching(look(turns=[3], search=["索引"], independent=True), default=conclusion(finding(), DRIFT))
        logs = []
        for played in (watching.see(research()), watching.do("intended", "我核对过"), watching.do("look_now"),
                       watching.do("wrong"), watching.do("correct_goal", "只看长问题"), watching.do("pause"),
                       watching.do("resume"), watching.see(extended(("再细分。", "", (), ts.RUNNING)))):
            logs += played.logs
        self.assertGreaterEqual(len(logs), 10)
        for entry in logs:
            self.assertRegex(entry.event, r"^watch_[a-z_]+$")
            self.assertEqual(store._safe(entry.data), entry.data, entry.event)
            self.assertNotRegex(repr(entry.data), "[一-鿿]")
        first = next(entry.data for entry in logs if entry.event == "watch_audit")
        self.assertEqual((first["looks"], first["searches"], first["independent"], first["requests"], first["findings"],
                          first["direction"]), (1, 1, True, 3, 2, 2))


if __name__ == "__main__":
    unittest.main()
