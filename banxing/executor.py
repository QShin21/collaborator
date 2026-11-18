"""Where work runs. The app has two kinds of thread and this is the only module
that knows it:

  UI thread       everything that touches the session, the ledger and the windows
  worker threads  reading the conversation again; calls to the model service

`background(job, done)` runs `job` on a worker and `done(result)` back on the UI
thread (through `pump`, which the UI calls regularly). Tests use InlineExecutor,
where everything runs at once on the calling thread — or is held back on purpose to
play "the model is slow".
"""
from __future__ import annotations

import logging
import queue
import threading

log = logging.getLogger(__name__)


class InlineExecutor:
    """Runs jobs on the calling thread. With hold=True jobs wait for release()."""

    def __init__(self, hold=False):
        self.hold = hold
        self.held = []

    def background(self, job, done):
        if self.hold:
            self.held.append((job, done))
        else:
            done(job())

    def release(self, count=None):
        jobs, self.held = self.held[:count], self.held[count:] if count else []
        for job, done in jobs:
            done(job())

    def pump(self, limit=50):
        pass


class ThreadedExecutor:
    """Production executor. `pump()` must be called regularly on the UI thread."""

    def __init__(self, max_workers=2):
        self._inbox = queue.Queue()          # callables for the UI thread
        self._slots = threading.Semaphore(max_workers)

    def background(self, job, done):
        """A job that raises hands over its exception, so the caller decides what
        that means."""
        def work():
            with self._slots:
                try:
                    result = job()
                except Exception as error:
                    log.exception("background job failed")
                    result = error
            self._inbox.put(lambda: done(result))
        threading.Thread(target=work, name="background", daemon=True).start()

    def pump(self, limit=50):
        for _ in range(limit):
            try:
                call = self._inbox.get_nowait()
            except queue.Empty:
                return
            try:
                call()
            except Exception:
                log.exception("handling a result failed")
