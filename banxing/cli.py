"""Entry points.

  Banxing.exe                         run the app
  ... --watch PATH [--conversation ID] [--list] [--from-turn N] [--stepwise] [--agree ORIGIN]
                                      read one conversation the user points at, print its
                                      ledger and what the supervising model finds;
                                      --stepwise replays it turn by turn
  ... --replay [--limit N] [--report F]
                                      play the labelled conversations to the watcher turn by
                                      turn: timing, aim, interruptions, one-shot control
  ... --selftest [--report F] [--require tk,win32]
                                      check that this build works on this machine;
                                      exit code 0 only if the required checks pass
  ... --summary                       numbers from the local trial log
  ... --version
"""
from __future__ import annotations

import argparse
import gc
import json
import logging
import logging.handlers
import sys
import tempfile
import time
from pathlib import Path

from . import APP_ID, __version__
from .config import Settings, home
from .store import Store

WIN = sys.platform == "win32"
log = logging.getLogger("banxing")


def setup_logging(folder):
    """A small rotating log in the data folder. Conversation text is never logged."""
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
def build(settings, store, view, executor, clock=time.monotonic, auditor=None, use_appserver=True):
    """Wire the application: one Watcher. Shared by the real run, the self-test and
    the tests (which pass `use_appserver=False` so that no `codex` process is started)."""
    from .context import appserver
    from .watch.driver import Watcher
    holder = {}

    def server():
        if not use_appserver:
            return None
        if "server" not in holder:
            command = appserver.find_command(settings.get("codex_command") or "")
            holder["server"] = appserver.AppServer(command) if command else None
        return holder["server"]

    watcher = Watcher(settings, store, executor, view, clock, auditor=auditor, server=server)
    watcher.close_server = lambda: holder.get("server") and holder["server"].close()
    return watcher


def run_app():
    from .ui import strings
    settings = Settings()
    setup_logging(settings.folder)
    strings.set_language(settings.get("language"))
    if WIN:
        from . import winshell
        winshell.enable_dpi_awareness()
        if not winshell.single_instance("Local\\" + APP_ID + "-Banxing"):
            import tkinter
            from tkinter import messagebox
            root = tkinter.Tk()
            root.withdraw()
            try:
                messagebox.showinfo(strings.t("app_name"), strings.t("already_running"))
            finally:
                root.destroy()                 # ended and freed on the thread that made it
                del root
                gc.collect()
            return 0
    from .executor import ThreadedExecutor
    from .ui.app import PetApp
    store = Store(settings.folder)
    app = PetApp(settings, store)
    executor = ThreadedExecutor()
    watcher = build(settings, store, app, executor)
    app.attach(watcher, executor)
    store.log("start")
    watcher.restore()                          # a ledger the user asked to keep
    app.root.after(600, app.greet)
    try:
        app.run()
    finally:
        # The windows are ended and freed here, on the thread that made them, while
        # worker threads may still be finishing — they must never be the ones to free
        # the Tk interpreter.
        try:
            app.destroy()
        except Exception:
            log.exception("closing the windows failed")
        watcher.close_server()
        store.log("stop")
    return 0


# ------------------------------------------------------------------ self-test
def _synthetic(folder):
    """A conversation written for the self-test, as a file, read back through the
    same code as a real one."""
    from .evalkit import replay_cases
    from .watch import sources
    case = replay_cases.all_cases()[0]
    path = Path(folder) / "watch" / "conversation.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"title": "self-test", "turns": [
        {"user": turn["user"], "assistant": turn["model"], "records": turn["records"]}
        for turn in case["turns"][:4]]}, ensure_ascii=False), encoding="utf-8")
    kind, listings = sources.listing(path)
    ref = sources.SourceRef(kind=kind, path=str(path), conversation=listings[0].id)
    return ref, sources.read(ref)


def _check_watch(folder):
    """Read a synthetic conversation, audit it with the offline stand-in — including
    a look back, a search and a blind reading — and keep the ledger."""
    from .llm import ModelConfig
    from .watch import audit as au, session as ss, vault
    ref, transcript = _synthetic(folder)
    config = ModelConfig(provider="mock")
    session, auditor = ss.Session(destination=config.destination), au.Auditor(lambda: config)
    queue, bubble = list(session.observe(transcript)), None
    while queue:
        effect = queue.pop(0)
        if isinstance(effect, ss.Run):
            queue += session.audited(effect.job.id, auditor.run(effect.job))
        elif isinstance(effect, ss.ShowBubble):
            bubble = effect.bubble
    if bubble is None or not bubble.finding.basis or not bubble.finding.ask:
        raise RuntimeError("no traceable concern for the synthetic conversation")
    trace = session.last.trace
    if not (trace.looks and trace.searches and trace.independent):
        raise RuntimeError("the audit did not go back to look, search and read blind")
    if not any(a.status == "unchecked" for a in session.ledger.anchors):
        raise RuntimeError("the ledger does not mark the unchecked claim")
    vault.save(Path(folder) / "watch", ref, session)
    stored = vault.load(Path(folder) / "watch")
    if stored is None or stored[1].ledger != session.ledger:
        raise RuntimeError("the stored ledger did not come back as it was saved")
    return {"concern": bubble.finding.kind, "requests": session.usage.requests, "looks": trace.looks}


def _check_prompts(folder):
    from .watch import audit
    versions = {}
    for name in ("audit", "blind"):
        text, version = audit.system_prompt(name)
        if "JSON" not in text or len(text) < 300 or not version:
            raise RuntimeError(f"the {name} prompt is missing, truncated or has no version line")
        versions[name] = version
    return versions


def _check_secretbox(folder):
    from . import secretbox
    stored = secretbox.protect("self-test-value")
    if secretbox.reveal(stored) != "self-test-value":
        raise RuntimeError("stored secret could not be read back")
    return {"encrypted": secretbox.is_encrypted(stored)}


def _check_tk(folder):
    """Build the real windows, show a bubble, open every dialog, answer the bubble.

    When this returns, nothing of Tk is left in memory: the interpreter was ended and
    freed on this thread (a Tk interpreter freed from another thread aborts the process)."""
    try:
        return _tk_walkthrough(folder)
    finally:
        gc.collect()        # the walkthrough's own frame is gone now; nothing may linger


def _tk_walkthrough(folder):
    from .executor import InlineExecutor
    from .ui import strings
    from .ui.app import PetApp
    strings.set_language("zh")
    settings = Settings(Path(folder) / "ui", environ={"BANXING_PROVIDER": "mock"})
    store = Store(settings.folder)
    app = PetApp(settings, store, use_tray=False)
    try:
        watcher = build(settings, store, app, InlineExecutor(), use_appserver=False)
        app.attach(watcher, None)
        app.place()
        ref, transcript = _synthetic(folder)
        problem = watcher.start(ref, transcript, "")
        app.root.update()
        if problem or app.bubble is None or not app.bubble_window.winfo_viewable():
            raise RuntimeError(f"the bubble was not shown ({problem or 'no bubble'})")
        app._toggle_sources()
        app.root.update()
        size = (app.bubble_window.winfo_width(), app.bubble_window.winfo_height())
        app._build_menu(__import__("banxing.ui.viewmodel", fromlist=["x"]).menu_items(watcher.state()))
        for opener in (app.open_model, app.open_watch, app.open_ledger):
            opener()
            app.root.update()
        for window in list(app.dialog_windows):
            window.destroy()
        window = opener = None
        app.root.update()
        app._copy_ask()
        app.root.update()
        marks = watcher.session.ledger.marks
        if app.bubble is not None or not marks or marks[-1].state != "asked":
            raise RuntimeError("copying the question did not close the bubble and note it")
        if min(size) < 40:
            raise RuntimeError(f"the bubble is not on screen (size {size})")
        watcher.stop()
        return {"bubble": list(size), "scale": round(app.scale, 2)}
    finally:
        app.destroy()


def _check_win32(folder):
    from . import winshell
    return {"dpi": bool(winshell.enable_dpi_awareness())}


def _check_tray(folder):
    import PIL            # noqa: F401
    import pystray        # noqa: F401
    return {}


def selftest(report_path="", require=()):
    checks = [("watch", _check_watch), ("prompts", _check_prompts), ("secretbox", _check_secretbox),
              ("tk", _check_tk), ("tray", _check_tray)]
    if WIN:
        checks.append(("win32", _check_win32))
    required = {"watch", "prompts", "secretbox"} | set(require)
    report = {"version": __version__, "system": sys.platform, "frozen": bool(getattr(sys, "frozen", False)),
              "python": sys.version.split()[0], "checks": {}}
    with tempfile.TemporaryDirectory(prefix="banxing-selftest-") as folder:
        for name, check in checks:
            try:
                report["checks"][name] = {"ok": True, **(check(Path(folder)) or {})}
            except Exception as error:
                report["checks"][name] = {"ok": False, "error": f"{type(error).__name__}: {error}"[:300]}
            # Whatever a check left behind (a failed one keeps its frames alive through
            # the exception) is freed here, on this thread, before the next check runs.
            gc.collect()
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


# ------------------------------------------------------------------ replay / watch / summary
def run_replay(report_path="", limit=0):
    """Play the labelled conversations to the watcher turn by turn. With the offline
    stand-in this proves the plumbing, nothing else."""
    from .evalkit import replay
    settings = Settings()
    config = settings.model_config()
    if not config.ready:
        emit("No model configured. Set BANXING_PROVIDER=mock for a plumbing-only run.")
        return 2
    if not settings.consented(config):
        emit(f"The conversations would be sent to {config.origin}. Nothing was sent.\n"
             f"Repeat the command with: --agree {config.origin}")
        return 3
    report = replay.run(lambda: config, permit=settings.consented, limit=limit)
    emit(replay.as_text(report))
    write_report(report_path, report)
    return 0 if report["plumbing_ok"] else 1


def watch(path, conversation="", first=1, agree="", list_only=False, report_path="", stepwise=False):
    """Read one conversation the user points at and audit it — once, as it stands
    now, or (`stepwise`) replayed turn by turn to see after which turns a bubble
    would have appeared. The second is how a real conversation is replayed.

    The conversation is sent only to a destination named on this command line
    (--agree ORIGIN): it is agreed to per conversation, per run. The report file
    holds numbers and names only — no text of the conversation."""
    from .evalkit import replay
    from .ui import strings, viewmodel
    from .watch import audit as au, session as ss, sources
    settings = Settings()
    strings.set_language(settings.get("language"))
    try:
        kind, listings = sources.listing(path)
        if list_only or (len(listings) > 1 and not conversation):
            for item in listings:
                emit(f"{item.id}\t{item.turns} turns\t{item.title}")
            if not list_only:
                emit("More than one conversation: repeat with --conversation ID.")
            return 0 if list_only else 2
        ref = sources.SourceRef(kind=kind, path=str(path), conversation=conversation or listings[0].id)
        transcript = sources.read(ref)
    except sources.SourceError as error:
        emit(f"cannot read the conversation: {error.kind} {error}")
        return 1
    config = settings.model_config()
    if not config.ready:
        emit("No model configured. Set BANXING_PROVIDER=mock for the offline stand-in.")
        return 2
    emit(f"supervising model: {config.provider} / {config.model_name}" + (f" @ {config.origin}" if config.origin else ""))
    if config.destination and agree.strip().rstrip("/").casefold() != config.origin.casefold():
        emit(f"Turns {first}–{len(transcript.turns)} of this conversation would be sent to {config.origin}. "
             f"Nothing was sent.\nRepeat the command with: --agree {config.origin}")
        return 3

    def show(bubble):
        view = viewmodel.nudge_view(bubble)
        emit("\n".join([f"▶ [{view.kind}] {view.title}"] + list(view.lines)
                       + [strings.t("nudge_ask"), "  " + view.ask] + [f"  · {line}" for line in view.evidence]))

    rows, bubbles = [], []
    if stepwise:
        played = replay.timeline(transcript, config, first=first)
        session, rows, bubbles = played["session"], played["rows"], played["bubbles"]
        for row in rows:
            emit(f"turn {row['turn']:>3}: {row['action'] or '-':<6} {row['why'] or row['error'] or '-':<12} "
                 f"{row['requests']} request(s)")
            for bubble in (b for b in bubbles if b.through == row["turn"] and row["bubble"]):
                show(bubble)
        emit("")
    else:
        session = ss.Session(destination=config.destination, first=first)
        auditor = au.Auditor(lambda: config)
        # A turn still running: the user asks anyway, so what is settled is judged.
        queue = list(session.observe(transcript)) or list(session.look_now())
        while queue:
            effect = queue.pop(0)
            if isinstance(effect, ss.Run):
                queue += session.audited(effect.job.id, auditor.run(effect.job))
            elif isinstance(effect, ss.ShowBubble):
                bubbles.append(effect.bubble)
    emit("\n".join(viewmodel.ledger_lines(session.ledger, session.findings, session.usage, config.origin, session.last)))
    if not stepwise:
        emit("")
        if bubbles:
            show(bubbles[-1])
        elif session.error:
            emit(f"error: {session.error}")
    last = session.last
    write_report(report_path, {
        "kind": "banxing-watch", "version": __version__, "source": kind, "turns": len(transcript.turns),
        "through": session.ledger.through, "error": session.error,
        "findings": [{"kind": f.kind, "weight": f.weight, "turns": sorted({r.turn for r in f.basis + f.now})}
                     for f in session.findings],
        "anchors": {status: sum(1 for a in session.ledger.anchors if a.status == status)
                    for status in ("backed", "unchecked", "broken", "retired")},
        "bubbles": [{"turn": b.through, "kind": b.finding.kind} for b in bubbles], "timeline": rows,
        "looks": last.trace.looks if last else 0, "searches": last.trace.searches if last else 0,
        "independent": bool(last and last.trace.independent),
        "requests": session.usage.requests, "chars_sent": session.usage.chars, "turns_sent": list(session.usage.turns),
        "provider": config.provider, "model": config.model_name,
        # One conversation, read by a model, checked by nobody: not evidence of quality yet.
        "evidence": "plumbing-only" if config.provider == "mock" else "real-model-output-unchecked"})
    return 1 if session.error else 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog=APP_ID, description="Banxing — an outside reader for a long conversation")
    parser.add_argument("--version", action="store_true")
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--require", default="", help="comma-separated checks the self-test must pass")
    parser.add_argument("--report", default="", help="write the result as JSON to this file")
    parser.add_argument("--replay", action="store_true",
                        help="play the labelled conversations to the watcher, turn by turn")
    parser.add_argument("--limit", type=int, default=0, help="with --replay: only the first N conversations")
    parser.add_argument("--watch", metavar="PATH", default="",
                        help="audit one conversation: a ChatGPT export (.zip / folder / json), a Codex or "
                             "Claude Code session .jsonl, or a transcript file")
    parser.add_argument("--conversation", metavar="ID", default="", help="with --watch: which conversation of an export")
    parser.add_argument("--list", action="store_true", help="with --watch: only list the conversations in PATH")
    parser.add_argument("--from-turn", type=int, default=1, help="with --watch: the first turn that may be read")
    parser.add_argument("--stepwise", action="store_true",
                        help="with --watch: replay the conversation turn by turn and show where a bubble would appear")
    parser.add_argument("--agree", metavar="ORIGIN", default="",
                        help="agree to send THIS run's conversation(s) to this origin, e.g. https://api.anthropic.com")
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args(argv)
    if args.version:
        emit(__version__)
        return 0
    if args.selftest:
        return selftest(args.report, [name.strip() for name in args.require.split(",") if name.strip()])
    if args.replay:
        if args.agree:
            settings = Settings()
            config = settings.model_config()
            if args.agree.strip().rstrip("/").casefold() == config.origin.casefold():
                settings.give_consent(config.destination)
        return run_replay(args.report, args.limit)
    if args.watch:
        return watch(args.watch, args.conversation, max(1, args.from_turn), args.agree, args.list, args.report,
                     args.stepwise)
    if args.summary:
        summary = Store(home()).summary()
        emit(json.dumps(summary, ensure_ascii=False, indent=2))
        write_report(args.report, summary)
        return 0
    return run_app()
