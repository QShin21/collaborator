"""Entry points.

  Banxing.exe                 run the pet
  ... --selftest [--report F] [--require tk,uia]
                                      check that this build works on this machine;
                                      exit code 0 only if the required checks pass
  ... --probe [--fill] [--wait N] [--report F]
                                      compatibility report for the Codex window in
                                      front (switch to it within N seconds)
  ... --demo "draft" [--mode critical] [--agree ORIGIN]
                                      print three suggestions for a draft; sends it
                                      only to a destination that has been agreed to
  ... --eval [--limit N] [--report F] run the evaluation scenarios through the configured
                                      model and check the structure of every answer
  ... --summary                       numbers from the local trial log
  ... --version
"""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import sys
import tempfile
import threading
import time
from pathlib import Path

from . import APP_ID, __version__, calibration, examples, levels, suggest
from .config import Settings, home
from .store import Store

WIN = sys.platform == "win32"
log = logging.getLogger("banxing")


def setup_logging(folder):
    """A small rotating log in the data folder. Draft and chat text are never logged."""
    try:
        folder.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            folder / "banxing.log", maxBytes=500_000, backupCount=1, encoding="utf-8")
    except OSError:
        handler = logging.NullHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])


def emit(text):
    """Print for a human. A windowed build has no console; then this is a no-op."""
    try:
        print(text)
    except (OSError, ValueError, UnicodeError):
        try:
            print(text.encode("ascii", "replace").decode("ascii"))
        except (OSError, ValueError):
            pass


def write_report(path, data):
    if path:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ------------------------------------------------------------------ run
def build(settings, store, view, client, executor_factory, context=None, capabilities=None):
    """Wire the application. Shared by the real run and the self-test (which passes
    its own `context` so that it never looks at the user's Codex data).
    `capabilities=None` means: whatever the stored calibration allows."""
    from .context import ContextProvider
    from .controller import Controller
    if context is None:
        context = ContextProvider(codex_command=settings.get("codex_command") or "",
                                  sessions_dir=settings.get("sessions_dir") or "")
    holder = {}
    suggester = suggest.Suggester(settings.model_config, context=context, permit=settings.consented,
                                  timeout=settings.number("model_timeout_seconds", 5.0, 120.0),
                                  about=lambda: holder["controller"].for_request())
    executor = executor_factory(lambda snap: holder["controller"].on_snapshot(snap))
    controller = Controller(client, suggester, view, settings, store, executor=executor, context=context,
                            capabilities=capabilities)
    holder["controller"] = controller
    return controller, executor, context


def calibrate_simulated(codex, controller):
    """Walk the calibration against the in-memory Codex, playing the user's part.
    Used by the self-test and the tests; returns the stored entry."""
    results, memo = {}, {}
    for step, _, _ in calibration.STEPS:
        if step == "read":
            codex.type(calibration.PHRASE)
        elif step in ("empty", "fill"):
            codex.type("")
        elif step == "focus":
            codex.type("x")
        calibration.prepare(codex, step, memo)
        if step == "chat":
            codex.type("")
            codex.switch_chat(codex.chat + "-other")
        if step == "sessions":
            codex.visible.setdefault(codex.chat, ["…"])
        results[step] = calibration.check(codex, step, memo, sleep=lambda seconds: None)
        if step == "chat":
            codex.switch_chat(codex.chat[:-len("-other")])
    return controller.finish_calibration(results, memo)


def run_pet():
    from .ui import strings
    settings = Settings()
    setup_logging(settings.folder)
    strings.set_language(settings.get("language"))
    if WIN:
        from .hosts import win32
        win32.enable_dpi_awareness()
        if not win32.single_instance("Local\\" + APP_ID + "-Banxing"):
            import tkinter
            from tkinter import messagebox
            root = tkinter.Tk()
            root.withdraw()
            messagebox.showinfo(strings.t("app_name"), strings.t("already_running"))
            return 0
    from .controller import ThreadedExecutor
    from .hosts import create_client
    from .ui.app import PetApp
    store = Store(settings.folder)
    client = create_client(settings)
    app = PetApp(settings, store)
    controller, executor, context = build(
        settings, store, app, client,
        lambda on_snapshot: ThreadedExecutor(client, on_snapshot, poll=settings.number("poll_seconds", 0.1, 2.0)))
    app.attach(controller, executor)
    if client.name == "none":
        app.pinned = True                      # nothing to follow: stay reachable for paste mode
        log.warning("no Codex client: %s", getattr(client, "reason", ""))
    store.log("start", {"client": client.name})
    executor.start()
    if controller.setup_reason():
        app.root.after(700, app.open_model)    # first run: say where drafts go, ask for the model
    else:
        app.root.after(700, app.greet)         # says so if this machine is not calibrated yet
    try:
        app.run()
    finally:
        executor.stop()
        context.close()
        store.log("stop")
    return 0


# ------------------------------------------------------------------ self-test
def _check_core(folder):
    from .controller import Controller, InlineExecutor, View
    from .hosts.simulated import SimulatedCodex

    class Seen(View):
        bubble = None

        def show_bubble(self, bubble, snap):
            self.bubble = bubble

        def hide_bubble(self, reason):
            self.bubble = None

    codex, view = SimulatedCodex(), Seen()
    settings = Settings(folder, environ={"BANXING_PROVIDER": "mock"})
    clock = {"now": 100.0}
    suggester = suggest.Suggester(settings.model_config)
    controller = Controller(codex, suggester, view, settings, Store(folder), executor=InlineExecutor(),
                            clock=lambda: clock["now"])
    draft = examples.get("furniture")["draft"]

    def pause_on(text):
        codex.type(text)
        for _ in range(10):
            clock["now"] += 0.25
            controller.on_snapshot(codex.snapshot(clock["now"]))

    pause_on(draft)
    if view.bubble is not None:
        raise RuntimeError("the input box was read without a calibration")
    entry = calibrate_simulated(codex, controller)
    if entry["verdict"] != "passed":
        raise RuntimeError(f"calibration against the simulator: {entry['verdict']}")
    pause_on("")
    pause_on(draft)
    if view.bubble is None or len(view.bubble.routes) != 3:
        raise RuntimeError("no bubble after the pause")
    prompt = view.bubble.routes[0].prompt
    if not prompt.startswith(draft):
        raise RuntimeError("the prompt does not start with the user's own words")
    controller.pick(0)
    if codex.draft != prompt or codex.sent:
        raise RuntimeError("the click did not fill the prompt")
    if [f.outcome for f in controller.machine.levels.recent] != [levels.ADOPT]:
        raise RuntimeError("the adoption was not counted")
    return {"rows": [r.approach for r in suggest.from_example("furniture").routes],
            "capabilities": entry["capabilities"]}


def _check_prompts(folder):
    versions = {}
    for mode in (levels.LEAD, levels.HINT):
        text, version = suggest.system_prompt(mode)
        if "JSON" not in text or len(text) < 500 or not version:
            raise RuntimeError("a prompt file is missing, truncated or has no version line")
        versions[mode] = version
    return versions


def _check_context(folder):
    from .context import ChatSignals, ContextProvider
    sessions = Path(folder) / "sessions" / "2026" / "01" / "01"
    sessions.mkdir(parents=True, exist_ok=True)
    thread = "00000000-0000-4000-8000-000000000001"
    lines = [
        {"type": "session_meta", "payload": {"id": thread, "cwd": "C:\\demo"}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": "自检：帮我做一个示例页面"}},
        {"type": "event_msg", "payload": {"type": "agent_message", "message": "自检：已生成 demo.html。\n\n页面使用示例数据。"}},
        {"type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": "自检：已生成 demo.html。\n\n页面使用示例数据。"}},
    ]
    (sessions / f"rollout-2026-01-01T00-00-00-{thread}.jsonl").write_text(
        "\n".join(json.dumps(line, ensure_ascii=False) for line in lines) + "\n", encoding="utf-8")
    provider = ContextProvider(sessions_dir=str(Path(folder) / "sessions"), use_appserver=False)
    signals = ChatSignals(texts=("自检：帮我做一个示例页面", "自检：已生成 demo.html。", "页面使用示例数据。"))
    found = provider.excerpt(signals, "把页面改成深色")
    if found is None or found.thread_id != thread:
        raise RuntimeError("the synthetic chat was not recognised")
    return {"turns": found.turns, "source": found.source}


def _check_secretbox(folder):
    from . import secretbox
    stored = secretbox.protect("self-test-value")
    if secretbox.reveal(stored) != "self-test-value":
        raise RuntimeError("stored secret could not be read back")
    return {"encrypted": secretbox.is_encrypted(stored)}


def _check_tk(folder):
    from .controller import InlineExecutor
    from .hosts.simulated import SimulatedCodex
    from .ui import strings
    from .ui.app import PetApp
    strings.set_language("zh")
    settings = Settings(Path(folder) / "ui", environ={"BANXING_PROVIDER": "mock"})
    codex = SimulatedCodex()
    app = PetApp(settings, Store(settings.folder), use_tray=False)
    try:
        from .context import ContextProvider
        empty = ContextProvider(sessions_dir=str(Path(folder) / "no-sessions"), use_appserver=False)
        controller, _, context = build(settings, Store(settings.folder), app, codex,
                                       lambda on_snapshot: InlineExecutor(), context=empty,
                                       capabilities=codex.capabilities())
        app.attach(controller, None)
        codex.type(examples.get("software")["draft"])
        now = 10.0
        for _ in range(10):
            now += 0.25
            controller.clock = lambda now=now: now
            controller.on_snapshot(codex.snapshot(now))
            app.root.update()
        if app.bubble is None:
            raise RuntimeError("the bubble was not shown")
        app._toggle_preview(0)
        app.root.update()
        size = (app.bubble_window.winfo_width(), app.bubble_window.winfo_height())
        visible = bool(app.bubble_window.winfo_viewable()) and bool(app.pet.winfo_viewable())
        app.status("error", "timeout")
        app.offer_copy("x", "failed")
        app._build_menu(__import__("banxing.ui.viewmodel", fromlist=["x"]).menu_items(controller.menu()))
        for opener in (app.open_model, app.open_paste, app.open_probe, app.open_aboutme, app.open_evidence,
                       app.open_calibration):
            opener()
            app.root.update()
        for window in list(app.dialog_windows):
            window.destroy()
        app.root.update()
        controller.pick(0)
        app.root.update()
        if codex.draft == examples.get("software")["draft"]:
            raise RuntimeError("clicking a row did not fill the prompt")
        if not visible or min(size) < 40:
            raise RuntimeError(f"the bubble is not on screen (size {size})")
        context.close()
        return {"bubble": list(size), "scale": round(app.scale, 2)}
    finally:
        app.destroy()


def _check_win32(folder):
    import ctypes
    import os
    from .hosts import win32
    name = win32.process_name(os.getpid())
    if not name.endswith(".exe"):
        raise RuntimeError(f"unexpected process name {name!r}")
    front = win32.foreground_window()
    return {"process": name, "input_size": ctypes.sizeof(win32.INPUT),
            "foreground": win32.process_name(win32.window_pid(front)) if front else "",
            "clipboard": win32.clipboard_snapshot()[0], "ime_like": len(win32.ime_like_windows())}


def _check_uia(folder):
    """UI Automation must load and answer — on its own thread, as in the real run."""
    result = {}

    def work():
        try:
            from .hosts.profile import Profile
            from .hosts.uia import WindowsCodexClient
            client = WindowsCodexClient(Profile.default())
            client.open()
            snap = client.snapshot(time.monotonic())
            result["ok"] = {"foreground": snap.foreground, "problem": snap.problem}
            client.close()
        except Exception as error:
            result["error"] = f"{type(error).__name__}: {error}"
    thread = threading.Thread(target=work, name="uia-check")
    thread.start()
    thread.join(30)
    if "ok" not in result:
        raise RuntimeError(result.get("error", "timed out"))
    return result["ok"]


def _check_tray(folder):
    import PIL            # noqa: F401
    import pystray        # noqa: F401
    return {}


def selftest(report_path="", require=()):
    checks = [("core", _check_core), ("prompts", _check_prompts), ("context", _check_context),
              ("secretbox", _check_secretbox), ("tk", _check_tk), ("tray", _check_tray)]
    if WIN:
        checks += [("win32", _check_win32), ("uia", _check_uia)]
    required = {"core", "prompts", "context", "secretbox"} | set(require)
    report = {"version": __version__, "system": sys.platform, "frozen": bool(getattr(sys, "frozen", False)),
              "python": sys.version.split()[0], "checks": {}}
    with tempfile.TemporaryDirectory(prefix="banxing-selftest-") as folder:
        for name, check in checks:
            try:
                report["checks"][name] = {"ok": True, **(check(Path(folder)) or {})}
            except Exception as error:
                report["checks"][name] = {"ok": False, "error": f"{type(error).__name__}: {error}"[:300]}
    missing = sorted(name for name in required if not report["checks"].get(name, {}).get("ok"))
    report["required"] = sorted(required)
    report["failed_required"] = missing
    report["ok"] = not missing
    write_report(report_path, report)
    for name, entry in report["checks"].items():
        emit(f"[{'ok' if entry['ok'] else 'FAIL' if name in required else 'n/a '}] {name}"
             + (f": {entry.get('error')}" if not entry["ok"] else ""))
    emit("self-test passed" if report["ok"] else f"self-test FAILED: {', '.join(missing)}")
    return 0 if report["ok"] else 1


# ------------------------------------------------------------------ probe / demo / summary
def probe(report_path="", try_fill=False, wait=15.0):
    from .hosts import create_client
    from .ui import viewmodel
    settings = Settings()
    client = create_client(settings)
    client.open()
    emit(f"Switch to the Codex window within {int(wait)} seconds…")
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if client.snapshot(time.monotonic()).foreground:
            break
        time.sleep(0.25)
    from .context import ContextProvider
    provider = ContextProvider(codex_command=settings.get("codex_command") or "",
                               sessions_dir=settings.get("sessions_dir") or "")
    appserver, sessions = provider.available()
    context = {"appserver": appserver, "sessions": sessions}
    # Waiting for the window above looked at nothing inside it. The report itself is
    # what the user asked for on the command line: reading is allowed for its duration.
    with client.authorized():
        report = client.probe(try_fill=try_fill)
        try:
            signals = client.signals()
            if signals is not None:
                provider.excerpt(signals, "")
                context.update(provider.last)
        except Exception as error:
            context["error"] = type(error).__name__
        finally:
            provider.close()
            client.close()
    emit(viewmodel.probe_text(report, context, __version__))
    write_report(report_path, {"version": __version__, "report": report, "context": context})
    return 0


def demo(draft, mode, agree=""):
    settings = Settings()
    config = settings.model_config()
    if not config.ready:
        emit("No model configured. Set BANXING_PROVIDER=mock for the offline stand-in.")
        return 2
    emit(f"model: {config.provider} / {config.model_name}" + (f" @ {config.origin}" if config.origin else ""))
    if agree and config.destination:
        # The origin has to be typed out: consent is for a named destination.
        if agree.strip().rstrip("/").casefold() != config.origin.casefold() or not settings.give_consent(
                config.destination):
            emit(f"--agree does not name where the draft would be sent ({config.origin}); nothing was sent.")
            return 3
    if not settings.consented(config):
        emit(f"The draft would be sent to {config.origin}. Nothing was sent.\n"
             f"Agree in the pet's model settings, or repeat the command with: --agree {config.origin}")
        return 3
    outcome = suggest.Suggester(lambda: config, permit=settings.consented).analyze(draft, mode)
    if outcome.error:
        emit(f"error: {outcome.error} {outcome.detail}")
        return 1
    if not outcome.task:
        emit("(not a task: nothing to suggest)")
        return 0
    if not outcome.show:
        emit(f"(standby{', ready to send as it is' if outcome.ready else ''}) {outcome.reason}")
        return 0
    if outcome.reason:
        emit(outcome.reason)
    for item in outcome.hints:
        emit(f"\n{'？' if item.ask else '＋'} {item.label}\n   {item.addition}")
    for index, item in enumerate(outcome.routes, 1):
        emit(f"\n{index}. [{'主线' if item.main else '支线'} · {item.kind}] {item.approach} — 拿到{item.deliverable}"
             + (f"\n   因为：{item.why}" if item.why else "")
             + (f"\n   ⚠ {', '.join(item.caution)}" if item.caution else "")
             + f"\n   适用：{item.fits}\n   {item.prompt}\n   拿到后：{item.next}")
    emit(f"\n(prompt {outcome.prompt_version}, {outcome.seconds:.1f}s)")
    return 0


def run_eval(report_path="", limit=0):
    """Run the scenario set through the configured model and check each answer's
    structure. With the offline stand-in this proves the plumbing, nothing else."""
    from .evalkit import run as eval_run
    settings = Settings()
    config = settings.model_config()
    if not config.ready:
        emit("No model configured. Set BANXING_PROVIDER=mock for a plumbing-only run.")
        return 2
    if not settings.consented(config):
        emit(f"The scenarios would be sent to {config.origin}; agree first (model settings, or --demo … --agree).")
        return 3
    report = eval_run.run(lambda: config, permit=settings.consented, limit=limit)
    emit(eval_run.as_text(report))
    write_report(report_path, report)
    return 0 if report["plumbing_ok"] else 1


def main(argv=None):
    parser = argparse.ArgumentParser(prog=APP_ID, description="Banxing — a companion beside Codex")
    parser.add_argument("--version", action="store_true")
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--require", default="", help="comma-separated checks the self-test must pass")
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--fill", action="store_true", help="with --probe: also test filling")
    parser.add_argument("--wait", type=float, default=15.0)
    parser.add_argument("--report", default="", help="write the result as JSON to this file")
    parser.add_argument("--demo", metavar="DRAFT", default="")
    parser.add_argument("--mode", choices=levels.ORDER, default=levels.LEAD)
    parser.add_argument("--eval", action="store_true", help="run the evaluation scenarios (structure checks)")
    parser.add_argument("--limit", type=int, default=0, help="with --eval: only the first N scenarios")
    parser.add_argument("--agree", metavar="ORIGIN", default="",
                        help="with --demo: agree to send drafts to this origin, e.g. https://api.anthropic.com")
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args(argv)
    if args.version:
        emit(__version__)
        return 0
    if args.selftest:
        return selftest(args.report, [name.strip() for name in args.require.split(",") if name.strip()])
    if args.probe:
        return probe(args.report, args.fill, args.wait)
    if args.demo:
        return demo(args.demo, args.mode, args.agree)
    if args.eval:
        return run_eval(args.report, args.limit)
    if args.summary:
        summary = Store(home()).summary()
        emit(json.dumps(summary, ensure_ascii=False, indent=2))
        write_report(args.report, summary)
        return 0
    return run_pet()
