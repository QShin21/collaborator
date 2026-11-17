"""The one entry point for chat context: signals from the window in, excerpt out.

    provider.excerpt(signals, draft) -> ContextExcerpt | None

Order of sources, per the brief: the App Server first, the session files as the
compatibility path. Either may be missing; with neither, or when the chat on
screen cannot be matched to a thread with confidence, the result is None.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from typing import Optional

from . import appserver as appserver_module
from . import extract, identify, rollout
from .model import ContextExcerpt, ThreadInfo

_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def thread_id_in(url):
    """A thread id embedded in the page address, if the app routes chats that way."""
    match = _UUID.search(url or "")
    return match.group(0).casefold() if match else ""

log = logging.getLogger(__name__)

CANDIDATES = 12            # recent threads considered
LIST_TTL = 4.0             # seconds a thread list is reused (a turn may just have finished)
RETRY_AFTER = 120.0        # seconds before a failed app server is tried again
TURNS_KEPT = 6


class ContextProvider:
    def __init__(self, codex_command="", sessions_dir="", server=None, use_appserver=True,
                 clock=time.monotonic):
        self._sessions_dir = sessions_dir
        self._clock = clock
        self._lock = threading.Lock()
        self._server = server
        self._own_server = server is None
        self._command = codex_command
        self._use_appserver = use_appserver
        self._server_down_until = 0.0
        self._listed = (0.0, [], "")            # (at, threads, source)
        self._turns = {}                        # thread id -> (stamp, turns, source)
        self.last = {}                          # diagnostics of the last excerpt() call

    # ------------------------------------------------------------ sources
    def _app(self):
        if not self._use_appserver or self._clock() < self._server_down_until:
            return None
        if self._server is None and self._own_server:
            command = appserver_module.find_command(self._command)
            if not command:
                self._server_down_until = self._clock() + RETRY_AFTER
                return None
            self._server = appserver_module.AppServer(command)
        return self._server

    def _app_failed(self, error):
        log.info("app server unavailable: %s", error)
        self._server_down_until = self._clock() + RETRY_AFTER

    def _threads(self):
        at, threads, source = self._listed
        if threads and self._clock() - at < LIST_TTL:
            return threads, source
        threads, source = [], ""
        app = self._app()
        if app is not None:
            try:
                threads, source = app.list_threads(CANDIDATES), "appserver"
            except appserver_module.AppServerError as error:
                self._app_failed(error)
        if not threads:
            try:
                threads = rollout.list_recent(rollout.sessions_dir(self._sessions_dir), CANDIDATES)
                source = "rollout" if threads else ""
            except OSError as error:
                log.info("session files unavailable: %s", error)
        threads = [t for t in threads if t.id]
        self._listed = (self._clock(), threads, source)
        return threads, source

    def _load_turns(self, thread):
        # Without a modification time the cache is only trusted for a few seconds.
        stamp = thread.updated_at or -(self._clock() // LIST_TTL)
        cached = self._turns.get(thread.id)
        if cached and cached[0] == stamp:
            return cached[1], cached[2]
        turns, source = None, ""
        app = self._app() if thread.source == "appserver" else None
        if app is not None:
            try:
                turns, source = app.list_turns(thread.id, TURNS_KEPT), "appserver"
            except appserver_module.AppServerError as error:
                if error.code is None:
                    self._app_failed(error)
        if turns is None and thread.path:
            try:
                turns, source = rollout.read_turns(thread.path, TURNS_KEPT), "rollout"
            except OSError as error:
                log.info("cannot read session file: %s", error)
        turns = turns or []
        self._turns[thread.id] = (stamp, turns, source)
        if len(self._turns) > 4 * CANDIDATES:
            for key in list(self._turns)[: 2 * CANDIDATES]:
                del self._turns[key]
        return turns, source

    # ------------------------------------------------------------ public
    def available(self):
        """(appserver_command_found, sessions_dir_exists) — for the self-check."""
        return (bool(appserver_module.find_command(self._command)) if self._own_server else True,
                rollout.sessions_dir(self._sessions_dir).is_dir())

    def _add_addressed(self, signals, threads, source):
        """A thread named in the page address but too old to be in the recent list."""
        wanted = thread_id_in(getattr(signals, "url", "") or "")
        if not wanted or any(t.id.casefold() == wanted for t in threads):
            return threads
        if source == "appserver":
            return threads + [ThreadInfo(id=wanted, source="appserver")]
        root = rollout.sessions_dir(self._sessions_dir)
        try:
            match = next(iter(root.rglob(f"rollout-*{wanted}.jsonl")), None) if root.is_dir() else None
        except OSError:
            match = None
        return threads + [rollout.read_meta(match)] if match else threads

    def probe(self):
        """Structure only — which sources answer and how much is there. No text."""
        with self._lock:
            appserver, sessions = self.available()
            threads, source = self._threads()
            turns = finished = 0
            for thread in threads[:3]:
                loaded, _ = self._load_turns(thread)
                turns += len(loaded)
                finished += sum(1 for turn in loaded if turn.completed)
            return {"appserver_command": bool(appserver), "sessions_folder": bool(sessions),
                    "threads": len(threads), "source": source or "none",
                    "turns_sampled": turns, "turns_finished": finished}

    def identify(self, signals):
        threads, source = self._threads()
        threads = self._add_addressed(signals, threads, source)
        for thread in threads:
            turns, _ = self._load_turns(thread)
            thread.probes = identify.probes_for(turns)
        return identify.identify(signals, threads)

    def excerpt(self, signals, draft) -> Optional[ContextExcerpt]:
        with self._lock:
            self.last = {"identified": False, "candidates": 0, "source": "", "turns": 0}
            if signals is None:
                return None
            found = self.identify(signals)
            self.last.update(candidates=found.candidates, score=found.score, runner_up=found.runner_up)
            if not found.confident:
                return None
            turns, source = self._turns[found.thread_id][1:]
            text, used = extract.excerpt(turns, draft)
            self.last.update(identified=True, source=source, turns=used)
            if not text:
                return None
            return ContextExcerpt(text=text, thread_id=found.thread_id, turns=used, source=source)

    def close(self):
        if self._server is not None and self._own_server:
            self._server.close()
            self._server = None
