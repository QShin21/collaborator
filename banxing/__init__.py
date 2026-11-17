"""伴行 (Banxing) — a companion that walks beside Codex on Windows.

The learner writes a rough idea in the Codex input box; after a short pause the
companion offers three routes (one marked as the main line), or only what the draft
is missing, or stays quiet — depending on the level the learner is at. One click
puts the composed prompt into the input box. The learner acts first and always
decides what is sent.

Layout of the package (see CLAUDE.md for the rules that go with it):

  core       textutil, drafts, levels, lint, follow, suggest,   pure logic, no I/O
             aboutme (weights), calibration (rules), geometry
  model      llm, mock, prompts/, evalkit/                       one configurable service
  context    context/                                            the current Codex chat
  hosts      hosts/                                              the host app: Windows UI Automation,
                                                                 the in-memory stand-in, structure replay
  shell      controller, ui/, cli, evidence, store, config       threads, Tk, files, entry points
"""

__version__ = "0.3.0"
APP_NAME = "Banxing"
APP_ID = "Banxing"
