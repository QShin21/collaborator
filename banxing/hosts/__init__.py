"""Talking to the Codex window.

  base       the interface the rest of the app sees (snapshot / signals / fill)
  simulated  an in-memory Codex for tests and the console demo
  profile    how to recognise Codex and its input box — data, tunable without code
  win32      thin ctypes helpers (windows, clipboard, key input, DPI, IME)
  uia        the Windows implementation on UI Automation

Only `uia` and `win32` are Windows-specific; they are imported lazily so that
everything else runs (and is tested) on any platform.
"""
from __future__ import annotations

import sys

from .base import CodexClient, FillResult, NullClient  # noqa: F401


def create_client(settings=None):
    """The client for this machine: UI Automation on Windows, otherwise a stub that
    sees nothing (the pet then offers paste mode and the built-in examples)."""
    if sys.platform == "win32":
        try:
            from .uia import WindowsCodexClient
            from .profile import Profile
            extra = (settings.get("process_names") if settings is not None else None) or []
            placeholders = (settings.get("placeholders") if settings is not None else None) or []
            return WindowsCodexClient(Profile.default(extra_process_names=extra, placeholders=placeholders))
        except Exception as error:          # missing comtypes, COM failure, ...
            return NullClient(reason=f"uia_unavailable: {error}")
    return NullClient(reason="not_windows")
