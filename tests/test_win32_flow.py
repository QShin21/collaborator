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

    def test_unicode_with_locale_captures_the_locale_bytes(self):
        text_buffer = self.formats([13, 16, 1, 7])
        locale_buffer = ctypes.create_string_buffer(b"\x09\x04\x00\x00")
        self.user32.set("GetClipboardData", lambda fmt: 44 if fmt == 16 else 33)
        self.kernel32.set("GlobalSize", lambda handle: 4 if handle == 44 else 0)
        self.kernel32.set("GlobalLock", lambda handle: ctypes.addressof(locale_buffer if handle == 44 else text_buffer))
        kind, value, sequence = self.win32.clipboard_capture()
        self.assertEqual((kind, value, sequence), ("text", "saved clipboard", 1))
        self.assertEqual(value.locale, b"\x09\x04\x00\x00")

    def test_malformed_locale_is_not_borrowed(self):
        buffer = self.formats([13, 16, 1, 7])
        self.user32.set("GetClipboardData", lambda fmt: 44 if fmt == 16 else 33)
        self.kernel32.set("GlobalLock", lambda handle: ctypes.addressof(buffer))
        for size in (0, 3, 65):
            with self.subTest(size=size):
                self.kernel32.set("GlobalSize", lambda handle: size)
                self.assertEqual(self.win32.clipboard_capture()[:2], ("other", None))
        self.kernel32.set("GlobalSize", lambda handle: 4)
        self.kernel32.set("GlobalLock", lambda handle: 0 if handle == 44 else ctypes.addressof(buffer))
        self.assertEqual(self.win32.clipboard_capture()[:2], ("other", None))

    def test_rich_or_synthesized_text_is_not_destroyed(self):
        for formats in ([13, 1, 0xC000], [0xC000, 13, 1], [1, 13],
                        [13, 1, 16], [13, 1, 7, 16], [13, 16, 7, 1]):
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

    def test_restoring_text_places_its_original_locale_after_unicode(self):
        original = self.win32._RestorableText("original", b"\x09\x04\x00\x00")
        state = {"sequence": 10, "placed": []}
        self.win32._clipboard_handle = lambda text: 101 if text == "original" else 102
        self.win32._clipboard_locale_handle = lambda text: 201 if getattr(text, "locale", None) else None
        self.user32.set("CreateWindowExW", lambda *args: 55)
        self.user32.set("GetClipboardSequenceNumber", lambda: state["sequence"])
        self.user32.set("EmptyClipboard", lambda: state.update(sequence=state["sequence"] + 1) or 1)
        self.user32.set("SetClipboardData", lambda fmt, handle: state["placed"].append((fmt, handle)) or handle)
        status, _ = self.win32.clipboard_replace_if_sequence(10, original, rollback=("text", "temporary"))
        self.assertEqual(status, "written")
        self.assertEqual(state["placed"], [(13, 101), (16, 201)])

    def test_failed_locale_write_rolls_back_temporary_text_under_the_lock(self):
        original = self.win32._RestorableText("original", b"\x09\x04\x00\x00")
        state = {"sequence": 10, "text": "temporary", "locale": None, "sets": 0}
        self.win32._clipboard_handle = lambda text: 101 if text == "original" else 102
        self.win32._clipboard_locale_handle = lambda text: 201 if getattr(text, "locale", None) else None
        self.user32.set("CreateWindowExW", lambda *args: 55)
        self.user32.set("GetClipboardSequenceNumber", lambda: state["sequence"])

        def empty():
            state.update(sequence=state["sequence"] + 1, text=None, locale=None)
            return 1

        def place(fmt, handle):
            state["sets"] += 1
            if fmt == 16:
                return 0            # partial restore must not be reported as success
            state["text"] = "original" if handle == 101 else "temporary"
            return handle

        self.user32.set("EmptyClipboard", empty)
        self.user32.set("SetClipboardData", place)
        status, _ = self.win32.clipboard_replace_if_sequence(10, original, rollback=("text", "temporary"))
        self.assertEqual((status, state["text"], state["sets"]), ("busy", "temporary", 3))

    def test_failed_locale_allocation_does_not_empty_the_clipboard(self):
        original = self.win32._RestorableText("original", b"\x09\x04\x00\x00")
        calls = []
        self.win32._clipboard_handle = lambda text: 101
        self.win32._clipboard_locale_handle = lambda text: 0 if getattr(text, "locale", None) else None
        self.user32.set("CreateWindowExW", lambda *args: 55)
        self.user32.set("EmptyClipboard", lambda: calls.append("empty") or 1)
        self.kernel32.set("GlobalFree", lambda handle: calls.append(("free", handle)) or 0)
        status, _ = self.win32.clipboard_replace_if_sequence(10, original, rollback=("text", "temporary"))
        self.assertEqual(status, "busy")
        self.assertNotIn("empty", calls)

    def test_failed_locale_and_failed_rollback_report_loss(self):
        original = self.win32._RestorableText("original", b"\x09\x04\x00\x00")
        state = {"sequence": 10, "text": "temporary"}
        self.win32._clipboard_handle = lambda text: 101 if text == "original" else 102
        self.win32._clipboard_locale_handle = lambda text: 201 if getattr(text, "locale", None) else None
        self.user32.set("CreateWindowExW", lambda *args: 55)
        self.user32.set("GetClipboardSequenceNumber", lambda: state["sequence"])
        self.user32.set("EmptyClipboard", lambda: state.update(sequence=state["sequence"] + 1, text=None) or 1)
        self.user32.set("SetClipboardData", lambda fmt, handle: 0 if fmt == 16 or handle == 102
                        else state.update(text="original") or handle)
        status, _ = self.win32.clipboard_replace_if_sequence(10, original, rollback=("text", "temporary"))
        self.assertEqual((status, state["text"]), ("lost", None))

    def test_failed_temporary_set_restores_both_original_text_and_locale(self):
        original = self.win32._RestorableText("original", b"\x09\x04\x00\x00")
        state = {"sequence": 10, "text": "original", "locale": b"old", "sets": 0}
        self.win32._clipboard_handle = lambda text: 101 if text == "temporary" else 102
        self.win32._clipboard_locale_handle = lambda text: 201 if getattr(text, "locale", None) else None
        self.user32.set("CreateWindowExW", lambda *args: 55)
        self.user32.set("GetClipboardSequenceNumber", lambda: state["sequence"])
        self.user32.set("EmptyClipboard", lambda: state.update(sequence=state["sequence"] + 1, text=None, locale=None) or 1)

        def place(fmt, handle):
            state["sets"] += 1
            if state["sets"] == 1:
                return 0
            if fmt == 13:
                state["text"] = "original"
            elif fmt == 16:
                state["locale"] = b"old"
            return handle

        self.user32.set("SetClipboardData", place)
        status, _ = self.win32.clipboard_replace_if_sequence(10, "temporary", rollback=("text", original))
        self.assertEqual((status, state["text"], state["locale"], state["sets"]),
                         ("busy", "original", b"old", 3))

    def paste_lease_world(self):
        """Fake CloseClipboard adding locale and conversions after the locked write."""
        marker_format = 0xC001
        state = {"sequence": 5, "owner": 99, "open_owner": None,
                 "text": "old", "locale": b"\x09\x04\x00\x00", "marker": None,
                 "formats": [13, 16, 1, 7], "empties": 0, "destroyed": [],
                 "close_hook": None, "deny_open": False, "fail_marker": False,
                 "buffers": {}, "data": {}, "next_handle": 100, "next_window": 55}

        def allocate(data):
            state["next_handle"] += 1
            handle = state["next_handle"]
            state["data"][handle] = data
            return handle

        self.win32._clipboard_handle = lambda text: None if text is None else allocate(str(text))
        self.win32._clipboard_locale_handle = lambda text: (
            allocate(text.locale) if getattr(text, "locale", None) else None)
        self.win32._clipboard_bytes_handle = allocate
        self.user32.set("RegisterClipboardFormatW", lambda name: marker_format)

        def create(*args):
            result = state["next_window"]
            state["next_window"] += 11
            return result

        def destroy(hwnd):
            state["destroyed"].append(hwnd)
            if state["owner"] == hwnd:
                state["owner"] = 0
            return 1

        def open_clip(owner):
            if state["deny_open"]:
                return 0
            state["open_owner"] = owner
            return 1

        def close():
            if state["text"] is not None:
                for fmt in (16, 1, 7):
                    if fmt not in state["formats"]:
                        state["formats"].append(fmt)
                        state["sequence"] += 1
                if state["locale"] is None:
                    state["locale"] = b"\x09\x04\x00\x00"
            hook, state["close_hook"] = state["close_hook"], None
            if hook:
                hook(state)
            return 1

        def empty():
            state.update(sequence=state["sequence"] + 1, owner=state["open_owner"],
                         text=None, locale=None, marker=None, formats=[],
                         empties=state["empties"] + 1)
            return 1

        def set_data(fmt, handle):
            if fmt == marker_format and state["fail_marker"]:
                return 0
            data = state["data"][handle]
            if fmt == 13:
                state["text"] = data
            elif fmt == 16:
                state["locale"] = data
            elif fmt == marker_format:
                state["marker"] = data
            state["formats"].append(fmt)
            state["sequence"] += 1
            return handle

        def enum(previous):
            if not previous:
                return state["formats"][0]
            index = state["formats"].index(previous) + 1
            return state["formats"][index] if index < len(state["formats"]) else 0

        def get_data(fmt):
            handle = 1000 + fmt
            if fmt == 13:
                buffer = ctypes.create_unicode_buffer(state["text"])
            else:
                content = state["locale"] if fmt == 16 else state["marker"]
                buffer = ctypes.create_string_buffer(content)
            state["buffers"][handle] = buffer
            return handle

        self.user32.set("CreateWindowExW", create)
        self.user32.set("DestroyWindow", destroy)
        self.user32.set("OpenClipboard", open_clip)
        self.user32.set("CloseClipboard", close)
        self.user32.set("GetClipboardOwner", lambda: state["owner"])
        self.user32.set("GetClipboardSequenceNumber", lambda: state["sequence"])
        self.user32.set("EmptyClipboard", empty)
        self.user32.set("SetClipboardData", set_data)
        self.user32.set("CountClipboardFormats", lambda: len(state["formats"]))
        self.user32.set("EnumClipboardFormats", enum)
        self.user32.set("GetClipboardData", get_data)
        self.kernel32.set("GlobalSize", lambda handle: len(state["buffers"][handle].raw))
        self.kernel32.set("GlobalLock", lambda handle: ctypes.addressof(state["buffers"][handle]))
        return state

    def test_paste_lease_adopts_post_close_sequence_and_keeps_owner_until_restored(self):
        state = self.paste_lease_world()
        status, lease = self.win32.clipboard_begin_paste(5, "temporary", ("text", "old"))
        self.assertEqual(status, "written")
        self.assertEqual(lease.sequence, 11)  # 8 in lock; locale/ANSI/OEM add 3 on Close
        self.assertEqual(state["destroyed"], [])
        self.assertTrue(self.win32.clipboard_paste_current(lease))
        restored, _ = self.win32.clipboard_end_paste(lease, "old")
        self.assertEqual((restored, state["text"], state["marker"]), ("written", "old", None))
        self.assertEqual(state["destroyed"], [66, 55])

    def test_close_time_new_copy_is_not_mistaken_for_our_paste(self):
        for new_text, clone_marker in (("different", False), ("temporary", False),
                                       ("temporary", True)):
            with self.subTest(new_text=new_text, clone_marker=clone_marker):
                self.setUp()
                state = self.paste_lease_world()

                def user_copy(s):
                    s.update(owner=77, text=new_text, sequence=s["sequence"] + 1,
                             marker=s["marker"] if clone_marker else None,
                             formats=list(s["formats"]) if clone_marker else [13, 16, 1, 7])

                state["close_hook"] = user_copy
                status, lease = self.win32.clipboard_begin_paste(5, "temporary", ("text", "old"))
                self.assertEqual((status, lease, state["text"], state["empties"]),
                                 ("changed", None, new_text, 1))

    def test_paste_lease_rejects_unknown_format_changed_nonce_or_invalid_owner(self):
        for mutation in ("format", "nonce", "owner", "sequence"):
            with self.subTest(mutation=mutation):
                self.setUp()
                state = self.paste_lease_world()

                def mutate(s):
                    if mutation == "format":
                        s["formats"].append(0xC100)
                    elif mutation == "nonce":
                        s["marker"] = b"x" * 32
                    elif mutation == "owner":
                        s["owner"] = 0
                    else:
                        s["sequence"] = 0

                state["close_hook"] = mutate
                status, lease = self.win32.clipboard_begin_paste(5, "temporary", ("text", "old"))
                self.assertEqual(status, "lost")
                self.assertIsNone(lease)
                self.assertEqual(state["empties"], 1)

    def test_paste_lease_fails_closed_if_post_close_verification_cannot_open(self):
        state = self.paste_lease_world()
        state["close_hook"] = lambda s: s.update(deny_open=True)
        status, lease = self.win32.clipboard_begin_paste(5, "temporary", ("text", "old"))
        self.assertEqual((status, lease, state["empties"]), ("lost", None, 1))

    def test_paste_lease_restoration_does_not_overwrite_a_new_user_copy(self):
        state = self.paste_lease_world()
        status, lease = self.win32.clipboard_begin_paste(5, "temporary", ("text", "old"))
        self.assertEqual(status, "written")
        state.update(owner=77, text="new user copy", marker=None,
                     sequence=state["sequence"] + 1, formats=[13, 16, 1, 7])
        self.assertFalse(self.win32.clipboard_paste_current(lease))
        empties = state["empties"]
        restored, _ = self.win32.clipboard_end_paste(lease, "old")
        self.assertEqual((restored, state["text"], state["empties"]),
                         ("changed", "new user copy", empties))

    def test_same_text_and_cloned_nonce_with_new_owner_are_not_our_lease(self):
        state = self.paste_lease_world()
        status, lease = self.win32.clipboard_begin_paste(5, "temporary", ("text", "old"))
        self.assertEqual(status, "written")
        state["owner"] = 77         # another process copied the same body and marker
        self.assertFalse(self.win32.clipboard_paste_current(lease))
        empties = state["empties"]
        restored, _ = self.win32.clipboard_end_paste(lease, "old")
        self.assertEqual((restored, state["text"], state["empties"]),
                         ("changed", "temporary", empties))

    def test_paste_lease_rejects_a_new_format_even_without_counter_change(self):
        state = self.paste_lease_world()
        status, lease = self.win32.clipboard_begin_paste(5, "temporary", ("text", "old"))
        self.assertEqual(status, "written")
        state["formats"].append(0xC100)
        self.assertFalse(self.win32.clipboard_paste_current(lease))
        empties = state["empties"]
        restored, _ = self.win32.clipboard_end_paste(lease, "old")
        self.assertEqual((restored, state["text"], state["empties"]),
                         ("lost", "temporary", empties))

    def test_failed_marker_write_rolls_back_the_original_before_unlocking(self):
        state = self.paste_lease_world()
        state["fail_marker"] = True
        status, lease = self.win32.clipboard_begin_paste(5, "temporary", ("text", "old"))
        self.assertEqual((status, lease, state["text"], state["marker"]),
                         ("busy", None, "old", None))
        self.assertEqual(state["empties"], 2)

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
