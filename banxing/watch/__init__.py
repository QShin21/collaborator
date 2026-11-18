"""Watching one long conversation with a different model (docs/design.md).

  transcript  a conversation as numbered turns: what the user said, what the main
              model said, what actually happened (commands, outputs, files); finding
              a quoted passage; searching; rendering with or without the main
              model's words. Pure.
  sources     reading the conversation the user chose: a copied chat page, a ChatGPT
              export, a Codex session or App Server thread, a Claude Code session, a
              transcript file
  ledger      what the conversation rests on, kept outside it — the user's question,
              the anchors and what backs each one, what became of every concern that
              was raised. Decides by itself what is backed and what was only said. Pure.
  audit       one audit by the supervising model, in steps it chooses itself: look
              back, search, ask for a blind reading, conclude; and what of its
              conclusion may stand
  nudge       when a bubble may appear, and when it may not. Pure.
  session     one watched conversation over time: when an audit is due, what it is
              given, what the user answered. Pure; the caller runs the model call.
  vault       the ledger on disk — encrypted, and only if the user asked for it
  driver      wiring for the app: threads, the view, the files
  standin     keyword rules in place of a model, for tests and demos only

The user decides the goal and every step. The main model does the work. This package
reads, and says something only when the thinking has gone astray; it writes nothing
into any application.

STATUS: everything here was written and run on Linux only, against synthetic
conversations, with scripted answers or the offline stand-in in place of a
supervising model. It has not been run on Windows, not against a real export or a
real session, and no real model has read the prompts. See docs/verification.md.
"""
