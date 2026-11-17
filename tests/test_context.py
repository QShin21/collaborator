"""Reading the current chat: session files, the app server, recognising the chat,
choosing excerpts. All input is synthetic — no real Codex data is read."""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import safety  # noqa: F401  (keeps tests away from real user data)

from banxing.context import ChatSignals, ContextProvider, Turn, extract, identify, rollout
from banxing.context.appserver import AppServer, AppServerError, find_command
from banxing.context.model import ThreadInfo, thread_from_api, turn_from_api

from rig import THREAD_A, THREAD_B, session_lines, write_session

FAKE_SERVER = str(Path(__file__).with_name("fake_appserver.py"))
TURNS = [
    ("帮我做一个阳台花架的尺寸示意", "我做了一个示意页 rack.html。\n\n页面里的尺寸目前都是示例值，等你量好阳台再替换。"),
    ("最上面一层会挡住窗户", "可以把总高降到 90，或者把花架挪到栏杆那一侧。\n\n- 方案一：降低高度\n- 方案二：换个位置\n\n需要你确认窗台有多高。"),
    ("按方案一改", "已把总高改为 90，并更新了 rack.html。"),
]


class TempCase(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="banxing-context-"))
        self.addCleanup(shutil.rmtree, self.folder, True)


class RolloutTests(TempCase):
    def test_turns_come_from_user_messages_and_final_answers_only(self):
        turns = rollout.parse_turns(session_lines(THREAD_A, "C:\\p", TURNS))
        self.assertEqual([t.user for t in turns], [u for u, _ in TURNS])
        self.assertEqual([t.answer for t in turns], [a for _, a in TURNS])
        self.assertTrue(all(t.completed for t in turns))
        joined = " ".join(t.user + t.answer for t in turns)
        for noise in ("environment_context", "thinking about it", "我先看一下项目里的文件", "shell"):
            self.assertNotIn(noise, joined)

    def test_unfinished_and_aborted_turns_are_not_completed(self):
        lines = session_lines(THREAD_A, "C:\\p", TURNS, finished_last=False)
        turns = rollout.parse_turns(lines)
        self.assertEqual([t.completed for t in turns], [True, True, False])
        lines = session_lines(THREAD_A, "C:\\p", TURNS[:2] + [("再改一版", None)])
        lines.append(json.dumps({"type": "event_msg", "payload": {"type": "turn_aborted", "reason": "interrupted"}}))
        turns = rollout.parse_turns(lines)
        self.assertEqual([t.completed for t in turns], [True, True, False])
        self.assertEqual(turns[-1].answer, "我先看一下项目里的文件。")

    def test_files_without_event_lines_are_read_from_response_items(self):
        turns = rollout.parse_turns(session_lines(THREAD_A, "C:\\p", TURNS, with_events=False))
        self.assertEqual([t.user for t in turns], [u for u, _ in TURNS])
        self.assertEqual(turns[1].answer, TURNS[1][1])
        self.assertTrue(all(t.completed for t in turns))

    def test_older_layout_and_junk_lines_are_tolerated(self):
        lines = [
            '{"id": "abc", "timestamp": "2025-01-01T00:00:00Z", "instructions": null}',
            "not json at all",
            '{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "<user_instructions>be nice</user_instructions>"}]}',
            '{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "做个记账用的小工具"}]}',
            '{"type": "reasoning", "summary": []}',
            '{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "好的，先做预览。"}]}',
            '{"record_type": "state"}',
            "",
            '{"type": "future_event_type", "payload": {"type": "something_new", "data": [1, 2, 3]}}',
            '[1, 2, 3]',
        ]
        turns = rollout.parse_turns(lines)
        self.assertEqual([(t.user, t.answer, t.completed) for t in turns],
                         [("做个记账用的小工具", "好的，先做预览。", True)])

    def test_ide_wrapper_is_stripped_from_user_messages(self):
        wrapped = "# Context from my IDE setup:\n\n## Open tabs:\n- a.py\n\n## My request for Codex:\n把按钮改成蓝色"
        self.assertEqual(rollout.user_text(wrapped), "把按钮改成蓝色")
        self.assertEqual(rollout.user_text("<environment_context>\n<cwd>x</cwd>\n</environment_context>"), "")
        self.assertEqual(rollout.user_text("# AGENTS.md instructions for C:\\p\n..."), "")
        self.assertEqual(rollout.user_text("<div> 这个标签怎么居中"), "<div> 这个标签怎么居中")

    def test_only_the_tail_of_a_large_file_is_needed(self):
        path = write_session(self.folder, THREAD_A, "C:\\p", TURNS)
        filler = json.dumps({"type": "response_item", "payload": {
            "type": "function_call_output", "output": "x" * 5000}})
        original = path.read_text(encoding="utf-8").splitlines()
        with open(path, "w", encoding="utf-8") as stream:
            stream.write(original[0] + "\n")
            for _ in range(700):                        # ~3.5 MB of tool output before the turns
                stream.write(filler + "\n")
            stream.write("\n".join(original[1:]) + "\n")
        self.assertGreater(path.stat().st_size, 2 * rollout.TAIL_BYTES)
        turns = rollout.read_turns(path, want=3)
        self.assertEqual([t.user for t in turns], [u for u, _ in TURNS])
        self.assertEqual(rollout.read_turns(path, want=2)[0].user, TURNS[1][0])

    def test_recent_sessions_are_listed_newest_first_with_id_and_cwd(self):
        write_session(self.folder, THREAD_A, "C:\\proj\\a", TURNS, mtime=1_000_000_000)
        write_session(self.folder, THREAD_B, "C:\\proj\\b", TURNS[:1], mtime=1_000_000_500)
        (self.folder / "2026" / "11" / "17" / "notes.txt").write_text("x", encoding="utf-8")
        found = rollout.list_recent(self.folder, now=1_000_001_000)
        self.assertEqual([t.id for t in found], [THREAD_B, THREAD_A])
        self.assertEqual(found[0].cwd, "C:\\proj\\b")
        self.assertEqual(rollout.list_recent(self.folder, now=1_000_000_000 + 40 * 86400), [])
        self.assertEqual(rollout.list_recent(self.folder / "missing"), [])

    def test_sessions_dir_honours_codex_home(self):
        old = os.environ.get("CODEX_HOME")
        os.environ["CODEX_HOME"] = str(self.folder)
        try:
            self.assertEqual(rollout.sessions_dir(), self.folder / "sessions")
        finally:
            if old is None:
                del os.environ["CODEX_HOME"]
            else:
                os.environ["CODEX_HOME"] = old
        self.assertEqual(rollout.sessions_dir("D:\\x"), Path("D:\\x"))


def api_turn(user, answer, status="completed", with_phase=True):
    items = [{"type": "userMessage", "id": "i1", "content": [{"type": "text", "text": user}]},
             {"type": "reasoning", "id": "i2", "summary": ["thinking"]},
             {"type": "commandExecution", "id": "i3", "command": "ls", "aggregatedOutput": "secret tool output"},
             {"type": "agentMessage", "id": "i4", "text": "我先看一下项目里的文件。",
              **({"phase": "commentary"} if with_phase else {})}]
    if answer:
        items.append({"type": "agentMessage", "id": "i5", "text": answer,
                      **({"phase": "final_answer"} if with_phase else {})})
    return {"id": "turn", "status": status, "items": items}


class ApiShapeTests(unittest.TestCase):
    def test_turn_keeps_user_text_and_final_answer(self):
        turn = turn_from_api(api_turn("按方案一改", "已更新 rack.html。"))
        self.assertEqual((turn.user, turn.answer, turn.completed), ("按方案一改", "已更新 rack.html。", True))
        self.assertEqual(turn_from_api(api_turn("q", "a", with_phase=False)).answer, "a")
        self.assertFalse(turn_from_api(api_turn("q", "", status="inProgress")).completed)
        self.assertFalse(turn_from_api(api_turn("q", "a", status={"type": "interrupted"})).completed)
        self.assertEqual(turn_from_api({"items": "nonsense"}), Turn())

    def test_thread_fields_are_read_tolerantly(self):
        thread = thread_from_api({"id": "t1", "preview": "做个花架", "name": "阳台花架", "cwd": "C:\\p",
                                  "updatedAt": 1_763_000_000, "path": "C:\\x.jsonl"})
        self.assertEqual((thread.id, thread.name, thread.updated_at), ("t1", "阳台花架", 1_763_000_000.0))
        self.assertEqual(thread_from_api({"id": "t2", "updatedAt": 1_763_000_000_000}).updated_at, 1_763_000_000.0)
        self.assertEqual(thread_from_api({}).id, "")


class AppServerTests(TempCase):
    def server(self, mode="full", timeout=5.0):
        data = {
            "threads": [
                {"id": THREAD_A, "preview": TURNS[0][0], "cwd": "C:\\p", "updatedAt": 100},
                {"id": THREAD_B, "preview": "别的事", "cwd": "C:\\p", "updatedAt": 200, "name": "另一个聊天"},
            ],
            "turns": {THREAD_A: [api_turn(u, a) for u, a in TURNS],
                      THREAD_B: [api_turn("别的事", "好的。")]},
        }
        self.data_path = self.folder / "data.json"
        self.data_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        server = AppServer([sys.executable, FAKE_SERVER, str(self.data_path), mode], timeout=timeout)
        self.addCleanup(server.close)
        return server

    def requests(self):
        log = Path(str(self.data_path) + ".log")
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]

    def test_handshake_then_read_only_requests(self):
        server = self.server()
        threads = server.list_threads()
        self.assertEqual([t.id for t in threads], [THREAD_B, THREAD_A])
        self.assertEqual(threads[0].name, "另一个聊天")
        turns = server.list_turns(THREAD_A, limit=2)
        self.assertEqual([t.user for t in turns], [TURNS[1][0], TURNS[2][0]])      # last two, oldest first
        self.assertEqual(turns[-1].answer, TURNS[2][1])
        methods = [r["method"] for r in self.requests()]
        self.assertEqual(methods[:2], ["initialize", "initialized"])
        self.assertTrue(set(methods) <= {"initialize", "initialized", "thread/list", "thread/turns/list"})
        self.assertEqual(self.requests()[0]["params"]["clientInfo"]["name"], "banxing")
        self.assertEqual(self.requests()[-1]["params"]["threadId"], THREAD_A)

    def test_older_server_falls_back_to_thread_read(self):
        server = self.server("old")
        turns = server.list_turns(THREAD_A, limit=2)
        self.assertEqual([t.user for t in turns], [TURNS[1][0], TURNS[2][0]])
        self.assertEqual(self.requests()[-1]["method"], "thread/read")

    def test_unknown_parameters_are_retried_without_them(self):
        server = self.server("strict")
        self.assertEqual(len(server.list_threads()), 2)
        self.assertEqual(len(server.list_turns(THREAD_A, limit=3)), 3)

    def test_server_requests_are_declined_and_do_not_confuse_replies(self):
        server = self.server("asks")
        self.assertEqual(len(server.list_threads()), 2)

    def test_silence_times_out_instead_of_hanging(self):
        server = self.server("silent", timeout=0.5)
        with self.assertRaises(AppServerError):
            server.list_threads()
        self.assertFalse(server.running)                # dropped, to start fresh next time

    def test_missing_binary_is_an_error_not_a_crash(self):
        server = AppServer([str(self.folder / "no-such-codex"), "app-server"])
        with self.assertRaises(AppServerError):
            server.list_threads()

    def test_command_lookup(self):
        self.assertEqual(find_command("C:\\tools\\codex.exe"), ["C:\\tools\\codex.exe", "app-server"])
        self.assertEqual(find_command(["codex", "app-server"]), ["codex", "app-server"])


def signals(turns, selected=(), title="Codex", extra=()):
    texts = list(extra)
    for user, answer in turns:
        texts.append(user)
        texts.extend(part for part in (answer or "").split("\n") if part.strip())
    return ChatSignals(title=title, selected=tuple(selected), texts=tuple(texts))


def thread(thread_id, turns, name="", updated=0.0):
    info = ThreadInfo(id=thread_id, name=name, updated_at=updated)
    info.probes = identify.probes_for([Turn(u, a or "", bool(a)) for u, a in turns])
    return info


class IdentifyTests(unittest.TestCase):
    OTHER = [("帮我写一个记账脚本", "已创建 ledger.py，可以运行 python ledger.py 查看示例。")]

    def test_the_chat_on_screen_is_recognised_by_its_content(self):
        found = identify.identify(signals(TURNS), [thread(THREAD_A, TURNS), thread(THREAD_B, self.OTHER)])
        self.assertTrue(found.confident)
        self.assertEqual(found.thread_id, THREAD_A)
        self.assertGreaterEqual(found.score, identify.MIN_SCORE)
        self.assertEqual(found.candidates, 2)

    def test_rendered_markdown_still_matches(self):
        turns = [("改一下", "**已完成**：把 `花架总高` 改为 90。\n\n1. 更新了 [rack.html](./rack.html) 里的尺寸")]
        rendered = ChatSignals(texts=("改一下", "已完成", "：把 ", "花架总高", " 改为 90。",
                                      "更新了 ", "rack.html", " 里的尺寸"))
        found = identify.identify(rendered, [thread(THREAD_A, turns), thread(THREAD_B, self.OTHER)])
        self.assertEqual(found.thread_id, THREAD_A)

    def test_same_opening_message_in_two_chats_is_not_enough(self):
        first = [(TURNS[0][0], None)]
        sidebar_only = ChatSignals(texts=(TURNS[0][0], "帮我写一个记账脚本", "新聊天"))
        found = identify.identify(sidebar_only, [thread(THREAD_A, first), thread(THREAD_B, self.OTHER)])
        self.assertFalse(found.confident)
        self.assertEqual(found.thread_id, "")

    def test_two_chats_with_the_same_visible_text_are_a_tie(self):
        found = identify.identify(signals(TURNS), [thread(THREAD_A, TURNS), thread(THREAD_B, TURNS)])
        self.assertFalse(found.confident)

    def test_two_chats_of_one_project_are_told_apart_by_their_latest_turns(self):
        # B continues (or was forked from) A: everything A has is also in B's window.
        longer = TURNS + [("再加一层隔板", "已在花架中间加了一层矮的隔板，层高 20。")]
        threads = [thread(THREAD_A, TURNS), thread(THREAD_B, longer)]
        self.assertEqual(identify.identify(signals(longer), threads).thread_id, THREAD_B)
        self.assertEqual(identify.identify(signals(TURNS), threads).thread_id, THREAD_A)

    def test_one_matching_line_is_not_enough(self):
        chatty = [("随便聊聊这个项目的进展和后面的安排", "好的，已经改好了，请打开查看效果。")]
        window = signals(TURNS, extra=("好的，已经改好了，请打开查看效果。",))
        found = identify.identify(window, [thread(THREAD_B, chatty)])
        self.assertFalse(found.confident)

    def test_scrolled_away_from_the_latest_turn_is_not_guessed(self):
        found = identify.identify(signals(TURNS[:1]), [thread(THREAD_A, TURNS)])
        self.assertFalse(found.confident)

    def test_new_chat_or_nothing_visible_is_not_guessed(self):
        threads = [thread(THREAD_A, TURNS), thread(THREAD_B, self.OTHER)]
        self.assertFalse(identify.identify(ChatSignals(texts=("新聊天", "有什么可以帮你？")), threads).confident)
        self.assertFalse(identify.identify(None, threads).confident)
        self.assertFalse(identify.identify(signals(TURNS), []).confident)

    def test_selected_sidebar_title_adds_evidence_but_a_listed_title_does_not(self):
        named = thread(THREAD_A, TURNS, name="阳台花架示意")
        self.assertEqual(identify.score(ChatSignals(selected=("阳台花架示意",)), named), 2)
        self.assertEqual(identify.score(ChatSignals(texts=("阳台花架示意",)), named), 0)


class ExtractTests(unittest.TestCase):
    def turns(self, items):
        return [Turn(u, a or "", bool(a)) for u, a in items]

    def test_last_three_finished_turns_in_order(self):
        four = TURNS + [("再加一层隔板", "已加隔板。"), ("还没回答的问题", None)]
        text, used = extract.excerpt(self.turns(four), "把隔板调高一点")
        self.assertEqual(used, 3)
        self.assertNotIn(TURNS[0][0], text)
        self.assertNotIn("还没回答的问题", text)
        self.assertLess(text.index("最上面一层会挡住窗户"), text.index("再加一层隔板"))
        self.assertIn("[第 3 轮 · Codex 的回答]\n已加隔板。", text)

    def test_nothing_finished_means_no_excerpt(self):
        self.assertEqual(extract.excerpt(self.turns([("问", None)]), "草稿"), ("", 0))
        self.assertEqual(extract.excerpt([], "草稿"), ("", 0))

    def test_long_answers_keep_what_bears_on_the_draft(self):
        filler = "\n\n".join(f"这一段在介绍第 {n} 种木材的历史和产地，与当前任务关系不大。" * 3 for n in range(30))
        answer = ("我做了一个示意页。\n\n" + filler + "\n\n注意：材料清单里的尺寸目前都是示例值，下单之前需要你量过再确认。"
                  "\n\n```python\nprint('long code')\n" + "x = 1\n" * 200 + "```\n\n下一步可以先量阳台能用的宽度。")
        text = extract.condense(answer, extract.keywords("就按这个尺寸列材料清单，我今晚下单。"))
        self.assertLessEqual(len(text), extract.ANSWER_CHARS + 20)
        self.assertIn("我做了一个示意页。", text)
        self.assertIn("示例值", text)
        self.assertIn("下一步可以先量阳台能用的宽度", text)
        self.assertNotIn("x = 1", text)

    def test_total_size_is_bounded(self):
        many = [("问" * 400, "答" * 3000)] * 3
        text, _ = extract.excerpt(self.turns(many), "草稿")
        self.assertLessEqual(len(text), extract.TOTAL_CHARS)


class ProviderTests(TempCase):
    def test_session_files_alone_are_enough(self):
        write_session(self.folder, THREAD_A, "C:\\p", TURNS)
        write_session(self.folder, THREAD_B, "C:\\p", IdentifyTests.OTHER)
        provider = ContextProvider(sessions_dir=str(self.folder), use_appserver=False)
        found = provider.excerpt(signals(TURNS), "把花架再改矮一点")
        self.assertEqual((found.thread_id, found.turns, found.source), (THREAD_A, 3, "rollout"))
        self.assertIn("已把总高改为 90", found.text)
        self.assertEqual(provider.last["identified"], True)
        self.assertIsNone(provider.excerpt(ChatSignals(texts=("新聊天",)), "草稿"))
        self.assertEqual(provider.last["identified"], False)
        self.assertIsNone(provider.excerpt(None, "草稿"))

    def test_app_server_is_preferred_and_files_are_the_fallback(self):
        class Server:
            calls = []

            def list_threads(self, limit=20):
                self.calls.append("list")
                return [ThreadInfo(id=THREAD_A, updated_at=5.0, source="appserver"),
                        ThreadInfo(id=THREAD_B, updated_at=4.0, source="appserver")]

            def list_turns(self, thread_id, limit=6):
                self.calls.append(thread_id)
                items = TURNS if thread_id == THREAD_A else IdentifyTests.OTHER
                return [Turn(u, a, True) for u, a in items]

        provider = ContextProvider(server=Server(), sessions_dir=str(self.folder / "none"))
        found = provider.excerpt(signals(TURNS), "草稿")
        self.assertEqual((found.thread_id, found.source), (THREAD_A, "appserver"))
        self.assertEqual(Server.calls.count(THREAD_A), 1)
        provider.excerpt(signals(TURNS), "草稿")
        self.assertEqual(Server.calls.count(THREAD_A), 1)        # unchanged thread: turns are cached

    def test_broken_app_server_falls_back_to_files_and_is_left_alone_for_a_while(self):
        from banxing.context.appserver import AppServerError

        class Broken:
            calls = 0

            def list_threads(self, limit=20):
                Broken.calls += 1
                raise AppServerError("pipe closed")

        write_session(self.folder, THREAD_A, "C:\\p", TURNS)
        clock = {"now": 0.0}
        provider = ContextProvider(server=Broken(), sessions_dir=str(self.folder), clock=lambda: clock["now"])
        self.assertEqual(provider.excerpt(signals(TURNS), "草稿").source, "rollout")
        clock["now"] = 30.0
        provider.excerpt(signals(TURNS), "草稿")
        self.assertEqual(Broken.calls, 1)
        clock["now"] = 500.0
        provider.excerpt(signals(TURNS), "草稿")
        self.assertEqual(Broken.calls, 2)

    def test_a_turn_finished_a_moment_ago_is_picked_up(self):
        path = write_session(self.folder, THREAD_A, "C:\\p", TURNS[:2], mtime=1_000_000_000)
        clock = {"now": 0.0}
        provider = ContextProvider(sessions_dir=str(self.folder), use_appserver=False, clock=lambda: clock["now"])
        rollout_now = 1_000_000_100
        original = rollout.time.time
        rollout.time.time = lambda: rollout_now
        try:
            self.assertEqual(provider.excerpt(signals(TURNS[:2]), "草稿").turns, 2)
            write_session(self.folder, THREAD_A, "C:\\p", TURNS, mtime=1_000_000_050)
            clock["now"] = 10.0
            found = provider.excerpt(signals(TURNS), "草稿")
            self.assertEqual(found.turns, 3)
            self.assertIn(TURNS[2][1], found.text)
        finally:
            rollout.time.time = original
        self.assertTrue(path.exists())

    def test_end_to_end_with_the_fake_app_server_process(self):
        data = {"threads": [{"id": THREAD_A, "preview": TURNS[0][0], "updatedAt": 100},
                            {"id": THREAD_B, "preview": "记账", "updatedAt": 200}],
                "turns": {THREAD_A: [api_turn(u, a) for u, a in TURNS],
                          THREAD_B: [api_turn(u, a) for u, a in IdentifyTests.OTHER]}}
        data_path = self.folder / "data.json"
        data_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        provider = ContextProvider(codex_command=[sys.executable, FAKE_SERVER, str(data_path), "app-server"],
                                   sessions_dir=str(self.folder / "none"))
        self.addCleanup(provider.close)
        found = provider.excerpt(signals(TURNS), "把花架再改矮一点")
        self.assertEqual((found.thread_id, found.source, found.turns), (THREAD_A, "appserver", 3))
        self.assertNotIn("secret tool output", found.text)
        self.assertNotIn("我先看一下项目里的文件", found.text)


if __name__ == "__main__":
    unittest.main()
