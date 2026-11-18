"""Settings, the secret box and the local log. Temp folders and made-up keys only."""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import safety  # noqa: F401  (keeps tests away from real user data)

from banxing import config, secretbox
from banxing.config import Settings
from banxing.store import MAX_LOG_BYTES, Store, wipe


class TempCase(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="banxing-config-"))
        self.addCleanup(shutil.rmtree, self.folder, True)

    def settings(self, environ=None):
        return Settings(self.folder, environ=environ or {})


class SettingsTests(TempCase):
    def test_a_fresh_install_has_no_model_and_sends_nothing(self):
        settings = self.settings()
        self.assertEqual((settings.get("provider"), settings.model_config().ready, settings.destination()), ("", False, ""))
        self.assertFalse(settings.path.exists())                                   # nothing written by looking

    def test_unknown_keys_and_files_of_other_versions(self):
        settings = self.settings()
        settings.path.parent.mkdir(parents=True, exist_ok=True)
        # A file of the draft-routes versions: the model fields carry over, the rest is dropped.
        settings.path.write_text(json.dumps({"schema": 2, "provider": "mock", "follow": False, "pause_seconds": 9,
                                             "unknown_key": 1}), encoding="utf-8")
        old = self.settings()
        self.assertEqual((old.get("provider"), old.get("follow"), old.get("unknown_key")), ("mock", None, None))
        settings.path.write_text(json.dumps({"schema": 99, "provider": "mock"}), encoding="utf-8")
        self.assertEqual(self.settings().get("provider"), "")                      # a newer file is not guessed at
        settings.path.write_text("{broken", encoding="utf-8")
        self.assertEqual(self.settings().get("provider"), "")
        with self.assertRaises(KeyError):
            settings.set(no_such_setting=1)
        self.assertEqual(settings.number("watch_poll_seconds", 1.0, 120.0), 5.0)
        settings.data["watch_poll_seconds"] = "soon"
        self.assertEqual(settings.number("watch_poll_seconds", 1.0, 120.0), 5.0)

    def test_the_model_dialog_validates_before_it_saves(self):
        settings = self.settings()
        for fields, problem in ((dict(provider="anthropic"), "api_key"),
                                (dict(provider="openai", base_url="http://gateway.example.com/v1", api_key="k"), "base_url"),
                                (dict(provider="telepathy", api_key="k"), "provider")):
            with self.assertRaises(ValueError) as caught:
                settings.save_model(**fields)
            self.assertEqual(str(caught.exception), problem)
        self.assertFalse(settings.path.exists())
        model = settings.save_model("anthropic", api_key="  key-one  ", effort="high")
        self.assertEqual((model.ready, model.api_key, model.effort, model.host), (True, "key-one", "high", "api.anthropic.com"))
        self.assertEqual(settings.save_model("anthropic", effort="whatever").effort, "medium")

    def test_a_saved_key_never_follows_a_changed_endpoint(self):
        settings = self.settings()
        settings.save_model("openai", base_url="https://gateway.example.com/v1", api_key="key-one")
        self.assertEqual(settings.save_model("openai", base_url="https://gateway.example.com/v1/", model="m2").api_key,
                         "key-one")                                               # same endpoint, key left blank
        for provider, url in (("openai", "https://evil.example.net/v1"), ("openai", ""), ("anthropic", "")):
            with self.assertRaises(ValueError) as caught:
                settings.save_model(provider, base_url=url)
            self.assertEqual(str(caught.exception), "api_key_again")
        again = self.settings().model_config()
        self.assertEqual((again.host, again.api_key), ("gateway.example.com", "key-one"))     # nothing changed on failure
        moved = settings.save_model("openai", base_url="https://other.example.net/v1", api_key="key-two")
        self.assertEqual((moved.host, moved.api_key), ("other.example.net", "key-two"))

    def test_the_key_is_not_written_in_the_clear(self):
        settings = self.settings()
        settings.save_model("anthropic", api_key="sk-ant-very-secret-value")
        raw = settings.path.read_text(encoding="utf-8")
        self.assertNotIn("sk-ant-very-secret-value", raw)
        self.assertEqual(self.settings().model_config().api_key, "sk-ant-very-secret-value")
        if sys.platform == "win32":
            self.assertTrue(secretbox.is_encrypted(json.loads(raw)["api_key"]))
        else:
            self.assertEqual(settings.path.stat().st_mode & 0o077, 0)             # private to the user
        self.assertTrue(settings.save_model("mock").ready)                        # the offline demo forgets the key
        self.assertEqual((settings.get("api_key"), settings.destination()), ("", ""))

    def test_a_general_consent_names_a_destination_and_lapses_when_it_changes(self):
        settings = self.settings()
        self.assertTrue(settings.consented())                                     # nothing configured: nothing is sent
        settings.save_model("openai", base_url="https://gateway.example.com/v1", api_key="k")
        self.assertFalse(settings.consented())
        self.assertFalse(settings.give_consent("openai|https://somewhere.else.example"))   # not what was on screen
        self.assertTrue(settings.give_consent("openai|https://gateway.example.com"))
        settings.save_model("openai", base_url="https://gateway.example.com/v1", model="other-model")
        self.assertTrue(self.settings().consented())                              # same destination
        settings.save_model("openai", base_url="https://elsewhere.example.org/v1", api_key="k2")
        self.assertFalse(settings.consented())

    def test_the_environment_can_supply_the_model_but_never_borrows_the_saved_key(self):
        self.settings().save_model("anthropic", api_key="saved-key")
        env = {"BANXING_PROVIDER": "openai", "BANXING_BASE_URL": "https://ci.example.com/v1"}
        settings = self.settings(env)
        self.assertEqual((settings.model_config().api_key, settings.model_config().ready, settings.env_locked),
                         ("", False, True))
        env["BANXING_API_KEY"] = "env-key"
        self.assertEqual(self.settings(env).model_config().api_key, "env-key")
        self.assertTrue(self.settings({"BANXING_PROVIDER": "mock"}).model_config().ready)

    def test_home_can_be_redirected(self):
        old = os.environ.get("BANXING_HOME")
        os.environ["BANXING_HOME"] = str(self.folder / "elsewhere")
        try:
            self.assertEqual(config.home(), self.folder / "elsewhere")
        finally:
            os.environ["BANXING_HOME"] = old


class SecretTests(unittest.TestCase):
    def setUp(self):
        self.saved = (secretbox._on_windows, secretbox._dpapi)
        self.addCleanup(lambda: setattr(secretbox, "_on_windows", self.saved[0]) or setattr(secretbox, "_dpapi", self.saved[1]))

    def test_round_trip_and_what_is_not_a_stored_secret(self):
        stored = secretbox.protect("sk-secret-value")
        self.assertNotIn("sk-secret-value", stored)
        self.assertEqual(secretbox.reveal(stored), "sk-secret-value")
        self.assertEqual(secretbox.protect(""), "")
        for bad in ("", "nonsense", "plain:!!!", "dpapi:AAAA" if sys.platform != "win32" else "plain:QUJD"):
            self.assertEqual(secretbox.reveal(bad), "", bad)

    @unittest.skipUnless(sys.platform == "win32", "DPAPI exists on Windows only")
    def test_windows_uses_dpapi(self):
        self.assertTrue(secretbox.is_encrypted(secretbox.protect("value")))

    def test_on_windows_a_failed_encryption_writes_nothing_readable(self):
        """Windows is simulated by switching the module's platform check."""
        for failure in (secretbox.SecretError("x"), OSError("no crypt32"), AttributeError("WinDLL"), ValueError("?")):
            def dpapi(data, protect, failure=failure):
                raise failure
            secretbox._on_windows, secretbox._dpapi = (lambda: True), dpapi
            with self.assertRaises(secretbox.SecretError):
                secretbox.protect("sk-secret-value")
        folder = Path(tempfile.mkdtemp(prefix="banxing-secret-"))
        self.addCleanup(shutil.rmtree, folder, True)
        settings = Settings(folder, environ={})
        with self.assertRaises(ValueError) as caught:
            settings.save_model("anthropic", api_key="sk-secret-value")
        self.assertEqual(str(caught.exception), "encrypt")
        self.assertFalse(settings.path.exists())                                   # not the key, not the rest either
        secretbox._dpapi = lambda data, protect: data[::-1]                        # a stand-in "cipher" that works
        stored = secretbox.protect("sk-secret-value")
        self.assertTrue(stored.startswith("dpapi:"))
        self.assertEqual(secretbox.reveal(stored), "sk-secret-value")
        self.assertEqual(secretbox.reveal("plain:c2stc2VjcmV0"), "")               # a readable value is not accepted there


class StoreTests(TempCase):
    def test_the_log_takes_numbers_and_code_style_names_only(self):
        log = Store(self.folder, wall=lambda: 12.5)
        log.log("watch_audit", {"requests": 3, "ok": True, "seconds": 1.25, "kind": "premise", "empty": None,
                                "title": "「同一份索引」只是说过", "claim": "the main model said so", "short": "好",
                                "path": "C:\\Users\\me\\talk.json", "list": [1, 2]})
        event = log.events()[0]
        self.assertEqual({key for key in event if key not in ("t", "v", "event")},
                         {"requests", "ok", "seconds", "kind", "empty"})           # every bit of prose was refused
        self.assertEqual((event["event"], event["t"]), ("watch_audit", 12.5))
        log.log("x" * 80)
        self.assertEqual(len(log.events()[-1]["event"]), 40)

    def test_the_log_is_capped_and_survives_damage(self):
        log = Store(self.folder)
        log.log_path.parent.mkdir(parents=True, exist_ok=True)
        log.log_path.write_text("x" * (MAX_LOG_BYTES + 10) + "\n", encoding="utf-8")
        log.log("after")
        self.assertTrue(log.log_path.with_suffix(".1.jsonl").exists())
        self.assertEqual([event["event"] for event in log.events()], ["after"])
        with open(log.log_path, "a", encoding="utf-8") as stream:
            stream.write("not json\n")
        self.assertEqual(len(log.events()), 1)
        self.assertEqual(Store(self.folder / "nowhere").events(), [])

    def test_the_summary_counts_what_the_user_made_of_it(self):
        log = Store(self.folder)
        self.assertEqual((log.summary()["audits"], log.summary()["wrong_rate"]), (0, None))
        log.log("watch_audit", {"ok": True, "manual": False, "requests": 3, "chars": 900, "looks": 1, "searches": 2,
                                "independent": True, "seconds": 4.0})
        log.log("watch_audit", {"ok": False, "manual": True, "requests": 1, "chars": 100, "seconds": 2.0})
        for action, why in (("show", "premise"), ("show", "manual"), ("quiet", "muted"), ("quiet", "too_late")):
            log.log("watch_decided", {"action": action, "why": why})
        for event in ("watch_took_back", "watch_intended", "watch_wrong", "watch_wrong", "watch_resolved"):
            log.log(event, {"kind": "premise"})
        numbers = log.summary()
        self.assertEqual((numbers["audits"], numbers["asked_for"], numbers["failed"], numbers["requests"],
                          numbers["chars_sent"], numbers["looked_back"], numbers["searches"],
                          numbers["independent_readings"]), (2, 1, 1, 4, 1000, 1, 2, 1))
        self.assertEqual((numbers["bubbles"], numbers["taken_back"], numbers["intended"], numbers["called_wrong"],
                          numbers["resolved_later"], numbers["held_back"], numbers["wrong_rate"]), (1, 1, 1, 2, 1, 2, 0.5))

    def test_wipe_removes_what_the_app_keeps_and_nothing_else(self):
        for name in ("settings.json", "events.jsonl", "banxing.log", "state.json", "calibration.json", "mine.txt"):
            (self.folder / name).write_text("x", encoding="utf-8")
        (self.folder / "vault").mkdir()
        (self.folder / "vault" / "watch.bin").write_text("x", encoding="utf-8")
        self.assertEqual(wipe(self.folder), 6)
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), ["mine.txt"])


if __name__ == "__main__":
    unittest.main()
