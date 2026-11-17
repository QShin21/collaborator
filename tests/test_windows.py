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
from ctypes import wintypes
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
        with self.assertRaises(ValueError):
            win32.send_shortcuts((0x0D,))  # never permit Enter, even from a caller

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

    @staticmethod
    def native_edit_message(hwnd, message, start=0, end=0):
        """Bounded system edit-control query; only selection offsets leave here."""
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        send = user32.SendMessageTimeoutW
        send.argtypes = [wintypes.HWND, wintypes.UINT, ctypes.c_size_t,
                         ctypes.c_ssize_t, wintypes.UINT, wintypes.UINT,
                         ctypes.POINTER(ctypes.c_size_t)]
        send.restype = ctypes.c_ssize_t
        result = ctypes.c_size_t(0)
        if not send(hwnd, message, start, end, 0x0002, 500, ctypes.byref(result)):
            return None
        return result.value

    def native_selection(self, hwnd):
        packed = self.native_edit_message(hwnd, 0x00B0)  # EM_GETSEL
        if packed is None or packed == 0xFFFFFFFF:
            return None
        return (packed & 0xFFFF, (packed >> 16) & 0xFFFF)

    def keyboard_diagnostic(self, snap, actual):
        """Only focus/selection facts, never draft or clipboard text."""
        result = {"draft_unchanged": actual == snap.draft,
                  "pasted_appended_to_original": actual == snap.draft + "pasted 文本",
                  "draft_empty": actual == "", "draft_length": len(actual or ""),
                  "foreground_target": self.win32.foreground_window() == self.hwnd}
        try:
            focused = self.client._uia.GetFocusedElement()
            result["uia_box_focused"] = bool(self.client._uia.CompareElements(focused, self.client._box))
            native_box = int(self.client._box.CurrentNativeWindowHandle or 0)
            result["native_box_handle"] = bool(native_box)
            if native_box:
                selection = self.native_selection(native_box)
                if selection is not None:
                    result["native_selection_start"] = selection[0]
                    result["native_selection_length"] = selection[1] - selection[0]

            class GuiThreadInfo(ctypes.Structure):
                _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                            ("hwndActive", wintypes.HWND), ("hwndFocus", wintypes.HWND),
                            ("hwndCapture", wintypes.HWND), ("hwndMenuOwner", wintypes.HWND),
                            ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND),
                            ("rcCaret", wintypes.RECT)]

            user32 = self.win32.user32
            user32.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(GuiThreadInfo)]
            user32.GetGUIThreadInfo.restype = wintypes.BOOL
            info = GuiThreadInfo()
            info.cbSize = ctypes.sizeof(info)
            thread_id = user32.GetWindowThreadProcessId(self.hwnd, None)
            result["thread_lookup_ok"] = bool(thread_id)
            if thread_id:
                result["gui_query_ok"] = bool(user32.GetGUIThreadInfo(thread_id, ctypes.byref(info)))
                if result["gui_query_ok"]:
                    result["gui_focus_root_target"] = self.win32.top_level(info.hwndFocus) == self.hwnd
                    result["gui_focus_is_native_box"] = bool(native_box and info.hwndFocus == native_box)
                else:
                    result["gui_error"] = ctypes.get_last_error()
            pattern = self.client._text_pattern(self.client._box)
            if pattern is not None:
                selection = pattern.GetSelection()
                result["selection_count"] = int(selection.Length)
                if selection.Length:
                    result["selection_length"] = len(selection.GetElement(0).GetText(-1))
        except Exception as error:
            result["probe_error"] = type(error).__name__
        return result

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

    def test_2a_native_multiline_ctrl_a_selects_all_without_changing_text(self):
        """The stand-in must honor the shortcut the paste path actually uses."""
        snap = self.snapshot()
        edit = int(self.client._box.CurrentNativeWindowHandle or 0)
        self.assertTrue(edit, "stand-in TextBox has no native edit handle")
        self.assertTrue(self.client._focus(self.hwnd, self.client._box), "stand-in edit cannot get focus")
        end = len(snap.draft)
        try:
            self.native_edit_message(edit, 0x00B1, end, end)  # EM_SETSEL: start unselected
            self.assertEqual(self.native_selection(edit), (end, end),
                             "stand-in caret could not be collapsed before Ctrl+A")
            self.assertTrue(self.win32.send_shortcuts((self.win32.VK_CONTROL, self.win32.VK_A)),
                            "Ctrl+A was not injected")
            deadline = time.monotonic() + 0.8
            selection = None
            while time.monotonic() < deadline:
                selection = self.native_selection(edit)
                if selection == (0, len(snap.draft)):
                    break
                time.sleep(0.05)
            self.assertTrue(selection == (0, len(snap.draft)),
                            f"stand-in Ctrl+A contract failed; selection={selection}; "
                            f"draft_length={len(snap.draft)}")
            self.assertTrue(self.client._read(self.client._box) == snap.draft,
                            "Ctrl+A changed stand-in draft content")
        finally:
            # Leave the caret at the end. The next test must prove its *own*
            # Ctrl+A works, not inherit a preselected draft from this check.
            self.native_edit_message(edit, 0x00B1, end, end)  # EM_SETSEL
            self.assertEqual(self.native_selection(edit), (end, end),
                             "stand-in caret could not be collapsed after Ctrl+A")

    def test_3_paste_path_fills_and_puts_the_clipboard_back(self):
        snap = self.snapshot()
        kind, saved, sequence = self.win32.clipboard_capture()
        deadline = time.monotonic() + 2
        while kind == "busy" and time.monotonic() < deadline:
            time.sleep(0.1)
            kind, saved, sequence = self.win32.clipboard_capture()
        self.assertIn(kind, ("empty", "text"),
                      f"cannot replace a clipboard that cannot be restored: {self.win32.clipboard_diagnostic()}")
        swapped, prefill_write_sequence = self.win32.clipboard_replace_if_sequence(
            sequence, "clipboard before", rollback=(kind, saved))
        self.assertEqual(swapped, "written", self.win32.clipboard_diagnostic())
        try:
            prefill_diagnostic = self.win32.clipboard_diagnostic()
            prefill_kind, prefill_text, _ = self.win32.clipboard_capture()
            self.assertTrue(prefill_kind == "text" and prefill_text == "clipboard before",
                            f"test clipboard unreadable after setup: {prefill_diagnostic}")
            prefill_locale = getattr(prefill_text, "locale", None)
            ok, why = self.client._fill_paste(self.hwnd, self.client._box, "pasted 文本")
            # Read this owned stand-in box directly: snapshot() would bring its
            # window forward again and conceal a focus loss during SendInput.
            actual = self.client._read(self.client._box)
            keyboard = self.keyboard_diagnostic(snap, actual)
            current_kind, current_text, _ = self.win32.clipboard_capture()
            clipboard_restored = (current_kind == "text" and current_text == "clipboard before"
                                  and getattr(current_text, "locale", None) == prefill_locale)
            if not ok:
                self.fail(f"{why}; draft_match={actual == 'pasted 文本'}; "
                          f"clipboard_restored={clipboard_restored}; "
                          f"clipboard_kind={current_kind}; "
                          f"write_sequence={prefill_write_sequence}; "
                          f"keyboard={keyboard}; "
                          f"before={prefill_diagnostic}; after={self.win32.clipboard_diagnostic()}")
            self.assertTrue(actual == "pasted 文本", f"draft changed after paste; length={len(actual or '')}")
            self.assertTrue(clipboard_restored,
                            f"text or locale was not restored: {self.win32.clipboard_diagnostic()}")
            ok, why = self.client._fill_paste(self.hwnd, self.client._box, "")
            self.assertTrue(ok, why)
            self.assertEqual(self.snapshot().draft, "")
        finally:
            # Only touch this test's own stand-in and clipboard, not a newer copy
            # or a draft someone changed while the live test was in flight.
            try:
                current = self.client._snapshot(time.monotonic(), fresh=True)
            except Exception:
                current = None
            if (current is not None and current.foreground and self.key(current) == self.key(snap)
                    and current.draft in ("", "pasted 文本", snap.draft)):
                self.client._fill_value(self.client._box, snap.draft)
            current_kind, current_text, current_sequence = self.win32.clipboard_capture()
            if (current_kind, current_text) == ("text", "clipboard before"):
                self.win32.clipboard_replace_if_sequence(
                    current_sequence, saved if kind == "text" else None,
                    rollback=("text", "clipboard before"))

    def test_4_probe_reports_without_content(self):
        self.snapshot()
        report = self.client.probe(try_fill=True)
        self.assertTrue(report["window"] and report["box"] and report["readable"])
        self.assertTrue(report["fill"]["draft_intact"])
        self.assertTrue(report["fill"]["value"]["write"])
        self.assertNotIn("stand-in draft", repr(report))


if __name__ == "__main__":
    unittest.main()
