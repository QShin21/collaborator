"""Exercise the real Win32 wrapper's decisions with a tiny fake DLL.

No native Windows calls or user clipboard are touched. This tests format refusal,
clipboard sequence guards and batched key ordering, not a live desktop (E1).
"""
import ctypes
import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import safety  # noqa: F401

from banxing import hosts


SOURCE = Path(hosts.__file__).parent / "win32.py"


class Function:
    def __init__(self, callback=None):
        self.callback = callback or (lambda *args: 1)

    def __call__(self, *args):
        return self.callback(*args)


class DLL:
    def __init__(self):
        self.functions = {}

    def __getattr__(self, name):
        return self.functions.setdefault(name, Function())

    def set(self, name, callback):
        self.functions[name] = Function(callback)


def load_win32():
    """Load the production file under a private name, restoring global imports."""
    user32, kernel32 = DLL(), DLL()
    name = "banxing.hosts._win32_under_test"
    saved = sys.modules.get(name)

    def dll(module, **kwargs):
        return user32 if module == "user32" else kernel32

    try:
        with patch.object(sys, "platform", "win32"), patch.object(ctypes, "WinDLL", dll, create=True), \
                patch.object(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE, create=True):
            spec = importlib.util.spec_from_file_location(name, SOURCE)
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
        return module, user32, kernel32
    finally:
        if saved is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = saved


class Win32Flow(unittest.TestCase):
    def setUp(self):
        self.win32, self.user32, self.kernel32 = load_win32()

    def formats(self, formats, text="saved clipboard"):
        buffer = ctypes.create_unicode_buffer(text)
        self.addCleanup(lambda: buffer)  # retain the backing memory for this test
        self.user32.set("CountClipboardFormats", lambda: len(formats))
        self.user32.set("EnumClipboardFormats", lambda previous: formats[0] if not previous else formats[formats.index(previous) + 1])
        self.user32.set("GetClipboardData", lambda fmt: 33)
        self.kernel32.set("GlobalLock", lambda handle: ctypes.addressof(buffer))
        return buffer

    def test_a_plain_unicode_clipboard_is_readable(self):
        self.formats([13, 1, 7])  # Unicode followed by Windows' synthesized text formats
        self.assertEqual(self.win32._clipboard_snapshot_open(), ("text", "saved clipboard"))

    def test_rich_or_synthesized_text_is_not_destroyed(self):
        for formats in ([13, 1, 0xC000], [0xC000, 13, 1], [1, 13], [13, 1, 16]):
            with self.subTest(formats=formats):
                self.formats(formats)
                self.assertEqual(self.win32._clipboard_snapshot_open(), ("other", None))

    def test_clipboard_diagnostic_lists_formats_without_content(self):
        self.formats([13, 1, 7, 16], "secret clipboard content")
        report = self.win32.clipboard_diagnostic()
        self.assertEqual(report["format_ids"], [13, 1, 7, 16])
        self.assertEqual(report["format_count"], 4)
        self.assertNotIn("secret clipboard content", repr(report))

    def test_an_initial_empty_zero_change_counter_can_be_captured(self):
        self.user32.set("CountClipboardFormats", lambda: 0)
        self.user32.set("GetClipboardSequenceNumber", lambda: 0)
        self.assertEqual(self.win32.clipboard_capture(), ("empty", None, 0))

    def test_a_nonempty_zero_counter_refuses_to_borrow_the_clipboard(self):
        self.formats([13, 1, 7])
        self.user32.set("GetClipboardSequenceNumber", lambda: 0)
        self.assertEqual(self.win32.clipboard_capture(), ("busy", None, 0))

    def test_a_new_copy_after_empty_zero_capture_is_not_overwritten(self):
        # A permanently zero counter cannot identify a newer copy. Re-read the
        # empty state under the swap lock before touching anything.
        self.formats([13, 1, 7], "user's newer copy")
        calls = []
        self.win32._clipboard_handle = lambda text: 101
        self.user32.set("CreateWindowExW", lambda *args: 55)
        self.user32.set("GetClipboardSequenceNumber", lambda: 0)
        self.user32.set("EmptyClipboard", lambda: calls.append("empty") or 1)
        status, _ = self.win32.clipboard_replace_if_sequence(0, "temporary", rollback=("empty", None))
        self.assertEqual(status, "changed")
        self.assertNotIn("empty", calls)

    def test_a_zero_counter_that_does_not_advance_is_rolled_back_before_unlock(self):
        state = {"text": None, "empties": 0}
        self.win32._clipboard_handle = lambda text: 101 if text == "temporary" else None
        self.user32.set("CreateWindowExW", lambda *args: 55)
        self.user32.set("GetClipboardSequenceNumber", lambda: 0)
        self.user32.set("CountClipboardFormats", lambda: 0)

        def empty():
            state["text"] = None
            state["empties"] += 1
            return 1

        self.user32.set("EmptyClipboard", empty)
        self.user32.set("SetClipboardData", lambda fmt, handle: state.update(text="temporary") or handle)
        status, sequence = self.win32.clipboard_replace_if_sequence(0, "temporary", rollback=("empty", None))
        self.assertEqual((status, sequence, state["text"], state["empties"]), ("busy", 0, None, 2))

    def test_a_fresh_zero_counter_that_advances_allows_the_paste_transaction(self):
        state = {"sequence": 0}
        self.win32._clipboard_handle = lambda text: 101
        self.user32.set("CreateWindowExW", lambda *args: 55)
        self.user32.set("GetClipboardSequenceNumber", lambda: state["sequence"])
        self.user32.set("CountClipboardFormats", lambda: 0)
        self.user32.set("EmptyClipboard", lambda: state.update(sequence=state["sequence"] + 1) or 1)
        self.user32.set("SetClipboardData", lambda fmt, handle: handle)
        self.assertEqual(self.win32.clipboard_replace_if_sequence(0, "temporary", rollback=("empty", None)),
                         ("written", 1))

    def test_a_new_user_copy_is_not_overwritten(self):
        calls = []
        self.user32.set("CreateWindowExW", lambda *args: 55)
        self.user32.set("GetClipboardSequenceNumber", lambda: 12)
        self.user32.set("EmptyClipboard", lambda: calls.append("empty") or 1)
        self.assertEqual(self.win32.clipboard_replace_if_sequence(11, None), ("changed", 12))
        self.assertNotIn("empty", calls)

    def test_a_failed_allocation_leaves_the_old_clipboard_alone(self):
        calls = []
        self.user32.set("CreateWindowExW", lambda *args: 55)
        self.user32.set("EmptyClipboard", lambda: calls.append("empty") or 1)
        self.kernel32.set("GlobalAlloc", lambda *args: 0)
        self.assertEqual(self.win32.clipboard_replace_if_sequence(11, "new")[0], "busy")
        self.assertNotIn("empty", calls)

    def test_a_failed_set_rolls_back_before_unlocking_and_returns_its_own_sequence(self):
        state = {"text": "old", "sequence": 10, "sets": 0}
        allocated = iter((101, 102))
        self.win32._clipboard_handle = lambda text: next(allocated) if text is not None else None
        self.user32.set("CreateWindowExW", lambda *args: 55)
        self.user32.set("GetClipboardSequenceNumber", lambda: state["sequence"])

        def empty():
            state.update(text=None, sequence=state["sequence"] + 1)
            return 1

        def set_data(fmt, handle):
            state["sets"] += 1
            if state["sets"] == 1:
                return 0               # failure after the old value was emptied
            state.update(text="old", sequence=state["sequence"] + 1)
            return handle

        def close():
            # A different program copies immediately after we release the lock.
            state.update(text="newer user copy", sequence=state["sequence"] + 1)
            return 1

        self.user32.set("EmptyClipboard", empty)
        self.user32.set("SetClipboardData", set_data)
        self.user32.set("CloseClipboard", close)
        status, sequence = self.win32.clipboard_replace_if_sequence(10, "temporary", rollback=("text", "old"))
        self.assertEqual((status, sequence, state["sets"]), ("busy", 12, 2))
        self.assertEqual(state["text"], "newer user copy")

    def test_a_failed_set_and_failed_rollback_report_data_loss(self):
        state = {"text": "old", "sequence": 10}
        allocated = iter((101, 102))
        self.win32._clipboard_handle = lambda text: next(allocated) if text is not None else None
        self.user32.set("CreateWindowExW", lambda *args: 55)
        self.user32.set("GetClipboardSequenceNumber", lambda: state["sequence"])

        def empty():
            state.update(text=None, sequence=11)
            return 1

        self.user32.set("EmptyClipboard", empty)
        self.user32.set("SetClipboardData", lambda fmt, handle: 0)
        status, sequence = self.win32.clipboard_replace_if_sequence(10, "temporary", rollback=("text", "old"))
        self.assertEqual((status, sequence, state["text"]), ("lost", 11, None))

    def test_shortcuts_are_atomic_ordered_and_cannot_include_enter(self):
        captured = []

        def send(count, events, size):
            captured.extend((events[i].ki.wVk, bool(events[i].ki.dwFlags & self.win32.KEYEVENTF_KEYUP))
                            for i in range(count))
            self.assertEqual(size, ctypes.sizeof(self.win32.INPUT))
            return count

        self.user32.set("SendInput", send)
        self.assertTrue(self.win32.send_shortcuts((self.win32.VK_CONTROL, self.win32.VK_A),
                                                  (self.win32.VK_CONTROL, self.win32.VK_V)))
        self.assertEqual(captured, [(0x11, False), (0x41, False), (0x41, True), (0x11, True),
                                    (0x11, False), (0x56, False), (0x56, True), (0x11, True)])
        with self.assertRaises(ValueError):
            self.win32.send_shortcuts((0x0D,))
        self.assertEqual(len(captured), 8)

    def test_a_partial_injection_only_releases_held_keys(self):
        batches = []

        def partial(count, events, size):
            batches.append([(events[i].ki.wVk, bool(events[i].ki.dwFlags & self.win32.KEYEVENTF_KEYUP))
                            for i in range(count)])
            return 3 if len(batches) == 1 else count

        self.user32.set("SendInput", partial)
        self.assertFalse(self.win32.send_shortcuts((self.win32.VK_CONTROL, self.win32.VK_A),
                                                   (self.win32.VK_CONTROL, self.win32.VK_V)))
        self.assertEqual(batches[1], [(self.win32.VK_CONTROL, True)])


if __name__ == "__main__":
    unittest.main()
