"""Watching wired into the app: the driver, what is (not) kept on disk, what reaches
the model service and when, the view model, the command line and the real Tk
windows. The offline stand-in, a scripted model or a local fake model server;
synthetic conversations (E1)."""
import gc
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path

import safety  # noqa: F401  (keeps tests away from real user data)

from banxing import cli, llm, secretbox, store
from banxing.config import Settings
from banxing.evalkit import replay, replay_cases
from banxing.executor import InlineExecutor, ThreadedExecutor
from banxing.ui import strings, viewmodel
from banxing.watch import audit as au
from banxing.watch import ledger as lg
from banxing.watch import session as ss
from banxing.watch import sources, vault
from banxing.watch import transcript as ts
from banxing.watch.driver import View, Watcher

from test_security import KEY, ServerCase
from watchrig import (ASK, BLIND, CLAIM, GOAL, ONLINE, Scripted, conclusion, finding, look, research)

TELLTALES = ("客服知识库", "同一份索引", "recall@5", "全面优于", "重排模型")     # words of the conversation


def _display_available():
    try:
        import tkinter
        root = tkinter.Tk()
        root.destroy()
        del root
        gc.collect()        # freed here, on the thread that made it (see ui/app.py, "Threads")
        return ""
    except Exception as error:      # no tkinter, or no display
        return f"{type(error).__name__}: {error}"


NO_DISPLAY = _display_available()


def as_file_data(transcript, upto=None):
    return {"title": "比较两套检索方案", "turns": [
        {"user": t.user, "assistant": t.model, "state": t.state,
         "records": [{"kind": r.kind, "title": r.title, "text": r.text, "ok": r.ok} for r in t.records]}
        for t in transcript.turns if upto is None or t.number <= upto]}


class RecordingView(View):
    def __init__(self):
        self.bubble, self.bubbles, self.hidden, self.notices, self.changes = None, [], [], [], 0

    def show_bubble(self, bubble):
        self.bubble = bubble
        self.bubbles.append(bubble)

    def hide_bubble(self, reason):
        self.bubble = None
        self.hidden.append(reason)

    def changed(self):
        self.changes += 1

    def notice(self, kind, detail=""):
        self.notices.append((kind, detail))

    def kinds(self):
        return [kind for kind, _ in self.notices]


class Rig:
    """A Watcher with a scripted supervising model and a conversation file to follow."""

    def __init__(self, case, *answers, default=None, environ=None, upto=7, persist=False, folder=None):
        self.folder = Path(folder or tempfile.mkdtemp(prefix="banxing-test-"))
        case.addCleanup(shutil.rmtree, self.folder, True)
        self.now = 1000.0
        self.view = RecordingView()
        self.settings = Settings(self.folder, environ={"BANXING_PROVIDER": "mock"} if environ is None else environ)
        self.store = store.Store(self.folder, wall=lambda: self.now)
        self.executor = InlineExecutor()
        self.model = Scripted(*answers, default=default if default is not None else conclusion(finding()))
        self.watcher = Watcher(self.settings, self.store, self.executor, self.view, lambda: self.now,
                               auditor=au.Auditor(self.settings.model_config, complete=self.model))
        self.path = self.folder / "conversation.json"
        self.write(upto)
        self.ref = sources.SourceRef(sources.FILE, str(self.path))
        self.persist = persist
        self.problems = []

    def write(self, upto=None, transcript=None):
        self.path.write_text(json.dumps(as_file_data(transcript or research(), upto), ensure_ascii=False),
                             encoding="utf-8")
        stamp = time.time_ns() + len(self.model.calls) * 10_000_000 + (upto or 0) * 1_000_000
        os.utime(self.path, ns=(stamp, stamp))                     # a visible change even on coarse clocks

    def start(self, shown=None, first=1):
        shown = self.settings.model_config().destination if shown is None else shown
        self.watcher.begin(self.ref, shown, first, self.persist, self.problems.append)
        return self.problems[-1]

    def later(self, seconds=6.0):
        """Time passes; the UI's regular call looks at the conversation again."""
        self.now += seconds
        self.watcher.tick()

    def events(self, name):
        return [event for event in self.store.events() if event["event"] == name]

    def on_disk(self):
        return "\n".join(path.read_text(encoding="utf-8", errors="replace")
                         for path in self.folder.rglob("*") if path.is_file() and path != self.path)


class DriverTests(unittest.TestCase):
    def test_begin_reads_the_chosen_file_audits_it_and_shows_the_bubble(self):
        rig = Rig(self)
        self.assertEqual(rig.start(), "")
        bubble = rig.view.bubble
        self.assertEqual((bubble.finding.kind, bubble.through, bubble.finding.ask), (au.PREMISE, 7, ASK))
        state = rig.watcher.state()
        self.assertEqual((state["active"], state["through"], state["turns"], state["bubble"], state["source"],
                          state["findings"]), (True, 7, 7, True, sources.FILE, 1))
        self.assertEqual(rig.watcher.title, "比较两套检索方案")
        self.assertEqual(rig.events("watch_started")[0]["turns"], 7)

    def test_the_file_is_looked_at_again_only_when_it_changed(self):
        rig = Rig(self, conclusion(), conclusion(), upto=6)
        rig.start()
        reads, original = [], rig.watcher._read
        rig.watcher._read = lambda ref, server: reads.append(ref) or original(ref, server)
        rig.later()
        rig.later()
        self.assertEqual(reads, [])                                                # unchanged: not even parsed
        rig.write(7)
        rig.later(1.0)
        self.assertEqual(reads, [])                                                # not yet time for another look
        rig.later()
        self.assertEqual((len(reads), rig.model.audits()[-1]["meta"]["new"]), (1, [7]))

    def test_the_users_answers_reach_the_ledger(self):
        rig = Rig(self)
        rig.start()
        rig.watcher.took_back()
        self.assertEqual((rig.view.bubble, rig.view.hidden), (None, ["took_back"]))
        self.assertEqual(rig.watcher.session.ledger.marks[0].state, lg.ASKED)
        rig.watcher.look_now()
        self.assertTrue(rig.view.bubble.manual)
        rig.watcher.intended()
        rig.watcher.add_note("索引我自己核对过")
        self.assertEqual(rig.watcher.session.ledger.marks[0].note, "索引我自己核对过")
        rig.watcher.pause()
        self.assertTrue(rig.watcher.state()["paused"])
        rig.watcher.resume()
        rig.watcher.correct_goal("只比较长问题")
        self.assertEqual(rig.watcher.session.ledger.goal.by, "user")
        self.assertGreater(rig.view.changes, 5)
        idle = Rig(self)
        idle.watcher.look_now()
        self.assertEqual(idle.view.kinds(), ["watch_none"])

    def test_a_file_that_disappears_is_said_once_and_picked_up_when_it_returns(self):
        rig = Rig(self, conclusion(), conclusion(), upto=6)
        rig.start()
        rig.path.unlink()
        rig.later()
        rig.later()
        self.assertEqual(rig.view.notices.count(("watch_unreadable", "missing")), 1)
        self.assertEqual(rig.watcher.state()["problem"], "missing")
        rig.write(7)
        rig.later()
        self.assertEqual((rig.watcher.state()["problem"], rig.watcher.session.ledger.through), ("", 7))

    def test_stopping_removes_the_bubble_and_drops_what_is_in_flight(self):
        rig = Rig(self)
        rig.executor.hold = True
        rig.watcher.begin(rig.ref, "", 1, False, rig.problems.append)
        rig.executor.release()                                                     # the read; the audit is now held
        self.assertTrue(rig.watcher.session.auditing)
        rig.watcher.stop()
        rig.executor.release()                                                     # the audit comes back to nothing
        self.assertEqual((rig.watcher.session, rig.view.bubble, rig.watcher.state()["active"]), (None, None, False))

    def test_begin_refuses_a_destination_the_user_was_not_shown_and_bad_sources(self):
        rig = Rig(self)
        self.assertEqual(rig.start(shown="openai|https://somewhere.example.test"), "destination")
        self.assertEqual((rig.model.calls, rig.watcher.active), ([], False))
        self.assertEqual(rig.start(first=40), "first")
        rig.path.write_text("no marks here", encoding="utf-8")
        self.assertEqual(rig.start(), "format")
        rig.path.unlink()
        self.assertEqual(rig.start(), "missing")
        self.assertEqual((rig.model.calls, rig.watcher.active), ([], False))
        unset = Rig(self, environ={})
        self.assertEqual(unset.start(shown=""), "model")

    def test_listing_a_file_and_the_local_codex_sessions(self):
        rig, got = Rig(self), []
        rig.watcher.listing(str(rig.path), lambda *result: got.append(result))
        kind, listings, problem = got[0]
        self.assertEqual((kind, problem, listings[0].turns), (sources.FILE, "", 7))
        rig.watcher.listing(str(rig.folder / "nothing.zip"), lambda *result: got.append(result))
        self.assertEqual(got[1], ("", [], "missing"))
        rig.watcher.codex_sessions(lambda kind, listings: got.append((kind, listings)))
        self.assertEqual(got[2], (sources.SESSION, []))                            # the sandbox has no Codex data

    def test_a_pasted_conversation_is_watched_and_grows_when_pasted_again(self):
        rig = Rig(self, conclusion(goal={"text": "数据能不能用", "state": "confirmed",
                                         "refs": [{"turn": 1, "quote": "这批数据能不能直接用来训练"}]}, anchors=[]))
        page = "You said:\n这批数据能不能直接用来训练？\nChatGPT said:\n可以，我已经检查过，没有重复。\n"
        done = []
        rig.watcher.begin(sources.SourceRef(sources.PASTED), "", 1, False, done.append, text=page)
        self.assertEqual((done, rig.watcher.state()["source"], rig.watcher.session.ledger.through),
                         ([""], sources.PASTED, 1))
        rig.later()                                                                # nothing to re-read by itself
        self.assertEqual(len(rig.model.audits()), 1)
        self.assertEqual(rig.watcher.paste(page + "You said:\n那继续。\nChatGPT said:\n基于此，我给出训练配置。\n"), "")
        self.assertEqual(rig.model.audits()[-1]["meta"]["new"], [2])
        self.assertEqual(rig.watcher.paste("没有标记的文字"), "format")

    def test_reads_and_audits_run_off_the_ui_thread_and_come_back_on_it(self):
        rig = Rig(self)
        threads = {"audit": [], "done": []}
        inner = rig.model

        def recording(*args, **kwargs):
            threads["audit"].append(threading.get_ident())
            return inner(*args, **kwargs)
        rig.watcher.auditor = au.Auditor(rig.settings.model_config, complete=recording)
        executor = rig.watcher.executor = ThreadedExecutor()
        rig.watcher.begin(rig.ref, "", 1, False, lambda problem: threads["done"].append(threading.get_ident()))
        deadline = time.monotonic() + 10
        while rig.view.bubble is None and time.monotonic() < deadline:
            executor.pump()
            time.sleep(0.01)
        self.assertIsNotNone(rig.view.bubble)
        me = threading.get_ident()
        self.assertEqual(threads["done"], [me])
        self.assertTrue(threads["audit"] and all(ident != me for ident in threads["audit"]))


class StorageTests(unittest.TestCase):
    def test_by_default_nothing_of_the_conversation_is_written_anywhere(self):
        rig = Rig(self, look(turns=[3], search=["同一份索引"], independent=True))
        rig.start()
        rig.watcher.intended("索引我自己核对过")
        rig.watcher.correct_goal("只比较长问题上的表现")
        rig.watcher.look_now()
        written = rig.on_disk()
        self.assertFalse(vault.path(rig.folder).exists())
        for word in TELLTALES + ("索引我自己核对过", "只比较长问题上的表现", ASK[:12]):
            self.assertNotIn(word, written, word)
        self.assertNotRegex(written, "[一-鿿]{4,}")                    # no sentence of Chinese text at all
        self.assertNotIn("conversation.json", written)                             # not even where it was read from

    def test_the_trial_log_takes_numbers_and_names_only(self):
        rig = Rig(self, look(search=["同一份索引"], independent=True))
        rig.start()
        rig.watcher.took_back()
        rig.watcher.stop()
        events = [event for event in rig.store.events() if event["event"].startswith("watch_")]
        self.assertGreaterEqual({event["event"] for event in events},
                                {"watch_started", "watch_audit", "watch_decided", "watch_took_back", "watch_stopped"})
        audit = rig.events("watch_audit")[0]
        self.assertEqual((audit["through"], audit["requests"], audit["looks"], audit["searches"], audit["independent"],
                          audit["findings"]), (7, 3, 1, 1, True, 1))
        for event in events:
            for value in event.values():
                self.assertTrue(isinstance(value, (bool, int, float)) or store._NAME.fullmatch(value), event)
        numbers = rig.store.summary()
        self.assertEqual((numbers["audits"], numbers["bubbles"], numbers["taken_back"], numbers["looked_back"],
                          numbers["independent_readings"], numbers["wrong_rate"]), (1, 1, 1, 1, 1, 0.0))

    def test_a_kept_ledger_is_one_encrypted_file_and_comes_back_after_a_restart(self):
        rig = Rig(self, persist=True)
        rig.start()
        rig.watcher.intended("我核对过")
        target = vault.path(rig.folder)
        self.assertEqual([p.name for p in (rig.folder / "vault").iterdir()], ["watch.bin"])
        stored = target.read_text(encoding="utf-8")
        self.assertRegex(stored, r"^(plain|dpapi):[A-Za-z0-9+/=]+$")              # the secret box's form, nothing else
        for word in TELLTALES:
            self.assertNotIn(word, stored)
        ref, session = vault.load(rig.folder)
        live = rig.watcher.session
        self.assertEqual((ref, session.ledger, session.known, session.usage, session.shown, session.muted,
                          session.first, session.destination),
                         (rig.ref, live.ledger, live.known, live.usage, live.shown, live.muted, 1, ""))
        again = Rig(self, default=conclusion())                                    # a new start of the app
        shutil.copytree(rig.folder / "vault", again.folder / "vault")
        again.ref = rig.ref
        self.assertTrue(again.watcher.restore())
        self.assertEqual(again.watcher.session.ledger, live.ledger)
        again.later(1.0)                                                           # reads the file again ...
        self.assertEqual(again.model.calls, [])                                    # ... and has nothing new to ask

    def test_a_restored_ledger_does_not_send_to_a_service_that_was_not_agreed(self):
        rig = Rig(self, conclusion(), persist=True, upto=6)
        rig.start()
        rig.write(7)
        other = Rig(self, environ={"BANXING_PROVIDER": "openai", "BANXING_BASE_URL": "https://models.example.test/v1",
                                   "BANXING_API_KEY": KEY})
        shutil.copytree(rig.folder / "vault", other.folder / "vault")
        self.assertTrue(other.watcher.restore())
        other.later(1.0)
        self.assertEqual(other.model.calls, [])                                    # agreed for the stand-in, not for this
        self.assertEqual(other.watcher.state()["state"], ss.NEEDS_CONSENT)
        self.assertEqual(other.watcher.agree("openai|https://elsewhere.example.test"), "destination")
        self.assertEqual(other.model.calls, [])
        self.assertEqual(other.watcher.agree("openai|https://models.example.test"), "")
        self.assertEqual((len(other.model.calls), other.view.bubble.finding.kind), (1, au.PREMISE))

    def test_stopping_or_clearing_deletes_the_kept_ledger(self):
        rig = Rig(self, persist=True)
        rig.start()
        self.assertTrue(vault.path(rig.folder).exists())
        rig.watcher.stop()
        self.assertFalse(vault.path(rig.folder).exists())
        rig.start()
        rig.watcher.set_persist(False)
        self.assertFalse(vault.path(rig.folder).exists())
        rig.watcher.set_persist(True)
        self.assertTrue(vault.path(rig.folder).exists())
        rig.watcher.clear_data()
        self.assertEqual((sorted(p.name for p in rig.folder.iterdir() if p != rig.path), rig.watcher.active,
                          rig.view.bubble), ([], False, None))

    def test_encrypted_or_not_stored(self):
        """Windows cannot encrypt → nothing is written, watching goes on in memory, and
        the user is told. (Windows is simulated by switching the platform check.)"""
        saved = (secretbox._on_windows, secretbox._dpapi)
        self.addCleanup(lambda: setattr(secretbox, "_on_windows", saved[0]) or setattr(secretbox, "_dpapi", saved[1]))

        def broken(data, protect):
            raise secretbox.SecretError("DPAPI failed (error 5)")
        secretbox._on_windows, secretbox._dpapi = (lambda: True), broken
        rig = Rig(self, persist=True)
        self.assertEqual(rig.start(), "")
        self.assertFalse((rig.folder / "vault").exists())
        self.assertEqual(rig.view.kinds().count("watch_error_encrypt"), 1)         # said once
        self.assertFalse(rig.watcher.persist)
        self.assertIsNotNone(rig.view.bubble)                                      # watching itself goes on
        self.assertEqual(rig.events("watch_store_failed")[0]["why"], "encrypt")
        for word in TELLTALES:
            self.assertNotIn(word, rig.on_disk())

    def test_a_ledger_written_by_a_newer_version_is_left_alone(self):
        rig = Rig(self, persist=True)
        target = vault.path(rig.folder)
        target.parent.mkdir(parents=True)
        newer = secretbox.protect(json.dumps({"schema": vault.WATCH_SCHEMA + 1, "ledger": {"schema": 9}}))
        target.write_text(newer, encoding="utf-8")
        self.assertIsNone(vault.load(rig.folder))
        self.assertFalse(rig.watcher.restore())
        self.assertEqual(rig.start(), "")
        self.assertEqual(target.read_text(encoding="utf-8"), newer)                # not overwritten
        self.assertIn("watch_error_newer", rig.view.kinds())

    def test_unreadable_or_foreign_stored_data_is_not_a_ledger(self):
        folder = Path(tempfile.mkdtemp(prefix="banxing-vault-"))
        self.addCleanup(shutil.rmtree, folder, True)
        target = vault.path(folder)
        target.parent.mkdir(parents=True)
        for content in ("", "garbage", "plain:!!!", secretbox.protect("not json"), secretbox.protect("[1, 2]"),
                        secretbox.protect(json.dumps({"schema": 1, "source": {"kind": "telepathy"}})),
                        secretbox.protect(json.dumps({"schema": 1, "source": {"kind": sources.FILE}, "ledger": {}}))):
            target.write_text(content, encoding="utf-8")
            self.assertIsNone(vault.load(folder), content[:20])
        self.assertTrue(vault.forget(folder))
        self.assertFalse(vault.forget(folder))


class WireTests(ServerCase):
    """What actually leaves the machine, seen by a local server standing in for the
    model service."""

    def answer(self, *replies):
        queue = [json.dumps(reply, ensure_ascii=False) for reply in replies]

        def respond(method, path):
            content = queue.pop(0) if len(queue) > 1 else queue[0]
            return 200, {}, {"choices": [{"message": {"content": content}}]}
        return respond

    def rig(self, server, upto=7):
        rig = Rig(self, upto=upto, environ={"BANXING_PROVIDER": "openai", "BANXING_BASE_URL": server.url + "/v1",
                                            "BANXING_API_KEY": KEY})
        rig.watcher.auditor = au.Auditor(rig.settings.model_config, timeout=10)    # the real client
        return rig

    def body(self, request):
        return json.loads(request["body"])["messages"][-1]["content"]

    def test_nothing_is_sent_before_the_user_agreed_to_this_destination(self):
        model = self.server(self.answer(conclusion(finding())))
        rig = self.rig(model)
        for shown in ("", "openai|https://api.openai.com", f"openai|{model.url.replace('127.0.0.1', 'localhost')}"):
            self.assertEqual(rig.start(shown=shown), "destination")
        self.assertEqual((model.requests, rig.watcher.active), ([], False))

    def test_only_the_allowed_turns_go_to_the_agreed_place(self):
        model = self.server(self.answer(conclusion(finding(basis=[{"turn": 6, "quote": "两者用的是同一份索引"}]))))
        rig = self.rig(model)
        self.assertEqual(rig.start(shown=f"openai|{model.url}", first=4), "")
        self.assertEqual(len(model.requests), 1)
        self.assertEqual((model.requests[0]["method"], model.requests[0]["path"]), ("POST", "/v1/chat/completions"))
        body = self.body(model.requests[0])
        self.assertIn("recall@5 = 0.71", body)                                     # turn 4
        self.assertNotIn("客服知识库", body)                                         # turn 1: outside the range
        self.assertNotIn("确认过了", body)                                           # turn 3
        usage = rig.watcher.session.usage
        self.assertEqual((usage.requests, usage.turns, usage.chars), (1, (4, 5, 6, 7), len(body)))

    def test_every_step_of_an_audit_goes_to_the_same_place_and_is_counted(self):
        model = self.server(self.answer(look(turns=[], search=["索引路径"], independent=True), BLIND,
                                        conclusion(finding())))
        rig = self.rig(model)
        self.assertEqual(rig.start(shown=f"openai|{model.url}"), "")
        self.assertEqual(len(model.requests), 3)                                   # audit, blind reading, audit
        blind = self.body(model.requests[1])
        self.assertIn("<evidence>", blind)
        self.assertNotIn("确认过了", blind)                                          # the main model's words were left out
        self.assertIn("[搜「索引路径」] 在允许读的范围内没有找到。", self.body(model.requests[2]))
        usage = rig.watcher.session.usage
        self.assertEqual((usage.requests, usage.chars), (3, sum(len(self.body(r)) for r in model.requests)))
        self.assertEqual(rig.view.bubble.finding.kind, au.PREMISE)

    def test_a_secret_in_the_conversation_is_masked_before_it_leaves(self):
        model = self.server(self.answer(conclusion()))
        rig = self.rig(model)
        leaky = research()
        turns = list(leaky.turns)
        turns[1] = replace(turns[1], user=turns[1].user + " 登录用的 token=tk_live_9f8e7d6c5b4a3210")
        rig.write(transcript=replace(leaky, turns=tuple(turns)))
        self.assertEqual(rig.start(shown=f"openai|{model.url}"), "")
        self.assertNotIn("tk_live_9f8e7d6c5b4a3210", model.requests[0]["body"])
        self.assertEqual(rig.watcher.session.usage.masked, 1)

    def test_a_destination_changed_by_the_environment_gets_nothing(self):
        model, other = self.server(self.answer(conclusion())), self.server(self.answer(conclusion(finding())))
        rig = self.rig(model, upto=6)
        self.assertEqual(rig.start(shown=f"openai|{model.url}"), "")
        rig.settings._environ["BANXING_BASE_URL"] = other.url + "/v1"              # e.g. a changed variable
        rig.write(7)
        rig.later()
        self.assertEqual((len(model.requests), other.requests), (1, []))
        self.assertIn("watch_consent", rig.view.kinds())
        self.assertEqual(rig.watcher.state()["state"], ss.NEEDS_CONSENT)
        self.assertEqual(rig.watcher.agree(f"openai|{other.url}"), "")
        self.assertEqual((len(other.requests), len(model.requests)), (1, 1))

    def test_a_redirect_is_refused_not_followed_and_not_retried(self):
        elsewhere = self.server()
        model = self.server(lambda method, path: (307, {"Location": elsewhere.url + "/v1/chat/completions"}, {}))
        rig = self.rig(model)
        self.assertEqual(rig.start(shown=f"openai|{model.url}"), "")
        rig.later()
        rig.later()
        self.assertEqual((len(model.requests), elsewhere.requests), (1, []))
        self.assertEqual((rig.watcher.session.error, rig.view.bubble), ("redirect", None))


class ViewModelTests(unittest.TestCase):
    def setUp(self):
        strings.set_language("zh")
        self.addCleanup(strings.set_language, "zh")

    def test_the_bubble_says_what_is_off_what_it_bears_on_and_what_to_ask(self):
        rig = Rig(self, conclusion(finding(), finding("drift", basis=[GOAL], now=[ONLINE], title="已经在写上线方案了")))
        rig.start()
        view = viewmodel.nudge_view(rig.view.bubble)
        self.assertEqual((view.kind, view.title), ("前提没查过", "「同一份索引」只是说过，后面的归因都靠它"))
        self.assertEqual(view.lines[1], "它影响：两套方案的索引如果不同，提升可能来自索引而不是重排，结论要重做。")
        self.assertEqual(view.lines[2], "最省事的核验：打印两套方案的索引路径和构建时间。")
        self.assertEqual(view.ask, ASK)
        self.assertEqual(view.buttons, ("sources", "copy", "intended", "wrong"))
        self.assertEqual(view.evidence, ("第 3 轮 · 主模型说：「A 和 B 用的是同一份索引」", "眼下正靠它往前走的一步：",
                                         "第 6 轮 · 主模型说：「差别只能来自重排模型」"))
        self.assertIn("读到第 7 轮 · 主模型刚结束一轮 · 账本里还有 1 处", view.footer)

    def test_the_ledger_puts_the_shakiest_first_and_says_what_backs_each_entry(self):
        rig = Rig(self, look(turns=[3], search=["索引路径", "同一份索引"], independent=True))
        rig.start()
        rig.watcher.intended("索引我自己核对过")
        session = rig.watcher.session
        ledger = replace(session.ledger, gaps=(ts.Gap("attachment", 2, 1), ts.Gap("branch", 0, 1)))
        lines = viewmodel.ledger_lines(ledger, session.findings, session.usage, "", session.last)
        text = "\n".join(lines)
        for needle in ("你想回答的问题（你说过）", "方案 B 是否比 A 更适合客服知识库（第 1 轮）",
                       "？ 没查过 · 只有主模型自己说过 · 前提  A 和 B 用同一份索引（第 3 轮）", "靠它的：第 6 轮的归因",
                       "✓ 有依据 · 你说的 · 你的限制  评测只用线下 300 条标注集，不动线上数据（第 2 轮）",
                       "✓ 有依据 · 有记录 · 结果  B 的 recall@5 为 0.78（第 5 轮）",
                       "! [前提没查过 · 正在影响方向]", "可以原样带回去问主模型：" + ASK,
                       "第 7 轮后 · 前提没查过：「同一份索引」只是说过，后面的归因都靠它 —— 你说有意如此", "索引我自己核对过",
                       "第 2 轮有 1 个附件或图片读不到", "这条对话有别的分支",
                       "它回头查了 1 次：看了 第 3、6 轮，搜了 2 个词（1 个有结果）；做了一次不看主模型说法的独立一读",
                       "使用范围：已读到第 7 轮；提交过 第 1–7 轮", "请求 3 次", "这台电脑（离线演示）"):
            self.assertIn(needle, text)
        anchors = [line for line in lines if line.startswith("  ？") or line.startswith("  ✓")]
        self.assertTrue(anchors[0].startswith("  ？"))                              # what was never checked comes first
        self.assertEqual(viewmodel.ledger_lines(lg.Ledger())[0], "还没有读完这条对话。")

    def test_a_clean_look_says_why_it_holds(self):
        rig = Rig(self, conclusion(sound="两次评测都有记录，目标没有变。"))
        rig.start()
        session = rig.watcher.session
        text = "\n".join(viewmodel.ledger_lines(session.ledger, session.findings, session.usage, "", session.last))
        self.assertIn("没有发现走偏：两次评测都有记录，目标没有变。", text)
        self.assertIn("它没有回头查", text)
        self.assertEqual(viewmodel.usage_line(lg.Ledger(through=10), ss.Usage(3, 4200, 2, (1, 2, 3, 7, 9, 10)),
                                              "https://models.example.test"),
                         "使用范围：已读到第 10 轮；提交过 第 1–3、7、9–10 轮（共 4200 字，请求 3 次）"
                         "→ https://models.example.test · 已隐去 2 处疑似密钥")

    def test_the_menu_and_the_status_follow_the_state(self):
        rig = Rig(self)
        ids = lambda: [item.id for item in viewmodel.menu_items(rig.watcher.state()) if item.id]      # noqa: E731
        self.assertEqual(ids(), ["watch", "model", "folder", "clear", "about", "quit"])
        self.assertEqual(viewmodel.menu_items(rig.watcher.state())[0].label, "还没有在旁听的对话")
        rig.start()
        self.assertEqual(ids()[:5], ["look", "ledger", "pause", "stop", "watch"])
        self.assertEqual(viewmodel.status_text(rig.watcher.state()), "旁听中 · 已读到第 7 轮 · 账上记着 1 处疑点")
        rig.watcher.pause()
        self.assertIn("resume", ids())
        for state, label in (({"state": "diverged"}, "对话的历史变了"), ({"state": "consent"}, "模型服务变了"),
                             ({"problem": "missing"}, "读不到这条对话了"), ({"auditing": True}, "正在回查原文")):
            self.assertIn(label, viewmodel.status_text(dict({"active": True, "model": "mock"}, **state)))
        self.assertEqual(viewmodel.status_text({"active": False, "model": ""}), "还没配置旁听用的模型")
        self.assertIn("paste", [i.id for i in viewmodel.menu_items({"active": True, "model": "x", "source": "pasted_text"})])
        self.assertIn("hide", [i.id for i in viewmodel.menu_items(rig.watcher.state(), has_tray=True)])
        self.assertEqual([i.id for i in viewmodel.tray_items({"active": False}, False) if i.id],
                         ["show", "watch", "model", "quit"])

    def test_every_notice_and_every_built_key_has_words_in_both_languages(self):
        for key, pair in strings.STRINGS.items():
            self.assertEqual(len(pair), 2, key)
            self.assertTrue(all(isinstance(text, str) and text for text in pair), key)
            self.assertEqual(set(re.findall(r"\{(\w+)\}", pair[0])), set(re.findall(r"\{(\w+)\}", pair[1])), key)
        built = (["kind_" + kind for kind in au.KINDS] + ["anchor_" + kind for kind in lg.ANCHOR_KINDS]
                 + ["status_" + status for status in lg.STATUSES]
                 + ["backing_" + b for b in (lg.BY_RECORD, lg.BY_USER, lg.BY_MODEL)]
                 + ["mark_" + state for state in lg.MARK_STATES] + ["weight_" + w for w in (au.DIRECTION, au.NOTE)]
                 + ["goal_" + s for s in (lg.CONFIRMED, lg.EXPLORING, "user", "none")]
                 + ["gap_" + kind for kind in ts.GAP_KINDS] + ["ref_" + part for part in ts.PARTS]
                 + ["provider_" + name for name in llm.PROVIDERS]
                 + ["source_error_" + k for k in ("missing", "too_large", "unreadable", "format", "empty", "not_found",
                                                  "appserver", "clipboard")]
                 + ["watch_error_" + k for k in ("model", "destination", "first", "choose", "source")]
                 + ["model_error_" + k for k in ("provider", "base_url", "api_key", "api_key_again", "encrypt")]
                 + ["notice_watch_error_" + k for k in ("encrypt", "newer", "write")]
                 + ["caution_" + c for c in ("privilege", "destructive", "other")]
                 + ["nudge_mode_" + m for m in (au.AUTO, au.MANUAL)])
        self.assertEqual([key for key in built if key not in strings.STRINGS], [])
        import inspect
        source = inspect.getsource(ss) + inspect.getsource(sys.modules["banxing.watch.driver"])
        kinds = {kind for kind in re.findall(r'[Nn]otice\("(watch_[a-z_]+)"', source) if not kind.endswith("_")}
        self.assertGreaterEqual(len(kinds), 6)
        for language in ("zh", "en"):
            strings.set_language(language)
            for kind in sorted(kinds) + ["watch_error_encrypt", "watch_error_newer", "watch_error_write", "copied"]:
                text = viewmodel.notice_text(kind, "missing")
                self.assertTrue(text and not text.startswith("notice_"), (language, kind))
            rig = Rig(self)
            rig.start()
            self.assertTrue(all(viewmodel.nudge_view(rig.view.bubble).lines))

    def test_every_key_used_in_the_code_is_defined(self):
        used = set()
        for path in sorted((Path(cli.__file__).parent).rglob("*.py")):
            used.update(re.findall(r"""\bt\(\s*["']([a-z_]+)["']\s*[,)]""", path.read_text(encoding="utf-8")))
        self.assertGreater(len(used), 60)
        self.assertEqual(sorted(used - set(strings.STRINGS)), [])


class CommandLineTests(ServerCase):
    def setUp(self):
        self.folder = self.temp()
        names = ("BANXING_HOME", "BANXING_PROVIDER", "BANXING_BASE_URL", "BANXING_API_KEY")
        self.saved = {name: os.environ.get(name) for name in names}
        self.addCleanup(self.restore)
        os.environ["BANXING_HOME"] = str(self.folder / "home")
        self.lines = []
        self.emit, cli.emit = cli.emit, self.lines.append
        self.addCleanup(setattr, cli, "emit", self.emit)
        self.addCleanup(strings.set_language, "zh")
        self.path = self.folder / "talk.json"
        self.path.write_text(json.dumps(as_file_data(research()), ensure_ascii=False), encoding="utf-8")

    def restore(self):
        for name, value in self.saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    @property
    def text(self):
        return "\n".join(self.lines)

    def stand_in_case(self, upto=None):
        """A conversation in the stand-in's own vocabulary (it is keyword rules, nothing more)."""
        case = replay_cases.transcript_of(replay_cases.all_cases()[0])
        self.path.write_text(json.dumps(as_file_data(case, upto), ensure_ascii=False), encoding="utf-8")

    def test_version_and_summary(self):
        self.assertEqual(cli.main(["--version"]), 0)
        self.assertRegex(self.lines[0], r"^\d+\.\d+\.\d+")
        self.assertEqual(cli.main(["--summary"]), 0)
        self.assertEqual(json.loads(self.lines[1])["bubbles"], 0)

    def test_selftest_passes_its_required_checks_and_leaves_nothing_behind(self):
        report = self.folder / "out" / "selftest.json"
        self.assertEqual(cli.main(["--selftest", "--report", str(report)]), 0)
        data = json.loads(report.read_text(encoding="utf-8"))
        self.assertTrue(data["ok"])
        for name in ("watch", "prompts", "secretbox"):
            self.assertTrue(data["checks"][name]["ok"], data["checks"][name])
        self.assertIn("self-test passed", self.lines[-1])
        self.assertFalse((self.folder / "home").exists())
        self.assertEqual(cli.main(["--selftest", "--require", "no_such_check"]), 1)

    def test_watch_prints_the_ledger_and_the_concern_with_the_stand_in(self):
        os.environ["BANXING_PROVIDER"] = "mock"
        self.stand_in_case(upto=4)
        report = self.folder / "out" / "watch.json"
        self.assertEqual(cli.main(["--watch", str(self.path), "--report", str(report)]), 0)
        self.assertEqual(self.text.count("▶ ["), 1)                                # the concern, with its question
        self.assertIn("请给出当时实际运行的命令和输出", self.text)
        data = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual((data["turns"], data["through"], data["bubbles"], data["evidence"]),
                         (4, 4, [{"turn": 4, "kind": "premise"}], "plumbing-only"))
        self.assertEqual((data["looks"], data["searches"], data["independent"], data["anchors"]["unchecked"]),
                         (1, 1, True, 1))
        self.assertNotRegex(report.read_text(encoding="utf-8"), "[一-鿿]")           # the report holds no text of it
        self.assertFalse((self.folder / "home").exists())                          # nothing kept by the command line

    def test_stepwise_replays_a_conversation_turn_by_turn(self):
        """How a real conversation is replayed: no labels, just where the watcher would
        have spoken. Here with the stand-in, so it shows the plumbing only."""
        os.environ["BANXING_PROVIDER"] = "mock"
        self.stand_in_case()
        report = self.folder / "steps.json"
        self.assertEqual(cli.main(["--watch", str(self.path), "--stepwise", "--from-turn", "2", "--report", str(report)]), 0)
        data = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual([row["turn"] for row in data["timeline"]], [2, 3, 4, 5])
        self.assertEqual(data["bubbles"], [{"turn": 4, "kind": "premise"}])         # once, not again at turn 5
        self.assertEqual(data["timeline"][-1]["why"], "repeat")
        self.assertEqual(min(data["turns_sent"]), 2)
        self.assertEqual(self.text.count("▶"), 1)
        self.assertNotRegex(report.read_text(encoding="utf-8"), "[一-鿿]")

    def test_watch_lists_an_export_and_wants_to_know_which_conversation(self):
        from test_watch_sources import export_conversation
        os.environ["BANXING_PROVIDER"] = "mock"
        export = self.folder / "conversations.json"
        export.write_text(json.dumps([export_conversation("甲"), export_conversation("乙", 300.0)], ensure_ascii=False),
                          encoding="utf-8")
        self.assertEqual(cli.main(["--watch", str(export), "--list"]), 0)
        self.assertEqual([line.split("\t")[0] for line in self.lines], ["conv-乙", "conv-甲"])
        self.assertEqual(cli.main(["--watch", str(export)]), 2)
        self.assertEqual(cli.main(["--watch", str(export), "--conversation", "conv-甲"]), 0)
        self.assertEqual(cli.main(["--watch", str(self.folder / "missing.json")]), 1)
        os.environ.pop("BANXING_PROVIDER")
        self.assertEqual(cli.main(["--watch", str(self.path)]), 2)

    def test_watch_sends_nothing_until_this_run_names_the_destination(self):
        answer = {"choices": [{"message": {"content": json.dumps(conclusion(finding()), ensure_ascii=False)}}]}
        model = self.server(lambda method, path: (200, {}, answer))
        os.environ.update(BANXING_PROVIDER="openai", BANXING_BASE_URL=model.url + "/v1", BANXING_API_KEY=KEY)
        self.assertEqual(cli.main(["--watch", str(self.path)]), 3)
        self.assertIn(model.url, self.text)                                        # it says where it would go
        self.assertEqual(cli.main(["--watch", str(self.path), "--agree", "https://api.openai.com"]), 3)
        settings = Settings()                                                      # a consent recorded earlier, in general,
        self.assertTrue(settings.give_consent(settings.model_config().destination))
        self.assertEqual(cli.main(["--watch", str(self.path)]), 3)                 # does not cover this conversation
        self.assertEqual(model.requests, [])
        self.assertEqual(cli.main(["--watch", str(self.path), "--agree", model.url]), 0)
        self.assertEqual(len(model.requests), 1)
        self.assertIn("「同一份索引」只是说过", self.text)

    def test_replay_is_marked_as_plumbing_only_and_needs_the_destination_named(self):
        os.environ["BANXING_PROVIDER"] = "mock"
        report = self.folder / "replay.json"
        self.assertEqual(cli.main(["--replay", "--report", str(report)]), 0)
        data = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual((data["evidence"], data["cases"], data["plumbing_ok"]),
                         ("plumbing-only", len(replay_cases.CASES), True))
        self.assertIn("NOT evidence", data["note"])
        model = self.server()
        os.environ.update(BANXING_PROVIDER="openai", BANXING_BASE_URL=model.url + "/v1", BANXING_API_KEY=KEY)
        self.assertEqual(cli.main(["--replay"]), 3)
        self.assertEqual(cli.main(["--replay", "--agree", "https://api.openai.com"]), 3)
        self.assertEqual(model.requests, [])
        for name in ("BANXING_PROVIDER", "BANXING_BASE_URL", "BANXING_API_KEY"):
            os.environ.pop(name)
        self.assertEqual(cli.main(["--replay"]), 2)


class ReplayTests(unittest.TestCase):
    def test_the_cases_come_in_labelled_pairs_and_are_well_formed(self):
        cases = replay_cases.all_cases()
        self.assertGreaterEqual(len(cases), 10)
        self.assertEqual(len({case["id"] for case in cases}), len(cases))
        self.assertGreaterEqual({case["quiet"] for case in cases if not case.get("expect")},
                                {"checked", "user_pivot", "deep_dive", "hedged"})
        self.assertGreaterEqual({case["expect"] for case in cases if case.get("expect")},
                                {"premise", "drift", "overreach", "stall"})
        for case in cases:
            turns = len(case["turns"])
            if case.get("expect"):
                first, last = case["window"]
                self.assertTrue(1 <= first <= last <= turns, case["id"])
                self.assertIn(case["expect"], au.KINDS)
                self.assertTrue(case.get("quotes"), case["id"])
            else:
                self.assertIn(case["quiet"], replay.QUIET_KINDS, case["id"])
            self.assertEqual(len(replay_cases.transcript_of(case).turns), turns)

    def test_each_turn_is_first_seen_while_the_main_model_is_still_working(self):
        pending = replay._pending(replay_cases.transcript_of(replay_cases.all_cases()[0]), 4)
        self.assertEqual([t.number for t in pending.turns], [1, 2, 3, 4])
        self.assertEqual((pending.turns[-1].state, pending.turns[-1].model, pending.settled_through), (ts.RUNNING, "", 3))

    def test_the_runner_reports_a_supervisor_that_cries_wolf(self):
        """A scripted model that raises a concern after every turn: the runner must
        report the early bubbles and the bubbles in quiet cases."""
        def eager(user, meta):
            turn = next(t for t in meta["turns"] if len(t["user"]) >= 8)
            said = next((t for t in reversed(meta["turns"]) if len(t["model"]) >= 8), None)
            ref = {"turn": turn["number"], "quote": turn["user"][:20]}
            now = {"turn": said["number"], "quote": said["model"][:16]} if said else ref
            return conclusion(finding("stall", basis=[ref], now=[now]),
                              goal={"text": "x", "state": "confirmed", "refs": [ref]}, anchors=[])
        report = replay.run(lambda: llm.ModelConfig(provider="mock"), complete=Scripted(default=eager))
        self.assertGreater(report["interruptions"]["early"], 0)
        self.assertGreater(sum(report["interruptions"]["in_quiet_cases"].values()), 0)
        self.assertLess(report["rates"]["stayed_quiet"]["rate"], 1.0)
        self.assertEqual(report["interruptions"]["repeated_after_answer"], 0)       # the session's own rule held
        self.assertIn("!! ", replay.as_text(report))

    def test_the_stand_in_plays_every_case_as_labelled(self):
        """Plumbing only: the stand-in's keyword rules were written alongside these
        cases, so this shows the pipeline runs end to end, not that a judgement is good."""
        report = replay.run(lambda: llm.ModelConfig(provider="mock"))
        failing = [row["id"] for row in report["rows"] if any(value is False for value in row["checks"].values())]
        self.assertEqual(failing, [])
        self.assertEqual((report["evidence"], report["timing"]["missed"], report["interruptions"]["early"],
                          report["interruptions"]["in_quiet_cases"]), ("plumbing-only", 0, 0, {}))
        self.assertGreater(report["agency"]["looks"], 0)
        self.assertGreater(report["agency"]["independent_readings"], 0)
        self.assertEqual(report["prompt_versions"], ["audit-1.0"])
        self.assertIn("agency:", replay.as_text(report))

    def test_a_real_provider_is_reported_as_structural_evidence_and_needs_consent(self):
        config = llm.ModelConfig(provider="openai", base_url="https://models.example.test/v1", api_key=KEY)
        with self.assertRaises(PermissionError):
            replay.run(lambda: config, permit=lambda config: False, complete=Scripted())
        report = replay.run(lambda: config, permit=lambda config: True,
                            complete=Scripted(default={"goal": {}, "anchors": [], "findings": []}), limit=2)
        self.assertEqual((report["evidence"], report["cases"]), ("E4-structural", 2))
        self.assertIn("SYNTHETIC", report["note"])


@unittest.skipIf(NO_DISPLAY, f"Tk cannot open a window here ({NO_DISPLAY})")
class ScreenTests(unittest.TestCase):
    """The real windows, on whatever platform runs the tests. This says nothing about
    focus behaviour next to the chat the user is typing in."""

    def setUp(self):
        from banxing.ui.app import PetApp
        strings.set_language("zh")
        self.folder = Path(tempfile.mkdtemp(prefix="banxing-screens-"))
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.settings = Settings(self.folder, environ={"BANXING_PROVIDER": "mock"})
        self.app = PetApp(self.settings, store.Store(self.folder), use_tray=False)
        self.addCleanup(self.app.destroy)
        self.model = Scripted(default=conclusion(finding()))
        self.watcher = cli.build(self.settings, store.Store(self.folder), self.app, InlineExecutor(),
                                 auditor=au.Auditor(self.settings.model_config, complete=self.model),
                                 use_appserver=False)
        self.app.attach(self.watcher, None)
        self.app.place()
        self.path = self.folder / "talk.json"
        self.path.write_text(json.dumps(as_file_data(research()), ensure_ascii=False), encoding="utf-8")

    def start(self):
        problems = []
        self.watcher.begin(sources.SourceRef(sources.FILE, str(self.path)), "", 1, False, problems.append)
        self.app.root.update()
        self.assertEqual(problems, [""])

    def texts(self, window):
        found = []

        def walk(widget):
            for child in widget.winfo_children():
                try:
                    found.append(str(child.cget("text")))
                except Exception:
                    pass
                walk(child)
        walk(window)
        return "\n".join(found)

    def click(self, window, label):
        def find(widget):
            for child in widget.winfo_children():
                try:
                    if child.cget("text") == label:
                        return child
                except Exception:
                    pass
                hit = find(child)
                if hit is not None:
                    return hit
            return None
        target = find(window)
        self.assertIsNotNone(target, label)
        target.event_generate("<Button-1>")
        self.app.root.update()

    def test_the_bubble_is_on_screen_with_the_question_and_its_four_answers(self):
        self.start()
        app = self.app
        self.assertTrue(app.bubble_window.winfo_viewable() and app.pet.winfo_viewable())
        x, y = app.bubble_window.winfo_rootx(), app.bubble_window.winfo_rooty()
        self.assertTrue(0 <= x and x + app.bubble_window.winfo_width() <= app.root.winfo_screenwidth())
        self.assertTrue(0 <= y and y + app.bubble_window.winfo_height() <= app.root.winfo_screenheight())
        text = self.texts(app.bubble_window).replace("\n", "")
        for label in ("前提没查过", "可以原样带回去问主模型：", "查看依据", "复制这句追问", "我知道，有意如此", "这条不成立"):
            self.assertIn(label, text)
        self.assertIn(ASK[:14], text)
        self.assertNotIn("第 3 轮 · 主模型说", text)                                 # the sources are folded at first
        before = app.bubble_window.winfo_height()
        self.click(app.bubble_window, "查看依据")
        self.assertIn("第 3 轮 · 主模型说", self.texts(app.bubble_window))
        self.assertGreater(app.bubble_window.winfo_height(), before)

    def test_copying_the_question_puts_it_on_the_clipboard_and_notes_it(self):
        self.start()
        self.click(self.app.bubble_window, "复制这句追问")
        self.assertEqual(self.app.root.clipboard_get(), ASK)
        self.assertFalse(self.app.bubble_window.winfo_viewable())
        self.assertEqual(self.watcher.session.ledger.marks[0].state, lg.ASKED)
        self.watcher.look_now()
        self.app.root.update()
        dialogs = self.app.dialogs
        self.click(self.app.bubble_window, "这条不成立")
        self.assertEqual(self.app.dialogs, dialogs)                                # noted at once, no dialog in the way
        self.assertEqual(self.watcher.session.ledger.marks[0].state, lg.REJECTED)
        self.assertTrue(self.app.toast.winfo_viewable())                           # a line may be added, optionally

    def test_the_ledger_window_shows_the_ledger_and_follows_changes(self):
        self.start()
        self.app.open_ledger()
        self.app.root.update()
        window = self.app._ledger["window"]
        output = next(child for child in window.winfo_children() if child.winfo_class() == "Text")
        text = output.get("1.0", "end")
        for needle in ("你想回答的问题（你说过）", "这段对话立在什么上面", "？ 没查过", "使用范围：已读到第 7 轮", "最近一次回查"):
            self.assertIn(needle, text)
        self.watcher.correct_goal("只比较长问题")
        self.app.root.update()
        self.assertIn("你想回答的问题（你亲手改写）", output.get("1.0", "end"))
        self.app.open_ledger()                                                     # asked twice: still one window
        self.assertEqual(len(self.app.dialog_windows), 1)
        self.click(window, "结束旁听")
        self.assertFalse(self.watcher.active)
        self.assertEqual(self.app._ledger, {})

    def test_the_watch_dialog_names_the_destination_and_starts_only_on_agreement(self):
        self.app.open_watch()
        self.app.root.update()
        window = self.app.dialog_windows[-1]
        self.assertIn("离线演示不会发送任何内容", self.texts(window).replace("\n", ""))
        self.click(window, "同意并开始旁听")
        self.assertIn("请先读取一个文件或剪贴板", self.texts(window).replace("\n", ""))
        self.assertFalse(self.watcher.active)
        entry = next(w for w in window.winfo_children()[2].winfo_children() if w.winfo_class() == "Entry")
        entry.insert(0, str(self.path))
        self.click(window, "读取")
        self.assertEqual(self.model.calls, [])                                     # listing a file sends nothing
        self.click(window, "同意并开始旁听")
        self.assertTrue(self.watcher.active)
        self.assertFalse(window.winfo_exists())
        self.assertEqual(len(self.model.calls), 1)
        self.assertTrue(self.app.bubble_window.winfo_viewable())

    def test_a_copied_chat_page_can_be_watched_from_the_clipboard(self):
        self.model.default = conclusion(goal={"text": "数据能不能用", "state": "confirmed", "refs": [
            {"turn": 1, "quote": "这批数据能不能直接用来训练"}]}, anchors=[])
        self.app.copy("You said:\n这批数据能不能直接用来训练？\nChatGPT said:\n可以，我已经检查过，没有重复。\n")
        self.app.open_watch()
        self.app.root.update()
        window = self.app.dialog_windows[-1]
        self.click(window, "从剪贴板读取（先在聊天页面全选、复制）")
        chooser = next(child for child in window.winfo_children() if child.winfo_class() == "Listbox")
        self.assertIn("剪贴板里的对话", "\n".join(chooser.get(0, "end")))
        self.click(window, "同意并开始旁听")
        self.assertEqual((self.watcher.active, self.watcher.state()["source"]), (True, sources.PASTED))

    def test_the_model_dialog_saves_without_sending_and_refuses_a_bad_address(self):
        fresh = Settings(self.folder / "other", environ={})
        self.app.settings = self.watcher.settings = fresh
        self.app.open_model()
        self.app.root.update()
        window = self.app.dialog_windows[-1]
        self.assertIn("它最好不是你正在对话的那一个".replace("不是", "**不是**"), self.texts(window).replace("\n", ""))
        self.click(window, "保存")
        self.assertIn("请填写 API Key", self.texts(window))
        self.assertEqual(fresh.get("provider"), "")
        self.assertTrue(window.winfo_exists())

    def test_closing_everything_leaves_no_tk_object_behind(self):
        import tkinter
        self.start()
        self.app.open_ledger()
        self.app.open_watch()
        self.app.open_model()
        self.app.root.update()
        with self.assertRaises(Exception):                                         # only the owning thread may end it
            worker = threading.Thread(target=lambda: errors.append(self._try(self.app.destroy)))
            errors = []
            worker.start()
            worker.join()
            raise errors[0]
        self.app.destroy()
        gc.collect()
        self.assertEqual([obj for obj in gc.get_objects() if isinstance(obj, (tkinter.Misc, tkinter.Variable))], [])

    @staticmethod
    def _try(call):
        try:
            call()
        except Exception as error:
            return error
        return AssertionError("destroy() ran on a foreign thread")


if __name__ == "__main__":
    unittest.main()
