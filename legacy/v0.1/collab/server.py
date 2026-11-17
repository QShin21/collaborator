"""HTTP server: a small JSON API plus the static web client. Standard library only."""
import json
import mimetypes
import os
import re
import sys
import threading
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import __version__, adapt, config
from .db import Store
from .engine import AGENTS, LIKERT_POST, LIKERT_PRE, RUBRIC, Engine, EngineError
from .llm import LLMError

WEB = config.ROOT / "web"
MAX_BODY = 8 * 1024 * 1024  # screenshots travel as base64 JSON
STATIC_TYPES = {
    ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8", ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon",
}

ROUTES = []


LOOPBACK_ADDRESSES = ("127.0.0.1", "::1", "::ffff:127.0.0.1")


def route(method, pattern, local_only=False):
    """`local_only` routes are refused unless the client connects from this machine."""
    def register(fn):
        fn.local_only = local_only
        ROUTES.append((method, re.compile("^" + pattern + "$"), fn))
        return fn
    return register


# ------------------------------------------------------------------ handlers
@route("GET", "/api/config")
def get_config(engine, body, query):
    return {"version": __version__, "provider": config.provider(), "model": config.model(),
            "agents": AGENTS, "kinds": adapt.KINDS,
            "likert": {"pre": LIKERT_PRE, "post": LIKERT_POST}, "rubric": RUBRIC}


@route("GET", "/api/sessions")
def list_sessions(engine, body, query):
    return {"sessions": engine.store.list_sessions()}


@route("POST", "/api/sessions")
def create_session(engine, body, query):
    session = engine.create_session(body.get("problem"), body.get("goal", ""),
                                    body.get("ideas", ""), body.get("agent", "collaborator"))
    pre = body.get("pretest")
    if isinstance(pre, dict) and (pre.get("answer") or pre.get("likert")):
        engine.submit_test(session["id"], "pre", pre.get("likert"), pre.get("answer"))
    return engine.view(session["id"])


@route("GET", "/api/sessions/([0-9a-f]+)")
def get_session(engine, body, query, sid):
    return engine.view(sid)


@route("PATCH", "/api/sessions/([0-9a-f]+)")
def patch_session(engine, body, query, sid):
    engine.update(sid, body)
    return engine.view(sid)


@route("DELETE", "/api/sessions/([0-9a-f]+)")
def delete_session(engine, body, query, sid):
    engine.store.delete_session(sid)
    return {"ok": True}


@route("POST", "/api/sessions/([0-9a-f]+)/frame")
def frame(engine, body, query, sid):
    engine.frame(sid, body.get("clarifications"), bool(body.get("force_map")))
    return engine.view(sid)


@route("POST", "/api/sessions/([0-9a-f]+)/plan")
def plan(engine, body, query, sid):
    engine.plan(sid, body.get("thread_ids") or [], body.get("custom_thread", ""),
                body.get("clarifications"))
    return engine.view(sid)


@route("POST", "/api/sessions/([0-9a-f]+)/chat")
def chat(engine, body, query, sid):
    return {"messages": engine.chat(sid, body.get("content"))}


@route("POST", "/api/sessions/([0-9a-f]+)/bubbles")
def bubbles(engine, body, query, sid):
    out = engine.bubbles(sid, notes=body.get("notes"), selection=body.get("selection", ""),
                         screenshot=body.get("screenshot"), trigger=body.get("trigger", "auto"),
                         active=body.get("active", 0))
    return {"bubbles": out}


@route("POST", "/api/bubbles/([0-9a-f]+)/feedback")
def bubble_feedback(engine, body, query, bid):
    return engine.feedback(bid, body.get("action"))


@route("POST", "/api/sessions/([0-9a-f]+)/test")
def submit_test(engine, body, query, sid):
    return engine.submit_test(sid, body.get("phase"), body.get("likert"), body.get("answer"))


@route("GET", "/api/sessions/([0-9a-f]+)/export")
def export(engine, body, query, sid):
    return {"markdown": engine.export_markdown(sid)}


@route("GET", "/api/sessions/([0-9a-f]+)/events")
def session_events(engine, body, query, sid):
    return {"events": engine.store.events(sid)}


@route("POST", "/api/arena")
def arena_create(engine, body, query):
    return engine.arena_create(body.get("problem"))


@route("POST", "/api/arena/([0-9a-f]+)/rate")
def arena_rate(engine, body, query, aid):
    return engine.arena_rate(aid, body.get("ratings"), body.get("best"))


@route("GET", "/api/stats")
def stats(engine, body, query):
    return engine.stats()


@route("GET", "/api/export")
def export_all(engine, body, query):
    """Full research dump: sessions, tests, bubbles, events, arena."""
    store = engine.store
    return {"sessions": store.all_sessions(), "tests": store.all_tests(),
            "bubbles": store.all_bubbles(), "events": store.events(), "arena": store.all_arena()}


@route("GET", "/api/settings", local_only=True)
def get_settings(engine, body, query):
    return config.settings()


@route("POST", "/api/settings", local_only=True)
def post_settings(engine, body, query):
    try:
        return config.save_settings(body)
    except ValueError as error:
        raise EngineError(str(error))
    except OSError as error:
        raise EngineError(f"could not write {config.ENV_FILE}: {error}", 500)


@route("POST", "/api/profile/reset")
def profile_reset(engine, body, query):
    engine.store.kv_set("profile", adapt.default_state())
    return {"profile": engine.profile()}


# -------------------------------------------------------------------- server
def make_handler(engine, quiet=False):
    class Handler(BaseHTTPRequestHandler):
        server_version = "collab/" + __version__

        def log_message(self, fmt, *args):
            if not quiet:
                sys.stderr.write("%s %s\n" % (self.log_date_time_string(), fmt % args))

        def _send(self, status, payload, content_type="application/json; charset=utf-8"):
            data = payload if isinstance(payload, bytes) else json.dumps(
                payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _static(self, path):
            if path in ("", "/"):
                path = "/index.html"
            target = (WEB / path.lstrip("/")).resolve()
            if WEB.resolve() not in target.parents or not target.is_file():
                return self._send(404, {"error": "not found"})
            # Explicit types first: on Windows, mimetypes consults the registry, which
            # can map .js to text/plain.
            kind = STATIC_TYPES.get(target.suffix.lower()) or mimetypes.guess_type(
                str(target))[0] or "application/octet-stream"
            self._send(200, target.read_bytes(), kind)

        def _refusal(self, is_api):
            """Why this request must not be served, or None.

            The server holds the user's work and API key and listens on localhost, so any
            website open in the same browser can try to reach it. Three checks:

            * Host must be a name we actually serve under. This is what stops DNS
              rebinding: a hostile page whose domain is re-pointed at 127.0.0.1 is
              same-origin as far as the browser is concerned, but it still has to send
              its own domain in Host. Comparing Origin with Host is NOT enough — under
              rebinding they match each other.
            * Origin, when the browser sends one, must be one of our own origins.
            * Sec-Fetch-Site, when present, must not be cross-site for API calls.
            """
            allowed = self.server.allowed_hosts
            if (self.headers.get("Host") or "").strip().lower() not in allowed:
                return "host not allowed"
            origin = self.headers.get("Origin")
            if origin is not None:
                parts = urlparse(origin.strip())
                if parts.scheme != "http" or parts.netloc.lower() not in allowed:
                    return "cross-origin request refused"
            site = (self.headers.get("Sec-Fetch-Site") or "").lower()
            if is_api and site and site not in ("same-origin", "none"):
                return "cross-site request refused"
            return None

        def _dispatch(self, method):
            parsed = urlparse(self.path)
            path = parsed.path
            is_api = path.startswith("/api/")
            refusal = self._refusal(is_api)
            if refusal:
                return self._send(403, {"error": refusal})
            if not is_api:
                if method == "GET":
                    return self._static(path)
                return self._send(405, {"error": "method not allowed"})
            body = {}
            if method in ("POST", "PATCH"):
                if "application/json" not in (self.headers.get("Content-Type") or "").lower():
                    return self._send(415, {"error": "Content-Type must be application/json"})
                length = int(self.headers.get("Content-Length") or 0)
                if length > MAX_BODY:
                    return self._send(413, {"error": "request too large"})
                raw = self.rfile.read(length) if length else b""
                if raw:
                    try:
                        body = json.loads(raw.decode("utf-8"))
                    except ValueError:
                        return self._send(400, {"error": "invalid JSON"})
                    if not isinstance(body, dict):
                        return self._send(400, {"error": "JSON object expected"})
            for route_method, pattern, fn in ROUTES:
                if route_method != method:
                    continue
                match = pattern.match(path)
                if not match:
                    continue
                if getattr(fn, "local_only", False) and self.client_address[0] not in LOOPBACK_ADDRESSES:
                    return self._send(403, {"error": "only available on the computer running the app"})
                try:
                    return self._send(200, fn(engine, body, parse_qs(parsed.query), *match.groups()))
                except EngineError as error:
                    return self._send(error.status, {"error": str(error)})
                except LLMError as error:
                    return self._send(502, {"error": f"model call failed: {error}"})
                except Exception as error:  # keep the server alive; report the bug
                    traceback.print_exc()
                    return self._send(500, {"error": f"internal error: {error}"})
            self._send(404, {"error": "not found"})

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_PATCH(self):
            self._dispatch("PATCH")

        def do_DELETE(self):
            self._dispatch("DELETE")

    return Handler


class _Server(ThreadingHTTPServer):
    # On Windows SO_REUSEADDR would let a second copy bind the same port silently.
    allow_reuse_address = os.name != "nt"


def make_server(host=None, port=None, store=None, quiet=False):
    engine = Engine(store or Store(config.db_path()))
    server = _Server((host or config.host(), config.port() if port is None else port),
                     make_handler(engine, quiet))
    server.daemon_threads = True
    server.engine = engine
    server.allowed_hosts = allowed_hosts(server.server_address[0], server.server_address[1])
    return server


def allowed_hosts(bound_host, port):
    """Host header values this server answers to: loopback names, the address it was
    explicitly bound to, and anything listed in COLLAB_ALLOWED_HOSTS (comma separated;
    needed when serving a LAN with COLLAB_HOST=0.0.0.0)."""
    names = {"127.0.0.1", "localhost", "[::1]"}
    bound = str(bound_host or "").lower()
    if bound and bound not in ("0.0.0.0", "::"):
        names.add(f"[{bound}]" if ":" in bound else bound)
    out = set()
    for name in names:
        out.add(f"{name}:{port}")
        if port == 80:
            out.add(name)
    for extra in (config.env("COLLAB_ALLOWED_HOSTS", "") or "").split(","):
        extra = extra.strip().lower()
        if not extra:
            continue
        has_port = extra.rsplit(":", 1)[-1].isdigit() and not extra.endswith("]")
        out.add(extra if has_port else f"{extra}:{port}")
    return out


def _bind():
    """Use the configured port, or the next free one if it is taken."""
    first = config.port()
    for port in range(first, first + 20):
        try:
            return make_server(port=port)
        except OSError:
            continue
    raise SystemExit(f"no free port between {first} and {first + 19}")


def main():
    for stream in (sys.stdout, sys.stderr):  # never die on a console that lacks a glyph
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    if sys.stderr is None:  # windowed build: no console to log to
        sys.stdout = sys.stderr = open(os.devnull, "w")
    server = _bind()
    host, port = server.server_address[:2]
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '') else host}:{port}"
    print(f"AI Collaborator {__version__} | provider: {config.provider()} | model: {config.model()}")
    if config.provider() == "mock":
        print("No API key yet: running in offline demo mode. "
              "Click the status label at the bottom left of the page to add one.")
    print(f"Your data: {config.HOME}")
    print(f"Open {url}   (keep this window open; close it or press Ctrl+C to stop)")
    if not config.env("COLLAB_NO_BROWSER"):
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
