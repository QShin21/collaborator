"""Thin ctypes wrappers over the few Win32 calls the pet needs. Windows only.

Kept deliberately small and explicit: every function declares its argument and
return types (pointer-sized values are truncated otherwise on 64-bit Windows).

STATUS: this file cannot run on the Linux machine it was written on. The CI job
on windows-latest imports it and exercises the parts that need no interactive
desktop; behaviour next to a real Codex window is listed in docs/verification.md.
"""
from __future__ import annotations

import sys

if sys.platform != "win32":
    raise ImportError("banxing.hosts.win32 is only available on Windows")

import ctypes
import logging
import os
import time
from ctypes import wintypes

from ..geometry import Rect

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

ULONG_PTR = ctypes.c_size_t
LONG_PTR = ctypes.c_ssize_t

# ------------------------------------------------------------------ signatures
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowRect.restype = wintypes.BOOL
user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsWindow.restype = wintypes.BOOL
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.IsIconic.argtypes = [wintypes.HWND]
user32.IsIconic.restype = wintypes.BOOL
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetAncestor.restype = wintypes.HWND
# 32-bit user32 exports only the non-"Ptr" names.
_get_window_long = getattr(user32, "GetWindowLongPtrW", None) or user32.GetWindowLongW
_set_window_long = getattr(user32, "SetWindowLongPtrW", None) or user32.SetWindowLongW
_get_window_long.argtypes = [wintypes.HWND, ctypes.c_int]
_get_window_long.restype = LONG_PTR
_set_window_long.argtypes = [wintypes.HWND, ctypes.c_int, LONG_PTR]
_set_window_long.restype = LONG_PTR
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.SetWindowPos.restype = wintypes.BOOL
user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
user32.MonitorFromWindow.restype = wintypes.HANDLE
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.OpenClipboard.restype = wintypes.BOOL
user32.CloseClipboard.restype = wintypes.BOOL
user32.EmptyClipboard.restype = wintypes.BOOL
user32.CountClipboardFormats.restype = ctypes.c_int
user32.EnumClipboardFormats.argtypes = [wintypes.UINT]
user32.EnumClipboardFormats.restype = wintypes.UINT
user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
user32.GetClipboardOwner.restype = wintypes.HWND
user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
user32.GetClipboardData.argtypes = [wintypes.UINT]
user32.GetClipboardData.restype = wintypes.HANDLE
user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
user32.SetClipboardData.restype = wintypes.HANDLE
kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalLock.restype = wintypes.LPVOID
kernel32.GlobalSize.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalSize.restype = ctypes.c_size_t
kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalUnlock.restype = wintypes.BOOL
kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalFree.restype = wintypes.HGLOBAL
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                ctypes.POINTER(wintypes.DWORD)]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = wintypes.HANDLE

_EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.argtypes = [_EnumWindowsProc, wintypes.LPARAM]
user32.EnumWindows.restype = wintypes.BOOL


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MONITORINFO)]
user32.GetMonitorInfoW.restype = wintypes.BOOL


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT
user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   wintypes.HWND, wintypes.HANDLE, wintypes.HANDLE, wintypes.LPVOID]
user32.CreateWindowExW.restype = wintypes.HWND
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.DestroyWindow.restype = wintypes.BOOL

# ------------------------------------------------------------------ constants
GWL_EXSTYLE = -20
WS_EX_TOPMOST, WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x00000008, 0x00000080, 0x08000000
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE, SWP_FRAMECHANGED = 0x0001, 0x0002, 0x0010, 0x0020
HWND_TOPMOST = wintypes.HWND(-1)
GA_ROOT = 2
MONITOR_DEFAULTTONEAREST = 2
CF_TEXT, CF_OEMTEXT, CF_UNICODETEXT, CF_LOCALE = 1, 7, 13, 16
GMEM_MOVEABLE = 0x0002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
INPUT_KEYBOARD, KEYEVENTF_KEYUP = 1, 0x0002
VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN, VK_RWIN = 0x10, 0x11, 0x12, 0x5B, 0x5C
VK_A, VK_V, VK_DELETE, VK_END = 0x41, 0x56, 0x2E, 0x23
DWMWA_EXTENDED_FRAME_BOUNDS, DWMWA_CLOAKED = 9, 14
ERROR_ALREADY_EXISTS = 183


# ------------------------------------------------------------------ windows
def foreground_window():
    return user32.GetForegroundWindow() or 0


def window_pid(hwnd):
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def process_path(pid):
    """Full path of a process's executable; "" if unknown."""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return ""
        return buffer.value
    finally:
        kernel32.CloseHandle(handle)


def process_name(pid):
    """Executable file name of a process ("codex.exe"), lower-cased; "" if unknown."""
    return os.path.basename(process_path(pid)).casefold()


def window_title(hwnd):
    length = user32.GetWindowTextLengthW(hwnd)
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


def window_class(hwnd):
    buffer = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buffer, 256)
    return buffer.value


def _dwm_attribute(hwnd, attribute, value):
    try:
        dwmapi = ctypes.WinDLL("dwmapi")
        dwmapi.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD]
        dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long
        return dwmapi.DwmGetWindowAttribute(hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value)) == 0
    except (OSError, AttributeError):
        return False


def window_rect(hwnd):
    """Visible bounds of a window (without the invisible resize border)."""
    rect = wintypes.RECT()
    if not _dwm_attribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, rect):
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
    return Rect(rect.left, rect.top, rect.right, rect.bottom)


def work_area(hwnd):
    """The usable area of the monitor a window is on."""
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(MONITORINFO)
    monitor = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
    if monitor and user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        work = info.rcWork
        return Rect(work.left, work.top, work.right, work.bottom)
    return Rect(0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1))


def is_usable_window(hwnd):
    return bool(hwnd and user32.IsWindow(hwnd) and user32.IsWindowVisible(hwnd) and not user32.IsIconic(hwnd))


def set_foreground(hwnd):
    return bool(user32.SetForegroundWindow(hwnd))


def top_level(hwnd):
    return user32.GetAncestor(hwnd, GA_ROOT) or hwnd


def make_overlay(hwnd, activate=False):
    """Turn a window into a floating tool window that stays on top and — unless
    `activate` — never takes the keyboard when shown or clicked."""
    style = _get_window_long(hwnd, GWL_EXSTYLE)
    style |= WS_EX_TOPMOST | WS_EX_TOOLWINDOW
    if not activate:
        style |= WS_EX_NOACTIVATE
    _set_window_long(hwnd, GWL_EXSTYLE, style)
    user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_FRAMECHANGED)


def enable_dpi_awareness():
    """Per-monitor DPI awareness, so our coordinates match UI Automation's."""
    try:
        user32.SetProcessDpiAwarenessContext.argtypes = [wintypes.HANDLE]
        user32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
        if user32.SetProcessDpiAwarenessContext(wintypes.HANDLE(-4)):     # PER_MONITOR_AWARE_V2
            return True
    except (AttributeError, OSError):
        pass
    try:
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
        return True
    except (AttributeError, OSError):
        return False


def single_instance(name):
    """True if this is the first running copy. The mutex lives as long as the process."""
    ctypes.set_last_error(0)
    handle = kernel32.CreateMutexW(None, False, name)
    return bool(handle) and ctypes.get_last_error() != ERROR_ALREADY_EXISTS


# ------------------------------------------------------------------ input methods
def visible_windows_of_class(class_names):
    """Class names (from `class_names`) of top-level windows that are really on
    screen right now. Used to tell whether an input method is showing candidates."""
    wanted = {name.casefold(): name for name in class_names}
    found = []

    def visit(hwnd, _):
        try:
            name = window_class(hwnd)
            if name.casefold() in wanted and user32.IsWindowVisible(hwnd):
                cloaked = wintypes.DWORD(0)
                _dwm_attribute(hwnd, DWMWA_CLOAKED, cloaked)
                rect = wintypes.RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(rect))
                if not cloaked.value and rect.right > rect.left and rect.bottom > rect.top:
                    found.append(name)
        except OSError:
            pass
        return True

    callback = _EnumWindowsProc(visit)
    user32.EnumWindows(callback, 0)
    return found


def ime_like_windows():
    """For the self-check: class names of visible top-level windows that look like
    input-method windows, so unknown IMEs can be added to the profile."""
    found = []

    def visit(hwnd, _):
        try:
            if user32.IsWindowVisible(hwnd):
                name = window_class(hwnd)
                folded = name.casefold()
                if any(word in folded for word in ("ime", "candidate", "pinyin", "comp", "sopy")):
                    found.append(name)
        except OSError:
            pass
        return True

    callback = _EnumWindowsProc(visit)
    user32.EnumWindows(callback, 0)
    return sorted(set(found))


# ------------------------------------------------------------------ keyboard
def modifiers_held():
    """Is the user physically holding a modifier? Synthetic chords would combine with it."""
    return any(user32.GetAsyncKeyState(key) & 0x8000 for key in (VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN, VK_RWIN))


def _key(vk, up=False):
    item = INPUT()
    item.type = INPUT_KEYBOARD
    item.ki = KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP if up else 0, 0, 0)
    return item


def send_shortcuts(*chords):
    """Inject complete shortcuts in one batch, with no gap for another keystroke.

    A failed SendInput is reported, not mistaken for a successful paste. This
    helper deliberately accepts only the three editor actions we use; it can
    never synthesize Enter or an arbitrary key supplied by a caller.
    """
    allowed = ((VK_CONTROL, VK_A), (VK_CONTROL, VK_V), (VK_DELETE,))
    if not chords or any(tuple(chord) not in allowed for chord in chords):
        raise ValueError("unsupported shortcut")
    events = []
    for chord in chords:
        events.extend(_key(vk) for vk in chord)
        events.extend(_key(vk, up=True) for vk in reversed(chord))
    array = (INPUT * len(events))(*events)
    inserted = user32.SendInput(len(events), array, ctypes.sizeof(INPUT))
    if inserted == len(events):
        return True
    if 0 < inserted < len(events):
        # If Windows accepted only a prefix, release exactly the keys that our
        # prefix left down. Never re-send the destructive shortcut itself.
        held = []
        for event in events[:inserted]:
            vk = event.ki.wVk
            if event.ki.dwFlags & KEYEVENTF_KEYUP:
                if vk in held:
                    held.remove(vk)
            else:
                held.append(vk)
        if held:
            releases = [_key(vk, up=True) for vk in reversed(held)]
            cleanup = (INPUT * len(releases))(*releases)
            if user32.SendInput(len(releases), cleanup, ctypes.sizeof(INPUT)) != len(releases):
                logging.getLogger(__name__).warning("partial SendInput left a key-release unconfirmed")
    return False


def send_keys(*chord):
    """One permitted editor shortcut (kept for existing callers)."""
    return send_shortcuts(chord)


# ------------------------------------------------------------------ clipboard
class _RestorableText(str):
    """Unicode text with the exact CF_LOCALE payload that accompanied it."""

    def __new__(cls, value, locale):
        result = str.__new__(cls, value)
        result.locale = locale
        return result


def _open_clipboard(attempts=8, owner=None):
    for _ in range(attempts):
        if user32.OpenClipboard(owner):
            return True
        time.sleep(0.03)
    return False


def _clipboard_snapshot_open():
    """("empty", None) | ("text", str) | ("other", None) | ("busy", None).
    "other" means the clipboard holds something that could not be put back."""
    count = user32.CountClipboardFormats()
    if count == 0:
        return "empty", None
    if count < 0:
        return "busy", None
    # Windows synthesizes CF_TEXT and CF_OEMTEXT for CF_UNICODETEXT and may add
    # CF_LOCALE when the clipboard closes. Any other format (HTML, image, file
    # list, etc.) would be destroyed by a text-only restoration, so do not
    # borrow it. Unicode must be first: a synthesized conversion is not a copy.
    formats, previous = [], 0
    for _ in range(count):
        previous = user32.EnumClipboardFormats(previous)
        if not previous:
            return "other", None
        formats.append(previous)
    # Only the observed Unicode-first conversion order is accepted. This
    # rejects many extra native formats, but cannot prove that CF_TEXT/OEMTEXT
    # were synthesized rather than explicitly supplied with identical IDs.
    if formats not in ([CF_UNICODETEXT], [CF_UNICODETEXT, CF_TEXT, CF_OEMTEXT],
                       [CF_UNICODETEXT, CF_LOCALE],
                       [CF_UNICODETEXT, CF_LOCALE, CF_TEXT, CF_OEMTEXT]):
        return "other", None
    locale = None
    if CF_LOCALE in formats:
        locale_handle = user32.GetClipboardData(CF_LOCALE)
        if not locale_handle:
            return "other", None
        size = kernel32.GlobalSize(locale_handle)
        # The value is an LCID DWORD. Retain any allocator padding byte-for-byte,
        # but reject malformed/unreasonably large handles rather than truncating.
        if not 4 <= size <= 64:
            return "other", None
        locale_pointer = kernel32.GlobalLock(locale_handle)
        if not locale_pointer:
            return "other", None
        try:
            locale = ctypes.string_at(locale_pointer, size)
        finally:
            kernel32.GlobalUnlock(locale_handle)
    handle = user32.GetClipboardData(CF_UNICODETEXT)
    if not handle:
        return "other", None
    pointer = kernel32.GlobalLock(handle)
    if not pointer:
        return "other", None
    try:
        value = ctypes.wstring_at(pointer)
        return "text", _RestorableText(value, locale) if locale is not None else value
    finally:
        kernel32.GlobalUnlock(handle)


def clipboard_capture():
    """Read content and its change counter under the same clipboard lock."""
    if not _open_clipboard():
        return "busy", None, 0
    try:
        kind, text = _clipboard_snapshot_open()
        sequence = user32.GetClipboardSequenceNumber()
        # Zero is also the initial sequence on a fresh window station. A write
        # below may probe it only while the clipboard is empty. A nonempty
        # zero-sequence snapshot cannot be safely identified after focus changes.
        if sequence == 0 and kind != "empty":
            return "busy", None, 0
        return kind, text, sequence
    finally:
        user32.CloseClipboard()


def clipboard_snapshot():
    kind, text, _ = clipboard_capture()
    return kind, text


def clipboard_diagnostic():
    """Content-free detail for a failed live compatibility check."""
    if not _open_clipboard(attempts=1):
        error = ctypes.get_last_error()  # preserve it before calling other APIs
        return {"open": False, "error": error,
                "sequence": user32.GetClipboardSequenceNumber(),
                "owner": bool(user32.GetClipboardOwner())}
    try:
        count = user32.CountClipboardFormats()
        formats = []
        previous = 0
        for _ in range(max(0, count)):
            previous = user32.EnumClipboardFormats(previous)
            if not previous:
                break
            formats.append(previous)
        result = {"open": True, "sequence": user32.GetClipboardSequenceNumber(),
                  "owner": bool(user32.GetClipboardOwner()),
                  "format_count": count, "format_ids": formats}
        if CF_LOCALE in formats:
            locale_handle = user32.GetClipboardData(CF_LOCALE)
            result["locale_size"] = kernel32.GlobalSize(locale_handle) if locale_handle else 0
    finally:
        user32.CloseClipboard()
    return result


def clipboard_set(text):
    """Put text on the clipboard; None empties it.

    The clipboard is opened with a short-lived owner window: with no owner,
    EmptyClipboard leaves the clipboard ownerless and SetClipboardData may fail.
    The window is destroyed right after, so nothing is ever sent to this thread.
    """
    owner = user32.CreateWindowExW(0, "STATIC", None, 0, 0, 0, 0, 0, None, None, None, None)
    try:
        return _clipboard_write(owner, text)[0] == "written"
    finally:
        if owner:
            user32.DestroyWindow(owner)


def clipboard_replace_if_sequence(sequence, text, rollback=None):
    """Replace only our own temporary clipboard value, never a newer user copy.

    ``rollback`` is the previously captured (kind, text). It is preallocated
    too, so if placing the new value fails after EmptyClipboard, the old value
    can be put back while still holding the clipboard lock.

    Returns (written | changed | busy | lost, new sequence number). ``lost``
    means even the rollback failed and must be reported to the user. The
    comparison, write and returned sequence share one clipboard lock.
    """
    owner = user32.CreateWindowExW(0, "STATIC", None, 0, 0, 0, 0, 0, None, None, None, None)
    try:
        return _clipboard_write(owner, text, sequence, rollback)
    finally:
        if owner:
            user32.DestroyWindow(owner)


def _clipboard_bytes_handle(data):
    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    if not handle:
        return 0
    pointer = kernel32.GlobalLock(handle)
    if not pointer:
        kernel32.GlobalFree(handle)
        return 0
    ctypes.memmove(pointer, data, len(data))
    kernel32.GlobalUnlock(handle)
    return handle


def _clipboard_handle(text):
    return None if text is None else _clipboard_bytes_handle(text.encode("utf-16-le") + b"\x00\x00")


def _clipboard_locale_handle(text):
    locale = getattr(text, "locale", None)
    return None if locale is None else _clipboard_bytes_handle(locale)


def _clipboard_write(owner, text, expected_sequence=None, rollback=None):
    if not owner:
        return "busy", 0
    handle = _clipboard_handle(text)
    locale_handle = _clipboard_locale_handle(text)
    rollback_kind, rollback_text = rollback if rollback is not None else (None, None)
    rollback_handle = _clipboard_handle(rollback_text) if rollback_kind == "text" else None
    rollback_locale_handle = _clipboard_locale_handle(rollback_text) if rollback_kind == "text" else None

    def free_pending():
        for pending in (handle, locale_handle, rollback_handle, rollback_locale_handle):
            if pending:
                kernel32.GlobalFree(pending)

    if any(pending == 0 for pending in (handle, locale_handle, rollback_handle, rollback_locale_handle)):
        free_pending()
        return "busy", 0
    if not _open_clipboard(owner=owner):
        free_pending()
        return "busy", 0

    def restore_original(needs_empty):
        # Called only after this transaction has emptied/changed the clipboard,
        # while the same lock is still held. Never claim success on partial locale.
        nonlocal rollback_handle, rollback_locale_handle
        if needs_empty and not user32.EmptyClipboard():
            return "lost"
        if rollback_kind == "empty":
            return "busy"
        if rollback_kind != "text" or not rollback_handle:
            return "lost"
        if not user32.SetClipboardData(CF_UNICODETEXT, rollback_handle):
            return "lost"
        rollback_handle = None
        if rollback_locale_handle:
            if not user32.SetClipboardData(CF_LOCALE, rollback_locale_handle):
                return "lost"
            rollback_locale_handle = None
        return "busy"

    status, sequence = "busy", 0
    try:
        if (expected_sequence is not None
                and user32.GetClipboardSequenceNumber() != expected_sequence):
            status = "changed"
        elif expected_sequence == 0 and (
                rollback_kind != "empty" or _clipboard_snapshot_open()[0] != "empty"):
            # With no advancing counter, another copy could otherwise replace
            # the initially empty clipboard before we acquire this lock.
            status = "changed"
        elif user32.EmptyClipboard():
            placed = text is None or bool(user32.SetClipboardData(CF_UNICODETEXT, handle))
            if placed:
                handle = None             # the system owns the memory now
                if locale_handle:
                    placed = bool(user32.SetClipboardData(CF_LOCALE, locale_handle))
                    if placed:
                        locale_handle = None
            status = "written" if placed else restore_original(needs_empty=handle is None)
        sequence = user32.GetClipboardSequenceNumber()  # before releasing the lock
        if status == "written" and expected_sequence is not None and sequence == expected_sequence:
            # On some desktops a zero counter is the fresh clipboard; on a
            # desktop where it never advances we cannot distinguish a later
            # user copy. Undo our swap *before* releasing the clipboard lock.
            status = restore_original(needs_empty=True)
            sequence = user32.GetClipboardSequenceNumber()
    finally:
        user32.CloseClipboard()
        free_pending()
    return status, sequence
