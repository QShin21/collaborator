"""The Windows client's own control flow (banxing/hosts/uia.py), run on any platform
against stand-ins for Win32 and UI Automation.

What this is: the real `WindowsCodexClient` code — snapshot, _note_host, may_read,
_find_box, _read, signals, raw_box_text, fill — executing, with the operating system
underneath replaced by fakes that count what is asked of them. It pins the ORDER of
decisions (which Codex is this → may it be read → only then touch the window).

What this is not: evidence about Windows, UI Automation or Codex. The fakes behave
the way this code assumes those behave. It stays E1; see docs/verification.md.
"""
import importlib.machinery
import importlib.util
import shutil
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

import safety  # noqa: F401  (keeps tests away from real user data)

import banxing.hosts as hosts_package
from banxing import calibration
from banxing.geometry import Rect
from banxing.hosts.profile import Profile

from rig import RigCase

UIA_SOURCE = Path(hosts_package.__file__).parent / "uia.py"
DRAFT = "这是写在 Codex 输入框里的一段草稿"
REAL_SLEEP = time.sleep


class FakeWin32(types.ModuleType):
    """Stands in for banxing.hosts.win32. One Codex window; the process behind it can
    be swapped for an "updated" one."""

    VK_CONTROL, VK_A, VK_V, VK_DELETE = 0x11, 0x41, 0x56, 0x2E

    def __init__(self, folder):
        super().__init__("banxing.hosts.win32")
        self.folder = Path(folder)
        self.hwnd, self.pid = 4242, 100
        self.front = True
        self.paths = {}
        self.install(100, size=1000)

    def install(self, pid, size):
        """A Codex executable of a given size: size and time make up the version."""
        path = self.folder / f"build-{pid}" / "Codex.exe"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)
        self.paths[pid] = str(path)

    def update_codex(self, pid, size, hwnd=None):
        self.install(pid, size)
        self.pid = pid
        if hwnd is not None:
            self.hwnd = hwnd

    def foreground_window(self):
        return self.hwnd if self.front else 0

    def window_pid(self, hwnd):
        return self.pid if hwnd == self.hwnd else 0

    def process_name(self, pid):
        return "codex.exe" if pid in self.paths else "other.exe"

    def process_path(self, pid):
        if pid not in self.paths:
            raise OSError("no such process")
        return self.paths[pid]

    def window_title(self, hwnd):
        return "Codex"

    def window_rect(self, hwnd):
        return Rect(100, 80, 1300, 880)

    def is_usable_window(self, hwnd):
        return hwnd == self.hwnd

    def visible_windows_of_class(self, names):
        return []

    def set_foreground(self, hwnd):
        return True


class FakeRect:
    left, top, right, bottom = 220, 780, 1180, 850


class FakeValue:
    def __init__(self, element):
        self.element = element

    def QueryInterface(self, interface):
        return self

    @property
    def CurrentIsReadOnly(self):
        return False

    @property
    def CurrentValue(self):
        self.element.world.text_reads += 1
        return self.element.text

    def SetValue(self, text):
        self.element.world.writes.append(text)
        self.element.text = text


class FakeElement:
    """The composer of one Codex process."""
    CurrentBoundingRectangle = FakeRect()
    CurrentControlType = 50004
    CurrentFrameworkId = "Win32"                # a native box: written through its value
    CurrentAriaProperties = "multiline=true"
    CurrentIsPassword = False
    CurrentName = ""
    CurrentHelpText = ""

    def __init__(self, world, pid, text):
        self.world, self.CurrentProcessId, self.text = world, pid, text

    def GetCurrentPattern(self, pattern_id):
        return FakeValue(self) if pattern_id == "UIA_ValuePatternId" else None

    def GetRuntimeId(self):
        return (42, self.CurrentProcessId)

    def SetFocus(self):
        pass


class FakeFound:
    Length = 0


class FakeRoot:
    def FindFirst(self, scope, condition):
        return None

    def FindAll(self, scope, condition):
        return FakeFound()

    FindAllBuildCache = lambda self, scope, condition, request: FakeFound()     # noqa: E731


class FakeWalker:
    def GetParentElement(self, element):
        return None


class FakeAutomation:
    """Stands in for IUIAutomation. Every call into "the window" is counted."""

    def __init__(self, world):
        self.world = world
        self.ControlViewWalker = FakeWalker()

    def GetFocusedElement(self):
        self.world.calls += 1
        return self.world.box()

    def ElementFromHandle(self, hwnd):
        self.world.calls += 1
        return FakeRoot()

    def CreatePropertyCondition(self, prop, value):
        return (prop, value)

    def CreateCacheRequest(self):
        return types.SimpleNamespace(AddProperty=lambda prop: None)

    def CompareElements(self, a, b):
        return a is b


class FakeApi:
    def __getattr__(self, name):
        return name                              # every constant and interface is just its own name


class World:
    """What is "on screen", plus the counters the tests read."""

    def __init__(self, win32):
        self.win32 = win32
        self.calls = 0                           # UI Automation calls into the window
        self.text_reads = 0                      # times the composer's text was fetched
        self.writes = []
        self.boxes = {}                          # pid -> element
        self.type(DRAFT)

    def box(self):
        pid = self.win32.pid
        if pid not in self.boxes:
            self.boxes[pid] = FakeElement(self, pid, "")
        return self.boxes[pid]

    def type(self, text):
        self.box().text = text

    @property
    def draft(self):
        return self.box().text


def load_uia(win32):
    """Import the real uia.py as if on Windows, with the fake win32 beneath it.
    Loaded under a private name; nothing is left behind in sys.modules, so the rest
    of the suite still sees a platform on which the Windows modules do not import."""
    name = "banxing.hosts._uia_under_test"
    comtypes = types.ModuleType("comtypes")
    comtypes.__spec__ = importlib.machinery.ModuleSpec("comtypes", None)
    missing = object()
    real_attr = getattr(hosts_package, "win32", missing)        # on Windows the real module may be loaded
    saved = {key: sys.modules.get(key) for key in ("banxing.hosts.win32", "comtypes", name)}
    platform = sys.platform
    sys.modules["banxing.hosts.win32"] = win32
    sys.modules["comtypes"] = comtypes
    hosts_package.win32 = win32
    try:
        sys.platform = "win32"
        spec = importlib.util.spec_from_file_location(name, UIA_SOURCE)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.platform = platform
        if real_attr is missing:
            del hosts_package.win32
        else:
            hosts_package.win32 = real_attr
        for key, value in saved.items():
            if value is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = value


class UiaFlow(RigCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="banxing-uia-"))
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.win32 = FakeWin32(self.folder)
        self.uia = load_uia(self.win32)
        self.world = World(self.win32)
        self.client = self.uia.WindowsCodexClient(Profile.default())
        self.client._uia = FakeAutomation(self.world)
        self.client._api = FakeApi()
        # The module's OWN view of time: no waiting. (Never patch the shared `time`
        # module — every later test in the process would stop sleeping.)
        self.uia.time = types.SimpleNamespace(monotonic=time.monotonic, sleep=lambda seconds: None)
        self.now = 10.0

    def look(self):
        self.now += 0.25
        return self.client.snapshot(self.now)

    def calibrated_for_current_version(self):
        """What the controller does once a calibration for this Codex version exists."""
        self.look()                              # blind: learns the version, reads nothing
        version = self.client.host_version()
        self.assertTrue(version.startswith("codex.exe:1000:"))
        self.client.set_reading(True, version)
        return version

    # ------------------------------------------------------------ the harness itself
    def test_the_real_module_is_loaded_and_leaves_no_trace(self):
        self.assertEqual(Path(self.uia.__file__), UIA_SOURCE)
        self.assertIs(self.uia.win32, self.win32)
        self.assertIsNot(sys.modules.get("banxing.hosts.win32"), self.win32)
        self.assertIsNot(getattr(hosts_package, "win32", None), self.win32)
        self.assertNotIn("banxing.hosts._uia_under_test", sys.modules)
        self.assertIs(time.sleep, REAL_SLEEP)                         # the shared clock was not touched
        if sys.platform != "win32":
            self.assertNotIn("banxing.hosts.win32", sys.modules)
            self.assertNotIn("comtypes", sys.modules)
            self.assertFalse(hasattr(hosts_package, "win32"))
            with self.assertRaises(ImportError):
                import banxing.hosts.win32  # noqa: F401

    # ------------------------------------------------------------ before any calibration
    def test_an_uncalibrated_client_never_calls_into_the_window(self):
        for _ in range(6):
            snap = self.look()
            self.assertEqual((snap.foreground, snap.draft, snap.box_id, snap.chat_key, snap.problem),
                             (True, None, "", "", "uncalibrated"))
            self.assertIsNotNone(snap.window_rect)
        self.assertEqual((self.world.calls, self.world.text_reads), (0, 0))
        self.assertIsNone(self.client.signals())
        self.assertIsNone(self.client.raw_box_text())
        self.assertEqual((self.world.calls, self.world.text_reads), (0, 0))

    def test_a_calibrated_client_reads_the_draft(self):
        self.calibrated_for_current_version()
        snap = self.look()
        self.assertEqual((snap.draft, snap.problem), (DRAFT, ""))
        self.assertTrue(snap.box_id and snap.chat_key)
        self.assertGreater(self.world.text_reads, 0)

    # ------------------------------------------------------------ the audit's finding
    def test_the_first_look_at_another_codex_version_reads_nothing(self):
        """Reading was granted for the old version. Codex is replaced by a new build
        between two looks. The very first look at the new one must be blind — the
        permission is worked out after the version is known, not before."""
        old = self.calibrated_for_current_version()
        self.assertEqual(self.look().draft, DRAFT)
        self.win32.update_codex(pid=200, size=2000, hwnd=5151)       # a new process, a new window
        self.world.type("写在新版本 Codex 里的另一段话")
        calls, reads = self.world.calls, self.world.text_reads

        snap = self.look()                                            # the first look at the new version

        self.assertNotEqual(self.client.host_version(), old)
        self.assertEqual((snap.draft, snap.box_id, snap.chat_key, snap.problem), (None, "", "", "uncalibrated"))
        self.assertEqual((self.world.calls, self.world.text_reads), (calls, reads))    # not one call into it
        for _ in range(5):
            self.assertIsNone(self.look().draft)
        self.assertEqual((self.world.calls, self.world.text_reads), (calls, reads))

    def test_the_same_holds_when_the_window_handle_is_reused(self):
        self.calibrated_for_current_version()
        self.assertEqual(self.look().draft, DRAFT)
        self.win32.update_codex(pid=200, size=2000)                   # same hwnd, another process
        self.world.type("另一段话")
        calls, reads = self.world.calls, self.world.text_reads
        self.assertIsNone(self.look().draft)
        self.assertEqual((self.world.calls, self.world.text_reads), (calls, reads))

    def test_an_error_on_the_new_version_does_not_replay_the_old_draft(self):
        self.calibrated_for_current_version()
        self.assertEqual(self.look().draft, DRAFT)                    # remembered as the last good look
        self.win32.update_codex(pid=200, size=2000, hwnd=5151)
        boom = lambda hwnd: (_ for _ in ()).throw(OSError("gone"))    # noqa: E731
        self.win32.window_rect, real = boom, self.win32.window_rect
        snap = self.look()
        self.assertIsNone(snap.draft)
        self.win32.window_rect = real
        self.assertIsNone(self.look().draft)

    def test_signals_and_the_placeholder_read_are_refused_on_the_new_version_too(self):
        self.calibrated_for_current_version()
        self.look()
        self.assertIsNotNone(self.client.signals())
        self.assertEqual(self.client.raw_box_text(), DRAFT)
        self.win32.update_codex(pid=200, size=2000)                   # no look in between
        calls, reads = self.world.calls, self.world.text_reads
        self.assertIsNone(self.client.signals())
        self.assertIsNone(self.client.raw_box_text())
        self.assertEqual((self.world.calls, self.world.text_reads), (calls, reads))

    def test_an_unknown_version_is_not_the_calibrated_one(self):
        self.calibrated_for_current_version()
        self.win32.update_codex(pid=300, size=1000, hwnd=6161)
        del self.win32.paths[300]                                     # its executable cannot be examined
        self.win32.process_name = lambda pid: "codex.exe"
        snap = self.look()
        self.assertEqual((self.client.host_version(), snap.draft, snap.problem), ("", None, "uncalibrated"))
        self.assertEqual((self.world.calls, self.world.text_reads), (0, 0))

    def test_going_back_to_the_calibrated_version_reads_again(self):
        self.calibrated_for_current_version()
        self.win32.update_codex(pid=200, size=2000, hwnd=5151)
        self.assertIsNone(self.look().draft)
        self.win32.update_codex(pid=100, size=1000, hwnd=4242)
        self.assertEqual(self.look().draft, DRAFT)

    # ------------------------------------------------------------ fills
    def key_of(self, snap):
        return (snap.window_id, snap.chat_key, snap.box_id)

    def paste_clipboard(self):
        """A clipboard with a change counter, like the real Win32 wrapper."""
        clip = {"kind": "text", "text": "saved clipboard", "sequence": 10}
        calls = []
        self.win32.modifiers_held = lambda: False
        self.win32.clipboard_capture = lambda: (clip["kind"], clip["text"], clip["sequence"])

        def replace(sequence, text, rollback=None):
            calls.append((sequence, text))
            if sequence != clip["sequence"]:
                return "changed", clip["sequence"]
            clip["kind"] = "text" if text is not None else "empty"
            clip["text"] = text
            clip["sequence"] += 1
            return "written", clip["sequence"]

        self.win32.clipboard_replace_if_sequence = replace

        def begin(sequence, text, rollback=None):
            status, new_sequence = self.win32.clipboard_replace_if_sequence(sequence, text, rollback)
            return status, (types.SimpleNamespace(sequence=new_sequence) if status == "written" else None)

        self.win32.clipboard_begin_paste = begin
        self.win32.clipboard_paste_current = lambda lease: clip["sequence"] == lease.sequence
        self.win32.clipboard_end_paste = lambda lease, saved: self.win32.clipboard_replace_if_sequence(
            lease.sequence, saved, rollback=("text", clip["text"]))
        return clip, calls

    def test_paste_is_one_batched_input_and_restores_the_clipboard(self):
        self.calibrated_for_current_version()
        snap = self.look()
        clip, swaps = self.paste_clipboard()
        shortcuts = []

        def send(*chords):
            shortcuts.append(chords)
            self.world.type(clip["text"])
            return True

        self.win32.send_shortcuts = send
        ok, why = self.client._fill_paste(self.win32.hwnd, self.client._box, "pasted 文本")
        self.assertEqual((ok, why, self.world.draft), (True, "", "pasted 文本"))
        self.assertEqual((clip["kind"], clip["text"]), ("text", "saved clipboard"))
        self.assertEqual(swaps, [(10, "pasted 文本"), (11, "saved clipboard")])
        self.assertEqual(shortcuts, [((self.win32.VK_CONTROL, self.win32.VK_A),
                                      (self.win32.VK_CONTROL, self.win32.VK_V))])
        self.assertEqual(snap.draft, DRAFT)  # never sent or altered the saved snapshot

    def test_a_failed_key_injection_does_not_change_the_draft_or_clipboard(self):
        self.calibrated_for_current_version()
        self.look()
        clip, _ = self.paste_clipboard()
        self.win32.send_shortcuts = lambda *chords: False
        ok, why = self.client._fill_paste(self.win32.hwnd, self.client._box, "new")
        self.assertEqual((ok, why, self.world.draft), (False, "send_input", DRAFT))
        self.assertEqual((clip["kind"], clip["text"]), ("text", "saved clipboard"))

    def test_clearing_the_box_does_not_need_or_touch_the_clipboard(self):
        self.calibrated_for_current_version()
        self.look()
        self.win32.modifiers_held = lambda: False
        self.win32.clipboard_capture = lambda: self.fail("Delete must not read the clipboard")
        self.win32.clipboard_replace_if_sequence = lambda *args: self.fail("Delete must not change the clipboard")
        chords = []

        def send(*shortcuts):
            chords.append(shortcuts)
            self.world.type("")
            return True

        self.win32.send_shortcuts = send
        ok, why = self.client._fill_paste(self.win32.hwnd, self.client._box, "")
        self.assertEqual((ok, why, self.world.draft), (True, "", ""))
        self.assertEqual(chords, [((self.win32.VK_CONTROL, self.win32.VK_A), (self.win32.VK_DELETE,))])

    def test_injected_but_not_received_does_not_replay_the_draft_or_lose_clipboard(self):
        self.calibrated_for_current_version()
        self.look()
        clip, _ = self.paste_clipboard()
        self.win32.send_shortcuts = lambda *chords: True
        self.client._wait_for = lambda box, wanted, timeout: False
        ok, why = self.client._fill_paste(self.win32.hwnd, self.client._box, "new")
        self.assertEqual((ok, why, self.world.draft), (False, "not_received", DRAFT))
        self.assertEqual((clip["kind"], clip["text"]), ("text", "saved clipboard"))

    def test_a_new_user_copy_is_never_overwritten_by_clipboard_restoration(self):
        self.calibrated_for_current_version()
        self.look()
        clip, _ = self.paste_clipboard()

        def send(*chords):
            self.world.type(clip["text"])
            clip.update(kind="text", text="user's newer copy", sequence=clip["sequence"] + 1)
            return True

        self.win32.send_shortcuts = send
        ok, why = self.client._fill_paste(self.win32.hwnd, self.client._box, "new")
        self.assertEqual((ok, why, self.world.draft), (True, "", "new"))
        self.assertEqual(clip["text"], "user's newer copy")

    def test_a_new_copy_before_the_swap_prevents_any_input(self):
        self.calibrated_for_current_version()
        self.look()
        clip, _ = self.paste_clipboard()
        original_replace = self.win32.clipboard_replace_if_sequence

        def replace(sequence, text, rollback=None):
            clip.update(text="user's newer copy", sequence=clip["sequence"] + 1)
            return original_replace(sequence, text, rollback)

        self.win32.clipboard_replace_if_sequence = replace
        self.win32.send_shortcuts = lambda *chords: self.fail("must not send keys")
        ok, why = self.client._fill_paste(self.win32.hwnd, self.client._box, "new")
        self.assertEqual((ok, why, self.world.draft), (False, "clipboard_busy", DRAFT))
        self.assertEqual(clip["text"], "user's newer copy")

    def test_a_clipboard_write_and_rollback_failure_never_injects_keys(self):
        self.calibrated_for_current_version()
        self.look()
        self.paste_clipboard()
        self.win32.clipboard_replace_if_sequence = lambda *args, **kwargs: ("lost", 11)
        self.win32.send_shortcuts = lambda *args: self.fail("must not inject after clipboard loss")
        ok, why = self.client._fill_paste(self.win32.hwnd, self.client._box, "new")
        self.assertEqual((ok, why, self.world.draft), (False, "clipboard_restore", DRAFT))

    def test_a_new_copy_during_focus_is_not_pasted_into_the_draft(self):
        self.calibrated_for_current_version()
        self.look()
        clip, _ = self.paste_clipboard()
        focus = self.client._focus
        times = []

        def moving_focus(hwnd, box):
            times.append(1)
            if len(times) == 2:
                clip.update(text="user's newer copy", sequence=clip["sequence"] + 1)
            return focus(hwnd, box)

        self.client._focus = moving_focus
        self.win32.send_shortcuts = lambda *chords: self.fail("must not paste the newer copy")
        ok, why = self.client._fill_paste(self.win32.hwnd, self.client._box, "new")
        self.assertEqual((ok, why, self.world.draft), (False, "clipboard_busy", DRAFT))
        self.assertEqual(clip["text"], "user's newer copy")

    def test_a_failed_clipboard_restoration_is_reported_even_if_the_paste_landed(self):
        self.calibrated_for_current_version()
        self.look()
        clip, _ = self.paste_clipboard()
        original_replace = self.win32.clipboard_replace_if_sequence
        calls = []

        def replace(sequence, text, rollback=None):
            calls.append(text)
            return ("busy", clip["sequence"]) if len(calls) == 2 else original_replace(sequence, text, rollback)

        self.win32.clipboard_replace_if_sequence = replace
        self.win32.send_shortcuts = lambda *chords: (self.world.type(clip["text"]), True)[1]
        with self.assertLogs(self.uia.log, level="WARNING"):
            ok, why = self.client._fill_paste(self.win32.hwnd, self.client._box, "new")
        self.assertEqual((ok, why, self.world.draft), (True, "clipboard_restore", "new"))
        self.assertEqual(clip["text"], "new")

    def test_a_failed_write_does_not_replay_the_saved_draft_over_a_user_edit(self):
        self.calibrated_for_current_version()
        snap = self.look()

        def failed_value(box, text):
            self.world.type("user edited the draft")
            return False, "failed"

        self.client._fill_value = failed_value
        self.client._fill_paste = lambda *args: self.fail("must not retry after an edit")
        result = self.client.fill(self.key_of(snap), DRAFT, "new prompt")
        self.assertEqual((result.ok, result.reason, self.world.draft),
                         (False, "failed", "user edited the draft"))

    def test_an_uncertain_paste_failure_does_not_start_a_second_write(self):
        self.calibrated_for_current_version()
        snap = self.look()
        self.world.box().CurrentFrameworkId = "Chrome"  # paste has priority for a rich editor
        self.client._fill_paste = lambda *args, **kwargs: (False, "not_received")
        self.client._fill_value = lambda *args: self.fail("must not retry an uncertain paste")
        result = self.client.fill(self.key_of(snap), DRAFT, "new prompt")
        self.assertEqual((result.ok, result.reason, self.world.draft), (False, "not_received", DRAFT))

    def test_a_chat_switch_during_clipboard_setup_prevents_the_paste(self):
        self.calibrated_for_current_version()
        snap = self.look()
        self.world.box().CurrentFrameworkId = "Chrome"
        clip, _ = self.paste_clipboard()
        original_replace = self.win32.clipboard_replace_if_sequence

        def replace(sequence, text, rollback=None):
            status, new_sequence = original_replace(sequence, text, rollback)
            if text == "new prompt" and status == "written":
                self.win32.window_title = lambda hwnd: "Another chat"
            return status, new_sequence

        self.win32.clipboard_replace_if_sequence = replace
        self.win32.send_shortcuts = lambda *args: self.fail("must not paste into another chat")
        result = self.client.fill(self.key_of(snap), DRAFT, "new prompt")
        self.assertEqual((result.ok, result.reason, self.world.draft), (False, "mismatch", DRAFT))
        self.assertEqual(clip["text"], "saved clipboard")

    def test_a_user_edit_during_clipboard_setup_is_left_in_place(self):
        self.calibrated_for_current_version()
        snap = self.look()
        self.world.box().CurrentFrameworkId = "Chrome"
        clip, _ = self.paste_clipboard()
        original_replace = self.win32.clipboard_replace_if_sequence

        def replace(sequence, text, rollback=None):
            status, new_sequence = original_replace(sequence, text, rollback)
            if text == "new prompt" and status == "written":
                self.world.type("the user's new draft")
            return status, new_sequence

        self.win32.clipboard_replace_if_sequence = replace
        self.win32.send_shortcuts = lambda *args: self.fail("must not replace the new draft")
        result = self.client.fill(self.key_of(snap), DRAFT, "new prompt")
        self.assertEqual((result.ok, result.reason, self.world.draft),
                         (False, "mismatch", "the user's new draft"))
        self.assertEqual(clip["text"], "saved clipboard")

    def test_a_fill_on_the_calibrated_version_checks_and_writes(self):
        self.calibrated_for_current_version()
        snap = self.look()
        refused = self.client.fill(self.key_of(snap), "not what is there", "新的提示词")
        self.assertEqual((refused.ok, refused.reason, self.world.draft), (False, "mismatch", DRAFT))
        result = self.client.fill(self.key_of(snap), DRAFT, "新的提示词")
        self.assertEqual((result.ok, result.method, self.world.draft), (True, "value", "新的提示词"))
        self.assertEqual(self.world.writes, ["新的提示词"])

    def test_a_fill_aimed_at_the_old_version_writes_nothing_into_the_new_one(self):
        self.calibrated_for_current_version()
        snap = self.look()                                            # a bubble was shown for this
        self.win32.update_codex(pid=200, size=2000)                   # same hwnd: the stale key could "match"
        self.world.type(DRAFT)                                        # ... and so could the text
        calls, reads = self.world.calls, self.world.text_reads
        result = self.client.fill(self.key_of(snap), DRAFT, "新的提示词")
        self.assertEqual((result.ok, result.reason), (False, "mismatch"))
        self.assertEqual((self.world.draft, self.world.writes), (DRAFT, []))
        self.assertEqual((self.world.calls, self.world.text_reads), (calls, reads))    # not even read

    def test_a_fill_without_any_calibration_is_refused_unread(self):
        result = self.client.fill((str(self.win32.hwnd), "", ""), DRAFT, "新的提示词")
        self.assertEqual((result.ok, self.world.writes, self.world.text_reads), (False, [], 0))

    # ------------------------------------------------------------ what the user starts
    def test_a_calibration_step_reads_on_an_uncalibrated_version_and_only_for_its_length(self):
        self.win32.update_codex(pid=200, size=2000, hwnd=5151)       # no calibration for this build
        self.assertIsNone(self.look().draft)
        self.world.type(calibration.PHRASE)
        result = calibration.check(self.client, "read", {})
        self.assertTrue(result["ok"], result)
        self.assertGreater(self.world.text_reads, 0)
        reads = self.world.text_reads
        self.assertIsNone(self.look().draft)                          # the step is over: blind again
        self.assertEqual(self.world.text_reads, reads)

    def test_the_calibration_write_test_works_and_removes_only_its_marker(self):
        self.world.type("")
        result = calibration.check(self.client, "fill", {})
        self.assertEqual((result["ok"], result["restored"], result["marker_left"]), (True, True, False), result)
        self.assertEqual(self.world.writes, [calibration.MARKER, ""])
        self.assertEqual(self.world.draft, "")
        self.assertFalse(self.client.may_read())

    def test_authorized_reads_survive_a_version_change_inside_the_check(self):
        """A diagnostic the user started reads whatever Codex is in front — that is its
        purpose — without leaving routine reading switched on afterwards."""
        self.calibrated_for_current_version()
        self.win32.update_codex(pid=200, size=2000, hwnd=5151)
        self.world.type("abc")
        with self.client.authorized():
            self.assertEqual(self.client.snapshot(self.now).draft, "abc")
            self.assertEqual(self.client.raw_box_text(), "abc")
        self.assertIsNone(self.look().draft)

    # ------------------------------------------------------------ through the controller
    def test_the_controller_drops_the_calibration_and_the_client_stays_blind(self):
        rig = self.rig(calibrated=False, client=self.client)
        tick = lambda: rig.controller.on_snapshot(self.look())        # noqa: E731
        tick()
        self.world.type("")
        results = {}
        memo = {}
        for step in ("window", "read", "empty", "fill"):
            if step == "read":
                self.world.type(calibration.PHRASE)
            if step in ("empty", "fill"):
                self.world.type("")
            results[step] = calibration.check(self.client, step, memo)
        entry = rig.controller.finish_calibration(results, memo)
        self.assertEqual(entry["verdict"], "passed", results)
        self.world.type(DRAFT)
        tick()
        self.assertEqual(rig.view.last_snap.draft, DRAFT)             # following, on the calibrated build

        self.win32.update_codex(pid=200, size=2000, hwnd=5151)
        self.world.type("新版本里的草稿")
        reads = self.world.text_reads
        for _ in range(12):
            tick()
        self.assertEqual(self.world.text_reads, reads)
        self.assertIsNone(rig.view.last_snap.draft)
        self.assertEqual((rig.controller.capabilities, rig.view.state), (frozenset(), ("blind", "uncalibrated")))
        self.assertEqual(rig.calls, [])


if __name__ == "__main__":
    unittest.main()
