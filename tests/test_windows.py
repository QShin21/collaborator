"""Checks that only make sense on Windows. Skipped everywhere else.

Two groups:

  WindowsBasics   pure Win32 / COM calls that need no window of ours in front.
                  They run in the normal test step of the Windows CI job.

  StandInWindow   drives UI Automation against a small WinForms text box started
                  from PowerShell — a stand-in with a native multi-line edit
                  control, NOT Codex. It needs an interactive desktop and the
                  foreground, so it runs only when BANXING_LIVE=1 (the CI job runs
                  it as a separate, non-blocking step and publishes the result).

Neither group says anything about the real Codex window; see docs/verification.md.
"""
import ctypes
import os
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

import safety  # noqa: F401  (keeps tests away from real user data)

WINDOWS = sys.platform == "win32"
LIVE = WINDOWS and os.environ.get("BANXING_LIVE") == "1"
STAND_IN = Path(__file__).resolve().parent.parent / "packaging" / "windows" / "uia-standin.ps1"
TITLE = "Banxing UIA Stand-in"


@unittest.skipUnless(WINDOWS, "Windows only")
class WindowsBasics(unittest.TestCase):
    def test_win32_wrappers_answer(self):
        from banxing.hosts import win32
        self.assertEqual(win32.process_name(os.getpid()), os.path.basename(sys.executable).casefold())
        self.assertEqual(ctypes.sizeof(win32.INPUT), 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)
        self.assertIsInstance(win32.modifiers_held(), bool)
        self.assertEqual(win32.visible_windows_of_class(["no-such-window-class-xyz"]), [])
        self.assertIsInstance(win32.ime_like_windows(), list)
        self.assertIn(win32.clipboard_snapshot()[0], ("empty", "text", "other", "busy"))
        area = win32.work_area(win32.foreground_window() or 0)
        self.assertGreater(area.width, 200)
        self.assertGreater(area.height, 200)

    def test_single_instance_mutex(self):
        from banxing.hosts import win32
        name = f"Local\\banxing-test-{os.getpid()}"
        self.assertTrue(win32.single_instance(name))
        self.assertFalse(win32.single_instance(name))

    def test_ui_automation_loads_on_a_worker_thread(self):
        """As in the real run: comtypes is first imported on the watcher thread."""
        result = {}

        def work():
            try:
                from banxing.hosts.profile import Profile
                from banxing.hosts.uia import WindowsCodexClient
                client = WindowsCodexClient(Profile.default())
                client.open()
                snap = client.snapshot(time.monotonic())
                result["snapshot"] = snap
                result["probe"] = client.probe()
                client.close()
            except Exception as error:      # reported below with its text
                result["error"] = f"{type(error).__name__}: {error}"
        thread = threading.Thread(target=work)
        thread.start()
        thread.join(60)
        self.assertNotIn("error", result)
        self.assertIn("snapshot", result)
        # Whatever is in front on a CI machine, it is not Codex.
        self.assertFalse(result["snapshot"].foreground)
        self.assertEqual(result["probe"]["client"], "uia")

    def test_create_client_gives_the_uia_client(self):
        from banxing.hosts import create_client
        self.assertEqual(create_client().name, "uia")


@unittest.skipUnless(LIVE, "set BANXING_LIVE=1 on an interactive Windows desktop")
class StandInWindow(unittest.TestCase):
    """UI Automation against a native edit control in a window we own."""

    @classmethod
    def setUpClass(cls):
        from banxing.hosts import win32
        from banxing.hosts.profile import Profile
        from banxing.hosts.uia import WindowsCodexClient
        cls.win32 = win32
        cls.process = subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(STAND_IN), "-Title", TITLE])
        find = ctypes.WinDLL("user32").FindWindowW
        find.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
        find.restype = ctypes.c_void_p
        cls.hwnd = 0
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline and not cls.hwnd:
            cls.hwnd = find(None, TITLE) or 0
            time.sleep(0.25)
        if not cls.hwnd:
            cls.process.kill()
            raise unittest.SkipTest("the stand-in window did not appear")
        exe = win32.process_name(win32.window_pid(cls.hwnd))
        cls.client = WindowsCodexClient(Profile(process_names=(exe,)))
        # A client reads nothing until reading is switched on (in the app: by a passed
        # calibration). This window is the test's own, holding the test's own text.
        cls.client.set_reading(True)
        cls.client.open()

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        cls.process.kill()

    def snapshot(self):
        """Bring the stand-in to the front and look at it."""
        deadline = time.monotonic() + 10
        snap = None
        while time.monotonic() < deadline:
            if self.win32.foreground_window() != self.hwnd:
                self.win32.set_foreground(self.hwnd)
            snap = self.client.snapshot(time.monotonic())
            if snap.foreground and snap.draft is not None:
                return snap
            time.sleep(0.3)
        self.skipTest(f"the stand-in could not be brought to the front ({snap.problem if snap else 'no snapshot'})")

    def key(self, snap):
        return (snap.window_id, snap.chat_key, snap.box_id)

    def test_1_draft_is_read_with_identity_and_geometry(self):
        snap = self.snapshot()
        self.assertEqual(snap.draft, "stand-in draft 12345")
        self.assertTrue(snap.box_id)
        self.assertTrue(snap.chat_key)
        self.assertFalse(snap.box_rect.empty)
        self.assertGreaterEqual(snap.box_rect.overlap(snap.window_rect), snap.box_rect.width)
        again = self.snapshot()
        self.assertEqual(self.key(snap), self.key(again))             # stable while nothing changes

    def test_2_fill_checks_the_draft_first_and_then_replaces_it(self):
        snap = self.snapshot()
        refused = self.client.fill(self.key(snap), "some other text", "must not be written")
        self.assertEqual((refused.ok, refused.reason), (False, "mismatch"))
        self.assertEqual(self.snapshot().draft, snap.draft)
        prompt = "完整提示词：请先做一个可以打开的预览。\nSecond line stays in the box."
        done = self.client.fill(self.key(snap), snap.draft, prompt)
        self.assertTrue(done.ok, done.reason)
        self.assertEqual(done.method, "value")                        # a native edit control takes a value
        after = self.snapshot()
        self.assertEqual(" ".join(after.draft.split()), " ".join(prompt.split()))
        back = self.client.fill(self.key(after), after.draft, snap.draft)
        self.assertTrue(back.ok, back.reason)

    def test_3_paste_path_fills_and_puts_the_clipboard_back(self):
        snap = self.snapshot()
        self.assertTrue(self.win32.clipboard_set("clipboard before"))
        ok, why = self.client._fill_paste(self.hwnd, self.client._box, "pasted 文本")
        self.assertTrue(ok, why)
        self.assertEqual(self.snapshot().draft, "pasted 文本")
        time.sleep(0.3)
        self.assertEqual(self.win32.clipboard_snapshot(), ("text", "clipboard before"))
        ok, why = self.client._fill_paste(self.hwnd, self.client._box, "")
        self.assertTrue(ok, why)
        self.assertEqual(self.snapshot().draft, "")
        self.client._fill_value(self.client._box, snap.draft)

    def test_4_probe_reports_without_content(self):
        self.snapshot()
        report = self.client.probe(try_fill=True)
        self.assertTrue(report["window"] and report["box"] and report["readable"])
        self.assertTrue(report["fill"]["draft_intact"])
        self.assertTrue(report["fill"]["value"]["write"])
        self.assertNotIn("stand-in draft", repr(report))


if __name__ == "__main__":
    unittest.main()
