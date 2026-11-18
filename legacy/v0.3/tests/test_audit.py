"""Regressions for the findings of the independent audit before the 0.3.0 release.

  1. The calibration's write test may only ever remove its own marker, from the box
     it wrote it into.
  2. An address is "already known" only if it leads to exactly the same place.
  3. On Windows a secret is encrypted or not stored at all.
  4. Before a calibration the input box is not read — by the host client itself, on
     the watcher's path, not merely ignored afterwards.

Synthetic input and the in-memory host only (E1). The Windows client that these
rules also bind (hosts/uia.py) has not been run; see docs/verification.md.
"""
import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path

import safety  # noqa: F401  (keeps tests away from real user data)

from banxing import aboutme, calibration, lint, secretbox
from banxing.cli import calibrate_simulated
from banxing.config import Settings
from banxing.controller import ThreadedExecutor
from banxing.hosts.base import FILL, READ, HostClient, NullClient
from banxing.hosts.simulated import SimulatedCodex
from banxing.ui import strings, viewmodel

from rig import RigCase

MARKER = calibration.MARKER
USERS = "IMPORTANT USER DRAFT"


# ================================================================ 1. calibration clean-up
class Meddling(SimulatedCodex):
    """A host in which the user does something right after the marker was written."""

    def __init__(self, then):
        super().__init__()
        self.then = then
        self.done = False

    def fill(self, key, expect, text):
        result = super().fill(key, expect, text)
        if text == MARKER and not self.done:
            self.done = True
            self.then(self)
        return result


class CalibrationCleanup(unittest.TestCase):
    def run_fill(self, then):
        codex = Meddling(then)
        return codex, calibration.check(codex, "fill", {})

    def test_switching_chat_after_the_write_never_costs_the_other_chats_draft(self):
        """The audit's reproduction."""
        def switch(codex):
            codex.switch_chat("chat-b")
            codex.type(USERS)
        codex, result = self.run_fill(switch)
        self.assertEqual(codex.draft, USERS)                                    # chat-b: untouched
        self.assertEqual(codex.drafts[("w1", "chat-a")], MARKER)                # not reachable safely: left alone
        self.assertEqual((result["ok"], result["restored"], result["marker_left"], result["interrupted"]),
                         (False, False, True, True))
        self.assertFalse(result["fatal"])                                       # the user's doing, not a broken host
        self.assertEqual(codex.fills, [MARKER])                                 # nothing else was ever written
        self.assertNotIn(FILL, calibration.capabilities_from(
            {"window": {"ok": True}, "read": {"ok": True}, "empty": {"ok": True}, "fill": result},
            codex.capabilities()))

    def test_switching_to_an_empty_chat_is_not_mistaken_for_a_clean_box(self):
        codex, result = self.run_fill(lambda c: c.switch_chat("chat-b"))
        self.assertEqual((result["ok"], result["restored"], result["marker_left"]), (False, False, True))
        self.assertEqual((codex.draft, codex.fills), ("", [MARKER]))

    def test_text_typed_next_to_the_marker_is_not_erased(self):
        codex, result = self.run_fill(lambda c: c.type(MARKER + " " + USERS))
        self.assertEqual(codex.draft, MARKER + " " + USERS)
        self.assertEqual((result["ok"], result["restored"], result["marker_left"], result["interrupted"],
                          result["fatal"]), (False, False, True, True, False))
        self.assertEqual(codex.fills, [MARKER])

    def test_a_draft_that_replaced_the_marker_is_not_erased(self):
        codex, result = self.run_fill(lambda c: c.type(USERS))
        self.assertEqual(codex.draft, USERS)
        self.assertEqual((result["ok"], result["restored"], result["marker_left"]), (False, False, False))
        self.assertEqual(codex.fills, [MARKER])

    def test_switching_window_is_handled_like_switching_chat(self):
        def other_window(codex):
            codex.switch_window("w2", "chat-z")
            codex.type(USERS)
        codex, result = self.run_fill(other_window)
        self.assertEqual((codex.draft, result["ok"], result["marker_left"]), (USERS, False, True))

    def test_a_host_that_goes_blind_after_the_write_is_left_alone(self):
        def blind(codex):
            codex.readable = False
        codex, result = self.run_fill(blind)
        self.assertEqual((result["ok"], result["restored"], result["marker_left"]), (False, False, True))
        self.assertEqual(codex.fills, [MARKER])

    def test_a_write_reported_as_failed_that_did_land_is_still_cleaned_up(self):
        class Lying(SimulatedCodex):
            def fill(self, key, expect, text):
                result = super().fill(key, expect, text)
                return type(result)(False, "failed") if text == MARKER else result
        codex = Lying()
        result = calibration.check(codex, "fill", {})
        self.assertEqual((codex.draft, result["ok"], result["restored"], result["marker_left"]),
                         ("", False, True, False))

    def test_the_clean_up_names_the_original_box_and_exactly_the_marker(self):
        calls = []

        class Recording(SimulatedCodex):
            def fill(self, key, expect, text):
                calls.append((tuple(key), expect, text))
                return super().fill(key, expect, text)
        codex = Recording()
        result = calibration.check(codex, "fill", {})
        key = ("w1", "chat-a", "box-w1")
        self.assertEqual(calls, [(key, "", MARKER), (key, MARKER, "")])
        self.assertEqual((result["ok"], result["restored"], result["marker_left"], result["interrupted"],
                          result["fatal"]), (True, True, False, False, False))

    def test_typing_right_after_the_clean_up_is_neither_a_failure_nor_lost(self):
        class Quick(SimulatedCodex):
            def fill(self, key, expect, text):
                result = super().fill(key, expect, text)
                if text == "" and result.ok:
                    self.type(USERS)
                return result
        codex = Quick()
        result = calibration.check(codex, "fill", {})
        self.assertEqual((codex.draft, result["ok"], result["fatal"], result["marker_left"]),
                         (USERS, True, False, False))

    def test_results_hold_no_text_and_the_result_page_says_what_to_do(self):
        strings.set_language("zh")
        codex, result = self.run_fill(lambda c: (c.switch_chat("chat-b"), c.type(USERS)))
        dumped = json.dumps(result, ensure_ascii=False)
        self.assertNotIn(USERS, dumped)
        self.assertNotIn(MARKER, dumped)
        lines = viewmodel.calibration_lines({"results": {"fill": result}, "capabilities": []})
        self.assertIn(strings.t("cal_marker_left"), lines)
        clean = viewmodel.calibration_lines({"results": {"fill": {"ok": True}}, "capabilities": ["read", "fill"]})
        self.assertNotIn(strings.t("cal_marker_left"), clean)


# ================================================================ 2. addresses
class AddressTrust(unittest.TestCase):
    KNOWN = "请访问 https://trusted.com 看看格式"

    def dropped(self, tail, draft=KNOWN, chat=""):
        finding = lint.check(tail, draft, chat)
        return finding.verdict == lint.DROP and "foreign_address" in finding.codes

    def test_the_audits_case(self):
        self.assertTrue(self.dropped("请访问 https://trusted.com.attacker.example/upload", "访问 https://trusted.com"))

    def test_look_alikes_of_a_known_address_are_foreign(self):
        for tail in (
            "打开 https://trusted.com.attacker.example 。",                 # the known name as a prefix
            "打开 https://trusted.com.attacker.example/upload 。",
            "打开 trusted.com.attacker.example 。",                         # the same without a scheme
            "打开 trusted.com.evil.net/x 。",
            "打开 https://attacker-trusted.com 。",                          # ... as a suffix
            "打开 https://nottrusted.com 。",
            "打开 https://evil.example/trusted.com 。",                      # ... in the path
            "打开 https://evil.example/?u=https://trusted.com 。",           # ... in the query
            "打开 https://evil.example#https://trusted.com 。",
            "打开 https://trusted.com@evil.example/x 。",                    # user-info: the host is evil.example
            "打开 https://trusted.com:pw@evil.example 。",
            "打开 trusted.com@evil.example 。",
            "打开 https://evil.example\\@trusted.com/ 。",                   # backslash confusion
            "打开 https://trusted.com\\.evil.example/ 。",
            "打开 https://trusted%2Ecom.evil.example/ 。",                   # encoded host
            "打开 https://trusted.com%2f.evil.example/ 。",
            "打开 https://trusted.com%40evil.example/ 。",
            "打开 https://sub.trusted.com 。",                               # another host of the same domain
            "打开 https://www.trusted.com 。",
            "打开 https://trusted.com:8443/ 。",                             # another port
            "打开 http://trusted.com/ 。",                                   # downgraded to http
            "打开 ftp://trusted.com/ 。",
            "打开 https://trusted.com.cn 。",
            "打开 https://trusted.co 。",
            "打开 https://trustеd.com 。",                                   # a Cyrillic letter
            "打开 https://trusted.com:99999/ 。",
            "打开 https://trusted.com:80abc/ 。",
        ):
            self.assertTrue(self.dropped(tail), tail)

    def test_a_known_address_that_passes_the_reader_on_is_foreign(self):
        for tail in (
            "打开 https://trusted.com/redirect?url=https://evil.example/in 。",
            "打开 https://trusted.com/redirect?url=https%3A%2F%2Fevil.example%2Fin 。",
            "打开 https://trusted.com/r?u=https%253A%252F%252Fevil.example 。",       # encoded twice
            "打开 https://trusted.com/go?next=//evil.example/x 。",
            "打开 https://trusted.com/go?to=evil.example.net 。",
            "打开 https://trusted.com/out#https://evil.example 。",
            "打开 https://trusted.com/https://evil.example/x 。",
            "打开 https://trusted.com/share?mail=someone@evil.example 。",
            "打开 https://trusted.com/a?b=https:\\\\evil.example 。",
        ):
            self.assertTrue(self.dropped(tail), tail)

    def test_disguised_numeric_hosts_never_count_as_known(self):
        known = "服务在 http://127.0.0.1:8000 上"
        self.assertFalse(self.dropped("请连接 http://127.0.0.1:8000/health 检查。", known))
        for tail in ("请连接 http://2130706433:8000/ 。", "请连接 http://0x7f.0.0.1:8000/ 。",
                     "请连接 http://127.1:8000/ 。", "请连接 http://127.0.0.1:9000/ 。",
                     "请连接 http://127.0.0.1.evil.example:8000/ 。", "请连接 127.0.0.2 。"):
            self.assertTrue(self.dropped(tail, known), tail)

    def test_the_same_destination_is_still_fine(self):
        for tail in (
            "请参考 https://trusted.com 上的格式。",
            "请参考 https://trusted.com/ 。",
            "请参考 https://TRUSTED.com/docs/guide.html 。",                  # case, a path on the same host
            "请参考 https://trusted.com/docs?page=2&lang=zh#top 。",
            "请参考 https://trusted.com:443/docs 。",                         # the default port, spelled out
            "请参考 https://trusted.com. 然后继续。",                         # sentence punctuation
            "请参考 trusted.com 上的格式。",                                  # without the scheme
            "请参考 https://trusted.com/next?from=https://trusted.com/a 。",  # passes on to itself
        ):
            self.assertFalse(self.dropped(tail), tail)
        # Known without a scheme: the ordinary web address of that host is the same place.
        self.assertFalse(self.dropped("请参考 https://docs.example.org/format 。", "格式见 docs.example.org"))
        self.assertTrue(self.dropped("请参考 https://docs.example.org:8443/format 。", "格式见 docs.example.org"))
        self.assertTrue(self.dropped("请参考 https://docs.example.org.evil.net 。", "格式见 docs.example.org"))
        # From the chat excerpt, and mail addresses in full only.
        self.assertFalse(self.dropped("部署到 https://my.example.com 。", "做个页面", "我的网站是 https://my.example.com"))
        mail = "结果发给 me@example.org"
        self.assertFalse(self.dropped("把结果整理好，收件人写 me@example.org 。", mail))
        for tail in ("收件人写 me@example.org.evil.net 。", "收件人写 notme@example.org 。",
                     "收件人写 me@example.org@evil.net 。", "收件人写 me%40example.org@evil.net 。"):
            self.assertTrue(self.dropped(tail, mail), tail)

    def test_an_unreadable_address_in_the_draft_vouches_for_nothing(self):
        odd = "见 https://trusted.com@evil.example/x"
        self.assertTrue(self.dropped("打开 https://evil.example/x 。", odd))
        self.assertTrue(self.dropped("打开 https://trusted.com/ 。", odd))
        self.assertTrue(self.dropped("打开 https://trusted.com@evil.example/x 。", odd))

    def test_destination_parsing(self):
        self.assertEqual(lint.destination("https://Trusted.com./a?b")[:3], ("net", "trusted.com", 443))
        self.assertEqual(lint.destination("trusted.com")[:3], ("net", "trusted.com", None))
        self.assertEqual(lint.destination("http://[::1]:8080/x")[:3], ("net", "[::1]", 8080))
        self.assertIsNone(lint.destination("https://a@b.example")[0])
        self.assertEqual(lint.destination("https://t.example/r?u=https%3A%2F%2Fe.example")[3], ("https://e.example",))


# ================================================================ 3. secrets on Windows
class FailClosedSecrets(unittest.TestCase):
    """Windows is simulated here by switching the module's platform check; the real
    DPAPI call is exercised only on a Windows machine (test_config, E0 until CI runs)."""

    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="banxing-audit-"))
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.saved = (secretbox._on_windows, secretbox._dpapi)
        self.addCleanup(self.restore)

    def restore(self):
        secretbox._on_windows, secretbox._dpapi = self.saved

    def windows(self, dpapi):
        secretbox._on_windows = lambda: True
        secretbox._dpapi = dpapi

    def broken(self, data, protect):
        raise secretbox.SecretError("DPAPI failed (error 5)")

    def everything_on_disk(self):
        return "\n".join(path.read_text(encoding="utf-8", errors="replace")
                         for path in self.folder.rglob("*") if path.is_file())

    def test_protect_raises_instead_of_falling_back(self):
        for failure in (secretbox.SecretError("x"), OSError("no crypt32"), AttributeError("WinDLL"), ValueError("?")):
            def dpapi(data, protect, failure=failure):
                raise failure
            self.windows(dpapi)
            with self.assertRaises(secretbox.SecretError):
                secretbox.protect("sk-secret-value")
        self.assertEqual(secretbox.protect(""), "")                 # nothing to protect, nothing to fail

    def test_a_working_dpapi_is_used_and_plain_values_are_not_accepted_on_windows(self):
        self.windows(lambda data, protect: data[::-1])              # a stand-in "cipher"
        stored = secretbox.protect("sk-secret-value")
        self.assertTrue(stored.startswith("dpapi:"))
        self.assertEqual(secretbox.reveal(stored), "sk-secret-value")
        secretbox._on_windows = lambda: False                       # not restore(): on a Windows machine that
        plain = secretbox.protect("sk-secret-value")                # is the real DPAPI. As written off Windows,
        #                                                             or by the old fallback:
        self.assertTrue(plain.startswith("plain:"))
        self.windows(lambda data, protect: data[::-1])
        self.assertEqual(secretbox.reveal(plain), "")               # asked for again, then stored encrypted

    def test_the_api_key_is_not_saved_when_it_cannot_be_encrypted(self):
        settings = Settings(self.folder, environ={})
        settings.save_model("mock")
        before = settings.path.read_text(encoding="utf-8")
        self.windows(self.broken)
        with self.assertRaises(ValueError) as caught:
            settings.save_model("openai", base_url="https://gateway.example.com/v1", api_key="sk-secret-value")
        self.assertEqual(str(caught.exception), "encrypt")
        self.assertEqual(settings.path.read_text(encoding="utf-8"), before)     # nothing changed at all
        self.assertEqual((settings.get("provider"), settings.get("api_key")), ("mock", ""))
        self.assertNotIn("c2stc2VjcmV0", self.everything_on_disk())             # not even encoded
        self.assertNotIn("sk-secret-value", self.everything_on_disk())
        again = Settings(self.folder, environ={})
        self.assertEqual(again.model_config().provider, "mock")

    def test_the_model_dialog_gets_a_reason_it_can_show(self):
        class Case(RigCase):
            def runTest(self):
                pass
        case = Case()
        try:
            rig = case.rig(environ={})
            self.windows(self.broken)
            problem = rig.controller.confirm_model(
                "", True, dict(provider="anthropic", model="", base_url="", api_key="sk-secret-value", effort="low"))
            self.assertEqual(problem, "encrypt")
            self.assertEqual(rig.controller.setup_reason(), "model")
            for language in ("zh", "en"):
                strings.set_language(language)
                text = strings.t("model_error_encrypt")
                self.assertNotEqual(text, "model_error_encrypt")
                self.assertIn("BANXING_API_KEY", text)                          # something the user can do
            raw = "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in rig.folder.rglob("*") if p.is_file())
            self.assertNotIn("sk-secret-value", raw)
        finally:
            strings.set_language("zh")
            case.doCleanups()

    def test_about_me_is_not_written_and_the_old_text_survives(self):
        about = aboutme.AboutMe(self.folder)
        about.save_facts(["家里没有电钻"])
        kept = about.facts_path.read_text(encoding="utf-8")
        self.windows(self.broken)
        with self.assertRaises(secretbox.SecretError):
            about.save_facts(["我住在一楼", "预算三百块"])
        self.assertEqual(about.facts_path.read_text(encoding="utf-8"), kept)
        self.assertEqual(about.facts(), ["家里没有电钻"])               # memory agrees with the disk
        self.restore()
        fresh = aboutme.AboutMe(self.folder / "new")
        self.windows(self.broken)
        with self.assertRaises(secretbox.SecretError):
            fresh.save_facts(["我住在一楼"])
        self.assertFalse(fresh.facts_path.exists())
        self.assertEqual(fresh.save_facts([]), [])                      # clearing needs no encryption

    def test_the_about_me_window_is_told(self):
        class Case(RigCase):
            def runTest(self):
                pass
        case = Case()
        try:
            rig = case.rig()
            self.windows(self.broken)
            self.assertIsNone(rig.controller.save_facts(["我住在一楼"]))
            self.assertEqual(rig.controller.about.facts(), [])
            self.assertEqual(len(rig.events("encrypt_failed")), 1)
            self.assertNotIn("一楼", rig.store.log_path.read_text(encoding="utf-8"))
            self.assertNotEqual(strings.t("aboutme_error_encrypt"), "aboutme_error_encrypt")
        finally:
            case.doCleanups()

    def test_the_self_test_reports_a_dead_dpapi(self):
        from banxing import cli
        self.windows(self.broken)
        with self.assertRaises(secretbox.SecretError):
            cli._check_secretbox(self.folder)


# ================================================================ 4. no reading before calibration
class Counting(SimulatedCodex):
    """Counts the looks, and what each of them was allowed to fetch."""

    def __init__(self):
        super().__init__()
        self.looks = 0

    def snapshot(self, now):
        self.looks += 1
        return super().snapshot(now)


class NoReadingBeforeCalibration(RigCase):
    SECRET = "这是校准之前写在输入框里的一段话"

    def pump_until(self, executor, condition, seconds=5.0):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and not condition():
            executor.pump()
            time.sleep(0.01)
        self.assertTrue(condition())

    def test_a_host_client_does_not_read_until_told_to(self):
        codex = SimulatedCodex()                                    # nobody switched reading on
        codex.type(self.SECRET)
        snap = codex.snapshot(1.0)
        self.assertEqual((snap.foreground, snap.draft, snap.box_id, snap.chat_key, snap.problem),
                         (True, None, "", "", "uncalibrated"))
        self.assertIsNotNone(snap.window_rect)                      # the figure can still sit by the window
        self.assertEqual((codex.signals(), codex.raw_box_text(), codex.reads), (None, None, 0))
        self.assertFalse(HostClient().may_read())
        self.assertFalse(NullClient().may_read())

    def test_the_watcher_thread_fetches_no_draft_before_calibration(self):
        """The real control path: ThreadedExecutor's watcher → client.snapshot."""
        codex = Counting()
        codex.type(self.SECRET)
        rig = self.rig(calibrated=False, client=codex)
        seen = []
        executor = ThreadedExecutor(codex, lambda snap: (seen.append(snap), rig.controller.on_snapshot(snap)),
                                    poll=0.01)
        executor.start()
        try:
            self.pump_until(executor, lambda: len(seen) >= 8)
        finally:
            executor.stop()
        self.assertGreaterEqual(codex.looks, 8)
        self.assertEqual(codex.reads, 0)                            # the text was never fetched
        self.assertTrue(all(snap.draft is None and not snap.box_id for snap in seen))
        self.assertEqual({snap.problem for snap in seen}, {"uncalibrated"})
        self.assertEqual((rig.calls, rig.view.state), ([], ("blind", "uncalibrated")))

    def test_calibration_reads_only_inside_its_own_steps_and_then_switches_reading_on(self):
        codex = Counting()
        rig = self.rig(calibrated=False, client=codex)
        seen = []
        executor = ThreadedExecutor(codex, lambda snap: (seen.append(snap), rig.controller.on_snapshot(snap)),
                                    poll=0.01)
        rig.controller.executor = executor
        executor.start()
        try:
            self.pump_until(executor, lambda: len(seen) >= 3)
            self.assertEqual(codex.reads, 0)
            # One step of the wizard, run on the watcher thread like the real one.
            codex.type(calibration.PHRASE)
            done = []
            rig.controller.calibration_step("read", {}, done.append)
            self.pump_until(executor, lambda: bool(done))
            self.assertTrue(done[0]["ok"])
            self.assertGreater(codex.reads, 0)                      # the explicit, user-started read
            after_step, mark = codex.reads, len(seen)
            self.pump_until(executor, lambda: len(seen) >= mark + 5)
            self.assertEqual(codex.reads, after_step)               # ... and the routine looks are blind again
            self.assertTrue(all(snap.draft is None for snap in seen))
            self.assertFalse(codex.may_read())
        finally:
            executor.stop()
        rig.controller.executor = rig.executor
        entry = calibrate_simulated(codex, rig.controller)
        self.assertEqual(entry["verdict"], "passed")
        self.assertTrue(codex.may_read())
        codex.type("想做一个放在玄关的换鞋凳")
        self.assertEqual(codex.snapshot(1.0).draft, "想做一个放在玄关的换鞋凳")

    def test_an_exception_inside_a_step_does_not_leave_reading_on(self):
        codex = SimulatedCodex()
        with self.assertRaises(ValueError):
            calibration.check(codex, "no-such-step", {})
        self.assertFalse(codex.may_read())
        with self.assertRaises(RuntimeError):
            with codex.authorized():
                self.assertTrue(codex.may_read())
                raise RuntimeError("boom")
        self.assertFalse(codex.may_read())

    def test_reading_follows_the_calibration_and_stops_for_another_host_version(self):
        rig = self.rig(calibrated=False)
        codex = rig.codex
        self.assertFalse(codex.may_read())
        calibrate_simulated(codex, rig.controller)
        self.assertTrue(codex.may_read())
        codex.version = "sim-2"                                     # Codex updated itself
        codex.type(self.SECRET)
        before = codex.reads
        self.assertFalse(codex.may_read())                          # at once: before the controller hears of it
        self.assertIsNone(codex.snapshot(2.0).draft)
        self.assertEqual(codex.reads, before)
        rig.tick()
        self.assertEqual((rig.controller.capabilities, codex.may_read()), (frozenset(), False))
        codex.version = "sim-1"
        rig.tick()
        self.assertTrue(codex.may_read())

    def test_reading_without_the_ability_and_clearing_data_switch_it_off(self):
        rig = self.rig()                                            # tests' default: calibrated
        self.assertTrue(rig.codex.may_read())
        blind = self.rig(capabilities=frozenset({FILL}))
        self.assertFalse(blind.codex.may_read())
        fresh = self.rig(calibrated=False, environ={})
        calibrate_simulated(fresh.codex, fresh.controller)
        self.assertTrue(fresh.codex.may_read())
        fresh.controller.clear_data()
        self.assertFalse(fresh.codex.may_read())
        fresh.codex.type(self.SECRET)
        fresh.tick()
        self.assertIsNone(fresh.view.last_snap.draft)

    def test_user_started_checks_still_work_before_calibration(self):
        rig = self.rig(calibrated=False)
        rig.codex.type("")
        rig.codex.placeholder = "Ask anything"
        reports, learned = [], []
        rig.controller.probe(False, lambda report, context: reports.append(report))
        rig.controller.learn_placeholder(learned.append)
        self.assertEqual((reports[0]["client"], learned), ("simulated", [True]))
        self.assertFalse(rig.codex.may_read())

    # The same gate in the Windows client is exercised — its real control flow, against
    # stand-ins for Win32 and UI Automation — in tests/test_uia_flow.py.


if __name__ == "__main__":
    unittest.main()
