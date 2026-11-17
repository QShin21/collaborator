"""Security properties, each with the attack it is there to stop.

  Redirects      a model endpoint (or anything in front of it) answering 3xx must
                 not get the API key and the draft forwarded to another origin, or
                 to plain http.
  Key binding    the saved API key belongs to the provider and endpoint it was
                 entered for; no environment variable or edited settings file may
                 point it somewhere else.
  Consent        drafts go only to a destination the user has seen and agreed to —
                 the effective one, after environment overrides.
  One request    no quiet second request with a changed body after an HTTP 400.
  Local only     the founder's brief is not published; tests cannot reach real
                 user data.

Everything here is synthetic: local servers on 127.0.0.1, made-up keys, temp folders.
"""
import json
import os
import shutil
import tempfile
import threading
import unicodedata
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import safety

from banxing import cli, config as config_module, examples, llm, mock, suggest
from banxing.config import Settings
from banxing.context import rollout

from rig import RigCase

ROOT = Path(__file__).resolve().parent.parent
KEY = "sk-test-not-a-real-key-0123456789"
DRAFT = examples.get("software")["draft"]
BRIEF = ROOT / "docs" / "product-brief-original.md"
QUOTE_LIMIT = 10        # a shared run this long (letters, digits, CJK) is a quote ...
QUOTE_CJK = 7           # ... if at least this much of it is Chinese text
_SKIPPED_ANYWHERE = {".git", "__pycache__", ".venv", "node_modules"}
_SKIPPED_AT_ROOT = {"build", "dist", "release", "data"}          # as in .gitignore
_TEXT_SUFFIXES = {".py", ".md", ".txt", ".yml", ".yaml", ".json", ".bat", ".ps1", ".html", ".js", ".css",
                  ".toml", ".cfg", ".ini", ".example", ".gitignore", ""}


def _letters(text):
    """Only what carries the wording: letters, digits and CJK, case- and width-folded."""
    return "".join(ch for ch in unicodedata.normalize("NFKC", text).casefold() if ch.isalnum())


def _published_text_files():
    """Text files that a commit of this working copy would publish: everything except
    what .gitignore keeps local (the brief, caches, build output) and local task notes."""
    for path in sorted(ROOT.rglob("*")):
        folders = path.relative_to(ROOT).parts[:-1]
        if not path.is_file() or _SKIPPED_ANYWHERE & set(folders) or folders[:1] and folders[0] in _SKIPPED_AT_ROOT:
            continue
        local_note = path.name.startswith(".") and path.name.endswith("-task.txt")     # see .gitignore
        if path == BRIEF or local_note or path.suffix.lower() not in _TEXT_SUFFIXES:
            continue
        yield path


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _serve(self):
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length) if length else b""
        self.server.requests.append({
            "method": self.command, "path": self.path, "body": raw.decode("utf-8", "replace"),
            "headers": {name.lower(): value for name, value in self.headers.items()}})
        status, headers, payload = self.server.responder(self.command, self.path)
        data = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except OSError:
            pass

    do_GET = do_POST = do_PUT = do_HEAD = _serve


class ServerCase(unittest.TestCase):
    def server(self, responder=None):
        """A local HTTP server that records every request, whatever the method."""
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        server.requests = []
        server.responder = responder or (lambda method, path: (200, {}, {"content": [{"type": "text", "text": "ok"}],
                                                                         "choices": [{"message": {"content": "ok"}}]}))
        server.url = f"http://127.0.0.1:{server.server_address[1]}"
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def temp(self):
        folder = Path(tempfile.mkdtemp(prefix="banxing-security-"))
        self.addCleanup(shutil.rmtree, folder, True)
        return folder

    @staticmethod
    def saw_key(server):
        return any(KEY in json.dumps(request) for request in server.requests)


# ============================================================== redirects
class RedirectTests(ServerCase):
    def setUp(self):
        self.other = self.server()                       # the place a redirect points to

    def redirecting(self, code, location):
        return self.server(lambda method, path: (code, {"Location": location}, b""))

    def configs(self, server):
        return [llm.ModelConfig("anthropic", base_url=server.url, api_key=KEY, effort="none"),
                llm.ModelConfig("openai", base_url=server.url + "/v1", api_key=KEY)]

    def test_no_redirect_status_sends_the_key_or_the_draft_to_another_origin(self):
        for code in (301, 302, 303, 307, 308):
            front = self.redirecting(code, self.other.url + "/v1/messages")
            for config in self.configs(front):
                with self.assertRaises(llm.LLMError) as caught:
                    llm.complete(config, "system", "用户的草稿", timeout=5)
                self.assertEqual(caught.exception.kind, "redirect", (code, config.provider))
                self.assertEqual(caught.exception.status, code)
            self.assertEqual(len(front.requests), 2)                 # one per provider, no second try
        self.assertEqual(self.other.requests, [])                    # nothing ever arrived there
        self.assertFalse(self.saw_key(self.other))

    def test_same_origin_redirects_are_not_followed_either(self):
        front = self.server()
        front.responder = lambda method, path: (
            (302, {"Location": front.url + "/moved"}, b"") if path != "/moved"
            else (200, {}, {"content": [{"type": "text", "text": "followed"}]}))
        with self.assertRaises(llm.LLMError) as caught:
            llm.complete(self.configs(front)[0], "s", "u", timeout=5)
        self.assertEqual(caught.exception.kind, "redirect")
        self.assertEqual([request["path"] for request in front.requests], ["/v1/messages"])

    def test_https_is_never_downgraded_and_never_leaves_its_host(self):
        """Without TLS certificates this cannot be played over the wire, so the rule
        is checked where urllib asks it: the redirect handler."""
        handler = llm._NoRedirects()
        request = urllib.request.Request("https://api.anthropic.com/v1/messages", data=b"{}", method="POST",
                                         headers={"x-api-key": KEY})
        for code in (301, 302, 303, 307, 308):
            for target in ("http://api.anthropic.com/v1/messages",          # downgrade, same host
                           "https://evil.example.net/v1/messages",          # another host
                           "https://api.anthropic.com:8443/v1/messages",    # another port
                           "https://api.anthropic.com/v2/messages"):        # even the same origin
                with self.assertRaises(llm.LLMError) as caught:
                    handler.redirect_request(request, None, code, "Found", {}, target)
                self.assertEqual(caught.exception.kind, "redirect", (code, target))

    def test_every_opener_uses_the_refusing_handler_and_only_that_one(self):
        for url in ("https://api.anthropic.com/v1/messages", "https://gateway.example.com/v1/chat/completions",
                    "http://127.0.0.1:11434/v1/chat/completions", "http://localhost:1/x"):
            handlers = [h for h in llm._opener(url).handlers if isinstance(h, urllib.request.HTTPRedirectHandler)]
            self.assertEqual([type(h) for h in handlers], [llm._NoRedirects], url)

    def test_the_error_names_only_scheme_and_host_of_the_target(self):
        front = self.redirecting(302, self.other.url + "/steal?token=abc123&echo=" + KEY)
        with self.assertRaises(llm.LLMError) as caught:
            llm.complete(self.configs(front)[0], "s", "u", timeout=5)
        text = str(caught.exception)
        self.assertIn("http://127.0.0.1", text)
        for hidden in (KEY, "abc123", "/steal"):
            self.assertNotIn(hidden, text)

    def test_a_redirect_without_a_location_is_still_an_error(self):
        front = self.server(lambda method, path: (302, {}, b""))
        with self.assertRaises(llm.LLMError) as caught:
            llm.complete(self.configs(front)[1], "s", "u", timeout=5)
        self.assertEqual(caught.exception.kind, "redirect")

    def test_the_refusal_reaches_the_user_as_an_error_state(self):
        front = self.redirecting(307, self.other.url + "/v1/messages")
        outcome = suggest.Suggester(lambda: self.configs(front)[0]).analyze(DRAFT)
        self.assertEqual((outcome.error, outcome.show), ("redirect", False))
        self.assertEqual(self.other.requests, [])

    def test_control_the_stock_urllib_opener_does_leak(self):
        """Why the handler exists: this is what urllib does when left alone."""
        front = self.redirecting(302, self.other.url + "/v1/messages")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        request = urllib.request.Request(front.url + "/v1/messages", data=b"{}", method="POST",
                                         headers={"x-api-key": KEY, "content-type": "application/json"})
        with opener.open(request, timeout=5) as response:
            response.read()
        if not self.saw_key(self.other):
            self.skipTest("this Python no longer forwards request headers across a redirect")
        self.assertEqual(self.other.requests[0]["headers"].get("x-api-key"), KEY)


# ============================================================== key binding
class KeyBindingTests(ServerCase):
    def saved(self, provider="anthropic", base_url="", folder=None):
        folder = folder or self.temp()
        Settings(folder, environ={}).save_model(provider, base_url=base_url, api_key=KEY)
        return folder

    def test_base_url_from_the_environment_alone_gets_no_saved_key(self):
        folder = self.saved()
        for url in ("https://evil.example.net", "https://evil.example.net/v1",
                    "https://api.anthropic.com:8443",          # same host, another port
                    "http://api.anthropic.com",                # same host, plain http
                    "https://api.anthropic.com/proxy",         # same origin, another path
                    "https://api.anthropic.com.evil.example.net"):
            config = Settings(folder, environ={"BANXING_BASE_URL": url}).model_config()
            self.assertEqual(config.api_key, "", url)
            self.assertFalse(config.ready, url)

    def test_nothing_reaches_a_destination_named_only_by_the_environment(self):
        trusted, other = self.server(), self.server()
        folder = self.saved("openai", trusted.url + "/v1")
        settings = Settings(folder, environ={"BANXING_BASE_URL": other.url + "/v1"})
        settings.data["consent"] = settings.destination()       # even if consent were somehow there
        outcome = suggest.Suggester(settings.model_config, permit=settings.consented).analyze(DRAFT)
        self.assertEqual(outcome.error, "config")
        self.assertEqual(other.requests, [])
        self.assertEqual(trusted.requests, [])

    def test_provider_from_the_environment_alone_gets_no_saved_key(self):
        folder = self.saved()
        for env in ({"BANXING_PROVIDER": "openai"},
                    {"BANXING_PROVIDER": "openai", "BANXING_BASE_URL": "https://api.anthropic.com"},
                    {"BANXING_PROVIDER": "anthropic", "BANXING_BASE_URL": "https://gateway.example.com"}):
            self.assertEqual(Settings(folder, environ=env).model_config().api_key, "", env)

    def test_an_override_must_bring_its_own_key(self):
        folder = self.saved()
        env = {"BANXING_BASE_URL": "https://gateway.example.com", "BANXING_API_KEY": "env-key"}
        config = Settings(folder, environ=env).model_config()
        self.assertEqual((config.api_key, config.origin), ("env-key", "https://gateway.example.com"))
        self.assertTrue(config.ready)

    def test_the_same_endpoint_written_differently_is_still_the_same(self):
        folder = self.saved()
        for url in ("https://api.anthropic.com", "https://API.Anthropic.com/", "https://api.anthropic.com:443"):
            self.assertEqual(Settings(folder, environ={"BANXING_BASE_URL": url}).model_config().api_key, KEY, url)
        self.assertEqual(Settings(folder, environ={"BANXING_MODEL": "another-model"}).model_config().api_key, KEY)
        self.assertEqual(Settings(folder, environ={"BANXING_PROVIDER": "anthropic"}).model_config().api_key, KEY)

    def test_an_edited_settings_file_does_not_move_the_key(self):
        folder = self.saved("openai", "https://gateway.example.com/v1")
        path = folder / "settings.json"
        for change in ({"base_url": "https://evil.example.net/v1"}, {"provider": "anthropic", "base_url": ""},
                       {"key_for": ""}):
            data = json.loads(path.read_text(encoding="utf-8"))
            original = dict(data)
            data.update(change)
            path.write_text(json.dumps(data), encoding="utf-8")
            config = Settings(folder, environ={}).model_config()
            self.assertEqual(config.api_key, "", change)
            self.assertFalse(config.ready, change)
            path.write_text(json.dumps(original), encoding="utf-8")
        self.assertEqual(Settings(folder, environ={}).model_config().api_key, KEY)

    def test_the_dialog_cannot_move_the_key_either(self):
        folder = self.saved("openai", "https://gateway.example.com/v1")
        settings = Settings(folder, environ={})
        for provider, url in (("openai", "https://gateway.example.com:8443/v1"), ("openai", "https://gateway.example.com/v2"),
                              ("openai", "https://evil.example.net/v1"), ("anthropic", "")):
            with self.assertRaises(ValueError) as caught:
                settings.save_model(provider, base_url=url)
            self.assertEqual(str(caught.exception), "api_key_again", (provider, url))
        self.assertEqual(Settings(folder, environ={}).model_config().target, "openai|https://gateway.example.com:443/v1")

    def test_env_locked_whenever_the_environment_decides_the_destination(self):
        folder = self.temp()
        self.assertFalse(Settings(folder, environ={}).env_locked)
        self.assertFalse(Settings(folder, environ={"BANXING_MODEL": "m"}).env_locked)
        self.assertTrue(Settings(folder, environ={"BANXING_BASE_URL": "https://x.example.com"}).env_locked)
        self.assertTrue(Settings(folder, environ={"BANXING_PROVIDER": "openai"}).env_locked)


# ============================================================== consent
class ConsentTests(ServerCase, RigCase):
    def configured(self, environ=None):
        folder = self.temp()
        settings = Settings(folder, environ={})
        settings.save_model("openai", base_url="https://gateway.example.com/v1", api_key=KEY)
        settings.give_consent()
        return folder, Settings(folder, environ=environ or {})

    def test_consent_names_scheme_host_and_port(self):
        folder, settings = self.configured()
        self.assertEqual(settings.get("consent"), "openai|https://gateway.example.com")
        self.assertTrue(settings.consented())
        settings.save_model("openai", base_url="https://gateway.example.com/v2", api_key=KEY)
        self.assertTrue(settings.consented())                       # same origin, another path
        for url in ("https://gateway.example.com:8443/v1", "https://other.example.net/v1",
                    "http://localhost:8080/v1"):
            settings.save_model("openai", base_url="https://gateway.example.com/v1", api_key=KEY)
            settings.give_consent()
            settings.save_model("openai", base_url=url, api_key=KEY)
            self.assertFalse(settings.consented(), url)
        settings.save_model("anthropic", base_url="https://gateway.example.com", api_key=KEY)
        self.assertEqual(settings.destination(), "anthropic|https://gateway.example.com")
        self.assertFalse(settings.consented())                      # another kind of service on that host

    def test_consent_does_not_cover_an_environment_override(self):
        other = self.server()
        folder, _ = self.configured()
        settings = Settings(folder, environ={"BANXING_BASE_URL": other.url + "/v1", "BANXING_API_KEY": "env-key"})
        self.assertTrue(settings.model_config().ready)
        self.assertEqual(settings.destination(), "openai|" + other.url)
        self.assertFalse(settings.consented())
        outcome = suggest.Suggester(settings.model_config, permit=settings.consented).analyze(DRAFT)
        self.assertEqual(outcome.error, "consent")
        self.assertEqual(other.requests, [])

    def test_consent_is_recorded_only_for_what_was_shown(self):
        folder, _ = self.configured()
        env = {"BANXING_BASE_URL": "https://override.example.org/v1", "BANXING_API_KEY": "env-key"}
        settings = Settings(folder, environ=env)
        self.assertFalse(settings.give_consent(shown="openai|https://gateway.example.com"))
        self.assertFalse(settings.consented())
        self.assertEqual(Settings(folder, environ=env).get("consent"), "openai|https://gateway.example.com")
        self.assertTrue(settings.give_consent(shown="openai|https://override.example.org"))
        self.assertTrue(Settings(folder, environ=env).consented())
        self.assertFalse(Settings(folder, environ={}).consented())  # and no longer for the saved one

    def test_the_suggester_refuses_before_looking_at_the_chat_or_the_network(self):
        class Context:
            asked = 0

            def excerpt(self, signals, draft):
                Context.asked += 1

        calls = []
        suggester = suggest.Suggester(lambda: llm.ModelConfig("openai", base_url="https://gateway.example.com/v1",
                                                              api_key=KEY),
                                      context=Context(), complete=lambda *a, **k: calls.append(a),
                                      permit=lambda config: False)
        outcome = suggester.analyze(DRAFT, signals=object())
        self.assertEqual((outcome.error, calls, Context.asked), ("consent", [], 0))

    def test_the_permit_is_asked_about_the_configuration_that_is_used(self):
        seen = []
        config = llm.ModelConfig("mock")
        suggest.Suggester(lambda: config, permit=lambda asked: seen.append(asked) or True).analyze(DRAFT)
        self.assertEqual(seen, [config])

    def test_following_cannot_send_without_consent_even_if_the_machine_thinks_it_may(self):
        rig = self.rig(environ={})
        rig.settings.save_model("openai", base_url="https://gateway.example.com/v1", api_key=KEY)
        self.assertEqual(rig.controller.setup_reason(), "consent")
        rig.machine.set_model_ready(True)                           # a bug elsewhere, simulated
        rig.pause_on(DRAFT)
        self.assertEqual(rig.calls, [])
        self.assertEqual(rig.view.state, ("error", "consent"))
        self.assertIsNone(rig.view.bubble)

    def test_the_dialog_agrees_only_to_the_destination_it_displayed(self):
        rig = self.rig(environ={"BANXING_API_KEY": "env-key"})      # not locked: the dialog edits freely
        fields = dict(provider="openai", base_url="https://gateway.example.com/v1", api_key=KEY, model="", effort="low")
        self.assertEqual(rig.controller.confirm_model("openai|https://evil.example.net", True, fields), "destination")
        self.assertEqual(rig.controller.setup_reason(), "consent")
        self.assertEqual(rig.controller.confirm_model("openai|https://gateway.example.com", False, fields), "")
        self.assertEqual(rig.controller.setup_reason(), "")
        self.assertFalse(rig.settings.get("use_context"))

    def test_the_dialog_with_an_environment_destination(self):
        env = {"BANXING_BASE_URL": "https://override.example.org/v1"}
        rig = self.rig(environ=env)
        rig.settings.data.update(provider="openai")
        fields = dict(provider="openai", base_url="https://gateway.example.com/v1", api_key=KEY, model="", effort="none")
        self.assertEqual(rig.controller.confirm_model("openai|https://override.example.org", True, fields), "env")
        self.assertEqual(rig.settings.get("api_key"), "")           # locked: the dialog's fields were not saved
        self.assertEqual(rig.settings.get("effort"), "none")        # only the effort is the dialog's to set
        env["BANXING_API_KEY"] = "env-key"
        self.assertEqual(rig.controller.confirm_model("openai|https://gateway.example.com", True, fields), "destination")
        self.assertEqual(rig.controller.setup_reason(), "consent")
        self.assertEqual(rig.controller.confirm_model("openai|https://override.example.org", True, fields), "")
        self.assertEqual(rig.controller.setup_reason(), "")

    def test_nothing_configured_and_the_offline_stand_in_need_no_consent(self):
        settings = Settings(self.temp(), environ={})
        self.assertEqual(settings.destination(), "")
        self.assertFalse(settings.model_config().ready)
        mock_settings = Settings(self.temp(), environ={"BANXING_PROVIDER": "mock"})
        self.assertEqual((mock_settings.destination(), mock_settings.consented()), ("", True))


class DemoConsentTests(ServerCase):
    """`--demo` sends a draft too, so it obeys the same rule."""

    def setUp(self):
        reply = {"choices": [{"message": {"content": mock.complete({"draft": DRAFT, "mode": "every"})}}]}
        self.model = self.server(lambda method, path: (200, {}, reply))
        self.saved = {name: os.environ.get(name) for name in
                      ("BANXING_HOME", "BANXING_PROVIDER", "BANXING_BASE_URL", "BANXING_API_KEY")}
        self.addCleanup(self.restore)
        os.environ.update(BANXING_HOME=str(self.temp()), BANXING_PROVIDER="openai",
                          BANXING_BASE_URL=self.model.url + "/v1", BANXING_API_KEY=KEY)
        self.lines = []
        self.emit, cli.emit = cli.emit, self.lines.append
        self.addCleanup(setattr, cli, "emit", self.emit)

    def restore(self):
        for name, value in self.saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_demo_sends_nothing_until_the_destination_is_agreed_to_by_name(self):
        self.assertEqual(cli.main(["--demo", DRAFT]), 3)
        self.assertIn(self.model.url, "\n".join(self.lines))            # it says where it would go
        self.assertEqual(cli.main(["--demo", DRAFT, "--agree", "https://api.anthropic.com"]), 3)
        self.assertEqual(cli.main(["--demo", DRAFT, "--agree", self.model.url.replace("127.0.0.1", "localhost")]), 3)
        self.assertEqual(self.model.requests, [])

        self.assertEqual(cli.main(["--demo", DRAFT, "--agree", self.model.url]), 0)
        self.assertEqual(len(self.model.requests), 1)
        self.assertIn("先用表格搭一版", "\n".join(self.lines))
        self.assertEqual(cli.main(["--demo", DRAFT]), 0)                # remembered for this destination
        self.assertEqual(len(self.model.requests), 2)

        other = self.server()
        os.environ["BANXING_BASE_URL"] = other.url + "/v1"              # a new destination: ask again
        self.assertEqual(cli.main(["--demo", DRAFT]), 3)
        self.assertEqual(other.requests, [])


# ============================================================== one request
class NoSilentRetryTests(ServerCase):
    def config(self, server, effort="low"):
        return llm.ModelConfig("anthropic", base_url=server.url, api_key=KEY, effort=effort)

    def test_an_arbitrary_400_is_reported_after_exactly_one_request(self):
        for message in ("max_tokens: 99999 > 8192, which is the maximum allowed", "model: not found",
                        "prompt is too long", "Your credit balance is too low", ""):
            server = self.server(lambda method, path, message=message: (400, {}, {"error": {"message": message}}))
            with self.assertRaises(llm.LLMError) as caught:
                llm.complete(self.config(server), "s", "含有用户草稿的请求", timeout=5)
            self.assertEqual((caught.exception.kind, caught.exception.status), ("http", 400), message)
            self.assertEqual(len(server.requests), 1, message)
            self.assertIn("output_config", server.requests[0]["body"])

    def test_a_rejected_effort_field_is_named_and_not_retried(self):
        server = self.server(lambda method, path: (
            400, {}, {"error": {"type": "invalid_request_error", "message": "output_config: Extra inputs are not permitted"}}))
        with self.assertRaises(llm.LLMError) as caught:
            llm.complete(self.config(server), "s", "u", timeout=5)
        self.assertEqual(caught.exception.kind, "effort")
        self.assertEqual(len(server.requests), 1)
        outcome = suggest.Suggester(lambda: self.config(server)).analyze(DRAFT)
        self.assertEqual(outcome.error, "effort")
        self.assertEqual(len(server.requests), 2)                   # one per attempt the user made

    def test_effort_none_is_how_the_user_turns_the_field_off(self):
        server = self.server()
        self.assertEqual(llm.complete(self.config(server, effort="none"), "s", "u", timeout=5), "ok")
        self.assertNotIn("output_config", server.requests[0]["body"])
        self.assertEqual(len(server.requests), 1)

    def test_other_failures_are_not_retried_either(self):
        for status in (401, 404, 429, 500, 529):
            server = self.server(lambda method, path, status=status: (status, {}, {"error": "x"}))
            for config in (self.config(server), llm.ModelConfig("openai", base_url=server.url, api_key=KEY)):
                with self.assertRaises(llm.LLMError):
                    llm.complete(config, "s", "u", timeout=5)
            self.assertEqual(len(server.requests), 2, status)


# ============================================================== local only
class LocalOnlyTests(unittest.TestCase):
    def test_the_original_brief_is_ignored_by_git_and_referenced_by_no_build(self):
        ignored = [line.strip() for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()]
        self.assertIn("docs/product-brief-original.md", ignored)
        for path in sorted((ROOT / ".github" / "workflows").glob("*.yml")) + sorted((ROOT / "banxing").rglob("*.py")):
            self.assertNotIn("product-brief-original", path.read_text(encoding="utf-8"), path.name)

    def test_nothing_published_quotes_the_brief_at_length(self):
        """Runs only where the brief exists (a working copy that has it); it is not in
        the repository, so on CI this is skipped. Every text file that would be
        published is compared with it: a shared run of QUOTE_LIMIT or more letters /
        digits / CJK characters (spacing, punctuation and markup ignored) is a quote."""
        if not BRIEF.is_file():
            self.skipTest("the original brief is not in this working copy")
        brief = _letters(BRIEF.read_text(encoding="utf-8"))
        grams = {brief[i:i + QUOTE_LIMIT] for i in range(len(brief) - QUOTE_LIMIT + 1)}
        quotes = []
        for path in _published_text_files():
            try:
                text = _letters(path.read_text(encoding="utf-8"))
            except (UnicodeDecodeError, OSError):
                continue
            start = None
            for i in range(len(text) - QUOTE_LIMIT + 2):
                hit = i <= len(text) - QUOTE_LIMIT and text[i:i + QUOTE_LIMIT] in grams
                if hit and start is None:
                    start = i
                elif not hit and start is not None:
                    run = text[start:i - 1 + QUOTE_LIMIT]
                    # Names of interfaces and products ("UI Automation", "thread/turns/list")
                    # are not wording; a quote is counted by its Chinese characters.
                    if sum(1 for ch in run if "一" <= ch <= "鿿") >= QUOTE_CJK:
                        quotes.append((len(run), str(path.relative_to(ROOT)), run))
                    start = None
        quotes.sort(key=lambda quote: (-quote[0], quote[1]))
        listing = "\n".join(f"{length:4d}  {name}: {run}" for length, name, run in quotes[:120])
        self.assertEqual(len(quotes), 0, f"{len(quotes)} passage(s) repeat the brief:\n{listing}")

    def test_tests_run_in_a_sandbox_away_from_real_user_data(self):
        sandbox = Path(safety.SANDBOX)
        self.assertIn(sandbox, rollout.sessions_dir().parents)
        self.assertIn(sandbox, config_module.home().parents)
        self.assertFalse(rollout.sessions_dir().exists())
        for name in safety.CLEARED:
            self.assertNotIn(name, os.environ)
        self.assertFalse(Settings().model_config().ready)

    def test_every_test_module_enters_the_sandbox_first(self):
        for path in sorted((ROOT / "tests").glob("test_*.py")) + [ROOT / "tests" / "rig.py"]:
            text = path.read_text(encoding="utf-8")
            if "import" not in text:
                continue                                            # a placeholder with no code
            self.assertIn("import safety", text, path.name)
            first_banxing = text.find("from banxing")
            if first_banxing >= 0:
                self.assertLess(text.find("import safety"), first_banxing, path.name)

    def test_no_test_names_the_real_codex_or_settings_folders(self):
        needles = ("expanduser", "Path.home()", "/.codex", "\\.codex", "APPDATA")
        for path in sorted((ROOT / "tests").glob("*.py")):
            if path.name == "test_security.py":
                continue
            text = path.read_text(encoding="utf-8")
            for needle in needles:
                self.assertNotIn(needle, text, f"{path.name} mentions {needle}")


if __name__ == "__main__":
    unittest.main()
