"""Watching, wired into the app: reads and model calls on worker threads, the
session on the UI thread, the view, the files. No rules live here — see session.py,
nudge.py, ledger.py, audit.py.

  UI thread       everything in Watcher (the session is never touched elsewhere)
  worker threads  reading the conversation again; the audit calls

Consent. `start` is given the destination the user was looking at when they agreed
to have THIS conversation read. It is recorded only if it is the destination that
would really be used (environment overrides included); the session carries it into
every job, and `Auditor.run` compares it with the configuration it is about to use.
A changed model service therefore stops everything until the user agrees again.

The app reads the file, thread or pasted text the user chose, and writes nowhere but
its own data folder. It never reads or writes another application's window.
"""
from __future__ import annotations

import logging
import os

from ..config import DEFAULTS
from ..store import wipe
from . import audit as au
from . import session as ss
from . import sources, vault

log = logging.getLogger(__name__)

SERVER_POLL_SECONDS = 15.0


class View:
    """What the watcher needs from a user interface. The Tk shell implements it;
    tests use a recording fake."""

    def show_bubble(self, bubble):
        """A concern worth interrupting for (session.Bubble), with its passages."""

    def hide_bubble(self, reason):
        pass

    def changed(self):
        """The ledger, its usage numbers or the watching state changed."""

    def notice(self, kind, detail=""):
        pass


def stamp_of(ref):
    """Something cheap that changes when a file source does; None for other sources."""
    if ref.kind in (sources.APPSERVER, sources.PASTED) or not ref.path:
        return None
    try:
        info = os.stat(ref.path)
    except OSError:
        return ("gone",)
    return (info.st_mtime_ns, info.st_size)


class Watcher:
    def __init__(self, settings, store, executor, view, clock, auditor=None, server=None, read=sources.read):
        """`server()` returns an App Server client for threads watched that way, or None."""
        self.settings = settings
        self.store = store
        self.executor = executor
        self.view = view
        self._clock = clock
        self.auditor = auditor or au.Auditor(
            settings.model_config, timeout=max(90.0, 3 * settings.number("model_timeout_seconds", 5.0, 300.0)))
        self._server = server
        self._read = read
        self.session = None
        self.ref = None
        self.title = ""                 # for the ledger window; memory only
        self.persist = False
        self.problem = ""               # why the conversation cannot be read right now
        self._stamp = None
        self._reading = False
        self._next_look = 0.0
        self._generation = 0            # reads and audits of a stopped watch are dropped
        self._thread_stamp = None       # when the watched App Server thread was last updated

    # ------------------------------------------------------------ state
    @property
    def active(self):
        return self.session is not None

    def state(self):
        """For the menu and the ledger window. No conversation text."""
        config = self.settings.model_config()
        base = {"active": False, "model": config.provider if config.ready else "", "host": config.host}
        session = self.session
        if session is None:
            return base
        return dict(base, active=True, paused=session.paused, state=session.state, auditing=session.auditing,
                    bubble=session.bubble is not None, through=session.ledger.through,
                    turns=len(session.transcript.turns), problem=self.problem, error=session.error,
                    persist=self.persist, source=self.ref.kind, findings=len(session.findings))

    # ------------------------------------------------------------ effects
    def _apply(self, effects):
        for effect in effects:
            try:
                if isinstance(effect, ss.Run):
                    self._run(effect.job)
                elif isinstance(effect, ss.ShowBubble):
                    self.view.show_bubble(effect.bubble)
                elif isinstance(effect, ss.HideBubble):
                    self.view.hide_bubble(effect.reason)
                elif isinstance(effect, ss.Changed):
                    self._store()
                    self.view.changed()
                elif isinstance(effect, ss.Notice):
                    self.view.notice(effect.kind, effect.detail)
                elif isinstance(effect, ss.Log):
                    self.store.log(effect.event, effect.data)
            except Exception:
                log.exception("watch effect failed: %r", type(effect).__name__)

    def _run(self, job):
        generation = self._generation

        def done(audit):
            if generation != self._generation or self.session is None:
                return
            if not hasattr(audit, "ok"):        # the worker failed outright
                audit = au.failure("invalid", "the audit could not be run")
            self._apply(self.session.audited(job.id, audit))
        self.executor.background(lambda: self.auditor.run(job), done)

    def _store(self):
        if not self.persist or self.session is None:
            return
        try:
            vault.save(self.settings.folder, self.ref, self.session)
        except vault.VaultError as error:
            # Encrypted or not stored: go on in memory, and say so once.
            self.persist = False
            self.store.log("watch_store_failed", {"why": error.kind})
            self.view.notice("watch_error_" + error.kind)

    # ------------------------------------------------------------ choosing a conversation
    def listing(self, path, done):
        """Which conversations a file holds; `done(kind, listings, problem)` on the UI
        thread. The file is the one the user just pointed at; nothing is sent."""
        def job():
            try:
                return sources.listing(path) + ("",)
            except sources.SourceError as error:
                return "", [], error.kind
        self.executor.background(job, lambda result: done(*result) if isinstance(result, tuple)
                                 else done("", [], "unreadable"))

    def codex_sessions(self, done):
        """The recent Codex conversations on this machine, for the user to pick one
        from; `done(kind, listings)`. The App Server is asked first (its paged history
        is the complete one); the session files are the fallback. Either is looked at
        only because the user asked for the list."""
        from ..context import rollout
        folder = rollout.sessions_dir(self.settings.get("sessions_dir") or "")

        def job():
            server = self._server() if self._server is not None else None
            if server is not None:
                try:
                    threads = sources.list_threads(server)
                    if threads:
                        return sources.APPSERVER, threads
                except sources.SourceError:
                    pass
            return sources.SESSION, sources.list_sessions(folder)
        self.executor.background(job, lambda result: done(*result) if isinstance(result, tuple)
                                 else done(sources.SESSION, []))

    def begin(self, ref, shown, first, persist, done, text=None):
        """Read the chosen conversation and start watching it; `done(problem)` with ""
        or: model | destination | first | a source error kind.

        `shown` is the destination the user was looking at when they agreed. Nothing is
        recorded, and nothing is read for sending, if it is not the one that would
        really be used. `text`: the conversation itself, for a pasted one."""
        config = self.settings.model_config()
        if not config.ready:
            done("model")
            return
        if shown != config.destination:
            done("destination")
            return

        def job():
            try:
                if ref.kind == sources.PASTED:
                    return sources.read_pasted(text or "")
                return self._read(ref, self._server() if ref.kind == sources.APPSERVER and self._server else None)
            except sources.SourceError as error:
                return error

        def finish(result):
            if not hasattr(result, "turns"):
                done(getattr(result, "kind", "unreadable"))
            elif first > len(result.turns):
                done("first")
            else:
                done(self.start(ref, result, shown, first=first, persist=persist))
        self.executor.background(job, finish)

    def start(self, ref, transcript, shown, first=1, persist=False):
        """Begin watching `ref` (already read once as `transcript`, on the user's
        request). Returns "" or why not: model | destination."""
        config = self.settings.model_config()
        if not config.ready:
            return "model"
        if shown != config.destination:
            return "destination"
        self.stop(quiet=True)
        self.ref, self.persist, self.title = ref, bool(persist), transcript.title
        self.session = ss.Session(destination=config.destination, first=first)
        self.problem, self._stamp, self._thread_stamp = "", stamp_of(ref), None
        self._next_look = self._clock() + self._interval()
        self.store.log("watch_started", {"source": ref.kind, "turns": len(transcript.turns), "first": int(first),
                                         "persist": bool(persist), "provider": config.provider})
        self._apply([ss.Changed()] + self.session.observe(transcript))
        return ""

    def paste(self, text):
        """The pasted conversation has grown: the user pasted the page again.
        Returns "" or a source error kind."""
        if self.session is None or self.ref.kind != sources.PASTED:
            return "format"
        try:
            transcript = sources.read_pasted(text)
        except sources.SourceError as error:
            return error.kind
        self._apply(self.session.observe(transcript))
        return ""

    def restore(self):
        """At start-up: pick up a ledger the user asked to keep. Nothing is sent until
        the conversation has been read again — and nothing at all if the model service
        is no longer the one that was agreed to."""
        try:
            found = vault.load(self.settings.folder)
        except Exception:
            log.exception("the stored ledger could not be read")
            found = None
        if found is None:
            return False
        self.ref, self.session = found
        self.persist, self.title, self.problem, self._stamp = True, "", "", None
        self._next_look = self._clock()
        if self.session.destination != self.settings.model_config().destination:
            self.session.state = ss.NEEDS_CONSENT
        self.store.log("watch_restored", {"source": self.ref.kind, "through": self.session.ledger.through})
        return True

    def stop(self, quiet=False):
        """End watching: the bubble goes, the stored ledger is deleted."""
        if self.session is None:
            return
        self._generation += 1
        had_bubble = self.session.bubble is not None
        self.session, self.ref, self.title, self.problem = None, None, "", ""
        removed = vault.forget(self.settings.folder)
        self.persist = False
        if had_bubble:
            self.view.hide_bubble("stopped")
        self.store.log("watch_stopped", {"stored_removed": removed})
        if not quiet:
            self.view.changed()

    def set_persist(self, keep):
        """Switch keeping the ledger on disk on or off for the watched conversation."""
        if self.session is None:
            return
        self.persist = bool(keep)
        if keep:
            self._store()
        else:
            vault.forget(self.settings.folder)
        self.view.changed()

    def clear_data(self):
        """"清除本机数据": everything this app stored, then back to a first-run state."""
        self.stop(quiet=True)
        removed = wipe(self.settings.folder)
        self.settings.data = dict(DEFAULTS)
        self.settings._key_cache = None
        self.view.changed()
        return removed

    def save_model(self, **fields):
        """Apply the model dialog. Raises ValueError with a reason the dialog shows."""
        config = self.settings.save_model(**fields)
        self.store.log("model", {"provider": config.provider})
        self.view.changed()
        return config

    # ------------------------------------------------------------ looking again
    def _interval(self):
        if self.ref is not None and self.ref.kind == sources.APPSERVER:
            return SERVER_POLL_SECONDS
        return self.settings.number("watch_poll_seconds", 1.0, 120.0)

    def tick(self):
        """Called regularly on the UI thread: is it time for another look?"""
        session = self.session
        if session is None or self._reading or self._clock() < self._next_look:
            return
        self._next_look = self._clock() + self._interval()
        if self.ref.kind == sources.PASTED or (session.paused and session.transcript.turns):
            return
        stamp = stamp_of(self.ref)
        if stamp is not None and stamp == self._stamp and session.transcript.turns and not self.problem:
            return                          # the file has not changed
        self.look(stamp)

    def look(self, stamp=None):
        """Read the conversation again, on a worker thread."""
        if self.session is None or self._reading or self.ref.kind == sources.PASTED:
            return
        self._reading = True
        ref, generation = self.ref, self._generation
        server = self._server() if (self._server is not None and ref.kind == sources.APPSERVER) else None
        known = self._thread_stamp if self.session.transcript.turns and not self.problem else None

        def job():
            try:
                if server is not None:
                    # A long thread is not paged through again when nothing was added to it.
                    updated = sources.thread_stamp(server, ref.conversation)
                    if updated and updated == known:
                        return None
                    return self._read(ref, server), updated
                return self._read(ref, server), None
            except sources.SourceError as error:
                return error

        def done(result):
            self._reading = False
            if generation != self._generation or self.session is None or result is None:
                return
            if isinstance(result, tuple):
                result, self._thread_stamp = result
            if not hasattr(result, "turns"):
                kind = getattr(result, "kind", "unreadable")
                if kind != self.problem:
                    self.problem = kind
                    self.store.log("watch_unreadable", {"why": kind})
                    self.view.notice("watch_unreadable", kind)
                    self.view.changed()
                return
            self._stamp = stamp if stamp is not None else stamp_of(ref)
            if self.problem:
                self.problem = ""
                self.view.changed()
            if not self.title:
                self.title = result.title
            self._apply(self.session.observe(result))

        self.executor.background(job, done)

    # ------------------------------------------------------------ the user
    def _do(self, action, *args):
        if self.session is not None:
            self._apply(getattr(self.session, action)(*args))

    def look_now(self):
        if self.session is None:
            self.view.notice("watch_none")
            return
        self._do("look_now")

    def took_back(self):
        self._do("took_back")

    def intended(self, note=""):
        self._do("intended", note)

    def wrong(self, note=""):
        self._do("wrong", note)

    def add_note(self, note):
        self._do("add_note", note)

    def dismiss(self):
        self._do("dismiss")

    def pause(self):
        self._do("pause")

    def resume(self):
        self._do("resume")
        self._next_look = self._clock()

    def correct_goal(self, text):
        self._do("correct_goal", text)

    def accept_branch(self):
        self._do("accept_branch")

    def agree(self, shown):
        """The model service changed since watching was agreed to; the user agrees
        again. Returns "" or "destination" / "model" as in `start`."""
        config = self.settings.model_config()
        if not config.ready:
            return "model"
        if shown != config.destination:
            return "destination"
        self._do("agree", config.destination)
        return ""
