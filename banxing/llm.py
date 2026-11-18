"""Model access: one configurable service behind one function.

  anthropic — Claude via the Messages API
  openai    — any OpenAI-compatible chat-completions endpoint
  mock      — deterministic offline stand-in for tests and demos (never a default
              for real use: without a configured model the pet stays quiet)

Standard library only. The configuration is passed in, so nothing here reads
settings or the environment.
"""
from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlparse

from . import mock

PROVIDERS = ("anthropic", "openai", "mock")
DEFAULT_MODEL = {"anthropic": "claude-fable-5-2", "openai": "gpt-4o", "mock": "mock"}
DEFAULT_BASE_URL = {"anthropic": "https://api.anthropic.com", "openai": "https://api.openai.com/v1",
                    "mock": ""}
LOOPBACK = ("127.0.0.1", "localhost", "::1")


@dataclass(frozen=True)
class ModelConfig:
    provider: str = "mock"
    model: str = ""
    base_url: str = ""
    api_key: str = ""
    effort: str = "low"          # anthropic only: low | medium | high | none

    @property
    def endpoint(self):
        return (self.base_url or DEFAULT_BASE_URL.get(self.provider, "")).rstrip("/")

    def _parts(self):
        """(scheme, host, port, path) of the endpoint, normalised — or None when
        nothing is sent anywhere (mock, not configured) or the address is unusable."""
        if self.provider not in ("anthropic", "openai"):
            return None
        try:
            parts = urlparse(self.endpoint)
            port = parts.port
        except ValueError:
            return None
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return None
        return (parts.scheme, parts.hostname.lower(), port or (443 if parts.scheme == "https" else 80),
                parts.path.rstrip("/"))

    @property
    def host(self):
        parts = self._parts()
        return parts[1] if parts else ""

    @property
    def origin(self):
        """Scheme, host and port the requests go to, e.g. "https://api.anthropic.com".
        This is what the user is shown, and asked to agree to, before anything is sent."""
        parts = self._parts()
        if parts is None:
            return ""
        scheme, host, port, _ = parts
        host = f"[{host}]" if ":" in host else host
        default = 443 if scheme == "https" else 80
        return f"{scheme}://{host}" + ("" if port == default else f":{port}")

    @property
    def destination(self):
        """What a consent names: the service kind and the origin. "" = nothing leaves
        this machine."""
        origin = self.origin
        return f"{self.provider}|{origin}" if origin else ""

    @property
    def target(self):
        """What a saved API key is bound to: provider and the exact endpoint, as a
        normalised string. "" when there is no endpoint."""
        parts = self._parts()
        if parts is None:
            return ""
        scheme, host, port, path = parts
        return f"{self.provider}|{scheme}://{host}:{port}{path}"

    @property
    def ready(self):
        if self.provider == "mock":
            return True
        if self.provider not in PROVIDERS or not self.api_key or self._parts() is None:
            return False
        try:
            check_base_url(self.endpoint)
        except ValueError:
            return False
        return True

    @property
    def model_name(self):
        return self.model or DEFAULT_MODEL.get(self.provider, "")


class LLMError(Exception):
    """kind: config | consent | auth | timeout | network | redirect | effort | http | invalid"""

    def __init__(self, kind, message, status=None):
        super().__init__(message)
        self.kind = kind
        self.status = status


def check_base_url(url):
    """An endpoint receives the API key and the conversation: https only, except on this machine."""
    if not url:
        return
    parts = urlparse(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        raise ValueError("base URL must look like https://host[:port][/path]")
    if parts.scheme == "http" and parts.hostname not in LOOPBACK:
        raise ValueError("base URL must use https (http is allowed only for localhost)")


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect.

    urllib's stock handler re-sends the request headers — the API key among them —
    to wherever the Location header points: another host, another port, or plain
    http. The user agreed to one destination and the key belongs to one endpoint,
    so a redirect is an error to show, not an instruction to follow. This holds
    for same-origin redirects too: a model endpoint that redirects is misconfigured,
    and the fix is to enter the final address.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if fp is not None:
            try:
                fp.close()
            except OSError:
                pass
        try:
            target = urlparse(newurl)
            where = f"{target.scheme}://{target.hostname}" if target.hostname else "another address"
        except ValueError:
            where = "another address"
        # Only scheme and host are named: the rest of a Location may carry anything.
        raise LLMError("redirect", f"HTTP {code}: the model service redirected to {where}; "
                                   "redirects are not followed. Set the base URL to the final address.",
                       status=code)


def _opener(url):
    handlers = [_NoRedirects()]
    # A model server on this machine is reached directly, never through a system proxy.
    if (urlparse(url).hostname or "") in LOOPBACK:
        handlers.append(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers)


def _post(url, headers, body, timeout):
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with _opener(url).open(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        with error:                          # the response is closed whatever happens next
            if 300 <= error.code < 400:      # a redirect status without a usable Location
                raise LLMError("redirect", f"HTTP {error.code}: redirects are not followed",
                               status=error.code) from error
            detail = error.read().decode("utf-8", "replace")[:300]
        kind = "auth" if error.code in (401, 403) else "http"
        raise LLMError(kind, f"HTTP {error.code}: {detail}", status=error.code) from error
    except (socket.timeout, TimeoutError) as error:
        raise LLMError("timeout", "the model service did not answer in time") from error
    except urllib.error.URLError as error:
        if isinstance(error.reason, (socket.timeout, TimeoutError)):
            raise LLMError("timeout", "the model service did not answer in time") from error
        raise LLMError("network", f"network error: {error.reason}") from error
    except OSError as error:
        raise LLMError("network", f"network error: {error}") from error
    try:
        return json.loads(raw)
    except ValueError as error:
        raise LLMError("invalid", f"the service did not return JSON: {raw[:200]}") from error


def _anthropic(config, system, user, max_tokens, timeout):
    body = {"model": config.model_name, "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": user}]}
    if system:
        body["system"] = system
    headers = {"content-type": "application/json", "x-api-key": config.api_key,
               "anthropic-version": "2023-06-01"}
    url = f"{config.endpoint}/v1/messages"
    with_effort = bool(config.effort) and config.effort != "none"
    if with_effort:
        body["output_config"] = {"effort": config.effort}
    # One request, never a quiet second try: a 400 can mean anything (wrong model,
    # request too long, a policy refusal), and re-sending the conversation in a different
    # shape behind the user's back is not ours to decide. If the service says it does
    # not know the effort field, that is reported so the user can set effort to "none".
    try:
        data = _post(url, headers, body, timeout)
    except LLMError as error:
        text = str(error).casefold()
        if with_effort and error.status == 400 and ("output_config" in text or "effort" in text):
            raise LLMError("effort", str(error), status=400) from error
        raise
    blocks = data.get("content") if isinstance(data, dict) else None
    if not isinstance(blocks, list):
        raise LLMError("invalid", f"unexpected response: {str(data)[:200]}")
    return "".join(b.get("text", "") for b in blocks
                   if isinstance(b, dict) and b.get("type") == "text").strip()


def _openai(config, system, user, max_tokens, timeout):
    messages = [{"role": "user", "content": user}]
    if system:
        messages.insert(0, {"role": "system", "content": system})
    body = {"model": config.model_name, "messages": messages, "max_tokens": max_tokens}
    headers = {"content-type": "application/json", "authorization": f"Bearer {config.api_key}"}
    data = _post(f"{config.endpoint}/chat/completions", headers, body, timeout)
    try:
        return (data["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError) as error:
        raise LLMError("invalid", f"unexpected response: {str(data)[:200]}") from error


def complete(config, system, user, max_tokens=2600, timeout=30.0, meta=None):
    """Run one model call and return the reply text.

    `meta` carries the structured request for the mock provider, which answers
    from it instead of reading the prompt. Real providers ignore it.
    """
    if config.provider == "mock":
        return mock.complete(meta or {})
    if config.provider not in PROVIDERS:
        raise LLMError("config", f"unknown provider: {config.provider}")
    if not config.api_key:
        raise LLMError("config", "no API key is configured")
    try:
        check_base_url(config.endpoint)
    except ValueError as error:
        raise LLMError("config", str(error)) from error
    if config.provider == "anthropic":
        return _anthropic(config, system, user, max_tokens, timeout)
    return _openai(config, system, user, max_tokens, timeout)
