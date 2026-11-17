"""SQLite persistence. One short-lived connection per operation; safe across threads."""
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

# Append new migrations; never edit old ones. PRAGMA user_version tracks progress.
MIGRATIONS = [
    """
    CREATE TABLE sessions (
        id TEXT PRIMARY KEY, title TEXT, agent TEXT, phase TEXT,
        state TEXT, created REAL, updated REAL);
    CREATE TABLE messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT,
        content TEXT, meta TEXT, created REAL);
    CREATE INDEX idx_messages_session ON messages(session_id, id);
    CREATE TABLE bubbles (
        id TEXT PRIMARY KEY, session_id TEXT, kind TEXT, text TEXT, why TEXT,
        status TEXT, created REAL, resolved REAL);
    CREATE INDEX idx_bubbles_session ON bubbles(session_id, created);
    CREATE TABLE tests (
        id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, phase TEXT,
        likert TEXT, answer TEXT, scores TEXT, created REAL);
    CREATE TABLE events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, type TEXT,
        payload TEXT, created REAL);
    CREATE TABLE arena (
        id TEXT PRIMARY KEY, problem TEXT, entries TEXT, result TEXT, created REAL);
    CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT);
    """,
]


def new_id():
    return uuid.uuid4().hex[:12]


def _dump(value):
    return json.dumps(value, ensure_ascii=False)


def _load(text, default=None):
    if text in (None, ""):
        return default
    try:
        return json.loads(text)
    except ValueError:
        return default


class Store:
    def __init__(self, path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._memory = None
        if self.path == ":memory:":
            self._memory = sqlite3.connect(":memory:", check_same_thread=False)
            self._memory.row_factory = sqlite3.Row
        self._migrate()

    # -- plumbing ---------------------------------------------------------
    def _run(self, fn):
        with self._lock:
            if self._memory is not None:
                result = fn(self._memory)
                self._memory.commit()
                return result
            conn = sqlite3.connect(self.path, timeout=15)
            conn.row_factory = sqlite3.Row
            try:
                result = fn(conn)
                conn.commit()
                return result
            finally:
                conn.close()

    def _migrate(self):
        def go(conn):
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            for index in range(version, len(MIGRATIONS)):
                conn.executescript(MIGRATIONS[index])
                conn.execute(f"PRAGMA user_version = {index + 1}")
        self._run(go)

    def _all(self, sql, args=()):
        return self._run(lambda c: [dict(r) for r in c.execute(sql, args).fetchall()])

    def _one(self, sql, args=()):
        rows = self._all(sql, args)
        return rows[0] if rows else None

    def _exec(self, sql, args=()):
        return self._run(lambda c: c.execute(sql, args).lastrowid)

    # -- sessions ---------------------------------------------------------
    @staticmethod
    def _session(row):
        if row is None:
            return None
        row["state"] = _load(row["state"], {})
        return row

    def create_session(self, title, agent, phase, state):
        sid = new_id()
        now = time.time()
        self._exec("INSERT INTO sessions VALUES (?,?,?,?,?,?,?)",
                   (sid, title, agent, phase, _dump(state), now, now))
        return self.get_session(sid)

    def get_session(self, sid):
        return self._session(self._one("SELECT * FROM sessions WHERE id=?", (sid,)))

    def list_sessions(self):
        rows = self._all(
            "SELECT id, title, agent, phase, created, updated FROM sessions ORDER BY updated DESC")
        return rows

    def all_sessions(self):
        return [self._session(r) for r in self._all("SELECT * FROM sessions ORDER BY created")]

    def save_session(self, session):
        self._exec("UPDATE sessions SET title=?, phase=?, state=?, updated=? WHERE id=?",
                   (session["title"], session["phase"], _dump(session["state"]),
                    time.time(), session["id"]))

    def delete_session(self, sid):
        def go(conn):
            for table in ("messages", "bubbles", "tests", "events"):
                conn.execute(f"DELETE FROM {table} WHERE session_id=?", (sid,))
            conn.execute("DELETE FROM sessions WHERE id=?", (sid,))
        self._run(go)

    # -- messages ---------------------------------------------------------
    def add_message(self, sid, role, content, meta=None):
        now = time.time()
        mid = self._exec(
            "INSERT INTO messages (session_id, role, content, meta, created) VALUES (?,?,?,?,?)",
            (sid, role, content, _dump(meta or {}), now))
        return {"id": mid, "session_id": sid, "role": role, "content": content,
                "meta": meta or {}, "created": now}

    def messages(self, sid, limit=None):
        rows = self._all("SELECT * FROM messages WHERE session_id=? ORDER BY id", (sid,))
        for row in rows:
            row["meta"] = _load(row["meta"], {})
        return rows[-limit:] if limit else rows

    # -- bubbles ----------------------------------------------------------
    def add_bubble(self, sid, kind, text, why):
        bid = new_id()
        now = time.time()
        self._exec("INSERT INTO bubbles VALUES (?,?,?,?,?,?,?,?)",
                   (bid, sid, kind, text, why, "shown", now, None))
        return {"id": bid, "session_id": sid, "kind": kind, "text": text, "why": why,
                "status": "shown", "created": now, "resolved": None}

    def get_bubble(self, bid):
        return self._one("SELECT * FROM bubbles WHERE id=?", (bid,))

    def resolve_bubble(self, bid, status):
        self._exec("UPDATE bubbles SET status=?, resolved=? WHERE id=?",
                   (status, time.time(), bid))

    def bubbles(self, sid, limit=None):
        rows = self._all("SELECT * FROM bubbles WHERE session_id=? ORDER BY created", (sid,))
        return rows[-limit:] if limit else rows

    def all_bubbles(self):
        return self._all("SELECT * FROM bubbles ORDER BY created")

    # -- tests ------------------------------------------------------------
    def add_test(self, sid, phase, likert, answer, scores):
        self._exec("DELETE FROM tests WHERE session_id=? AND phase=?", (sid, phase))
        self._exec(
            "INSERT INTO tests (session_id, phase, likert, answer, scores, created) "
            "VALUES (?,?,?,?,?,?)",
            (sid, phase, _dump(likert), answer, _dump(scores), time.time()))

    @staticmethod
    def _test(row):
        row["likert"] = _load(row["likert"], {})
        row["scores"] = _load(row["scores"], {})
        return row

    def tests(self, sid):
        return [self._test(r) for r in
                self._all("SELECT * FROM tests WHERE session_id=? ORDER BY id", (sid,))]

    def all_tests(self):
        return [self._test(r) for r in self._all("SELECT * FROM tests ORDER BY id")]

    # -- events (research log) -------------------------------------------
    def log(self, sid, type_, payload=None):
        self._exec("INSERT INTO events (session_id, type, payload, created) VALUES (?,?,?,?)",
                   (sid, type_, _dump(payload or {}), time.time()))

    def events(self, sid=None):
        if sid:
            rows = self._all("SELECT * FROM events WHERE session_id=? ORDER BY id", (sid,))
        else:
            rows = self._all("SELECT * FROM events ORDER BY id")
        for row in rows:
            row["payload"] = _load(row["payload"], {})
        return rows

    # -- arena ------------------------------------------------------------
    def add_arena(self, problem, entries):
        aid = new_id()
        self._exec("INSERT INTO arena VALUES (?,?,?,?,?)",
                   (aid, problem, _dump(entries), None, time.time()))
        return aid

    def get_arena(self, aid):
        row = self._one("SELECT * FROM arena WHERE id=?", (aid,))
        if row:
            row["entries"] = _load(row["entries"], [])
            row["result"] = _load(row["result"])
        return row

    def rate_arena(self, aid, result):
        self._exec("UPDATE arena SET result=? WHERE id=?", (_dump(result), aid))

    def all_arena(self):
        rows = self._all("SELECT * FROM arena ORDER BY created")
        for row in rows:
            row["entries"] = _load(row["entries"], [])
            row["result"] = _load(row["result"])
        return rows

    # -- key/value (long-term learner profile etc.) -----------------------
    def kv_get(self, key, default=None):
        row = self._one("SELECT value FROM kv WHERE key=?", (key,))
        return _load(row["value"], default) if row else default

    def kv_set(self, key, value):
        self._exec("INSERT INTO kv (key, value) VALUES (?,?) "
                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, _dump(value)))
