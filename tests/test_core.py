"""Backend tests. Run with:  python3 -m unittest discover -s tests

Everything runs on the offline mock model and an in-memory database.
"""
import json
import os
import random
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

os.environ["COLLAB_PROVIDER"] = "mock"

from collab import adapt  # noqa: E402
from collab.db import Store  # noqa: E402
from collab.engine import Engine, EngineError  # noqa: E402
from collab.jsonutil import extract_json  # noqa: E402
from collab.llm import normalize_messages  # noqa: E402
from collab.server import make_server  # noqa: E402

ILL = "我想让我们社区的垃圾分类做得更好，但不知道从哪里入手"
WELL = "计算一个半径为 3 米的圆形花坛需要多少米围栏"


class JsonTests(unittest.TestCase):
    def test_plain_fenced_and_embedded(self):
        self.assertEqual(extract_json('{"a": 1}'), {"a": 1})
        self.assertEqual(extract_json('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(extract_json('Sure! {"a": {"b": 2}} hope it helps'), {"a": {"b": 2}})
        self.assertIsNone(extract_json("no json here"))


class EnvFileTests(unittest.TestCase):
    def test_write_env_sets_replaces_and_removes(self):
        import tempfile
        from pathlib import Path
        from collab.config import write_env
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "sub" / ".env"
            write_env(path, {"A": "1", "B": "2"})
            path.write_text(path.read_text(encoding="utf-8") + "# note\nC=3\n", encoding="utf-8")
            write_env(path, {"A": "9", "B": "", "D": "4"})
            self.assertEqual(path.read_text(encoding="utf-8").splitlines(),
                             ["A=9", "# note", "C=3", "D=4"])


class SettingsTests(unittest.TestCase):
    """The saved API key must never be sent to an endpoint it was not entered for."""

    def setUp(self):
        import tempfile
        from pathlib import Path
        from collab import config
        self.config = config
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.env_file = Path(self.folder.name) / ".env"
        clean = {k: v for k, v in os.environ.items()
                 if not k.startswith(("ANTHROPIC_", "OPENAI_", "COLLAB_"))}
        clean.update({"COLLAB_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": "sk-saved"})
        for patcher in (mock.patch.dict(os.environ, clean, clear=True),
                        mock.patch.object(config, "ENV_FILE", self.env_file)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def save(self, **data):
        return self.config.save_settings(dict({"provider": "anthropic"}, **data))

    def test_endpoint_change_needs_the_key_again(self):
        with self.assertRaises(ValueError):
            self.save(base_url="https://untrusted.example")
        self.assertIsNone(os.environ.get("ANTHROPIC_BASE_URL"))
        self.assertFalse(self.env_file.exists(), "a refused change must not be written")

        out = self.save(base_url="https://gateway.example/", api_key="sk-new")
        self.assertEqual(out["base_url"], "https://gateway.example")
        self.assertEqual(os.environ["ANTHROPIC_API_KEY"], "sk-new")
        self.assertNotIn("sk-new", str(out))

        self.save(base_url="https://gateway.example", model="some-model")  # unchanged URL: fine
        self.assertEqual(os.environ["COLLAB_MODEL"], "some-model")
        with self.assertRaises(ValueError):  # going back to the default endpoint is a change too
            self.save(base_url="")
        with self.assertRaises(ValueError):
            self.save(base_url="https://gateway.example.untrusted.example")

    def test_other_provider_cannot_borrow_a_key(self):
        with self.assertRaises(ValueError):
            self.save(provider="openai", base_url="https://untrusted.example")
        with self.assertRaises(ValueError):
            self.save(provider="openai")  # no OpenAI key was ever entered
        self.assertEqual(os.environ["COLLAB_PROVIDER"], "anthropic")

    def test_endpoint_must_be_https_unless_local(self):
        for bad in ("http://gateway.example", "ftp://x", "gateway.example", "https://",
                    "https://user:pw@gateway.example", "javascript:alert(1)"):
            with self.assertRaises(ValueError, msg=bad):
                self.save(base_url=bad, api_key="sk-new")
        for good in ("http://localhost:11434/v1", "http://127.0.0.1:8080", "https://gateway.example/v1"):
            self.assertEqual(self.save(base_url=good, api_key="sk-new")["base_url"], good)

    def test_newlines_cannot_smuggle_extra_keys_into_env(self):
        self.save(model="m\nANTHROPIC_BASE_URL=https://untrusted.example")
        self.assertNotIn("\nANTHROPIC_BASE_URL", self.env_file.read_text(encoding="utf-8"))
        self.assertIsNone(os.environ.get("ANTHROPIC_BASE_URL"))


class MessageTests(unittest.TestCase):
    def test_alternation(self):
        out = normalize_messages([{"role": "assistant", "content": "hi"},
                                  {"role": "user", "content": "a"},
                                  {"role": "user", "content": "b"}])
        self.assertEqual([m["role"] for m in out], ["user", "assistant", "user"])
        self.assertEqual(out[-1]["content"], "a\n\nb")


class AdaptTests(unittest.TestCase):
    def test_feedback_moves_agency_and_prefs(self):
        base = adapt.default_state()
        up = adapt.apply_feedback(base, "question", "expand")
        down = adapt.apply_feedback(base, "question", "ignore")
        self.assertGreater(up["agency"], base["agency"])
        self.assertGreater(up["prefs"]["question"], base["prefs"]["question"])
        self.assertLess(down["agency"], base["agency"])
        self.assertLess(down["prefs"]["question"], base["prefs"]["question"])
        self.assertEqual(base["agency"], 0.5, "input must not be mutated")

    def test_locked_agency_keeps_learning_prefs(self):
        state = adapt.default_state()
        state["locked"] = True
        out = adapt.apply_feedback(state, "risk", "ignore")
        self.assertEqual(out["agency"], 0.5)
        self.assertLess(out["prefs"]["risk"], 0.5)

    def test_bounds_and_floor(self):
        state = adapt.default_state()
        for _ in range(100):
            state = adapt.apply_feedback(state, "risk", "ignore")
        self.assertEqual(state["agency"], 0.0)
        self.assertGreaterEqual(state["prefs"]["risk"], adapt.PREF_FLOOR)
        self.assertFalse(adapt.bubble_params(state)["auto"])
        for _ in range(100):
            state = adapt.apply_feedback(state, "risk", "expand")
        self.assertEqual(state["agency"], 1.0)

    def test_params_scale_with_agency(self):
        low = adapt.bubble_params({"agency": 0.2})
        high = adapt.bubble_params({"agency": 0.9})
        self.assertGreater(low["interval"], high["interval"])
        self.assertLess(low["ttl"], high["ttl"])
        self.assertLessEqual(low["per_round"], high["per_round"])

    def test_pick_kinds_follows_preferences(self):
        state = adapt.default_state()
        state["prefs"] = {k: 0.05 for k in adapt.KINDS}
        state["prefs"]["knowledge"] = 1.0
        rng = random.Random(7)
        firsts = [adapt.pick_kinds(state, 1, rng)[0] for _ in range(300)]
        self.assertGreater(firsts.count("knowledge"), 200)
        self.assertLess(firsts.count("knowledge"), 300, "other kinds must still be explored")
        picked = adapt.pick_kinds(state, 3, rng)
        self.assertEqual(len(set(picked)), 3)

    def test_normalize_tolerates_garbage(self):
        self.assertEqual(adapt.normalize(None)["agency"], 0.5)
        self.assertEqual(adapt.normalize({"agency": "x", "prefs": {"risk": 9}})["prefs"]["risk"], 1.0)


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.engine = Engine(Store(":memory:"), rng=random.Random(1))

    def test_ill_structured_flow(self):
        session = self.engine.create_session(ILL)
        self.assertEqual(session["phase"], "frame")
        frame = session["state"]["frame"]
        self.assertEqual(frame["structuredness"], "ill")
        self.assertTrue(frame["landscape"])
        self.assertTrue(any(t["type"] == "main" for t in frame["threads"]))
        self.assertTrue(any(t["type"] == "side" for t in frame["threads"]))

        session = self.engine.plan(session["id"], ["t1", "t3"], "我自己的方向")
        self.assertEqual(session["phase"], "work")
        self.assertEqual(session["state"]["chosen"], ["t1", "t3"])
        self.assertTrue(session["state"]["card"]["goal"])
        self.assertTrue(session["state"]["tasks"])
        messages = self.engine.store.messages(session["id"])
        self.assertEqual(messages[0]["role"], "assistant")

        pair = self.engine.chat(session["id"], "我该怎么衡量效果？")
        self.assertEqual([m["role"] for m in pair], ["user", "assistant"])

    def test_well_structured_skips_the_map(self):
        session = self.engine.create_session(WELL)
        self.assertEqual(session["state"]["frame"]["structuredness"], "well")
        self.assertEqual(session["phase"], "work")
        self.assertTrue(session["state"]["tasks"])

    def test_reframe_with_clarifications(self):
        session = self.engine.create_session(ILL)
        session = self.engine.frame(session["id"], "主要面向老年住户")
        self.assertEqual(session["state"]["clarifications"], "主要面向老年住户")
        self.assertEqual(session["phase"], "frame")

    def test_baseline_has_no_scaffold_or_bubbles(self):
        session = self.engine.create_session(ILL, agent="baseline")
        self.assertEqual(session["phase"], "work")
        self.assertIsNone(session["state"]["frame"])
        self.assertEqual(len(self.engine.store.messages(session["id"])), 2)
        self.assertEqual(self.engine.bubbles(session["id"], trigger="manual"), [])
        with self.assertRaises(EngineError):
            self.engine.frame(session["id"])

    def test_bubbles_and_feedback_loop(self):
        session = self.engine.create_session(ILL)
        sid = self.engine.plan(session["id"], ["t1"])["id"]
        first = self.engine.bubbles(sid, notes="我先观察了三个垃圾点", trigger="auto")
        self.assertTrue(first)
        self.assertEqual(self.engine.store.get_session(sid)["state"]["notes"], "我先观察了三个垃圾点")
        for bubble in first:
            self.assertIn(bubble["kind"], adapt.KINDS)
            self.assertGreater(bubble["ttl"], 0)

        before = self.engine.store.get_session(sid)["state"]["adapt"]["agency"]
        result = self.engine.feedback(first[0]["id"], "accept")
        self.assertGreater(result["adapt"]["agency"], before)
        self.assertEqual(result["shelf"][0]["text"], first[0]["text"])
        again = self.engine.feedback(first[0]["id"], "ignore")  # second reaction is a no-op
        self.assertEqual(again["adapt"]["agency"], result["adapt"]["agency"])

        more = self.engine.bubbles(sid, trigger="manual")
        shown = {b["text"] for b in first}
        self.assertTrue(all(b["text"] not in shown for b in more), "bubbles must not repeat")
        if more:
            expanded = self.engine.feedback(more[0]["id"], "expand")
            self.assertEqual([m["role"] for m in expanded["messages"]], ["user", "assistant"])
            self.assertEqual(expanded["messages"][0]["meta"]["type"], "expand")

    def test_auto_bubbles_respect_capacity_and_pause(self):
        session = self.engine.create_session(ILL)
        sid = self.engine.plan(session["id"], ["t1"])["id"]
        self.assertEqual(self.engine.bubbles(sid, trigger="auto", active=99), [])
        self.engine.update(sid, {"adapt": {"paused": True}})
        self.assertEqual(self.engine.bubbles(sid, trigger="auto"), [])
        self.assertTrue(self.engine.bubbles(sid, trigger="manual"))

    def test_profile_carries_to_next_session(self):
        session = self.engine.create_session(ILL)
        sid = self.engine.plan(session["id"], ["t1"])["id"]
        for bubble in self.engine.bubbles(sid, trigger="manual"):
            self.engine.feedback(bubble["id"], "ignore")
        fresh = self.engine.create_session(ILL)
        self.assertLess(fresh["state"]["adapt"]["agency"], 0.5)

    def test_update_edits(self):
        session = self.engine.create_session(ILL)
        sid = self.engine.plan(session["id"], ["t1"])["id"]
        tasks = self.engine.store.get_session(sid)["state"]["tasks"]
        tasks[0]["done"] = True
        tasks.append({"title": "我加的任务"})
        out = self.engine.update(sid, {"tasks": tasks, "notes": "n",
                                       "adapt": {"agency": 0.9, "locked": True}})
        self.assertTrue(out["state"]["tasks"][0]["done"])
        self.assertEqual(out["state"]["tasks"][-1]["title"], "我加的任务")
        self.assertEqual(out["state"]["adapt"]["agency"], 0.9)
        self.assertTrue(out["state"]["adapt"]["locked"])

    def test_pre_post_and_stats(self):
        session = self.engine.create_session(ILL)
        sid = session["id"]
        self.engine.submit_test(sid, "pre", {"clarity": 2, "next_step": 1, "confidence": 2}, "不太清楚")
        post = ("我的目标是提升分类准确率，成功标准是两周内错投率下降。计划：首先观察现状，"
                "然后做一次小实验，下一步对照标准复盘。")
        out = self.engine.submit_test(sid, "post", {"clarity": 4, "next_step": 5, "confidence": 4,
                                                    "ownership": 5, "helpful": 4}, post)
        self.assertEqual([t["phase"] for t in out["tests"]], ["pre", "post"])
        self.assertGreater(out["tests"][1]["scores"]["total"], out["tests"][0]["scores"]["total"])
        row = next(r for r in self.engine.stats()["agents"] if r["agent"] == "collaborator")
        self.assertEqual(row["paired"], 1)
        self.assertGreater(row["likert_gain"], 0)
        self.assertGreater(row["rubric_gain"], 0)
        self.assertEqual(row["ownership"], 5)

    def test_arena_is_blind_until_rated(self):
        arena = self.engine.arena_create(ILL)
        self.assertEqual(sorted(e["label"] for e in arena["entries"]), ["A", "B", "C"])
        self.assertTrue(all("agent" not in e for e in arena["entries"]))
        out = self.engine.arena_rate(arena["id"], {"A": 5, "B": 2, "C": 3}, "A")
        self.assertEqual(sorted(out["reveal"].values()), ["baseline", "collaborator", "socratic"])
        self.assertEqual(out["result"]["best"], out["reveal"]["A"])
        self.assertEqual(self.engine.stats()["arena_rounds"], 1)

    def test_export_and_delete(self):
        session = self.engine.create_session(ILL)
        sid = self.engine.plan(session["id"], ["t1"])["id"]
        self.assertIn("## Problem card", self.engine.export_markdown(sid))
        self.engine.store.delete_session(sid)
        with self.assertRaises(EngineError):
            self.engine.view(sid)

    def test_empty_problem_rejected(self):
        with self.assertRaises(EngineError):
            self.engine.create_session("   ")


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = make_server("127.0.0.1", 0, Store(":memory:"), quiet=True)
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method,
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read().decode())

    def test_full_http_round_trip(self):
        status, cfg = self.call("GET", "/api/config")
        self.assertEqual((status, cfg["provider"]), (200, "mock"))

        status, view = self.call("POST", "/api/sessions", {
            "problem": ILL, "pretest": {"likert": {"clarity": 2}, "answer": "还不清楚"}})
        self.assertEqual(status, 200)
        sid = view["session"]["id"]
        self.assertEqual(view["session"]["phase"], "frame")
        self.assertEqual(view["tests"][0]["phase"], "pre")

        status, view = self.call("POST", f"/api/sessions/{sid}/plan", {"thread_ids": ["t2"]})
        self.assertEqual(view["session"]["phase"], "work")
        status, out = self.call("POST", f"/api/sessions/{sid}/chat", {"content": "从哪开始？"})
        self.assertEqual(len(out["messages"]), 2)
        status, out = self.call("POST", f"/api/sessions/{sid}/bubbles", {"trigger": "manual"})
        self.assertTrue(out["bubbles"])
        status, out = self.call("POST", f"/api/bubbles/{out['bubbles'][0]['id']}/feedback",
                                {"action": "fade"})
        self.assertEqual(status, 200)
        status, view = self.call("PATCH", f"/api/sessions/{sid}", {"notes": "hello"})
        self.assertEqual(view["session"]["state"]["notes"], "hello")
        status, out = self.call("GET", f"/api/sessions/{sid}/export")
        self.assertIn("hello", out["markdown"])
        status, out = self.call("GET", "/api/stats")
        self.assertEqual(status, 200)
        status, out = self.call("GET", "/api/export")
        self.assertTrue(out["events"])
        status, out = self.call("DELETE", f"/api/sessions/{sid}")
        self.assertEqual(status, 200)

    def test_other_websites_cannot_call_the_api(self):
        def post(headers):
            request = urllib.request.Request(self.base + "/api/profile/reset", data=b"{}",
                                             method="POST", headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=10) as response:
                    return response.status
            except urllib.error.HTTPError as error:
                return error.code
        self.assertEqual(post({"Content-Type": "application/json",
                               "Origin": "https://evil.example"}), 403)
        self.assertEqual(post({"Content-Type": "text/plain"}), 415)
        self.assertEqual(post({"Content-Type": "application/json", "Origin": self.base}), 200)

    def raw(self, method, path, headers, body=None):
        request = urllib.request.Request(self.base + path, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status
        except urllib.error.HTTPError as error:
            return error.code

    def test_dns_rebinding_is_refused(self):
        """A rebinding page sends its own domain as Host AND as Origin — they agree with
        each other, so only an allow-list of Host names stops it."""
        port = self.server.server_address[1]
        evil = {"Host": f"untrusted.example:{port}", "Origin": f"http://untrusted.example:{port}",
                "Content-Type": "application/json"}
        self.assertEqual(self.raw("POST", "/api/profile/reset", evil, b"{}"), 403)
        self.assertEqual(self.raw("POST", "/api/settings", evil, b'{"provider": "mock"}'), 403)
        # reads matter as much as writes: sessions, the research export, the page itself
        only_host = {"Host": f"untrusted.example:{port}"}
        for path in ("/api/sessions", "/api/export", "/api/settings", "/", "/app.js"):
            self.assertEqual(self.raw("GET", path, only_host), 403, path)
        self.assertEqual(self.raw("GET", "/api/config", {"Host": "untrusted.example"}), 403)
        self.assertEqual(self.raw("GET", "/api/config", {"Host": f"127.0.0.1:{port + 1}"}), 403)

    def test_loopback_names_are_served(self):
        port = self.server.server_address[1]
        for host in (f"127.0.0.1:{port}", f"localhost:{port}", f"LOCALHOST:{port}"):
            self.assertEqual(self.raw("GET", "/api/config", {"Host": host}), 200, host)
        ok = {"Host": f"localhost:{port}", "Origin": f"http://localhost:{port}",
              "Sec-Fetch-Site": "same-origin", "Content-Type": "application/json"}
        self.assertEqual(self.raw("POST", "/api/profile/reset", ok, b"{}"), 200)

    def test_foreign_origin_and_cross_site_are_refused(self):
        json_type = {"Content-Type": "application/json"}
        for origin in ("https://untrusted.example", "null", self.base.replace("http://", "https://"),
                       "http://127.0.0.1:1"):
            self.assertEqual(self.raw("POST", "/api/profile/reset", dict(json_type, Origin=origin), b"{}"),
                             403, origin)
            self.assertEqual(self.raw("GET", "/api/export", {"Origin": origin}), 403, origin)
        for site in ("cross-site", "same-site"):
            self.assertEqual(self.raw("GET", "/api/export", {"Sec-Fetch-Site": site}), 403, site)
        # following a link to the app from another site is harmless and still works
        self.assertEqual(self.raw("GET", "/", {"Sec-Fetch-Site": "cross-site"}), 200)

    def test_allowed_hosts(self):
        from collab.server import allowed_hosts
        self.assertEqual(allowed_hosts("0.0.0.0", 8765),
                         {"127.0.0.1:8765", "localhost:8765", "[::1]:8765"})
        self.assertIn("192.168.1.5:8765", allowed_hosts("192.168.1.5", 8765))
        self.assertIn("localhost", allowed_hosts("127.0.0.1", 80))
        with mock.patch.dict(os.environ, {"COLLAB_ALLOWED_HOSTS": "Study-PC, 10.0.0.2:9000"}):
            hosts = allowed_hosts("0.0.0.0", 8765)
        self.assertTrue({"study-pc:8765", "10.0.0.2:9000"} <= hosts)
        self.assertNotIn("0.0.0.0:8765", hosts)

    def test_settings_report_without_leaking_the_key(self):
        status, out = self.call("GET", "/api/settings")
        self.assertEqual((status, out["provider"]), (200, "mock"))
        self.assertNotIn("api_key", out)
        self.assertEqual(self.call("POST", "/api/settings", {"provider": "nope"})[0], 400)

    def test_errors(self):
        self.assertEqual(self.call("POST", "/api/sessions", {"problem": ""})[0], 400)
        self.assertEqual(self.call("GET", "/api/sessions/deadbeef")[0], 404)
        self.assertEqual(self.call("GET", "/api/nope")[0], 404)

    def test_static_and_traversal(self):
        with urllib.request.urlopen(self.base + "/", timeout=10) as response:
            self.assertIn(b"<html", response.read().lower())
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(self.base + "/../run.py", timeout=10)


if __name__ == "__main__":
    unittest.main()
