"""Preferred source: the Codex App Server (`codex app-server`), read-only.

Protocol as implemented here: JSON-RPC 2.0 messages, one per line, over the child
process's stdin/stdout (the "jsonrpc" field is optional on the wire). After the
`initialize` / `initialized` handshake only two kinds of request are ever sent:

  thread/list         recent threads (id, preview, name, cwd, updatedAt, path)
  thread/turns/list   the turns of one thread, by thread id, paged
                      (falls back to thread/read with includeTurns)

No thread is started, resumed or modified, and requests coming from the server
(approvals and the like) are declined.

STATUS: written from the published protocol description and exercised against a
synthetic server in tests/. It has not been run against a real Codex install —
see docs/verification.md. Every call is wrapped so that any mismatch simply makes
the provider fall back to the .jsonl files.
"""
from __future__ import annotations

import json
import queue
import shutil
import subprocess
import sys
import threading

from .. import __version__
from .model import thread_from_api, turns_from_api

METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602


class AppServerError(Exception):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


def find_command(explicit=""):
    """The command line for the app server, or None when Codex is not on PATH."""
    if explicit:
        parts = explicit if isinstance(explicit, (list, tuple)) else [explicit]
        return list(parts) + ([] if "app-server" in parts else ["app-server"])
    path = shutil.which("codex")
    return [path, "app-server"] if path else None


class AppServer:
    def __init__(self, command, timeout=6.0):
        self.command = list(command)
        self.timeout = timeout
        self._process = None
        self._lock = threading.Lock()         # one request at a time
        self._write_lock = threading.Lock()   # the reader thread also writes (declines)
        self._replies = queue.Queue()
        self._next_id = 0

    # ------------------------------------------------------------ lifecycle
    @property
    def running(self):
        return self._process is not None and self._process.poll() is None

    def start(self):
        if self.running:
            return
        flags = 0x08000000 if sys.platform == "win32" else 0     # CREATE_NO_WINDOW
        try:
            self._process = subprocess.Popen(
                self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, creationflags=flags)
        except OSError as error:
            self._process = None
            raise AppServerError(f"cannot start the app server: {error}") from error
        self._replies = queue.Queue()
        threading.Thread(target=self._read, args=(self._process, self._replies),
                         name="appserver-reader", daemon=True).start()
        try:
            self._request("initialize", {"clientInfo": {
                "name": "banxing", "title": "Banxing", "version": __version__}})
            self._send({"method": "initialized", "params": {}})
        except AppServerError:
            self.close()
            raise

    def close(self):
        process, self._process = self._process, None
        if process is None:
            return
        # Order matters: the reader thread is blocked on stdout, and closing a pipe
        # another thread is reading waits for that read. End the process first.
        try:
            process.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            process.terminate()
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                pass
        try:
            process.stdout.close()
        except (OSError, ValueError):
            pass

    # ------------------------------------------------------------ wire
    def _read(self, process, replies):
        try:
            for raw in process.stdout:
                try:
                    message = json.loads(raw.decode("utf-8", "replace"))
                except ValueError:
                    continue
                if not isinstance(message, dict):
                    continue
                if "id" in message and "method" in message:
                    # A request from the server. This client only reads; decline.
                    try:
                        self._send({"id": message["id"], "error": {
                            "code": METHOD_NOT_FOUND, "message": "not supported by this client"}}, process)
                    except AppServerError:
                        pass
                elif "id" in message:
                    replies.put(message)
        except (OSError, ValueError):
            pass
        replies.put(None)                      # the stream ended

    def _send(self, message, process=None):
        process = process or self._process
        if process is None or process.stdin is None:
            raise AppServerError("the app server is not running")
        try:
            with self._write_lock:
                process.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
                process.stdin.flush()
        except (OSError, ValueError) as error:
            raise AppServerError(f"cannot write to the app server: {error}") from error

    def _request(self, method, params):
        self._next_id += 1
        request_id = self._next_id
        self._send({"id": request_id, "method": method, "params": params})
        while True:
            try:
                reply = self._replies.get(timeout=self.timeout)
            except queue.Empty:
                raise AppServerError(f"{method}: no reply within {self.timeout}s") from None
            if reply is None:
                raise AppServerError(f"{method}: the app server closed the connection")
            if reply.get("id") != request_id:
                continue                           # a reply to a request that timed out earlier
            if reply.get("error"):
                error = reply["error"] if isinstance(reply["error"], dict) else {}
                raise AppServerError(f"{method}: {error.get('message', 'error')}", error.get("code"))
            result = reply.get("result")
            return result if isinstance(result, dict) else {}

    def request(self, method, params):
        with self._lock:
            self.start()
            try:
                return self._request(method, params)
            except AppServerError as error:
                if error.code is None:             # transport problem: start fresh next time
                    self.close()
                raise

    # ------------------------------------------------------------ reads
    def list_threads(self, limit=20):
        try:
            result = self.request("thread/list", {"limit": limit, "sortKey": "updated_at"})
        except AppServerError as error:
            if error.code != INVALID_PARAMS:
                raise
            result = self.request("thread/list", {"limit": limit})
        items = result.get("data") or result.get("threads") or []
        threads = [thread_from_api(item) for item in items if isinstance(item, dict)]
        return [t for t in threads if t.id]

    def list_turns(self, thread_id, limit=6):
        """The last `limit` turns of a thread, oldest first."""
        try:
            result = self.request("thread/turns/list", {
                "threadId": thread_id, "limit": limit, "sortDirection": "desc"})
            items = result.get("data") or result.get("turns") or []
            return turns_from_api(list(reversed(items)))
        except AppServerError as error:
            if error.code not in (METHOD_NOT_FOUND, INVALID_PARAMS):
                raise
        # Older servers: read the whole thread and keep its tail.
        result = self.request("thread/read", {"threadId": thread_id, "includeTurns": True})
        thread = result.get("thread") if isinstance(result.get("thread"), dict) else result
        return turns_from_api((thread.get("turns") or [])[-limit:])
