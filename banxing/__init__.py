"""伴行 (Banxing) — an outside reader for a long conversation with an AI model.

A model that has said something goes on believing it. Over a long conversation an
unverified remark becomes a foundation, the user's question fades, and a tired user
says "go on". Banxing is a small desktop app that follows ONE conversation the user
chose and has a DIFFERENT model watch its line of thought:

  * it keeps a ledger outside the conversation — the user's question, and what the
    work rests on, each entry marked by what backs it (a record, the user's word, or
    only the main model's own word);
  * the supervising model works with agency: it goes back to re-read, searches the
    whole conversation, and can ask for a blind reading that never saw what the main
    model said;
  * when the thinking has gone astray — drift from the question, an unchecked
    premise carrying new work, a conclusion beyond its evidence, a contradiction, a
    path narrowed too early, polishing in place — it shows one bubble with its
    passages and a question the user can take back to the main model;
  * otherwise it says nothing.

It only reads. The user decides the goal and every step.

Layout of the package (see CLAUDE.md for the rules that go with it):

  watching   watch/: transcript, ledger, nudge, session (pure);   one conversation
             sources, audit, vault, driver
  model      llm, mock, prompts/, evalkit/                        one configurable service
  codex      context/                                             Codex's own data, read-only
  shell      ui/, cli, executor, store, config, secretbox         threads, Tk, files, entry points
"""

__version__ = "0.4.0a2"
APP_NAME = "Banxing"
APP_ID = "Banxing"
