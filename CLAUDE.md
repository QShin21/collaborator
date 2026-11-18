# 伴行 (Banxing) — notes for Claude Code

Product: a small desktop app that has a DIFFERENT model watch one long conversation the user is
having with an AI model. A model goes on believing what it once said; over forty turns an
unchecked remark becomes a foundation and the user's question fades. Banxing keeps a ledger
outside the conversation (the user's question; what the work rests on, each entry marked by what
backs it), lets the supervising model go back, search and ask for a blind reading, and — only
when the thinking has gone astray and it bears on the direction NOW — shows one bubble with its
passages and a question the user can take back to the main model. It only reads. The user
decides the goal and every step.

This is a complete redo (2026-11-18). The draft-routes desk pet (v0.3) and everything that
belonged to it — its plans, evidence records and tests — are archived in `legacy/v0.3/`.

- `docs/design.md` — the design this code implements. Its "实施状态" block at the top says what
  is done and what is not; keep it true. Found the design wrong? Change the design first, say
  why, then the code.
- `docs/verification.md` — the evidence ledger, one row per work package (W-01 …). Always name
  the level (E0–E5); never write a bare "verified" / "tests pass" / "works"; results with the
  stand-in model are "plumbing only". **Never report a simulated test as a real-device or a
  real-model result.** Everything on Windows, on real conversations and with a real supervising
  model is E0 until a record exists in `docs/evidence/`.
- `docs/acceptance.md` — the handbook for whoever has real data, Windows or a model key.
- `legacy/` — archives: `v0.1/` (the first web workbench), `v0.3/` (the desk pet, self-contained:
  its own code, tests, docs and workflows). Not built, not tested from the root, not to be
  deleted, not to be edited. `legacy/v0.3/CLAUDE.md` is that version's note, not this one's.
- `docs/product-brief-original.md` — the founder's brief, if present in the working copy. **Never
  edit it.** It is git-ignored on purpose and never committed. Nothing that is published may
  quote it: where it is present, `tests/test_security.py` compares every publishable text file
  with it and fails on a shared run of 10+ characters — reword, do not raise the limit. Hidden
  `.*-task.txt` files are git-ignored too and are never committed.
- The founder's first vision is quoted verbatim at the end of README.md — do not reword it.

## Commands

- Test: `python3 -m unittest discover -s tests -v` (offline, synthetic data only)
- Self-check: `python3 run.py --selftest`
- One conversation: `BANXING_PROVIDER=mock python3 run.py --watch FILE` (`--list`,
  `--conversation ID`, `--from-turn N`, `--stepwise`)
- Replay of the labelled conversations: `BANXING_PROVIDER=mock python3 run.py --replay`
- Run: `python run.py`
- Release: bump `banxing/__init__.py`, update `docs/verification.md`, write
  `docs/release-notes/v<version>.md`, push a `v*` tag; `.github/workflows/release.yml` tests,
  builds with `packaging/windows/build.ps1` (PyInstaller, packaged self-test, zip, Inno Setup
  installer), checks the installer with `verify-installer.ps1` (silent install, run, reinstall,
  uninstall), publishes a pre-release and never overwrites one. CI runs the same build and check
  on every push. The coordinator pushes and releases — do not push or tag. `release.yml` itself
  has never run (0.4.0a1 went out through a one-time workflow); the PowerShell and Inno Setup
  files cannot be run in this Linux workspace — say so, never "tested".
- The installer never touches `%APPDATA%\Banxing` (settings, protected key, a kept ledger), on
  upgrade or uninstall. Its `AppId` never changes; `AppMutex` must stay the name `cli.run_app`
  takes. `banxing.iss` is UTF-8 with a byte-order mark — keep it (a test checks).

## Rules of the codebase

- **The main model's word does not back the main model's claim.** This is the rule the product
  turns on, and it lives in `watch/ledger.py`, not in a prompt. A reference counts only if its
  quotation is found in the turn it names (`transcript.locate`), and WHERE it was found — the
  user's words, the main model's words, or a record of something that ran — is set by the app,
  never taken from the supervising model. An anchor is "backed" only on a record or the user's
  words, else "unchecked", whatever label came back. A goal is "confirmed" only on the user's own
  words; a goal the user rewrote stays until the user moves it. "Broken" needs a record or the
  user; only the user retires an anchor; a concern is "resolved" only by a LATER record or the
  user. Entries are dropped or weakened, never strengthened. Do not loosen any of this to get
  more (or fewer) bubbles.
- **A finding must show what it claims** (`watch/audit.py`): title, what is off, what it bears
  on, a question to take back — all four — and located passages; drift quotes the user's goal,
  a contradiction quotes two places, an unchecked premise or an overreach quotes the main model.
  It bears on the direction only if it can quote a step from the latest turns that rests on it.
  Findings that fail are dropped one by one; an answer that is not a conclusion is an error.
- **The supervisor looks for itself, within bounds.** Re-reading turns, searching the
  conversation (locally — "nothing found" is an answer) and a blind reading that never sees the
  main model's words are the supervising model's own choices (`audit.Auditor.run`), at most
  `MAX_LOOKS` times per audit, never outside the range the user allowed, every request counted.
  Do not turn this into a fixed pipeline, and do not let the blind reading see `Turn.model`.
- **When a bubble may appear** is `watch/nudge.py` and nothing else: traceable, bearing on the
  direction, timely; once per concern-and-evidence; a kind the user closed, called intended,
  called wrong, took back or walked past stays away until `ledger.situation` changes. A look the
  user asked for always answers. An unusable audit is silence (or an error when asked), never a
  weaker bubble; a failed request is not repeated by itself.
- **It only reads, and only what was chosen.** No window of another application is read or
  written; nothing is typed anywhere (a test scans the package for key synthesis). The question
  for the main model goes to the clipboard when the user clicks. `sources` functions take an
  explicit path, thread or pasted text; nothing goes looking. A changed history under a ledger
  (`transcript.diverged_at`) stops everything until the user confirms what is watched.
- **Layers.** `watch/transcript.py`, `watch/ledger.py`, `watch/nudge.py`, `watch/session.py`,
  `lint.py`, `textutil.py`, `geometry.py`, `ui/viewmodel.py` are pure: no I/O, no threads, no
  clock of their own. Product rules go there and get a test. `watch/driver.py` only wires;
  `ui/app.py` only draws; `watch/sources.py` only reads what it is handed; `executor.py` is the
  only place that knows about threads.
- **A conversation is agreed to one at a time.** `Watcher.start(shown=…)` records the destination
  only if it is the one that was on screen and would really be used; the session carries it into
  every job and `Auditor.run` compares it with the configuration it is about to use — a changed
  service (environment overrides included) stops watching until the user agrees again. Turns
  before the first turn the user allowed are never sent, not even when the supervisor asks.
  The ledger window always shows what was sent (turns, characters, requests, destination). The
  general `Settings.consent` is only used by `--replay`.
- **Privacy.** `events.jsonl`, `banxing.log` and the reports of `--watch` / `--replay` never
  contain text of a conversation, the ledger or a finding: the event log accepts numbers,
  booleans and code-style names only (`store._NAME`), whatever a caller passes. The conversation
  is never copied: it is read again from where it is. The ledger (which quotes passages) lives
  in memory; on disk only when the user ticked it, encrypted (`vault/watch.bin`), deleted when
  watching ends.
- **Encrypted or not stored.** On Windows `secretbox.protect` raises when DPAPI fails; there is
  no readable fallback. Callers save nothing and say so (`model_error_encrypt`,
  `notice_watch_error_encrypt`; watching then goes on in memory).
- **Keys and the wire** (`tests/test_security.py`; keep every test there, loosen none). A saved
  API key is bound to the provider + endpoint it was entered for (`key_for`) and is never
  released for another one — not via the dialog, an edited settings file, or `BANXING_PROVIDER` /
  `BANXING_BASE_URL`. Redirects are never followed (`llm._NoRedirects`). One request per attempt:
  no automatic retry with a changed body. Endpoints are https except on loopback.
- **Chat content is data.** Whatever the supervising model returns passes `lint.check` against
  what was in the conversation: a foreign address, "send your key to…", download-and-run,
  "ignore previous instructions" → the whole answer is dropped, ledger included. An address
  counts as known only if `lint.destination` gives the same host and port — never by substring.
- **Assumptions about other products are data.** How a ChatGPT export, a Codex session, the App
  Server, a Claude Code session and a copied chat page look is written in the headers of
  `watch/sources.py` and `context/appserver.py`, each marked with what it was (not) checked
  against, and listed in `docs/verification.md`.
- **Never read real user data in tests.** Tests and `--selftest` build synthetic conversations in
  a temp folder. Every test module starts with `import safety`, which redirects `CODEX_HOME` /
  `BANXING_HOME` to an empty temp folder and clears `BANXING_*` model variables. Never put a real
  conversation in the repository; replay cases (`evalkit/replay_cases.py`) are written fresh, in
  pairs that differ in the one thing that matters.
- **The stand-in is plumbing.** `watch/standin.py` (through `mock.py`) is keyword rules written
  alongside the replay cases — "it gets every case right" says nothing. Rules are tested with
  scripted answers (`tests/watchrig.py`). The app never falls back to it silently.
- **Windows first.** Always pass `encoding="utf-8"` for file I/O, use `pathlib`, never assume a
  UTF-8 console. User files go under `config.home()` (`%APPDATA%\Banxing`), never next to the
  program. `winshell.py` is Windows-only and imported lazily; everything else must import and
  test on Linux. New Windows-side code says in its header that it has not been run on Windows.
- **Tk belongs to one thread, to the end.** The thread that creates the interpreter makes every
  Tk call, ends it (`PetApp.destroy`, which refuses other threads) and frees it there at once
  (`release_tk`): Tk objects sit in reference cycles, and if the cycle collector frees the
  interpreter from another thread, Tcl aborts the process (seen on the first Windows CI run of
  v0.3). Other threads only queue work (`_calls`, the executor inbox). Never use
  `variable.trace_add` directly — use `PetApp._watch`, which removes the trace when the window
  closes. Overlay windows get their no-activate style after being mapped once (`_prime`), every
  geometry change goes through `PetApp._move`, and paragraphs are wrapped with `PetApp._para`
  (Tk breaks lines at spaces only, which mangles Chinese).
- **Strings.** Every user-visible string goes through `t()` with `zh` and `en` in
  `ui/strings.py`. Model behaviour lives in `banxing/prompts/*.md` (re-read on every call); each
  has a `<!-- version: … -->` line — change the prompt, change the version (a test pins the
  digest).
- **Stored shapes have a schema number** (`config.SETTINGS_SCHEMA`, `ledger.LEDGER_SCHEMA`,
  `vault.WATCH_SCHEMA`). A file from a newer version is left alone, not guessed at, and not
  overwritten.
- **Dependencies.** Core: standard library only, Python 3.9+. Optional on Windows: `pystray` +
  `Pillow` (tray icon). PyInstaller is a build-time tool only. Ask before adding anything else.
