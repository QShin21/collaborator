> **ARCHIVE.** These are the notes of v0.3 (the draft-routes desk pet), kept with its code. They are NOT instructions for
> this repository as it is now — see the CLAUDE.md at the repository root. Nothing under legacy/ is built, tested from the
> root, or to be changed.

# 伴行 (Banxing) — notes for Claude Code

Product: a companion that stays beside the Codex desktop app on Windows. The learner writes a
rough idea in the Codex input box; after a pause the companion — depending on the level the
learner is at — offers three routes (one marked as the main line), or only what the draft is
missing, or stays quiet. One click puts the composed prompt into the input box: the learner's
own words first, unchanged. The learner always acts first and always decides what is sent.

- `docs/product-brief-original.md` — the founder's brief. **Never edit it.** It is git-ignored on
  purpose: it stays in the working copy and is never committed, bundled or published. Nothing
  that is published may quote it: examples, prompt wording, test fixtures and docs are written
  fresh. Where the brief is present, `tests/test_security.py` compares every publishable text
  file with it and fails on a shared run of 10+ characters — reword, do not raise the limit.
  Hidden `.*-task.txt` files are git-ignored too and are never committed.
- `docs/banxing-generation2.md` — the plan this code implements. Its "实施状态" block at the top
  says what is done and what is not; keep it true. Follow its work packages (section 8), its stop
  line (8.3: nothing from M3 on) and its edge-case list (appendix D); keep the nine rules it lists
  in 2.2. Found the plan wrong? Change the plan first, say why, then the code.
- `docs/verification.md` — the evidence ledger, one row per work package. Use the plan's wording
  (7.1): always name the level (E0–E5); never write a bare "verified" / "tests pass" / "works";
  results with the stand-in model are "plumbing only". **Never report a simulated test as a
  real-device result.** Everything on Windows, on a real Codex and with a real model is E0 until
  a record exists in `docs/evidence/`.
- `docs/acceptance.md` — the real-device handbook (calibration, counted checks).
- `docs/product-design.md` — generation 1's design, kept as an archive. Do not update it.
- `legacy/v0.1/` — the first web workbench, archived. Not built, not tested, not to be deleted.
- The founder's first vision is quoted verbatim at the end of README.md — do not reword it.

## Commands

- Test: `python3 -m unittest discover -s tests -v` (offline; Windows-only tests skip elsewhere)
- Self-check: `python3 run.py --selftest` · Demo: `BANXING_PROVIDER=mock python3 run.py --demo "…"`
  (`--mode lead|hint|walk`) · Scenario checks: `BANXING_PROVIDER=mock python3 run.py --eval`
- Run: `python run.py` (only useful on Windows next to Codex)
- Release: bump `banxing/__init__.py`, update `docs/verification.md`, push a `v*` tag;
  `.github/workflows/release.yml` tests, builds with PyInstaller, self-tests the build, publishes.
  The coordinator pushes and releases — do not push or tag.

## Rules of the codebase

- **Layers.** `follow.py`, `levels.py`, `lint.py`, `drafts.py`, `geometry.py`, `textutil.py`,
  `ui/viewmodel.py`, the weight functions in `aboutme.py` and `hosts/replay.py` are pure: no
  I/O, no threads, no clock of their own. Product rules go there and get a test.
  `controller.py` only wires; `ui/app.py` only draws; `hosts/uia.py` only talks to Windows.
- **Three identities.** Nothing is shown or written unless chat, input box and draft all still
  match (`follow.py`; again inside `HostClient.fill`). This holds for "add one sentence" too.
  Never weaken this.
- **Never send.** The only keys ever synthesised are Ctrl+A, Ctrl+V and Delete. A test scans the
  source for anything else. The companion never presses Enter.
- **The user's words are not rewritten.** The model returns the tail only (`do`, `deliver`,
  `check`); `suggest.compose` puts the draft in front, character for character. A hint is added
  to the END of the draft (`suggest.append`).
- **Not calibrated, not following.** Abilities are `platform ∩ calibration` (`calibration.py`,
  `Controller.capabilities`). Without READ the input box is **not read at all**: the gate is in
  the host client (`HostClient.may_read`, default off; `snapshot()` then reports the window and
  nothing else) — not a discard further up. Only a check the user started reads without it,
  inside `client.authorized()` (a calibration step, the self-check). Without FILL a click
  copies. A calibration belongs to one host version and one app version. In `hosts/uia.py`
  the order is fixed: `_note_host` (which Codex is this) → `may_read()` → only then anything
  inside the window; never compute the permission earlier or pass it in. Fills use the same
  gate. `tests/test_uia_flow.py` runs the real `uia.py` over fake Win32/UIA to hold this (E1).
- **Calibration removes only its own marker.** The write test cleans up through
  `client.fill(original key, MARKER, "")` and nothing else; whatever is in front afterwards is
  never used as the thing to erase. Switched chat or edited text → leave it, tell the user.
- **Wrong context is worse than none.** `context/identify.py` answers "not sure" unless the
  evidence is clear; then the draft alone is used. Do not loosen it to raise the hit rate.
- **Silence over a generic bubble.** Invalid model output becomes an error state with retry, or
  silence — never a degraded bubble. Output that fails the safety gate (`lint.py`: foreign
  addresses, exfiltration, download-and-run, "ignore previous instructions") is dropped whole;
  destructive or privileged routes need the preview before they are written. An address counts
  as already known only if `lint.destination` gives the same host and port — never by substring;
  user-info, encoded hosts, backslashes and onward addresses in a query are foreign. The mock model is
  plumbing, not evidence, and is never a silent fallback.
- **Levels only get quieter by themselves.** Rule A (4 ignores in 6) steps one level down and
  can be undone; rule B (drafts usually ready) only asks; louder is the user's choice alone.
- **Privacy.** Drafts go only to the configured model service, after consent that names the
  effective origin (scheme, host, port — `BANXING_*` overrides included). Consent is recorded
  only for the destination that was on screen (`Settings.give_consent(shown)`), lapses when it
  changes, and is checked again where the request is made (`Suggester(permit=...)`) — every new
  path that sends a draft must pass it. `state.json`, `events.jsonl`, `calibration.json`,
  `banxing.log` and the evidence bundle never contain draft or chat text: the event log accepts
  numbers, booleans and code-style names only (`store._NAME`), whatever a caller passes; the
  bundle goes through `evidence.scrub`. "About me" facts are the one piece of user text kept on
  disk, encrypted (`vault/`). No trail of a task is stored. Window text collected to recognise
  the chat stays in memory.
- **Encrypted or not stored.** On Windows `secretbox.protect` raises when DPAPI fails; there is
  no readable fallback. Callers save nothing and show `model_error_encrypt` /
  `aboutme_error_encrypt`. (`tests/test_audit.py` holds these four audit regressions; keep them.)
- **Keys and the wire** (`tests/test_security.py`; keep every test there, loosen none). A saved
  API key is bound to the provider + endpoint it was entered for (`key_for`) and is never
  released for another one — not via the dialog, an edited settings file, or `BANXING_PROVIDER` /
  `BANXING_BASE_URL`. Redirects are never followed (`llm._NoRedirects`): urllib would forward the
  key. One request per attempt: no automatic retry with a changed body (an HTTP 400 is an error,
  not a cue to resend). Endpoints are https except on loopback.
- **Assumptions about Codex are data.** They live in `hosts/profile.py` and
  `context/appserver.py`/`rollout.py`. Anything not checked on a real Codex is marked as such
  in the file header and in `docs/verification.md`. New Windows-side code says in its header
  that it has not been run on Windows.
- **Never read real user data in tests.** Tests and `--selftest` build synthetic sessions in a
  temp folder and pass an explicit `sessions_dir` / `use_appserver=False`. Never point them at
  `~/.codex`. Every test module starts with `import safety`, which redirects `CODEX_HOME` /
  `BANXING_HOME` to an empty temp folder and clears `BANXING_*` model variables.
- **Windows first.** Always pass `encoding="utf-8"` for file I/O, use `pathlib`, never assume a
  UTF-8 console. User files go under `config.home()` (`%APPDATA%\Banxing`), never next
  to the program. Windows-only modules (`hosts/win32.py`, `hosts/uia.py`) are imported
  lazily; everything else must import and test on Linux. comtypes is first imported on the
  watcher thread (see `uia.open`), never on the Tk thread.
- **Tk belongs to one thread, to the end.** The thread that creates the interpreter makes every
  Tk call, ends it (`PetApp.destroy`, which refuses other threads) and frees it there at once
  (`release_tk`): Tk objects sit in reference cycles, and if the cycle collector frees the
  interpreter from another thread, Tcl aborts the process (first Windows CI run, EV-2026-001).
  Other threads only queue work (`_calls`, the executor inbox). Never use `variable.trace_add`
  directly — use `PetApp._watch`, which removes the trace when the window closes (a trace
  keeps the whole interpreter alive otherwise). Code that builds windows outside PetApp
  destroys them and collects before any other thread starts. `tests/test_tk_lifecycle.py`.
- **Tk.** Overlay windows get their no-activate style after being mapped once (`_prime`), and
  every geometry change goes through `PetApp._move` (see its comment). Wrap paragraphs with
  `PetApp._para` — Tk breaks lines at spaces only, which mangles Chinese.
- **Strings.** Every user-visible string goes through `t()` with `zh` and `en` in
  `ui/strings.py`. Model behaviour lives in `banxing/prompts/*.md` (re-read on every call); each
  has a `<!-- version: … -->` line — change the prompt, change the version (a test pins the
  digest).
- **Data files have a schema number** (`store.STATE_SCHEMA`, `config.SETTINGS_SCHEMA`). Changing
  a file's shape means a migration in `store.MIGRATIONS` and a test; a file from a newer version
  is left alone, not guessed at.
- **Dependencies.** Core: standard library only, Python 3.9+. Windows runtime: `comtypes`
  (required for UI Automation), `pystray` + `Pillow` (optional tray icon). PyInstaller is a
  build-time tool only. Ask before adding anything else.
- Every model task needs a branch in `mock.py` so tests stay offline.
