"""Reading the Codex chat the user is looking at.

  model      Turn / ThreadInfo / ChatSignals and conversion from both sources
  appserver  preferred source: `codex app-server` (thread/list, thread/turns/list)
  rollout    compatibility source: the session .jsonl files
  identify   which stored thread is the chat on screen — or "not sure"
  extract    the few excerpts of the last three finished turns worth sending
  provider   ties them together; the only thing the rest of the app uses

Everything here is read-only. When the chat cannot be identified with confidence
the provider returns nothing and the pet works from the draft alone: a wrong
chat's context is worse than none.
"""
from .model import ChatSignals, ContextExcerpt, ThreadInfo, Turn  # noqa: F401
from .provider import ContextProvider  # noqa: F401
