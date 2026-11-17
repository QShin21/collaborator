"""Glue between the state machine, the Codex client, the model and the view.

The controller owns no rules. It turns the machine's effects into calls and the
world's events back into machine inputs, and it is the only place that knows
about threads:

  UI thread       everything in Controller (the machine is never touched elsewhere)
  watcher thread  the Codex client — snapshots, signals, fills (UI Automation is
                  kept on one thread)
  worker threads  model calls

Tests use InlineExecutor, where all of that runs synchronously (or is held back
on purpose to simulate a slow model).
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import replace

from . import aboutme, calibration, evidence, examples, follow, levels as lv, secretbox, suggest
from .config import DEFAULTS
from .hosts.base import FILL, READ, SIGNALS, FillResult
from .store import wipe

log = logging.getLogger(__name__)


# ------------------------------------------------------------------ executors
class InlineExecutor:
    """Runs jobs on the calling thread. With hold=True jobs wait for release()."""

    def __init__(self, hold=False):
        self.hold = hold
        self.held = []

    def _run(self, job, done):
        if self.hold:
            self.held.append((job, done))
        else:
            done(job())

    analysis = _run
    client = _run

    def call_client(self, job, timeout=3.0):
        return job()

    def release(self, count=None):
        jobs, self.held = self.held[:count], self.held[count:] if count else []
        for job, done in jobs:
            done(job())


def poll_interval(base, snap):
    """How long to wait before the next look. Looking at another application's
    interface costs that application something, so: often while the user can type
    into it, rarely when it is not even in front."""
    if not snap.foreground:
        return max(base, 1.0)
    if snap.draft is None:
        return max(base, 0.6)          # nothing readable: no reason to hurry
    return base


class ThreadedExecutor:
    """Production executor. `pump()` must be called regularly on the UI thread."""

    def __init__(self, client, on_snapshot, poll=0.25, clock=time.monotonic, max_workers=2):
        self._client = client
        self._on_snapshot = on_snapshot
        self._poll = poll
        self._clock = clock
        self._inbox = queue.Queue()          # callables for the UI thread
        self._tasks = queue.Queue()          # callables for the watcher thread
        self._stop = threading.Event()
        self._slots = threading.Semaphore(max_workers)
        self.last_snapshot_at = clock()
        self._watcher = threading.Thread(target=self._watch, name="codex-watcher", daemon=True)

    def stalled_for(self):
        """Seconds since the watcher last produced a look at the host."""
        return self._clock() - self.last_snapshot_at

    def start(self):
        self._watcher.start()

    def stop(self):
        self._stop.set()
        self._tasks.put(None)
        self._watcher.join(timeout=3)

    def _watch(self):
        try:
            self._client.open()
        except Exception:
            log.exception("the Codex client could not be opened")
        while not self._stop.is_set():
            try:
                snap = self._client.snapshot(self._clock())
            except Exception:
                log.exception("snapshot failed")
                snap = follow.Snapshot(at=self._clock(), foreground=False, problem="error")
            self.last_snapshot_at = self._clock()
            self._inbox.put(lambda snap=snap: self._on_snapshot(snap))
            deadline = self._clock() + poll_interval(self._poll, snap)
            while not self._stop.is_set():
                try:
                    task = self._tasks.get(timeout=max(0.0, deadline - self._clock()))
                except queue.Empty:
                    break
                if task is not None:
                    task()
        try:
            self._client.close()
        except Exception:
            log.exception("closing the Codex client failed")

    def client(self, job, done):
        def task():
            try:
                result = job()
            except Exception as error:
                log.exception("client job failed")
                result = FillResult(False, f"error: {error}"[:80])
            self._inbox.put(lambda: done(result))
        self._tasks.put(task)

    def call_client(self, job, timeout=3.0):
        """Run `job` on the watcher thread and wait for it (used by worker threads)."""
        box, ready = [], threading.Event()

        def task():
            try:
                box.append(job())
            except Exception:
                log.exception("client call failed")
            ready.set()
        self._tasks.put(task)
        return box[0] if ready.wait(timeout) and box else None

    def analysis(self, job, done):
        def work():
            with self._slots:
                try:
                    result = job()
                except Exception as error:
                    log.exception("analysis failed")
                    result = suggest.failure("invalid", str(error))
            self._inbox.put(lambda: done(result))
        threading.Thread(target=work, name="analysis", daemon=True).start()

    def pump(self, limit=50):
        """Run what the other threads handed over. Only the newest snapshot matters,
        but all are cheap, so they are simply processed in order."""
        for _ in range(limit):
            try:
                call = self._inbox.get_nowait()
            except queue.Empty:
                return
            try:
                call()
            except Exception:
                log.exception("handling an event failed")


# ------------------------------------------------------------------ view contract
class View:
    """What the controller needs from a user interface. The Tk shell implements it;
    tests use a recording fake."""

    def status(self, state, detail):
        pass

    def follow(self, snap):
        pass

    def show_bubble(self, bubble, snap):
        pass

    def hide_bubble(self, reason):
        pass

    def copy(self, text):
        pass

    def offer_copy(self, text, reason):
        pass

    def notice(self, kind, detail=""):
        pass

    def ask_paste(self):
        pass

    def need_preview(self, index):
        """A route carries a safety caution: open its preview instead of filling."""

    def ask(self, kind):
        """Put a question to the user ("quieter"); the answer comes back through
        Controller.answer_offer."""


STALLED_AFTER = 5.0        # seconds without a look at the host before the user is told


# ------------------------------------------------------------------ controller
class Controller:
    def __init__(self, client, suggester, view, settings, store, executor=None, clock=time.monotonic,
                 context=None, capabilities=None, about=None):
        """`capabilities`: what may be used. None = whatever the stored calibration for
        this host version allows (nothing, if there is none). Tests pass a set."""
        self.client = client
        self.context = context
        self.suggester = suggester
        self.view = view
        self.settings = settings
        self.store = store
        self.executor = executor or InlineExecutor()
        self.clock = clock
        self.about = about if about is not None else aboutme.AboutMe(settings.folder)
        self.weights = store.load_weights()
        self._fixed_capabilities = capabilities is not None
        self.capabilities = frozenset(capabilities or ())
        self.calibrated = None                  # the stored calibration entry, if any
        self._host_version = None
        self.calibrating = False                # the wizard is open: do not analyse what is typed for it
        self._stalled = False
        self.machine = follow.FollowMachine(
            levels=store.load_levels(), tuning=settings.tuning(),
            model_ready=self._model_ready(), paused=not settings.get("follow"),
            fill_supported=FILL in self.capabilities)
        self._sync_reading()
        if not self._fixed_capabilities:
            self.load_calibration()

    # ------------------------------------------------------------ abilities
    def load_calibration(self):
        """Switch on what the stored calibration allows. Called at start (the host
        version is read on the watcher thread in the real app) and after a calibration."""
        if self._fixed_capabilities:
            return
        try:
            self.capabilities, self.calibrated = calibration.effective(self.settings.folder, self.client)
        except Exception:
            log.exception("could not read the calibration")
            self.capabilities, self.calibrated = frozenset(), None
        if self.calibrated:
            self.client.set_placeholders(self.calibrated.get("placeholders") or [])
            self.client.set_signature(self.calibrated.get("signature"))
        self.machine.fill_supported = FILL in self.capabilities
        self._sync_reading()

    def _sync_reading(self):
        """Tell the host client whether its routine looks may read the input box at
        all. Without a calibration for this host version they may not: the text is not
        fetched in the first place, rather than fetched and thrown away."""
        allowed = READ in self.capabilities
        version = self.calibrated.get("host_version") if (self.calibrated and not self._fixed_capabilities) else None
        self.client.set_reading(allowed, version or None)

    def for_request(self):
        return self.about.for_request(self.weights)

    # ------------------------------------------------------------ setup state
    def _model_ready(self):
        return self.settings.model_config().ready and self.settings.consented()

    def setup_reason(self):
        """Why the pet cannot analyse yet: "model", "consent" or ""."""
        if not self.settings.model_config().ready:
            return "model"
        return "" if self.settings.consented() else "consent"

    def refresh_setup(self):
        self._apply(self.machine.set_model_ready(self._model_ready()))

    def give_consent(self, use_context=None, shown=None):
        """Record the user's yes. `shown` is the destination they were looking at; if
        it is not the effective one, nothing is recorded and False is returned."""
        if not self.settings.give_consent(shown):
            return False
        if use_context is not None:
            self.settings.set(use_context=bool(use_context))
        self.store.log("consent", {"context": bool(self.settings.get("use_context"))})
        self.refresh_setup()
        return True

    def confirm_model(self, shown, use_context, fields):
        """The "agree" button of the model dialog: save, then record consent — but only
        for the destination the dialog was showing (`shown`).

        Returns "" on success, otherwise a code the dialog explains:
          provider | base_url | api_key | api_key_again   nothing was saved
          env           the BANXING_* variables decide the model and are incomplete
          destination   requests would go somewhere other than `shown`; no consent
        """
        if self.settings.env_locked:
            effort = str(fields.get("effort") or "").strip().lower()
            if effort in ("low", "medium", "high", "none") and effort != self.settings.get("effort"):
                self.settings.set(effort=effort)
        else:
            try:
                self.save_model(**fields)
            except ValueError as problem:
                return str(problem)
        if not self.settings.model_config().ready:
            self.refresh_setup()
            return "env"
        if not self.give_consent(use_context, shown):
            self.refresh_setup()
            return "destination"
        return ""

    def save_model(self, **fields):
        config = self.settings.save_model(**fields)
        self.store.log("model", {"provider": config.provider})
        self.refresh_setup()
        return config

    # ------------------------------------------------------------ world → machine
    def on_snapshot(self, snap):
        self._stalled = False
        if snap.foreground and not self._fixed_capabilities:
            # The host's version is only known once its window has been seen, and it
            # changes when the host is updated: either way, look the calibration up again.
            version = self.client.host_version()
            if version != self._host_version:
                self._host_version = version
                self.load_calibration()
        if (READ not in self.capabilities or self.calibrating) and snap.foreground and snap.draft is not None:
            # Not calibrated on this machine: the input box is not read, whatever the
            # client could see. Paste mode and the examples remain.
            snap = replace(snap, draft=None, box_id="", composing=False, problem="uncalibrated")
        effects = self.machine.observe(snap)
        self.view.follow(snap)
        self._apply(effects)

    def watchdog(self):
        """Called regularly on the UI thread. If the watcher has stopped producing
        looks at the host (a hung interface call), say so and give up a waiting fill —
        neither may depend on snapshots arriving."""
        now = self.clock()
        self._apply(self.machine.expire(now))
        stalled_for = getattr(self.executor, "stalled_for", None)
        if stalled_for is None or self._stalled or stalled_for() < STALLED_AFTER:
            return
        self._stalled = True
        last = self.machine.snap
        snap = follow.Snapshot(at=now, foreground=bool(last and last.foreground),
                               window_id=last.window_id if last else "",
                               window_rect=last.window_rect if last else None, problem="stalled")
        effects = self.machine.observe(snap)
        self.view.follow(snap)
        self._apply(effects)
        self.store.log("stalled")

    def _signals(self):
        return self.executor.call_client(self.client.signals)

    def _apply(self, effects):
        for effect in effects:
            try:
                self._do(effect)
            except Exception:
                log.exception("effect failed: %r", type(effect).__name__)

    def _do(self, effect):
        if isinstance(effect, follow.Analyze):
            self._analyze(effect)
        elif isinstance(effect, follow.ShowBubble):
            self.view.show_bubble(effect.bubble, self.machine.snap)
            self.machine.bubble_shown(effect.bubble.id, self.clock())
        elif isinstance(effect, follow.HideBubble):
            self.view.hide_bubble(effect.reason)
        elif isinstance(effect, follow.Fill):
            self.executor.client(
                lambda: self.client.fill(effect.key, effect.expect, effect.text),
                lambda result: self._apply(self.machine.fill_done(
                    effect.token, result.ok, result.reason, self.clock(), result.method)))
        elif isinstance(effect, follow.CopyText):
            self.view.copy(effect.text)
            self.view.notice("copied", effect.why)
        elif isinstance(effect, follow.OfferCopy):
            self.view.offer_copy(effect.text, effect.reason)
        elif isinstance(effect, follow.Status):
            self.view.status(effect.state, effect.detail or self._status_detail(effect.state))
        elif isinstance(effect, follow.Notice):
            self.view.notice(effect.kind, effect.detail)
        elif isinstance(effect, follow.Persist):
            self.store.save_levels(self.machine.levels)
        elif isinstance(effect, follow.Log):
            self.store.log(effect.event, effect.data)
        elif isinstance(effect, follow.AskPaste):
            self.view.ask_paste()
        elif isinstance(effect, follow.NeedPreview):
            self.view.need_preview(effect.index)
        elif isinstance(effect, follow.Ask):
            self.view.ask(effect.kind)
        elif isinstance(effect, follow.Prefer):
            self.weights = aboutme.adjust(self.weights, effect.kind, effect.delta)
            self.store.save_weights(self.weights)

    def _status_detail(self, state):
        return self.setup_reason() if state == "setup" else ""

    def _analyze(self, effect):
        use_context = (bool(self.settings.get("use_context")) and effect.source in follow.FOLLOWING
                       and SIGNALS in self.capabilities)

        def job():
            signals = self._signals() if use_context else None
            return self.suggester.analyze(effect.draft, effect.mode, signals, use_context,
                                          pull=effect.pull, given=effect.given)

        self.executor.analysis(job, lambda outcome: self._apply(
            self.machine.result(effect.request_id, outcome, self.clock())))

    # ------------------------------------------------------------ user → machine
    def pick(self, index, confirmed=False):
        self._apply(self.machine.pick(index, self.clock(), confirmed))

    def apply_hint(self, index):
        self._apply(self.machine.apply_hint(index, self.clock()))

    def want_routes(self):
        self._apply(self.machine.want_routes(self.clock()))

    def more(self):
        self._apply(self.machine.more(self.clock()))

    def set_level(self, level):
        self._apply(self.machine.set_level(level))

    def undo_step(self):
        self._apply(self.machine.undo_step())

    def answer_offer(self, accept):
        self._apply(self.machine.answer_offer(bool(accept)))

    def save_facts(self, lines):
        """Returns the stored facts, or None when they could not be encrypted (nothing
        was written then; the window says so and stays open)."""
        try:
            facts = self.about.save_facts(lines)
        except secretbox.SecretError:
            self.store.log("encrypt_failed", {"what": "about"})
            return None
        self.store.log("about", {"facts": len(facts)})
        return facts

    def reset_weights(self):
        self.weights = aboutme.default_weights()
        self.store.save_weights(self.weights)

    def clear_data(self):
        """"清除本机数据": everything this app stored, then back to a first-run state."""
        self.about.save_facts([])
        removed = wipe(self.settings.folder)
        self.settings.data = dict(DEFAULTS)
        self.settings._key_cache = None
        self.weights = aboutme.default_weights()
        self.machine.levels = lv.Levels()
        if not self._fixed_capabilities:
            self.capabilities, self.calibrated = frozenset(), None
            self.machine.fill_supported = False
            self._sync_reading()
        self.refresh_setup()
        return removed

    # ------------------------------------------------------------ calibration
    def calibration_step(self, step, memo, done, prepare=False):
        """Run one calibration step on the watcher thread; `done(result)` on the UI thread."""
        def job():
            if prepare:
                calibration.prepare(self.client, step, memo)
                return {"ok": True, "prepared": True}
            return calibration.check(self.client, step, memo, context=self.context)

        def finish(result):
            done(result if isinstance(result, dict) else {"ok": False, "error": getattr(result, "reason", "failed")})
        self.executor.client(job, finish)

    def collect_evidence(self, done):
        """Build the evidence bundle (no draft, chat or prompt text in it); `done(bundle)`
        on the UI thread. Nothing is sent anywhere."""
        def job():
            try:
                structure = self.client.record_structure()
            except Exception:
                log.exception("structure recording failed")
                structure = []
            with self.client.authorized():          # the user asked for this report
                return {"probe": self.client.probe(try_fill=False), "structure": structure}

        def finish(parts):
            parts = parts if isinstance(parts, dict) else {"probe": {"client": self.client.name}, "structure": []}
            done(evidence.build(calibration_entry=self.calibrated, probe=parts["probe"],
                                structure=parts["structure"], summary=self.store.summary()))
        self.executor.client(job, finish)

    def finish_calibration(self, results, memo):
        """Store the results and switch on what they proved. Returns the stored entry."""
        entry = calibration.save(self.settings.folder, self.client, results, memo)
        self.store.log("calibrated", {"verdict": entry["verdict"], "capabilities": len(entry["capabilities"])})
        self.load_calibration()
        return entry

    def copy(self, index):
        self._apply(self.machine.copy(index, self.clock()))

    def dismiss(self):
        self._apply(self.machine.dismiss(self.clock()))

    def offer_copied(self):
        self._apply(self.machine.offer_copied(self.clock()))

    def undo(self):
        self._apply(self.machine.undo(self.clock()))

    def pet_clicked(self):
        if self.calibrating:
            return                      # the wizard asks for a click on the figure; it means nothing else
        if self.setup_reason():
            self.view.notice("setup", self.setup_reason())
            return
        self._apply(self.machine.request_now(self.clock()))

    def retry(self):
        self._apply(self.machine.retry(self.clock()))

    def paste(self, draft):
        if self.setup_reason():
            self.view.notice("setup", self.setup_reason())
            return
        self._apply(self.machine.manual(draft, self.clock()))

    def example(self, name):
        self._apply(self.machine.show_example(
            examples.get(name)["draft"], suggest.from_example(name), self.clock()))
        self.store.log("example", {"name": name})

    def set_paused(self, paused):
        self.settings.set(follow=not paused)
        self._apply(self.machine.set_paused(paused, self.clock()))
        self.store.log("paused" if paused else "resumed")

    def set_use_context(self, enabled):
        self.settings.set(use_context=bool(enabled))
        self.store.log("context", {"enabled": bool(enabled)})

    # ------------------------------------------------------------ self-check
    def probe(self, try_fill, done):
        """Run the compatibility self-check on the watcher thread; `done(report, context)`
        is called on the UI thread. Neither part contains draft or chat text."""
        def job():
            with self.client.authorized():          # the user pressed "start the check"
                report = self.client.probe(try_fill=try_fill)
                context = None
                if self.context is not None:
                    appserver, sessions = self.context.available()
                    context = {"appserver": appserver, "sessions": sessions}
                    try:
                        signals = self.client.signals()
                        if signals is not None:
                            self.context.excerpt(signals, "")
                            context.update(self.context.last)
                    except Exception as error:
                        context["error"] = type(error).__name__
                return report, context
        self.executor.client(job, lambda result: done(*result) if isinstance(result, tuple) else done(
            {"client": self.client.name, "error": getattr(result, "reason", "failed")}, None))

    def learn_placeholder(self, done):
        """Record what the (emptied) input box reads as, so it stops counting as a draft."""
        def finish(text):
            text = text if isinstance(text, str) else ""
            if text and len(text) <= 120:
                known = [p for p in (self.settings.get("placeholders") or []) if p != text] + [text]
                self.settings.set(placeholders=known[-8:])
                self.client.set_placeholders(known[-8:])
                self.store.log("placeholder_learned", {"chars": len(text)})
                done(True)
            else:
                done(False)
        def job():
            with self.client.authorized():          # the user pressed "record"
                self.client.snapshot(self.clock())  # find the box first
                return self.client.raw_box_text()
        self.executor.client(job, finish)

    def menu(self):
        state = self.machine.menu()
        config = self.settings.model_config()
        state.update(use_context=bool(self.settings.get("use_context")), setup=self.setup_reason(),
                     provider=config.provider if config.ready else "", host=config.host if config.ready else "",
                     model=config.model_name if config.ready else "", can_fill=FILL in self.capabilities,
                     calibrated=READ in self.capabilities, host_client=self.client.name)
        return state
