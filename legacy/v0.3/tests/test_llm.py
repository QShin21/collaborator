"""The two real providers, against a local HTTP server that imitates their wire
format. This checks our side of the request/response handling; it is not a test
of the services themselves and uses no real key."""
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import safety  # noqa: F401  (keeps tests away from real user data)

from banxing import llm


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        server = self.server
        body = json.loads(self.rfile.read(int(self.headers.get("content-length", "0"))).decode("utf-8"))
        server.requests.append({"path": self.path, "headers": dict(self.headers.items()), "body": body})
        status, reply = server.responder(self.path, body)
        raw = reply if isinstance(reply, bytes) else json.dumps(reply, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        except OSError:
            pass                    # the client gave up waiting (the timeout test)


class LLMWireTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.requests = []
        self.server.responder = lambda path, body: (500, {"error": "unset"})
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def headers(self, index=-1):
        return {name.lower(): value for name, value in self.server.requests[index]["headers"].items()}

    def test_anthropic_request_and_reply(self):
        self.server.responder = lambda path, body: (200, {"content": [
            {"type": "thinking", "thinking": "…"}, {"type": "text", "text": ' {"show": false} '}]})
        config = llm.ModelConfig("anthropic", base_url=self.base, api_key="test-key", effort="low")
        self.assertEqual(llm.complete(config, "系统提示", "用户内容", max_tokens=321), '{"show": false}')
        request = self.server.requests[0]
        self.assertEqual(request["path"], "/v1/messages")
        self.assertEqual(self.headers()["x-api-key"], "test-key")
        self.assertEqual(self.headers()["anthropic-version"], "2023-06-01")
        self.assertEqual(request["body"], {
            "model": "claude-fable-5-2", "max_tokens": 321, "system": "系统提示",
            "messages": [{"role": "user", "content": "用户内容"}], "output_config": {"effort": "low"}})

    def test_gateway_that_rejects_the_effort_field_is_reported_not_retried(self):
        """See tests/test_security.py for why there is no quiet second request."""
        self.server.responder = lambda path, body: (400, {"error": {"message": "unknown field output_config"}})
        config = llm.ModelConfig("anthropic", base_url=self.base, api_key="k", model="some-model")
        with self.assertRaises(llm.LLMError) as caught:
            llm.complete(config, "s", "u")
        self.assertEqual((caught.exception.kind, caught.exception.status), ("effort", 400))
        self.assertEqual(len(self.server.requests), 1)

    def test_effort_none_sends_no_effort_field(self):
        self.server.responder = lambda path, body: (200, {"content": [{"type": "text", "text": "ok"}]})
        llm.complete(llm.ModelConfig("anthropic", base_url=self.base, api_key="k", effort="none"), "s", "u")
        self.assertNotIn("output_config", self.server.requests[0]["body"])

    def test_openai_compatible_request_and_reply(self):
        self.server.responder = lambda path, body: (200, {"choices": [{"message": {"content": " 好的 "}}]})
        config = llm.ModelConfig("openai", base_url=self.base + "/v1/", api_key="test-key", model="local-model")
        self.assertEqual(llm.complete(config, "系统提示", "用户内容"), "好的")
        request = self.server.requests[0]
        self.assertEqual(request["path"], "/v1/chat/completions")
        self.assertEqual(self.headers()["authorization"], "Bearer test-key")
        self.assertEqual(request["body"]["messages"], [{"role": "system", "content": "系统提示"},
                                                       {"role": "user", "content": "用户内容"}])
        self.assertEqual(request["body"]["model"], "local-model")

    def test_failures_are_classified(self):
        cases = [
            ((401, {"error": "bad key"}), "auth"),
            ((403, {"error": "forbidden"}), "auth"),
            ((429, {"error": "slow down"}), "http"),
            ((500, {"error": "boom"}), "http"),
            ((200, b"<html>gateway error</html>"), "invalid"),
            ((200, {"unexpected": True}), "invalid"),
        ]
        for response, kind in cases:
            self.server.responder = lambda path, body, response=response: response
            for provider in ("anthropic", "openai"):
                config = llm.ModelConfig(provider, base_url=self.base, api_key="k", effort="none")
                with self.assertRaises(llm.LLMError) as caught:
                    llm.complete(config, "s", "u")
                self.assertEqual(caught.exception.kind, kind, (provider, response))

    def test_error_text_never_contains_the_key(self):
        self.server.responder = lambda path, body: (401, {"error": "invalid x-api-key"})
        config = llm.ModelConfig("anthropic", base_url=self.base, api_key="sk-very-secret", effort="none")
        with self.assertRaises(llm.LLMError) as caught:
            llm.complete(config, "s", "u")
        self.assertNotIn("sk-very-secret", str(caught.exception))

    def test_slow_service_times_out(self):
        release = threading.Event()
        self.addCleanup(release.set)

        def responder(path, body):
            release.wait(5)
            return 200, {"content": [{"type": "text", "text": "late"}]}
        self.server.responder = responder
        config = llm.ModelConfig("anthropic", base_url=self.base, api_key="k", effort="none")
        with self.assertRaises(llm.LLMError) as caught:
            llm.complete(config, "s", "u", timeout=0.3)
        self.assertEqual(caught.exception.kind, "timeout")

    def test_unreachable_service_is_a_network_error(self):
        self.server.shutdown()
        self.server.server_close()
        config = llm.ModelConfig("openai", base_url=self.base, api_key="k")
        # Windows does not refuse a connection to a closed local port at once: it tries
        # again for about two seconds first. With a shorter allowance the attempt ends
        # as a timeout (seen in the first Windows CI run, where this test used 2 s). The
        # app's own allowance is never below 5 s, so a refusal is reported as one.
        with self.assertRaises(llm.LLMError) as caught:
            llm.complete(config, "s", "u", timeout=15)
        self.assertEqual(caught.exception.kind, "network")

    def test_missing_key_or_unknown_provider_never_reaches_the_network(self):
        for config in (llm.ModelConfig("anthropic", base_url=self.base), llm.ModelConfig("telepathy", api_key="k")):
            with self.assertRaises(llm.LLMError) as caught:
                llm.complete(config, "s", "u")
            self.assertEqual(caught.exception.kind, "config")
        self.assertEqual(self.server.requests, [])


if __name__ == "__main__":
    unittest.main()
