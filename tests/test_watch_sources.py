"""Reading the conversation the user chose: a ChatGPT export, a Codex session file,
the Codex App Server, a plain transcript. Every input is synthetic and lives in a
temp folder — no real export, session or server is read (E1)."""
import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

import safety  # noqa: F401  (keeps tests away from real user data)

from banxing.context.appserver import AppServer
from banxing.watch import sources
from banxing.watch import transcript as ts

THREAD_A = "0199aaaa-1111-7222-8333-444455556666"

FAKE_SERVER = str(Path(__file__).with_name("fake_appserver.py"))


class TempCase(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="banxing-sources-"))
        self.addCleanup(shutil.rmtree, self.folder, True)

    def write(self, name, data):
        path = self.folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data if isinstance(data, str) else json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return path


# ------------------------------------------------------------------ ChatGPT export
def node(node_id, parent, role=None, text="", children=(), **message):
    """One node of an export's `mapping`."""
    body = None
    if role:
        body = {"id": "m-" + node_id, "author": {"role": role, "name": message.pop("name", None)},
                "content": message.pop("content", {"content_type": "text", "parts": [text]}),
                "recipient": message.pop("recipient", "all"), "metadata": message.pop("metadata", {})}
    return node_id, {"id": node_id, "parent": parent, "children": list(children), "message": body}


def export_conversation(title="数据准备", update_time=200.0):
    """A conversation with an edited message (two branches), a tool run, an attachment
    and hidden system context. The current branch is root → u1 → a1 → u2b → …"""
    mapping = dict([
        node("root", None, children=["sys"]),
        node("sys", "root", "system", "you are helpful", children=["ctx"]),
        node("ctx", "sys", "user", children=["u1"], content={"content_type": "user_editable_context", "user_profile": "x"},
             metadata={"is_visually_hidden_from_conversation": True}),
        node("u1", "ctx", "user", "我想先看看这批问卷数据能不能直接拿来训练。", children=["a1"]),
        node("a1", "u1", "assistant", "可以，我先看一下字段。", children=["u2a", "u2b"]),
        node("u2a", "a1", "user", "直接全量训练吧。", children=["a2a"]),
        node("a2a", "u2a", "assistant", "好的，开始全量训练。"),
        node("u2b", "a1", "user", "先用附件里的三百条做个小样本检查。", children=["c1"],
             metadata={"attachments": [{"name": "sample_300.csv", "id": "file-1", "size": 1234}]}),
        node("c1", "u2b", "assistant", children=["o1"], recipient="python",
             content={"content_type": "code", "language": "python", "text": "import pandas as pd\nprint(len(pd.read_csv('sample_300.csv')))"}),
        node("o1", "c1", "tool", children=["a2b"], name="python",
             content={"content_type": "execution_output", "text": "300\nall rows parsed"}),
        node("a2b", "o1", "assistant", "三百条都读进来了。", children=["u3"]),
        node("u3", "a2b", "user", children=["a3"], content={"content_type": "multimodal_text", "parts": [
            "这张图里的分布正常吗？", {"content_type": "image_asset_pointer", "asset_pointer": "file-service://x"}]}),
        node("a3", "u3", "assistant", "看起来右偏，但不影响小样本检查。", children=["u4"]),
        node("u4", "a3", "user", "那下一步呢？"),
    ])
    return {"title": title, "create_time": 100.0, "update_time": update_time, "conversation_id": "conv-" + title,
            "mapping": mapping, "current_node": "u4"}


class ChatGPTExportTests(TempCase):
    def test_the_current_branch_becomes_numbered_turns(self):
        path = self.write("conversations.json", [export_conversation()])
        transcript = sources.read_export(path, "conv-数据准备")
        self.assertEqual((transcript.source, transcript.title, len(transcript.turns)),
                         (sources.EXPORT, "数据准备", 4))
        self.assertEqual([turn.user for turn in transcript.turns][:2],
                         ["我想先看看这批问卷数据能不能直接拿来训练。", "先用附件里的三百条做个小样本检查。"])
        self.assertNotIn("全量训练", " ".join(turn.user + turn.model for turn in transcript.turns))   # the other branch
        self.assertNotIn("you are helpful", " ".join(turn.user + turn.model for turn in transcript.turns))

    def test_what_ran_and_what_came_out_are_records_not_talk(self):
        transcript = sources.read_export(self.write("conversations.json", [export_conversation()]), "conv-数据准备")
        second = transcript.turns[1]
        self.assertEqual(second.model, "三百条都读进来了。")
        self.assertEqual([(r.kind, r.title) for r in second.records],
                         [(ts.ATTACHMENT, "sample_300.csv"), (ts.COMMAND, "python"), (ts.OUTPUT, "python")])
        self.assertEqual(ts.locate(second, "all rows parsed"), ts.RECORD)
        self.assertEqual(ts.locate(second, "三百条都读进来了"), ts.MODEL)

    def test_what_could_not_be_read_is_reported_as_gaps(self):
        transcript = sources.read_export(self.write("conversations.json", [export_conversation()]), "conv-数据准备")
        self.assertEqual(set(transcript.gaps), {ts.Gap("branch", 0, 1), ts.Gap("attachment", 2, 1),
                                                ts.Gap("attachment", 3, 1)})
        self.assertEqual(transcript.turns[2].user, "这张图里的分布正常吗？")

    def test_an_unanswered_last_message_is_still_running(self):
        transcript = sources.read_export(self.write("conversations.json", [export_conversation()]), "conv-数据准备")
        self.assertEqual([turn.state for turn in transcript.turns], [ts.DONE, ts.DONE, ts.DONE, ts.RUNNING])
        self.assertEqual(transcript.settled_through, 3)

    def test_listing_is_newest_first_and_counts_turns(self):
        path = self.write("conversations.json", [export_conversation("旧的", 50.0), export_conversation("新的", 500.0),
                                                 {"title": "no mapping"}, "junk"])
        listings = sources.list_export(path)
        self.assertEqual([(item.title, item.turns) for item in listings], [("新的", 4), ("旧的", 4)])
        self.assertEqual(sources.read_export(path, listings[1].id).title, "旧的")
        with self.assertRaises(sources.SourceError) as caught:
            sources.read_export(path, "conv-不存在")
        self.assertEqual(caught.exception.kind, "not_found")

    def test_a_zip_a_folder_and_sharded_files_are_all_read_without_unpacking(self):
        first, second = export_conversation("甲"), export_conversation("乙", 300.0)
        archive = self.folder / "export.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("chat.html", "<html>not this</html>")
            bundle.writestr("user.json", json.dumps({"email": "nobody@example.invalid"}))
            bundle.writestr("data/conversations-000.json", json.dumps([first], ensure_ascii=False))
            bundle.writestr("data/conversations-001.json", json.dumps([second], ensure_ascii=False))
            bundle.writestr("../../conversations-evil.json", json.dumps([export_conversation("丙", 1.0)]))
        self.assertEqual([item.title for item in sources.list_export(archive)], ["乙", "甲", "丙"])
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), ["export.zip"])    # nothing was extracted
        self.assertFalse(any(self.folder.parent.glob("conversations-evil.json")))
        self.assertFalse(any(self.folder.parent.parent.glob("conversations-evil.json")))
        unpacked = self.folder / "unpacked"
        (unpacked / "sub").mkdir(parents=True)
        (unpacked / "sub" / "conversations.json").write_text(json.dumps([first], ensure_ascii=False), encoding="utf-8")
        (unpacked / "message_feedback.json").write_text("[]", encoding="utf-8")
        self.assertEqual([item.title for item in sources.list_export(unpacked)], ["甲"])

    def test_without_a_branch_pointer_the_last_child_is_followed(self):
        conversation = export_conversation()
        del conversation["current_node"]
        transcript = sources.read_export(self.write("c.json", [conversation]), "conv-数据准备")
        self.assertEqual(transcript.turns[-1].user, "那下一步呢？")

    def test_files_that_are_not_an_export_are_refused_by_kind(self):
        cases = {"empty.json": "[]", "object.json": "{\"a\": 1}", "broken.json": "[{", "text.json": "hello"}
        for name, content in cases.items():
            with self.assertRaises(sources.SourceError) as caught:
                sources.list_export(self.write(name, content))
            self.assertEqual(caught.exception.kind, "format", name)
        with self.assertRaises(sources.SourceError) as caught:
            sources.list_export(self.folder / "missing.zip")
        self.assertEqual(caught.exception.kind, "missing")
        with zipfile.ZipFile(self.folder / "other.zip", "w") as bundle:
            bundle.writestr("notes.txt", "x")
        with self.assertRaises(sources.SourceError) as caught:
            sources.list_export(self.folder / "other.zip")
        self.assertEqual(caught.exception.kind, "format")

    def test_an_oversized_member_is_not_loaded(self):
        path = self.write("conversations.json", [export_conversation()])
        old, sources.MAX_FILE_BYTES = sources.MAX_FILE_BYTES, 10
        self.addCleanup(setattr, sources, "MAX_FILE_BYTES", old)
        with self.assertRaises(sources.SourceError) as caught:
            sources.list_export(path)
        self.assertEqual(caught.exception.kind, "too_large")


# ------------------------------------------------------------------ Codex session file
def event(kind, **payload):
    return json.dumps({"type": "event_msg", "payload": dict(payload, type=kind)}, ensure_ascii=False)


def item(kind, **payload):
    return json.dumps({"type": "response_item", "payload": dict(payload, type=kind)}, ensure_ascii=False)


def session_file_lines():
    return [
        json.dumps({"type": "session_meta", "payload": {"id": THREAD_A, "cwd": "C:\\proj"}}),
        item("message", role="user", content=[{"type": "input_text", "text": "<environment_context>x</environment_context>"}]),
        event("user_message", message="先检查一下小样本能不能读。"),
        event("agent_message", message="我来跑读取检查。"),
        item("function_call", name="shell", call_id="c1", arguments=json.dumps({"command": ["python", "load_check.py"]})),
        item("function_call_output", call_id="c1",
             output=json.dumps({"output": "512 rows loaded\nload check: OK", "metadata": {"exit_code": 0}})),
        item("reasoning", summary=[{"type": "summary_text", "text": "thinking"}]),
        event("agent_message", message="读取检查通过，512 条都能读。"),
        event("task_complete", last_agent_message="读取检查通过，512 条都能读。"),
        event("user_message", message="再核对划分。"),
        item("function_call", name="shell", call_id="c2", arguments="{\"cmd\": \"python split_check.py\"}"),
        item("function_call_output", call_id="c2", output="Traceback (most recent call last):\nKeyError: 'subject'"),
        item("function_call", name="shell", call_id="c3", arguments=json.dumps({"command": ["ls"]})),
        event("agent_message", message="划分检查报错了，缺少 subject 列。"),
        event("task_complete", last_agent_message="划分检查报错了，缺少 subject 列。"),
        "this line is not json",
        event("user_message", message="那先补上这一列。"),
        event("agent_message", message="正在补……"),
    ]


class CodexSessionTests(TempCase):
    def turns(self, lines=None):
        return sources.session_turns(lines or session_file_lines())

    def test_turns_keep_what_was_said_and_what_was_run(self):
        turns, gaps = self.turns()
        self.assertEqual([t.user for t in turns], ["先检查一下小样本能不能读。", "再核对划分。", "那先补上这一列。"])
        first = turns[0]
        self.assertEqual(first.model, "我来跑读取检查。\n\n读取检查通过，512 条都能读。")       # said once, in order
        self.assertEqual(first.records, (ts.Record(ts.COMMAND, "python load_check.py", "512 rows loaded\nload check: OK", True),))
        self.assertEqual(ts.locate(first, "512 rows loaded"), ts.RECORD)
        self.assertNotIn("thinking", first.model)
        self.assertNotIn("environment_context", first.user)

    def test_states_and_missing_outputs(self):
        turns, gaps = self.turns()
        self.assertEqual([t.state for t in turns], [ts.DONE, ts.DONE, ts.RUNNING])
        second = turns[1]
        self.assertEqual([(r.title, r.ok) for r in second.records], [("python split_check.py", None), ("ls", None)])
        self.assertIn("KeyError", second.records[0].text)
        self.assertEqual(second.records[1].text, "")
        self.assertEqual(gaps, (ts.Gap("output", 2, 1),))                           # the `ls` left no output

    def test_aborted_turns_and_compaction_are_marked(self):
        lines = session_file_lines()[:9] + [json.dumps({"type": "compacted", "payload": {"message": "summary"}}),
                                            event("user_message", message="继续。"), event("turn_aborted")]
        turns, gaps = self.turns(lines)
        self.assertEqual([t.state for t in turns], [ts.DONE, ts.ABORTED])
        self.assertEqual(gaps, (ts.Gap("compacted", 1, 1),))

    def test_files_without_event_lines_are_read_from_response_items(self):
        lines = [item("message", role="user", content=[{"type": "input_text", "text": "跑一下检查"}]),
                 item("function_call", name="shell", call_id="c1", arguments=json.dumps({"command": ["pytest", "-q"]})),
                 item("function_call_output", call_id="c1", output="3 passed"),
                 item("message", role="assistant", content=[{"type": "output_text", "text": "三项都通过了。"}])]
        turns, _ = self.turns(lines)
        self.assertEqual((turns[0].user, turns[0].model, turns[0].state), ("跑一下检查", "三项都通过了。", ts.DONE))
        self.assertEqual(turns[0].records[0], ts.Record(ts.COMMAND, "pytest -q", "3 passed", None))

    def test_a_session_file_is_read_whole_and_listed_by_its_opening(self):
        folder = self.folder / "sessions" / "2026" / "11" / "17"
        folder.mkdir(parents=True)
        path = folder / f"rollout-2026-11-17T03-00-00-{THREAD_A}.jsonl"
        path.write_text("\n".join(session_file_lines()) + "\n", encoding="utf-8")
        transcript = sources.read_session(path)
        self.assertEqual((transcript.source, transcript.conversation_id, len(transcript.turns)),
                         (sources.SESSION, THREAD_A, 3))
        listings = sources.list_sessions(self.folder / "sessions")
        self.assertEqual([(item.id, item.title, item.turns) for item in listings],
                         [(str(path), "先检查一下小样本能不能读。", 3)])
        self.assertEqual(sources.list_sessions(self.folder / "nowhere"), [])
        with self.assertRaises(sources.SourceError) as caught:
            sources.read_session(self.folder / "gone.jsonl")
        self.assertEqual(caught.exception.kind, "missing")
        with self.assertRaises(sources.SourceError) as caught:
            sources.read_session(self.write("empty.jsonl", "\n"))
        self.assertEqual(caught.exception.kind, "empty")


# ------------------------------------------------------------------ Codex App Server
def api_turn(number, user, answer, status="completed"):
    return {"id": f"turn-{number}", "status": status, "items": [
        {"type": "userMessage", "id": "u", "content": [{"type": "text", "text": user}]},
        {"type": "reasoning", "id": "r", "summary": ["hidden thoughts"], "content": []},
        {"type": "commandExecution", "id": "c", "command": f"python step_{number}.py", "cwd": "C:\\p",
         "status": "completed", "aggregatedOutput": f"step {number} finished without errors", "exitCode": 0},
        {"type": "agentMessage", "id": "a", "text": answer, "phase": "final_answer"}]}


class AppServerHistoryTests(TempCase):
    def server(self, turns, mode="full"):
        data_path = self.write("data.json", {"threads": [{"id": THREAD_A, "updatedAt": 1}], "turns": {THREAD_A: turns}})
        self.log = Path(str(data_path) + ".log")
        server = AppServer([sys.executable, FAKE_SERVER, str(data_path), mode], timeout=5.0)
        self.addCleanup(server.close)
        return server

    def requests(self, method):
        return [json.loads(line)["params"] for line in self.log.read_text(encoding="utf-8").splitlines()
                if json.loads(line)["method"] == method]

    def test_the_whole_thread_is_read_page_by_page_oldest_first(self):
        server = self.server([api_turn(n, f"第 {n} 个问题", f"第 {n} 个回答") for n in range(1, 8)])
        raw = server.read_history(THREAD_A, page=3)
        self.assertEqual([turn["id"] for turn in raw], [f"turn-{n}" for n in range(1, 8)])
        pages = self.requests("thread/turns/list")
        self.assertEqual([p.get("cursor") for p in pages], [None, "3", "6"])
        self.assertTrue(all(p["itemsView"] == "full" and p["sortDirection"] == "asc" and p["limit"] == 3 for p in pages))
        transcript = sources.read_appserver(server, THREAD_A)
        self.assertEqual((transcript.source, len(transcript.turns), transcript.conversation_id),
                         (sources.APPSERVER, 7, THREAD_A))
        fifth = transcript.turns[4]
        self.assertEqual((fifth.number, fifth.user, fifth.model, fifth.id), (5, "第 5 个问题", "第 5 个回答", "turn-5"))
        self.assertEqual(fifth.records, (ts.Record(ts.COMMAND, "python step_5.py", "step 5 finished without errors", True),))
        self.assertNotIn("hidden thoughts", ts.render([fifth])[0])

    def test_turns_that_arrive_without_items_are_completed_from_the_item_list(self):
        server = self.server([api_turn(n, f"第 {n} 个问题", f"第 {n} 个回答") for n in range(1, 4)], mode="summary")
        transcript = sources.read_appserver(server, THREAD_A)
        self.assertEqual([t.model for t in transcript.turns], ["第 1 个回答", "第 2 个回答", "第 3 个回答"])
        self.assertEqual(transcript.turns[2].records[0].text, "step 3 finished without errors")
        self.assertTrue(self.requests("thread/items/list"))

    def test_older_servers_are_asked_in_the_way_they_understand(self):
        turns = [api_turn(n, f"第 {n} 个问题", f"第 {n} 个回答") for n in range(1, 4)]
        plain = self.server(turns, mode="plainview")
        self.assertEqual(len(plain.read_history(THREAD_A)), 3)
        self.assertEqual(["itemsView" in p for p in self.requests("thread/turns/list")], [True, False])
        old = self.server(turns, mode="old")
        self.assertEqual(len(sources.read_appserver(old, THREAD_A).turns), 3)
        self.assertEqual(self.requests("thread/read")[-1], {"threadId": THREAD_A, "includeTurns": True})

    def test_only_reads_are_ever_sent(self):
        server = self.server([api_turn(1, "问", "答")])
        sources.read_appserver(server, THREAD_A)
        methods = {json.loads(line)["method"] for line in self.log.read_text(encoding="utf-8").splitlines()}
        self.assertEqual(methods, {"initialize", "initialized", "thread/turns/list"})

    def test_item_kinds_and_turn_states(self):
        turn, gaps = sources.turn_from_items(3, {"id": "t", "status": {"type": "inProgress"}, "items": [
            {"type": "userMessage", "content": [{"type": "text", "text": "看看这张图"}, {"type": "localImage", "path": "a.png"}]},
            {"type": "plan", "text": "1. 读数据 2. 训练"},
            {"type": "commandExecution", "command": "python train.py", "status": "inProgress"},
            {"type": "commandExecution", "command": "python eval.py", "status": "completed", "exitCode": 2},
            {"type": "fileChange", "status": "completed", "changes": [{"path": "train.py", "kind": "update", "diff": "+lr=1e-4"}]},
            {"type": "mcpToolCall", "tool": "search_docs", "result": {"content": [{"type": "text", "text": "found 2 pages"}]}},
            {"type": "functionCallOutput", "name": "lookup", "output": "42"},
            {"type": "contextCompaction"}, {"type": "imageView", "path": "plot.png"}, {"type": "somethingNew", "x": 1}, "junk"]})
        self.assertEqual((turn.state, turn.user, turn.model), (ts.RUNNING, "看看这张图", "1. 读数据 2. 训练"))
        self.assertEqual([(r.kind, r.title, r.ok) for r in turn.records],
                         [(ts.COMMAND, "python train.py", None), (ts.COMMAND, "python eval.py", False),
                          (ts.FILE, "train.py", None), (ts.TOOL, "search_docs", None), (ts.OUTPUT, "lookup", None)])
        self.assertEqual(turn.records[3].text, "found 2 pages")
        self.assertEqual(ts.merge_gaps(gaps), (ts.Gap("attachment", 3, 2), ts.Gap("compacted", 3, 1), ts.Gap("output", 3, 1)))
        for status, state in (("completed", ts.DONE), ("interrupted", ts.ABORTED), ("failed", ts.ABORTED)):
            self.assertEqual(sources.turn_from_items(1, {"status": status, "items": []})[0].state, state)

    def test_a_server_that_does_not_answer_is_a_source_error(self):
        server = self.server([api_turn(1, "问", "答")], mode="silent")
        server.timeout = 0.4
        with self.assertRaises(sources.SourceError) as caught:
            sources.read(sources.SourceRef(sources.APPSERVER, conversation=THREAD_A), server)
        self.assertEqual(caught.exception.kind, "appserver")
        with self.assertRaises(sources.SourceError):
            sources.read(sources.SourceRef(sources.APPSERVER, conversation=THREAD_A), None)


# ------------------------------------------------------------------ transcript files
MARKED = """## 用户
想先确认这批数据能不能用来训练。

## 助手
我先写一个读取脚本。

### 记录: python load.py
300 rows read
no errors

用户：再看看划分。
助手：划分没有问题。

**User**: and then?
"""


class TranscriptFileTests(TempCase):
    def test_a_marked_text_file_becomes_turns(self):
        transcript = sources.read_transcript_file(self.write("talk.md", MARKED))
        self.assertEqual([(t.user, t.model, t.state) for t in transcript.turns], [
            ("想先确认这批数据能不能用来训练。", "我先写一个读取脚本。", ts.DONE),
            ("再看看划分。", "划分没有问题。", ts.DONE), ("and then?", "", ts.RUNNING)])
        self.assertEqual(transcript.turns[0].records, (ts.Record(ts.OUTPUT, "python load.py", "300 rows read\nno errors"),))

    def test_the_neutral_json_shape(self):
        data = {"title": "演示", "id": "demo-1", "gaps": [{"kind": "head", "turn": 1}, {"kind": "made-up"}], "turns": [
            {"user": "跑检查", "assistant": "跑了", "records": [{"kind": "command", "title": "check.py", "text": "ok all", "ok": True},
                                                          {"kind": "weird", "text": "x"}, "junk"]},
            {"user": "继续", "state": "running"}, "junk"]}
        transcript = sources.read_transcript_file(self.write("talk.json", data))
        self.assertEqual((transcript.title, transcript.conversation_id, len(transcript.turns)), ("演示", "demo-1", 2))
        self.assertEqual(transcript.turns[0].records[0], ts.Record(ts.COMMAND, "check.py", "ok all", True))
        self.assertEqual(transcript.turns[0].records[1].kind, ts.OUTPUT)
        self.assertEqual(transcript.turns[1].state, ts.RUNNING)
        self.assertEqual(transcript.gaps, (ts.Gap("head", 1, 1),))

    def test_files_without_a_conversation_are_refused(self):
        for name, content, kind in (("a.txt", "just some notes\nwithout marks", "format"),
                                    ("b.json", "{\"turns\": []}", "empty"), ("c.json", "{\"turns\": 3}", "format"),
                                    ("d.json", "{broken", "format")):
            with self.assertRaises(sources.SourceError) as caught:
                sources.read_transcript_file(self.write(name, content))
            self.assertEqual(caught.exception.kind, kind, name)

    def test_one_entry_point_recognises_each_kind_by_its_shape(self):
        export = self.write("conversations.json", [export_conversation()])
        session = self.write("rollout-x.jsonl", "\n".join(session_file_lines()))
        marked = self.write("talk.md", MARKED)
        self.assertEqual(sources.sniff(export), sources.EXPORT)
        self.assertEqual(sources.sniff(session), sources.SESSION)
        self.assertEqual(sources.sniff(marked), sources.FILE)
        self.assertEqual(sources.sniff(self.folder), sources.EXPORT)                # an unpacked export
        kind, listings = sources.listing(session)
        self.assertEqual((kind, len(listings), listings[0].turns), (sources.SESSION, 1, 3))
        kind, listings = sources.listing(marked)
        self.assertEqual((kind, listings[0].title), (sources.FILE, "想先确认这批数据能不能用来训练。"))
        for ref, turns in ((sources.SourceRef(sources.EXPORT, str(export), "conv-数据准备"), 4),
                           (sources.SourceRef(sources.SESSION, str(session)), 3),
                           (sources.SourceRef(sources.FILE, str(marked)), 3)):
            self.assertEqual(len(sources.read(ref).turns), turns)
        with self.assertRaises(sources.SourceError):
            sources.read(sources.SourceRef("telepathy", "x"))

    def test_reading_never_changes_the_file(self):
        path = self.write("conversations.json", [export_conversation()])
        before = (path.read_bytes(), os.stat(path).st_mtime_ns)
        sources.listing(path)
        sources.read(sources.SourceRef(sources.EXPORT, str(path), "conv-数据准备"))
        self.assertEqual((path.read_bytes(), os.stat(path).st_mtime_ns), before)


# ------------------------------------------------------------------ Claude Code session, pasted page
def claude_line(kind, content, **more):
    return json.dumps(dict({"type": kind, "sessionId": "s-1", "message": {"role": kind, "content": content}}, **more),
                      ensure_ascii=False)


def claude_lines():
    return [
        claude_line("user", "先看看测试能不能过。"),
        claude_line("assistant", [{"type": "text", "text": "我来跑一下测试。"},
                                  {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "pytest -q"}}]),
        claude_line("user", [{"type": "tool_result", "tool_use_id": "t1", "content": "3 passed, 1 failed", "is_error": False}]),
        claude_line("assistant", [{"type": "text", "text": "有一项没过，是日期解析。"}]),
        claude_line("assistant", [{"type": "text", "text": "子代理的话不算。"}], isSidechain=True),
        claude_line("user", "<system-reminder>内部提示</system-reminder>", isMeta=True),
        "not json",
        claude_line("user", "那修一下。"),
        claude_line("assistant", [{"type": "tool_use", "id": "t2", "name": "Edit", "input": {"file_path": "dates.py"}}]),
    ]


class OtherSourcesTests(TempCase):
    def test_a_claude_code_session_keeps_tool_results_as_records(self):
        turns, gaps = sources.claude_turns(claude_lines())
        self.assertEqual([(t.user, t.state) for t in turns], [("先看看测试能不能过。", ts.DONE), ("那修一下。", ts.RUNNING)])
        first = turns[0]
        self.assertEqual(first.model, "我来跑一下测试。\n\n有一项没过，是日期解析。")
        self.assertEqual(first.records, (ts.Record(ts.COMMAND, "Bash pytest -q", "3 passed, 1 failed", True),))
        self.assertEqual(ts.locate(first, "3 passed, 1 failed"), ts.RECORD)                # a tool result is not "the user said"
        self.assertNotIn("子代理", first.model)
        self.assertEqual(gaps, ())
        path = self.write("session-abc.jsonl", "\n".join(claude_lines()))
        self.assertEqual(sources.sniff(path), sources.CLAUDE)
        kind, listings = sources.listing(path)
        self.assertEqual((kind, listings[0].turns, listings[0].title), (sources.CLAUDE, 2, "先看看测试能不能过。"))
        transcript = sources.read(sources.SourceRef(sources.CLAUDE, str(path)))
        self.assertEqual((transcript.source, transcript.conversation_id, transcript.settled_through),
                         (sources.CLAUDE, "session-abc", 1))
        self.assertEqual(sources.sniff(self.write("rollout-y.jsonl", "\n".join(session_file_lines()))), sources.SESSION)

    def test_a_copied_chat_page_becomes_turns_with_nothing_that_counts_as_a_record(self):
        page = ("You said:\n这批数据能不能直接用来训练？\nChatGPT said:\n可以，我已经检查过，没有重复。\n\n"
                "You said:\n那继续。\nChatGPT said:\n基于此，我给出训练配置。\n")
        transcript = sources.read_pasted(page)
        self.assertEqual(transcript.source, sources.PASTED)
        self.assertEqual([(t.user, t.model) for t in transcript.turns],
                         [("这批数据能不能直接用来训练？", "可以，我已经检查过，没有重复。"), ("那继续。", "基于此，我给出训练配置。")])
        self.assertTrue(all(turn.records == () for turn in transcript.turns))             # whatever it "ran" is only its word
        self.assertEqual(ts.locate(transcript.turns[0], "我已经检查过，没有重复"), ts.MODEL)
        chinese = sources.read_pasted("您说：\n先看数据。\nChatGPT 说：\n好的。\n")
        self.assertEqual([(t.user, t.model) for t in chinese.turns], [("先看数据。", "好的。")])
        for text, kind in (("随便一段没有标记的文字", "format"), ("", "format")):
            with self.assertRaises(sources.SourceError) as caught:
                sources.read_pasted(text)
            self.assertEqual(caught.exception.kind, kind)
        with self.assertRaises(sources.SourceError):
            sources.read(sources.SourceRef(sources.PASTED))                               # it cannot be read again by itself


if __name__ == "__main__":
    unittest.main()
