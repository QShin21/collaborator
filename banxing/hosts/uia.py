"""The Codex client for Windows, on UI Automation (via comtypes).

What it does, all from the watcher thread:

  snapshot()  Is a Codex window in front? Where is its input box, what is in it,
              which chat is shown, is an input method composing?
  signals()   The text visible in the window, to recognise the chat (local use).
  fill()      Re-check chat, box and draft, then replace the draft. Never sends:
              the only keys ever synthesised are Ctrl+A, Ctrl+V and Delete.
  probe()     A compatibility report without any draft or chat text.

STATUS — NOT VERIFIED ON A REAL CODEX WINDOW. This file was written on Linux
against the documented UI Automation API and what is generally true of Chromium
windows. Which parts hold for the Codex desktop app (does the composer expose its
text, does pasting register with the editor, does the page address identify the
chat, is IME composition detectable) must be established with the self-check on
Windows; see docs/verification.md. Every assumption about the window itself is in
profile.py, and every failure here degrades to "cannot read" / "could not fill",
for which the pet has fallbacks (paste a draft, copy the prompt).
"""
from __future__ import annotations

import hashlib
import importlib.util
import logging
import os
import sys
import time
from dataclasses import replace

if sys.platform != "win32":
    raise ImportError("banxing.hosts.uia is only available on Windows")
if importlib.util.find_spec("comtypes") is None:
    raise ImportError("the comtypes package is required for UI Automation")

from ..context.model import ChatSignals
from ..context.provider import thread_id_in
from ..follow import Snapshot
from ..geometry import Rect
from ..textutil import loose, similarity, strict
from . import win32
from .base import CHAT_KEY, FILL, READ, SIGNALS, CodexClient, FillResult
from .profile import BoxCandidate, page_address

log = logging.getLogger(__name__)

SEARCH_EVERY = 1.0          # seconds before the first new search for a box that was not found ...
SEARCH_SLOWEST = 8.0        # ... doubling up to this: a window without an input box is not hammered
SELECTED_EVERY = 3.0        # seconds between whole-tree looks for selected items — and only when the
#                             page address does not already tell the chats apart
TOLERATED_FAILURES = 3      # consecutive read errors before the pet is told
FILL_MARKER = "〔伴行写入测试〕"


class WindowsCodexClient(CodexClient):
    name = "uia"
    can_fill = True

    def __init__(self, profile):
        self.profile = profile
        self._uia = None
        self._api = None
        self._errors = (OSError, ValueError, AttributeError)
        self._hwnd = 0                 # the Codex window being followed
        self._front = False            # it was the foreground window at the last look
        self._names = {}               # pid -> executable name
        self._box = None
        self._box_hwnd = 0
        self._frame = None             # (box runtime id, Rect) of the composer frame
        self._document = None
        self._document_hwnd = 0
        self._selected = ()
        self._selected_at = 0.0
        self._searched_at = 0.0
        self._last_good = None
        self._failures = 0
        self._last_ids = None          # (runtime id, text, rect) of the previous look
        self._id_flips = 0
        self._coarse_ids = False
        self._host_version = ""
        self._versioned_hwnd = 0
        self._search_wait = SEARCH_EVERY

    # ------------------------------------------------------------ lifecycle
    def open(self):
        # comtypes initialises COM for the thread that first imports it, using
        # sys.coinit_flags. UI Automation clients belong on a multithreaded
        # apartment, away from the UI thread.
        sys.coinit_flags = 0
        import comtypes
        import comtypes.client
        try:
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        except OSError:
            pass                        # already initialised on this thread
        try:
            from comtypes.gen import UIAutomationClient as api
        except ImportError:
            comtypes.client.GetModule("UIAutomationCore.dll")
            from comtypes.gen import UIAutomationClient as api
        self._api = api
        self._errors = (comtypes.COMError, OSError, ValueError, AttributeError)
        self._uia = comtypes.CoCreateInstance(
            api.CUIAutomation._reg_clsid_, interface=api.IUIAutomation,
            clsctx=comtypes.CLSCTX_INPROC_SERVER)

    def close(self):
        self._box = self._document = self._uia = None

    # ------------------------------------------------------------ element helpers
    def _pattern(self, element, pattern_id, interface):
        try:
            unknown = element.GetCurrentPattern(pattern_id)
            return unknown.QueryInterface(interface) if unknown else None
        except self._errors:
            return None

    def _value_pattern(self, element):
        return self._pattern(element, self._api.UIA_ValuePatternId, self._api.IUIAutomationValuePattern)

    def _text_pattern(self, element):
        return self._pattern(element, self._api.UIA_TextPatternId, self._api.IUIAutomationTextPattern)

    @staticmethod
    def _rect(element):
        r = element.CurrentBoundingRectangle
        return Rect(int(r.left), int(r.top), int(r.right), int(r.bottom))

    @staticmethod
    def _runtime_id(element):
        try:
            return "-".join(str(part) for part in element.GetRuntimeId())
        except Exception:
            return ""

    def _candidate(self, element, focused=False):
        rect = self._rect(element)
        value = self._value_pattern(element)
        read_only = False
        if value is not None:
            try:
                read_only = bool(value.CurrentIsReadOnly)
            except self._errors:
                read_only = False

        def text_of(name):
            try:
                return str(getattr(element, name) or "")
            except self._errors:
                return ""

        try:
            is_password = bool(element.CurrentIsPassword)
        except self._errors:
            is_password = True          # cannot tell: treat it as one and stay away
        return BoxCandidate(
            control_type=int(element.CurrentControlType), framework=text_of("CurrentFrameworkId"),
            aria=text_of("CurrentAriaProperties"), has_value=value is not None, read_only=read_only,
            has_text=self._text_pattern(element) is not None, focused=focused,
            left=rect.left, top=rect.top, width=rect.width, height=rect.height, is_password=is_password)

    # ------------------------------------------------------------ what calibration needs
    def capabilities(self):
        # Turn-completion events and reading a selection are not implemented yet.
        return frozenset({READ, FILL, CHAT_KEY, SIGNALS})

    def host_version(self):
        """Size and modification time of the Codex executable: changes when Codex is
        updated. Known once a Codex window has been seen. No COM involved."""
        return self._host_version

    def _note_host(self, hwnd):
        """Work out which Codex this window belongs to. Runs before anything inside
        the window is looked at: the permission to read is tied to this version.
        Keyed on window AND process — an updated Codex is a new process, even if a
        window handle happens to be reused."""
        pid = win32.window_pid(hwnd)
        if (hwnd, pid) == self._versioned_hwnd:
            return
        self._versioned_hwnd = (hwnd, pid)
        self._box = self._document = None       # nothing found in another process is carried over
        self._last_good = None
        try:
            path = win32.process_path(pid)
            stat = os.stat(path)
            self._host_version = f"{os.path.basename(path).casefold()}:{stat.st_size}:{int(stat.st_mtime)}"
        except OSError:
            self._host_version = ""

    def box_signature(self):
        if self._box is None or not self._hwnd:
            return None
        try:
            window = win32.window_rect(self._hwnd)
            return self._candidate(self._box).signature(window.width if window else 0)
        except Exception:
            return None

    def set_signature(self, signature):
        self.profile = self.profile.with_signature(signature)
        self._box = None

    def record_structure(self):
        """Editable and document elements of the Codex window: types, patterns,
        relative positions and text lengths. Never any text."""
        if self._uia is None or not win32.is_usable_window(self._hwnd):
            return []
        api, nodes = self._api, []
        window = win32.window_rect(self._hwnd)
        if window is None or window.empty:
            return []
        root = self._uia.ElementFromHandle(self._hwnd)
        for type_id in (api.UIA_EditControlTypeId, api.UIA_DocumentControlTypeId):
            condition = self._uia.CreatePropertyCondition(api.UIA_ControlTypePropertyId, type_id)
            found = root.FindAll(api.TreeScope_Descendants, condition)
            for index in range(min(int(found.Length), 40)):
                element = found.GetElement(index)
                try:
                    box = self._candidate(element)
                    try:
                        name_len = len(str(element.CurrentName or ""))
                    except self._errors:
                        name_len = 0
                    text = None if box.is_password else self._read(element)
                    nodes.append({
                        "role": box.control_type, "framework": box.framework,
                        "multiline": "multiline=true" in box.aria.casefold().replace(" ", ""),
                        "patterns": [n for n, on in (("value", box.has_value), ("text", box.has_text)) if on],
                        "read_only": box.read_only, "password": box.is_password,
                        "focused": self._box is not None and bool(self._uia.CompareElements(element, self._box)),
                        "box": [round((box.left - window.left) / window.width, 3),
                                round((box.top - window.top) / window.height, 3),
                                round((box.left + box.width - window.left) / window.width, 3),
                                round((box.top + box.height - window.top) / window.height, 3)],
                        "name_len": name_len, "value_len": len(text) if text is not None else None})
                except self._errors:
                    continue
        return nodes

    def _read(self, element):
        """The draft in the box, or None if it cannot be read."""
        text = None
        pattern = self._text_pattern(element)
        if pattern is not None:
            try:
                text = pattern.DocumentRange.GetText(-1)
            except self._errors:
                text = None
        if text is None:
            value = self._value_pattern(element)
            if value is not None:
                try:
                    text = value.CurrentValue
                except self._errors:
                    text = None
        if text is None:
            return None

        def label(name):
            try:
                return str(getattr(element, name) or "")
            except self._errors:
                return ""

        return self.profile.draft_from(str(text), label("CurrentName"), label("CurrentHelpText"))

    # ------------------------------------------------------------ finding things
    def _is_codex(self, hwnd):
        pid = win32.window_pid(hwnd)
        if pid not in self._names:
            if len(self._names) > 200:
                self._names.clear()
            self._names[pid] = win32.process_name(pid)
        return self.profile.is_codex(self._names[pid], win32.window_title(hwnd))

    def _current_window(self):
        """The Codex window to follow now, or 0. Our own menus and dialogs coming
        to the front do not count as leaving Codex."""
        hwnd = win32.foreground_window()
        if hwnd and win32.window_pid(hwnd) == os.getpid():
            return self._hwnd if self._front and win32.is_usable_window(self._hwnd) else 0
        if hwnd and self._is_codex(hwnd):
            self._hwnd, self._front = hwnd, True
            return hwnd
        self._front = False
        return 0

    def _find_box(self, hwnd, window, now):
        pid = win32.window_pid(hwnd)
        try:
            focused = self._uia.GetFocusedElement()
            if focused and focused.CurrentProcessId == pid:
                candidate = self._candidate(focused, focused=True)
                if self.profile.accepts(candidate, window.width):
                    self._box, self._box_hwnd = focused, hwnd
                    return focused, candidate
        except self._errors:
            pass
        if self._box is not None and self._box_hwnd == hwnd:
            try:
                candidate = self._candidate(self._box)
                if candidate.width > 0 and candidate.height > 0:
                    return self._box, candidate
            except self._errors:
                pass
            self._box = None
        if now - self._searched_at < self._search_wait:
            return None, None
        self._searched_at = now
        self._search_wait = min(SEARCH_SLOWEST, self._search_wait * 2)      # reset when a box is found
        api = self._api
        root = self._uia.ElementFromHandle(hwnd)
        condition = self._uia.CreatePropertyCondition(api.UIA_ControlTypePropertyId, api.UIA_EditControlTypeId)
        found = root.FindAll(api.TreeScope_Descendants, condition)
        elements = [found.GetElement(i) for i in range(min(int(found.Length), 40))]
        candidates = []
        for element in elements:
            try:
                candidates.append(self._candidate(element))
            except self._errors:
                candidates.append(BoxCandidate())
        index = self.profile.pick(candidates, window.width)
        if index is None:
            return None, None
        self._search_wait = SEARCH_EVERY
        self._box, self._box_hwnd = elements[index], hwnd
        return elements[index], candidates[index]

    def _frame_rect(self, box, box_rect, window, runtime_id):
        """The composer frame around the box (it holds the send button). The bubble
        keeps clear of it."""
        if self._frame is not None and self._frame[0] == runtime_id and self._frame[2] == box_rect:
            return self._frame[1]
        frame = box_rect
        try:
            walker = self._uia.ControlViewWalker
            element = box
            for _ in range(4):
                element = walker.GetParentElement(element)
                if not element:
                    break
                rect = self._rect(element)
                if rect.empty or rect.height > 0.5 * window.height or rect.width > window.width:
                    break
                if rect.width >= frame.width and rect.height >= frame.height:
                    frame = rect
        except self._errors:
            pass
        self._frame = (runtime_id, frame, box_rect)
        return frame

    def _chat_parts(self, hwnd, now, fresh=False):
        """(title, page address, selected item names) — what tells chats apart."""
        api = self._api
        title = win32.window_title(hwnd)
        url = ""
        try:
            if self._document is None or self._document_hwnd != hwnd:
                root = self._uia.ElementFromHandle(hwnd)
                condition = self._uia.CreatePropertyCondition(
                    api.UIA_ControlTypePropertyId, api.UIA_DocumentControlTypeId)
                self._document = root.FindFirst(api.TreeScope_Descendants, condition) or None
                self._document_hwnd = hwnd
            if self._document is not None:
                value = self._value_pattern(self._document)
                url = page_address(str(value.CurrentValue or "")) if value is not None else ""
        except self._errors:
            self._document = None
        if url and not fresh:
            return title, url, self._selected       # the address is the key; no tree walk needed
        if fresh or now - self._selected_at >= SELECTED_EVERY:
            self._selected_at = now
            try:
                root = self._uia.ElementFromHandle(hwnd)
                condition = self._uia.CreatePropertyCondition(api.UIA_SelectionItemIsSelectedPropertyId, True)
                request = self._uia.CreateCacheRequest()
                request.AddProperty(api.UIA_NamePropertyId)
                found = root.FindAllBuildCache(api.TreeScope_Descendants, condition, request)
                names = [str(found.GetElement(i).CachedName or "") for i in range(min(int(found.Length), 8))]
                self._selected = tuple(name for name in names if name)
            except self._errors:
                self._selected = ()
        return title, url, self._selected

    @staticmethod
    def _chat_key(title, url, selected):
        # A page address that changes with the chat is the best key there is.
        parts = [url] if url else [title, *selected]
        return hashlib.sha1("\x1f".join(parts).encode("utf-8", "replace")).hexdigest()[:16]

    def _box_id(self, hwnd, box, text, rect):
        """Identity of the input box. Normally the UI Automation runtime id; if that
        turns out to change while nothing else does, a coarser id is used instead so
        that results are not thrown away for no reason."""
        runtime_id = self._runtime_id(box)
        previous = self._last_ids
        if previous and previous[0] != runtime_id and previous[1] == text and previous[2] == rect:
            self._id_flips += 1
            if self._id_flips >= 3:
                self._coarse_ids = True
        self._last_ids = (runtime_id, text, rect)
        return f"{hwnd}:box" if self._coarse_ids or not runtime_id else runtime_id

    # ------------------------------------------------------------ snapshot
    def snapshot(self, now):
        try:
            snap = self._snapshot(now)
            self._failures = 0
            self._last_good = snap
            return snap
        except Exception as error:      # a COM hiccup must not close the bubble
            self._failures += 1
            self._box = self._document = None
            if not self.may_read():
                self._last_good = None  # an earlier look may hold a draft: never replay it
            if self._last_good is not None and self._failures <= TOLERATED_FAILURES:
                return replace(self._last_good, at=now)
            if self._failures == TOLERATED_FAILURES + 1:        # say it once, not four times a second
                log.warning("cannot read the Codex window: %s: %s", type(error).__name__, error)
            # Still next to Codex, just blind: keep the pet there so paste mode is at hand.
            try:
                if self._front and win32.is_usable_window(self._hwnd):
                    return Snapshot(at=now, foreground=True, window_id=str(self._hwnd),
                                    window_rect=win32.window_rect(self._hwnd), problem="error")
            except OSError:
                pass
            return Snapshot(at=now, foreground=False, problem="error")

    def _snapshot(self, now, fresh=False):
        hwnd = self._current_window()
        if not hwnd:
            return Snapshot(at=now, foreground=False, problem="no_window")
        self._note_host(hwnd)
        window = win32.window_rect(hwnd)
        if window is None or window.empty:
            return Snapshot(at=now, foreground=False, problem="no_window")
        # Asked HERE, after the host's version is known and before anything inside the
        # window is touched — never earlier, never handed in by a caller: a permission
        # worked out before _note_host could belong to the previous Codex version.
        if not self.may_read():
            # Not calibrated (or calibrated for another Codex version): where the window
            # is, and nothing more. No UI Automation call is made into the window — the
            # input box is not searched for, its text and the page address are not fetched.
            self._box = self._document = None
            self._last_ids = None
            return Snapshot(at=now, foreground=True, window_id=str(hwnd), window_rect=window,
                            problem="uncalibrated")
        chat_key = self._chat_key(*self._chat_parts(hwnd, now, fresh))
        box, candidate = self._find_box(hwnd, window, now)
        if box is None:
            return Snapshot(at=now, foreground=True, window_id=str(hwnd), chat_key=chat_key,
                            window_rect=window, problem="no_box")
        text = self._read(box)
        box_rect = Rect.at(candidate.left, candidate.top, candidate.width, candidate.height)
        if text is None:
            return Snapshot(at=now, foreground=True, window_id=str(hwnd), chat_key=chat_key,
                            window_rect=window, box_rect=box_rect, problem="unreadable")
        box_id = self._box_id(hwnd, box, text, box_rect)
        composing = bool(win32.visible_windows_of_class(self.profile.ime_classes))
        return Snapshot(at=now, foreground=True, window_id=str(hwnd), chat_key=chat_key, box_id=box_id,
                        draft=text, composing=composing, window_rect=window, box_rect=box_rect,
                        send_rect=self._frame_rect(box, box_rect, window, box_id))

    def signals(self):
        hwnd = self._hwnd
        if not (self._front and win32.is_usable_window(hwnd)):
            return None
        self._note_host(hwnd)           # which Codex is this now? — before the permission is asked
        if not self.may_read():
            return None
        api = self._api
        try:
            root = self._uia.ElementFromHandle(hwnd)
            condition = self._uia.CreatePropertyCondition(api.UIA_ControlTypePropertyId, api.UIA_TextControlTypeId)
            request = self._uia.CreateCacheRequest()
            request.AddProperty(api.UIA_NamePropertyId)
            found = root.FindAllBuildCache(api.TreeScope_Descendants, condition, request)
            count = int(found.Length)
            texts = []
            for index in range(max(0, count - self.profile.max_text_nodes), count):
                name = found.GetElement(index).CachedName
                if name:
                    texts.append(str(name))
            title, url, selected = self._chat_parts(hwnd, time.monotonic(), fresh=True)
            return ChatSignals(title=title, selected=selected, texts=tuple(texts), url=url)
        except self._errors as error:
            log.info("cannot collect the window text: %s", error)
            return None

    # ------------------------------------------------------------ fill
    @staticmethod
    def _same(current, wanted):
        if current is None:
            return False
        if strict(current) == strict(wanted):
            return True
        a, b = loose(current), loose(wanted)
        return bool(a) and bool(b) and similarity(a, b) >= 0.98      # editors may tidy the text

    def _wait_for(self, box, wanted, timeout):
        deadline = time.monotonic() + timeout
        while True:
            if self._same(self._read(box), wanted):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)

    def _focus(self, hwnd, box):
        """Keyboard focus on the box, in a foreground Codex window."""
        try:
            if win32.foreground_window() != hwnd:
                win32.set_foreground(hwnd)
                time.sleep(0.08)
            box.SetFocus()
            time.sleep(0.03)
            if win32.foreground_window() != hwnd:
                return False
            return bool(self._uia.CompareElements(self._uia.GetFocusedElement(), box))
        except self._errors:
            return False

    def focus_codex(self):
        if self._box is not None and self._hwnd:
            self._focus(self._hwnd, self._box)

    def _fill_value(self, box, text):
        value = self._value_pattern(box)
        try:
            if value is None or value.CurrentIsReadOnly:
                return False, "unsupported"
            value.SetValue(text)
        except self._errors:
            return False, "failed"
        return (True, "") if self._wait_for(box, text, 0.6) else (False, "failed")

    def _fill_paste(self, hwnd, box, text, key=None, expect=None):
        if win32.modifiers_held():
            return False, "keys_held"
        kind, saved, before = (win32.clipboard_capture() if text else ("empty", None, 0))
        if text and kind in ("other", "busy"):
            return False, "clipboard_busy"         # would destroy something that cannot be put back
        if not self._focus(hwnd, box):
            return False, "focus"
        temporary = None
        if text:
            swapped, temporary = win32.clipboard_begin_paste(before, text, rollback=(kind, saved))
            if swapped != "written":
                return False, "clipboard_restore" if swapped == "lost" else "clipboard_busy"
        ok, why = False, "not_received"
        try:
            if key is not None:
                just_before = self._snapshot(time.monotonic(), fresh=True)
                if ((just_before.window_id, just_before.chat_key, just_before.box_id) != tuple(key)
                        or just_before.draft is None or strict(just_before.draft) != strict(expect)):
                    why = "mismatch"
            if text:
                if why == "mismatch":
                    sent = False
                elif not self._focus(hwnd, box):
                    why = "focus"
                    sent = False
                elif not win32.clipboard_paste_current(temporary):
                    # Another copy may have arrived while focus was being set.
                    # Never paste that content into the user's draft.
                    why = "clipboard_busy"
                    sent = False
                else:
                    sent = win32.send_shortcuts((win32.VK_CONTROL, win32.VK_A),
                                                (win32.VK_CONTROL, win32.VK_V))
            else:
                sent = (False if why == "mismatch" else
                        win32.send_shortcuts((win32.VK_CONTROL, win32.VK_A), (win32.VK_DELETE,)))
            if sent:
                ok = self._wait_for(box, text, 1.2)
                why = "" if ok else "not_received"
            elif why not in ("focus", "clipboard_busy", "mismatch"):
                why = "send_input"
        finally:
            if temporary is not None:
                time.sleep(0.15)                   # let the paste finish reading the clipboard
                restored, _ = win32.clipboard_end_paste(
                    temporary, saved if kind == "text" else None)
                if restored in ("busy", "lost"):
                    log.warning("temporary paste clipboard could not be restored")
                    why = "clipboard_restore"
        return ok, why

    def _write(self, hwnd, box, text, expect, key):
        """Try the ways of writing in the profile's order. Returns (method, reason)."""
        try:
            framework = str(box.CurrentFrameworkId or "")
        except self._errors:
            framework = ""
        reason = "failed"
        for method in self.profile.fill_order(framework):
            ok, why = (self._fill_value(box, text) if method == "value" else
                       self._fill_paste(hwnd, box, text, key=key, expect=expect))
            if ok:
                return method, why
            reason = why or reason
            # An attempted paste may still arrive late, and a changed draft may be
            # the user's new edit. Never start a second write in either case.
            if method == "paste" and why not in ("keys_held", "clipboard_busy", "focus"):
                break
            again = self._snapshot(time.monotonic(), fresh=True)
            if ((again.window_id, again.chat_key, again.box_id) != tuple(key)
                    or again.draft is None or strict(again.draft) != strict(expect)):
                break
        return "", reason

    def fill(self, key, expect, text):
        try:
            # The box is read to make sure it still is the one, and still holds the text,
            # that was meant. That needs the same permission as any other read: a
            # calibration for this Codex version, or a calibration step in progress
            # (`authorized()`). On an uncalibrated version nothing is read or written —
            # the look comes back without a draft and the fill is a mismatch.
            snap = self._snapshot(time.monotonic(), fresh=True)
            current = (snap.window_id, snap.chat_key, snap.box_id)
            if (not snap.foreground or snap.draft is None or current != tuple(key)
                    or strict(snap.draft) != strict(expect)):
                return FillResult(False, "mismatch")
            hwnd, box = self._hwnd, self._box
            method, reason = self._write(hwnd, box, text, expect, key)
            if not method:
                # The editor may have accepted the write but reformatted it. Do not
                # blindly replay the old draft over a change made by the user.
                after = self._snapshot(time.monotonic(), fresh=True)
                same_box = (after.window_id, after.chat_key, after.box_id) == tuple(key)
                if same_box and after.draft is not None and self._same(after.draft, text):
                    method = "paste"
            if not method:
                return FillResult(False, reason)
            self._focus(hwnd, box)                 # the cursor goes back to the input box
            return FillResult(True, reason=reason, method=method)
        except Exception as error:
            log.warning("fill failed: %s", error)
            return FillResult(False, "error")

    # ------------------------------------------------------------ placeholder learning
    def raw_box_text(self):
        if self._box is not None and self._hwnd and win32.is_usable_window(self._hwnd):
            self._note_host(self._hwnd)     # forgets the box if this is another Codex process
        if self._box is None or not self.may_read():
            return None
        try:
            bare = replace(self.profile, placeholders=())
            pattern = self._text_pattern(self._box)
            text = pattern.DocumentRange.GetText(-1) if pattern is not None else None
            if text is None:
                value = self._value_pattern(self._box)
                text = value.CurrentValue if value is not None else None
            return None if text is None else bare.draft_from(str(text))
        except self._errors:
            return None

    def set_placeholders(self, texts):
        self.profile = replace(self.profile, placeholders=tuple(strict(t) for t in texts if strict(t)))

    # ------------------------------------------------------------ self-check
    def probe(self, try_fill=False):
        """What works on this machine. Contains sizes, counts and yes/no answers —
        never the draft, the chat or any window text."""
        report = {"client": self.name, "process_names": list(self.profile.process_names)}
        try:
            front = win32.foreground_window()
            if front and win32.window_pid(front) != os.getpid():
                report["foreground_process"] = win32.process_name(win32.window_pid(front))
            hwnd = self._hwnd if win32.is_usable_window(self._hwnd) else 0
            report["window"] = bool(hwnd)
            if not hwnd:
                return report
            now = time.monotonic()
            window = win32.window_rect(hwnd)
            title, url, selected = self._chat_parts(hwnd, now, fresh=True)
            report["address"] = {"found": bool(url), "scheme": url.split(":", 1)[0][:12] if ":" in url else "",
                                 "has_thread_id": bool(thread_id_in(url))}
            report["selected_items"] = len(selected)
            signals = self.signals()
            report["text_nodes"] = len(signals.texts) if signals else 0
            self._searched_at = 0.0
            box, candidate = self._find_box(hwnd, window, now)
            report["box"] = box is not None
            if box is None:
                return report
            report["box_info"] = {
                "control_type": candidate.control_type, "framework": candidate.framework,
                "value_pattern": candidate.has_value, "text_pattern": candidate.has_text,
                "read_only": candidate.read_only, "multiline_hint": "multiline=true" in candidate.aria.casefold(),
                "width": candidate.width, "height": candidate.height}
            first_id = self._runtime_id(box)
            text = self._read(box)
            report["readable"] = text is not None
            report["draft_chars"] = len(text) if text is not None else None
            time.sleep(0.3)
            again, _ = self._find_box(hwnd, window, time.monotonic())
            report["box_id_stable"] = bool(first_id) and again is not None and self._runtime_id(again) == first_id
            report["coarse_box_id"] = self._coarse_ids
            report["ime_windows_now"] = win32.visible_windows_of_class(self.profile.ime_classes)
            report["ime_like_windows"] = win32.ime_like_windows()
            if try_fill and text is not None:
                report["fill"] = self._probe_fill(hwnd, box, text)
        except Exception as error:
            report["error"] = f"{type(error).__name__}: {error}"[:200]
        return report

    def _probe_fill(self, hwnd, box, original):
        """Write a marker with each method and put the original back. Opt-in only."""
        results = {}
        for method in ("paste", "value"):
            write = self._fill_paste if method == "paste" else (lambda h, b, t: self._fill_value(b, t))
            ok, why = write(hwnd, box, FILL_MARKER)
            entry = {"write": ok, "reason": why}
            if not self._same(self._read(box), original):
                back, back_why = write(hwnd, box, original)
                if not back:                        # whatever happened, do not leave the marker behind
                    other = self._fill_value if method == "paste" else None
                    back = other(box, original)[0] if other else self._fill_paste(hwnd, box, original)[0]
                entry["restore"] = back
            results[method] = entry
        results["draft_intact"] = self._same(self._read(box), original)
        self._focus(hwnd, box)
        return results
