"""Test rig: a simulated Codex, the offline stand-in model, a recording view and a
controllable clock, wired through the real Controller.

Everything here is synthetic. Passing these tests shows the app's own logic does
what the brief asks; it does not show anything about a real Codex window."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import safety  # noqa: F401  (keeps tests away from real user data)

from banxing import follow, llm, suggest
from banxing.config import Settings
from banxing.controller import Controller, InlineExecutor, View
from banxing.hosts.simulated import SimulatedCodex
from banxing.store import Store

STEP = 0.25


class RecordingView(View):
    def __init__(self):
        self.bubble = None
        self.state = ("away", "")
        self.states = []
        self.hidden = []
        self.copied = []
        self.offers = []
        self.notices = []
        self.paste_requests = 0
        self.previews = []
        self.questions = []
        self.last_snap = None

    def status(self, state, detail):
        self.state = (state, detail)
        self.states.append(state)

    def follow(self, snap):
        self.last_snap = snap

    def show_bubble(self, bubble, snap):
        self.bubble = bubble

    def hide_bubble(self, reason):
        self.bubble = None
        self.hidden.append(reason)

    def copy(self, text):
        self.copied.append(text)

    def offer_copy(self, text, reason):
        self.offers.append((text, reason))

    def notice(self, kind, detail=""):
        self.notices.append(kind)

    def ask_paste(self):
        self.paste_requests += 1

    def need_preview(self, index):
        self.previews.append(index)

    def ask(self, kind):
        self.questions.append(kind)


class Rig:
    def __init__(self, hold=False, environ=None, context=None, complete=None, client=None, calibrated=True,
                 capabilities=None):
        self.folder = Path(tempfile.mkdtemp(prefix="banxing-test-"))
        self.now = 1000.0
        self.codex = client or SimulatedCodex()
        self.view = RecordingView()
        self.settings = Settings(self.folder, environ={"BANXING_PROVIDER": "mock"} if environ is None else environ)
        self.store = Store(self.folder, wall=lambda: self.now)
        self.executor = InlineExecutor(hold=hold)
        self.calls = []

        def counting(config, system, user, timeout=30.0, meta=None, **more):
            self.calls.append({"user": user, "system": system, "meta": dict(meta or {})})
            return (complete or llm.complete)(config, system, user, timeout=timeout, meta=meta)

        self.suggester = suggest.Suggester(self.settings.model_config, context=context, complete=counting,
                                           clock=lambda: self.now, permit=self.settings.consented,
                                           about=lambda: self.controller.for_request())
        # Most tests are about what happens once this machine is calibrated; those about
        # the calibration itself pass calibrated=False and start with nothing switched on.
        self.controller = Controller(self.codex, self.suggester, self.view, self.settings, self.store,
                                     executor=self.executor, clock=lambda: self.now,
                                     capabilities=capabilities if capabilities is not None else (
                                         self.codex.capabilities() if calibrated else None))

    # ------------------------------------------------------------ time
    def tick(self, seconds=STEP):
        self.now += seconds
        self.controller.on_snapshot(self.codex.snapshot(self.now))

    def wait(self, seconds):
        for _ in range(int(round(seconds / STEP))):
            self.tick()

    # ------------------------------------------------------------ the user
    def type(self, text, then_wait=0.0):
        self.codex.type(text)
        self.tick()
        if then_wait:
            self.wait(then_wait)

    def pause_on(self, text):
        """Type a draft and stop for long enough to be analysed."""
        self.type(text, then_wait=2.0)

    def send(self):
        text = self.codex.send()
        self.tick()
        return text

    # ------------------------------------------------------------ reading results
    @property
    def machine(self):
        return self.controller.machine

    @property
    def level(self):
        return self.machine.levels.level

    def feedback(self):
        return [f.outcome for f in self.machine.levels.recent]

    def events(self, name=None):
        events = self.store.events()
        return [e for e in events if name is None or e["event"] == name]

    def rows(self):
        return [(r.approach, r.deliverable) for r in self.view.bubble.routes] if self.view.bubble else []

    def close(self):
        shutil.rmtree(self.folder, ignore_errors=True)


class RigCase(unittest.TestCase):
    def rig(self, **options):
        rig = Rig(**options)
        self.addCleanup(rig.close)
        return rig


# ------------------------------------------------------------ synthetic Codex sessions
def session_lines(thread_id, cwd, turns, finished_last=True, with_events=True):
    """Lines of a rollout .jsonl in the current Codex layout. `turns` is a list of
    (user_text, answer_text) pairs."""
    stamp = "2026-11-17T03:00:00.000Z"
    lines = [{"timestamp": stamp, "type": "session_meta",
              "payload": {"id": thread_id, "timestamp": stamp, "cwd": cwd, "originator": "codex_desktop"}},
             {"timestamp": stamp, "type": "response_item", "payload": {
                 "type": "message", "role": "user",
                 "content": [{"type": "input_text", "text": "<environment_context>\n  <cwd>%s</cwd>\n</environment_context>" % cwd}]}}]
    for index, (user, answer) in enumerate(turns):
        last = index == len(turns) - 1
        lines.append({"timestamp": stamp, "type": "event_msg", "payload": {"type": "task_started"}})
        lines.append({"timestamp": stamp, "type": "response_item", "payload": {
            "type": "message", "role": "user", "content": [{"type": "input_text", "text": user}]}})
        if with_events:
            lines.append({"timestamp": stamp, "type": "event_msg",
                          "payload": {"type": "user_message", "message": user, "kind": "plain"}})
        lines.append({"timestamp": stamp, "type": "response_item", "payload": {
            "type": "reasoning", "summary": [{"type": "summary_text", "text": "thinking about it"}]}})
        lines.append({"timestamp": stamp, "type": "response_item", "payload": {
            "type": "function_call", "name": "shell", "arguments": "{\"command\": [\"ls\"]}", "call_id": "c1"}})
        if with_events:
            lines.append({"timestamp": stamp, "type": "event_msg",
                          "payload": {"type": "agent_message", "message": "我先看一下项目里的文件。"}})
        lines.append({"timestamp": stamp, "type": "response_item", "payload": {
            "type": "message", "role": "assistant", "phase": "commentary",
            "content": [{"type": "output_text", "text": "我先看一下项目里的文件。"}]}})
        if answer is None:
            continue
        if with_events:
            lines.append({"timestamp": stamp, "type": "event_msg",
                          "payload": {"type": "agent_message", "message": answer}})
        lines.append({"timestamp": stamp, "type": "response_item", "payload": {
            "type": "message", "role": "assistant", "phase": "final_answer",
            "content": [{"type": "output_text", "text": answer}]}})
        if with_events and (finished_last or not last):
            lines.append({"timestamp": stamp, "type": "event_msg",
                          "payload": {"type": "task_complete", "last_agent_message": answer}})
    return [json.dumps(line, ensure_ascii=False) for line in lines]


def write_session(root, thread_id, cwd, turns, mtime=None, **options):
    folder = Path(root) / "2026" / "11" / "17"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"rollout-2026-11-17T03-00-00-{thread_id}.jsonl"
    path.write_text("\n".join(session_lines(thread_id, cwd, turns, **options)) + "\n", encoding="utf-8")
    if mtime is not None:
        import os
        os.utime(path, (mtime, mtime))
    return path


THREAD_A = "0199aaaa-1111-7222-8333-444455556666"
THREAD_B = "0199bbbb-1111-7222-8333-444455556666"
