"""Codex's own data, read-only: what a conversation with it looked like.

  model      conversion helpers for the app-server JSON
  appserver  `codex app-server`: thread list, paged turn history
  rollout    the session .jsonl files: locating them, reading their lines

Used by watch/sources.py to read the conversation the user chose. Nothing here
starts, resumes or changes a thread.
"""
