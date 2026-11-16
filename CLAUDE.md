# AI Collaborator — notes for Claude Code

Product: a collaborator for problem-solving of authentic challenges. The learner always acts
first; the AI scaffolds ill-structured problems into well-structured ones (breadth, threads,
main/side lines, the learner chooses) and floats fading "bubbles" whose feedback tunes the
agent's agency and preferences. The founder's original brief is quoted verbatim in README.md —
do not reword it.

## Commands

- Run: `python3 run.py` (http://127.0.0.1:8765; mock model when no API key)
- Test: `python3 -m unittest discover -s tests -v`
- Offline comparison: `python3 eval/run_eval.py [--agents ...] [--turns N]`

- Release (Windows is the primary platform): bump `collab/__init__.py`, push a `v*` tag;
  `.github/workflows/release.yml` builds `AI-Collaborator.exe` with PyInstaller and publishes
  the zip to GitHub Releases. PyInstaller is a build-time tool only.

## Rules of the codebase

- Windows first: always pass `encoding="utf-8"` for file I/O, use `pathlib`, never assume a
  UTF-8 console. Bundled resources are read from `config.ROOT`; anything written (`.env`,
  `data/`) goes under `config.HOME` — in the packaged build these differ.

- Security: every request passes `Handler._refusal` (Host allow-list against DNS rebinding,
  Origin, Sec-Fetch-Site). Never compare Origin with Host — under rebinding they agree. Routes
  that touch credentials use `@route(..., local_only=True)`. A saved API key must never follow
  a changed endpoint (`config.save_settings`). Keep the tests for these.
- Bubble lifetime is owned by JS (`startLife`/`stopLife` in `web/app.js`) and only runs while
  the learner can see the bubble; "fade" feedback must never be recorded for an unseen bubble.
- In `web/app.js` use `fill(el, ...)` rather than `el.replaceChildren(...)` when passing arrays
  or possibly-null children.
- Standard library only, Python 3.9+. No build step for `web/`. Keep it that way unless asked.
- All model behaviour lives in `prompts/*.md` (re-read on every call). Adaptation maths lives in
  `collab/adapt.py` (pure functions). Engine logic must stay callable without HTTP.
- Database changes: append to `MIGRATIONS` in `collab/db.py`; never edit an existing entry.
- Every model task needs a branch in `collab/mock.py` so tests stay offline. The mock must not
  favour any agent — it is plumbing, not evidence.
- UI strings go through `t()` with both `zh` and `en` entries in `web/i18n.js`.
- New comparison agent: add to `AGENTS` in `collab/engine.py`, add its prompt, add `agent_<name>`
  strings.
- Design stance to preserve: offer options, never impose; do not scaffold a well-structured
  question; silence is better than a generic bubble; the learner can see and override
  everything the agent has learned about them.
