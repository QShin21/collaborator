"""The few Win32 calls the shell needs. Windows only.

Every function declares its argument and return types (pointer-sized values are
truncated otherwise on 64-bit Windows).

STATUS: these functions were taken unchanged from the v0.3 module that the Windows
CI runner imported and exercised (see legacy/v0.3 and docs/verification.md). THIS
file, as it stands now, has not been run on Windows: it cannot run on the Linux
machine it was put together on.
"""
from __future__ import annotations

import sys

if sys.platform != "win32":
    raise ImportError("banxing.winshell is only available on Windows")

import ctypes
from ctypes import wintypes

from .geometry import Rect

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LONG_PTR = ctypes.c_ssize_t

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
kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = wintypes.HANDLE


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MONITORINFO)]
user32.GetMonitorInfoW.restype = wintypes.BOOL

GWL_EXSTYLE = -20
WS_EX_TOPMOST, WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x00000008, 0x00000080, 0x08000000
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE, SWP_FRAMECHANGED = 0x0001, 0x0002, 0x0010, 0x0020
HWND_TOPMOST = wintypes.HWND(-1)
GA_ROOT = 2
MONITOR_DEFAULTTONEAREST = 2
ERROR_ALREADY_EXISTS = 183


def work_area(hwnd):
    """The usable area of the monitor a window is on."""
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(MONITORINFO)
    monitor = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
    if monitor and user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        work = info.rcWork
        return Rect(work.left, work.top, work.right, work.bottom)
    return Rect(0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1))


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
    """Per-monitor DPI awareness, so the windows are sharp and placed correctly."""
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
