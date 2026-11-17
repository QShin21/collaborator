"""Model access. Three providers behind one function:

  anthropic — Claude via the Messages API (default model: claude-fable-5-2, effort medium)
  openai    — any OpenAI-compatible chat-completions endpoint
  mock      — deterministic offline stand-in, so the app and tests run without a key

Only the standard library is used, so there is nothing to install.
"""
import json
import urllib.error
import urllib.request

from . import config
from .mock import mock_complete


class LLMError(Exception):
    pass


def _post(url, headers, body, timeout=180):
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:600]
        raise LLMError(f"HTTP {error.code}: {detail}") from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise LLMError(f"network error: {error}") from error


def normalize_messages(messages):
    """Make the history start with a user turn and strictly alternate."""
    out = []
    for message in messages:
        role = "assistant" if message.get("role") == "assistant" else "user"
        content = (message.get("content") or "").strip()
        if not content:
            continue
        if out and out[-1]["role"] == role:
            out[-1]["content"] += "\n\n" + content
        else:
            out.append({"role": role, "content": content})
    if not out or out[0]["role"] != "user":
        out.insert(0, {"role": "user", "content": "(start)"})
    return out


def _anthropic(system, messages, max_tokens, image_b64):
    key = config.env("ANTHROPIC_API_KEY")
    if not key:
        raise LLMError("ANTHROPIC_API_KEY is not set")
    base = config.env("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/")
    msgs = [dict(m) for m in messages]
    if image_b64 and msgs and msgs[-1]["role"] == "user":
        msgs[-1]["content"] = [
            {"type": "image",
             "source": {"type": "base64", "media_type": "image/jpeg", "data": image_b64}},
            {"type": "text", "text": msgs[-1]["content"]},
        ]
    body = {"model": config.model(), "max_tokens": max_tokens, "messages": msgs}
    if system:
        body["system"] = system
    headers = {"content-type": "application/json", "x-api-key": key,
               "anthropic-version": "2023-06-01"}
    effort = config.effort()
    if effort and effort != "none":
        try:
            data = _post(f"{base}/v1/messages", headers,
                         dict(body, output_config={"effort": effort}))
        except LLMError as error:
            # Models / gateways that don't know the effort field: retry plain.
            if "HTTP 400" not in str(error):
                raise
            data = _post(f"{base}/v1/messages", headers, body)
    else:
        data = _post(f"{base}/v1/messages", headers, body)
    parts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
    return "".join(parts).strip()


def _openai(system, messages, max_tokens, image_b64):
    key = config.env("OPENAI_API_KEY")
    if not key:
        raise LLMError("OPENAI_API_KEY is not set")
    base = config.env("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    msgs = [dict(m) for m in messages]
    if image_b64 and msgs and msgs[-1]["role"] == "user":
        msgs[-1]["content"] = [
            {"type": "text", "text": msgs[-1]["content"]},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
        ]
    if system:
        msgs.insert(0, {"role": "system", "content": system})
    body = {"model": config.model(), "messages": msgs, "max_tokens": max_tokens}
    headers = {"content-type": "application/json", "authorization": f"Bearer {key}"}
    data = _post(f"{base}/chat/completions", headers, body)
    try:
        return (data["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError) as error:
        raise LLMError(f"unexpected response: {str(data)[:300]}") from error


def complete(task, system, messages, max_tokens=1600, image_b64=None, meta=None):
    """Run one model call.

    `task` names the job (frame, plan, chat, bubbles, ...). Real providers ignore
    it; the mock provider uses it together with `meta` to produce a plausible reply.
    """
    messages = normalize_messages(messages)
    name = config.provider()
    if name == "anthropic":
        return _anthropic(system, messages, max_tokens, image_b64)
    if name == "openai":
        return _openai(system, messages, max_tokens, image_b64)
    if name == "mock":
        return mock_complete(task, system, messages, meta or {})
    raise LLMError(f"unknown provider: {name}")
