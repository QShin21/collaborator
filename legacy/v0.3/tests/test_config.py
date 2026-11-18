"""Settings, key storage and local records."""
import json
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import safety  # noqa: F401  (keeps tests away from real user data)

from banxing import config, levels as lv, secretbox
from banxing.config import Settings
from banxing.store import MAX_LOG_BYTES, Store


class TempCase(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="banxing-config-"))
        self.addCleanup(shutil.rmtree, self.folder, True)

    def settings(self, environ=None):
        return Settings(self.folder, environ=environ or {})


class SettingsTests(TempCase):
    def test_fresh_install_has_no_model_and_follows(self):
        settings = self.settings()
        self.assertFalse(settings.model_config().ready)
        self.assertTrue(settings.get("follow"))
        self.assertTrue(settings.get("use_context"))
        self.assertEqual(settings.tuning().pause, 1.5)
        self.assertEqual(settings.tuning().ignore_after, 2.0)
        self.assertEqual((lv.WINDOW, lv.THRESHOLD, lv.READY_NEEDED), (6, 4, 5))     # rules, not settings
        self.assertEqual(settings.get("schema"), config.SETTINGS_SCHEMA)

    def test_values_survive_a_restart_and_junk_is_ignored(self):
        settings = self.settings()
        settings.set(pet_offset=[-80, -200], use_context=False)
        self.assertEqual(self.settings().get("pet_offset"), [-80, -200])
        self.assertFalse(self.settings().get("use_context"))
        with self.assertRaises(KeyError):
            settings.set(surprise=1)
        settings.path.write_text('{"follow": false, "unknown_key": 1}', encoding="utf-8")
        again = self.settings()
        self.assertFalse(again.get("follow"))
        self.assertNotIn("unknown_key", again.data)
        settings.path.write_text("{ this is not json", encoding="utf-8")
        self.assertTrue(self.settings().get("follow"))

    def test_tuning_is_clamped_to_sane_values(self):
        settings = self.settings()
        settings.set(pause_seconds=0, ignore_after_seconds="oops")
        self.assertEqual(settings.tuning().pause, 0.4)
        self.assertEqual(settings.tuning().ignore_after, 2.0)

    def test_saving_a_model_needs_a_key_and_a_safe_endpoint(self):
        settings = self.settings()
        with self.assertRaises(ValueError) as caught:
            settings.save_model("anthropic")
        self.assertEqual(str(caught.exception), "api_key")
        with self.assertRaises(ValueError) as caught:
            settings.save_model("openai", base_url="http://gateway.example.com/v1", api_key="k")
        self.assertEqual(str(caught.exception), "base_url")
        with self.assertRaises(ValueError) as caught:
            settings.save_model("telepathy", api_key="k")
        self.assertEqual(str(caught.exception), "provider")
        model = settings.save_model("anthropic", api_key="  key-one  ", effort="medium")
        self.assertTrue(model.ready)
        self.assertEqual((model.api_key, model.effort, model.host), ("key-one", "medium", "api.anthropic.com"))

    def test_saved_key_never_follows_a_changed_endpoint(self):
        settings = self.settings()
        settings.save_model("openai", base_url="https://gateway.example.com/v1", api_key="key-one")
        # Same endpoint, key field left blank: the key is kept.
        self.assertEqual(settings.save_model("openai", base_url="https://gateway.example.com/v1/",
                                             model="m2").api_key, "key-one")
        for provider, url in (("openai", "https://evil.example.net/v1"), ("openai", ""), ("anthropic", "")):
            with self.assertRaises(ValueError) as caught:
                settings.save_model(provider, base_url=url)
            self.assertEqual(str(caught.exception), "api_key_again")
        # Nothing changed on failure.
        again = self.settings().model_config()
        self.assertEqual((again.host, again.api_key), ("gateway.example.com", "key-one"))
        moved = settings.save_model("openai", base_url="https://other.example.net/v1", api_key="key-two")
        self.assertEqual((moved.host, moved.api_key), ("other.example.net", "key-two"))

    def test_key_is_not_written_in_the_clear(self):
        settings = self.settings()
        settings.save_model("anthropic", api_key="sk-ant-very-secret-value")
        raw = settings.path.read_text(encoding="utf-8")
        self.assertNotIn("sk-ant-very-secret-value", raw)
        self.assertEqual(self.settings().model_config().api_key, "sk-ant-very-secret-value")
        if sys.platform == "win32":
            self.assertTrue(secretbox.is_encrypted(json.loads(raw)["api_key"]))
        else:
            self.assertEqual(settings.path.stat().st_mode & 0o077, 0)     # private to the user

    def test_offline_demo_needs_no_key_and_forgets_the_old_one(self):
        settings = self.settings()
        settings.save_model("anthropic", api_key="key-one")
        self.assertTrue(settings.save_model("mock").ready)
        self.assertEqual(settings.get("api_key"), "")
        self.assertEqual(settings.destination(), "")

    def test_consent_names_a_destination_and_lapses_when_it_changes(self):
        settings = self.settings()
        self.assertTrue(settings.consented())                 # nothing configured: nothing is sent
        settings.save_model("openai", base_url="https://gateway.example.com/v1", api_key="k")
        self.assertFalse(settings.consented())
        settings.give_consent()
        self.assertTrue(self.settings().consented())
        settings.save_model("openai", base_url="https://gateway.example.com/v1", model="other-model")
        self.assertTrue(settings.consented())                 # same destination
        settings.save_model("openai", base_url="https://elsewhere.example.org/v1", api_key="k2")
        self.assertFalse(settings.consented())
        settings.give_consent()
        settings.save_model("mock")
        self.assertTrue(settings.consented())                 # nothing leaves the machine

    def test_environment_can_supply_the_model_but_never_borrows_the_saved_key(self):
        self.settings().save_model("anthropic", api_key="saved-key")
        env = {"BANXING_PROVIDER": "openai", "BANXING_BASE_URL": "https://ci.example.com/v1"}
        settings = self.settings(env)
        self.assertEqual(settings.model_config().api_key, "")
        self.assertFalse(settings.model_config().ready)
        self.assertTrue(settings.env_locked)
        env["BANXING_API_KEY"] = "env-key"
        self.assertEqual(self.settings(env).model_config().api_key, "env-key")
        self.assertTrue(self.settings({"BANXING_PROVIDER": "mock"}).model_config().ready)

    def test_home_can_be_redirected(self):
        import os
        old = os.environ.get("BANXING_HOME")
        os.environ["BANXING_HOME"] = str(self.folder / "elsewhere")
        try:
            self.assertEqual(config.home(), self.folder / "elsewhere")
        finally:
            if old is None:
                del os.environ["BANXING_HOME"]
            else:
                os.environ["BANXING_HOME"] = old


class SecretTests(unittest.TestCase):
    def test_round_trip_and_unreadable_values(self):
        stored = secretbox.protect("值 with ünïcode")
        self.assertEqual(secretbox.reveal(stored), "值 with ünïcode")
        self.assertEqual(secretbox.protect(""), "")
        for junk in ("", "nonsense", "plain:!!!", "dpapi:AAAA", "rot13:abc"):
            self.assertEqual(secretbox.reveal(junk), "")

    @unittest.skipUnless(sys.platform == "win32", "DPAPI exists on Windows only")
    def test_windows_uses_dpapi(self):
        stored = secretbox.protect("windows-secret")
        self.assertTrue(stored.startswith("dpapi:"))
        self.assertEqual(secretbox.reveal(stored), "windows-secret")


class StoreTests(TempCase):
    def test_level_state_survives_a_restart(self):
        store = Store(self.folder)
        self.assertEqual(store.load_levels(), lv.Levels())
        state = lv.Levels()
        for index in range(4):
            state, _ = lv.record(state, f"m{index}", lv.IGNORE, now=1.0)
        self.assertEqual(state.level, lv.HINT)
        store.save_levels(state)
        self.assertEqual(Store(self.folder).load_levels(), state)
        store.state_path.write_text("[]", encoding="utf-8")
        self.assertEqual(store.load_levels(), lv.Levels())

    def test_a_generation_one_state_file_is_carried_over(self):
        store = Store(self.folder)
        store.state_path.write_text(json.dumps({"frequency": {"mode": "critical", "by": "user"}}), encoding="utf-8")
        self.assertEqual(store.load_levels().level, lv.WALK)
        store.state_path.write_text(json.dumps({"schema": 99, "levels": {"level": "walk"}}), encoding="utf-8")
        self.assertEqual(store.load_levels(), lv.Levels())          # written by a newer version: not guessed at

    def test_log_keeps_numbers_and_names_and_drops_anything_text_like(self):
        store = Store(self.folder, wall=lambda: 12.5)
        store.log("shown", {"wait": 1.25, "context": True, "mode": "every", "reason": "app_switch",
                            "draft": "想在阳台做一个三层的花架，我没做过木工。",
                            "short": "做个花架", "english": "fix the login page", "path": "C:\\Users\\me\\a.txt",
                            "note": "two\nlines", "nested": {"a": 1}, "long": "x" * 41})
        (event,) = store.events()
        self.assertEqual(event["wait"], 1.25)
        self.assertEqual(event["mode"], "every")
        self.assertEqual(event["reason"], "app_switch")
        self.assertEqual(event["t"], 12.5)
        # Not by length: a draft of four characters is still a draft.
        for dropped in ("draft", "short", "english", "path", "note", "nested", "long"):
            self.assertNotIn(dropped, event)

    def test_every_string_the_app_logs_is_a_plain_name(self):
        """What follow.py and the controller hand to the log, found in the source."""
        from banxing import store as store_module
        root = Path(store_module.__file__).resolve().parent
        logged = set()
        for name in ("follow.py", "controller.py", "cli.py"):
            text = (root / name).read_text(encoding="utf-8")
            for call in re.findall(r"""(?:Log|store\.log)\(\s*["']([^"']+)["']""", text):
                logged.add(call)
        self.assertGreater(len(logged), 12)
        for name in logged:
            self.assertTrue(store_module._NAME.fullmatch(name), name)

    def test_log_rotates_instead_of_growing_forever(self):
        store = Store(self.folder)
        store.log_path.parent.mkdir(parents=True, exist_ok=True)
        store.log_path.write_text("x" * (MAX_LOG_BYTES + 10), encoding="utf-8")
        store.log("start")
        self.assertLess(store.log_path.stat().st_size, 1000)
        self.assertTrue(store.log_path.with_suffix(".1.jsonl").exists())

    def test_summary_counts_a_trial(self):
        store = Store(self.folder)
        for event, data in (("analyze", {}), ("shown", {"wait": 2.0}), ("adopted", {}), ("analyze", {}),
                            ("shown", {"wait": 4.0}), ("dismissed", {}), ("analyze", {}), ("late", {}),
                            ("analyze", {}), ("standby", {}), ("analyze", {}), ("shown", {"wait": 3.0}),
                            ("sent_original", {})):
            store.log(event, data)
        summary = store.summary()
        self.assertEqual((summary["analysed"], summary["shown"], summary["adopted"], summary["dismissed"],
                          summary["late"], summary["standby"], summary["sent_original"]),
                         (5, 3, 1, 1, 1, 1, 1))
        self.assertEqual((summary["incomplete"], summary["in_time_rate"]), (0, 0.75))
        self.assertEqual(summary["median_wait_seconds"], 3.0)
        self.assertIsNone(Store(self.folder / "empty").summary()["median_wait_seconds"])


if __name__ == "__main__":
    unittest.main()
