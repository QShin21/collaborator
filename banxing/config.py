"""Where things live, and the user's settings.

  ROOT  bundled, read-only resources (the package itself; inside the PyInstaller
        bundle when packaged).
  home() the user's files: settings.json, state.json, events.jsonl, banxing.log.
        %APPDATA%\\Banxing on Windows, ~/.config/banxing elsewhere,
        or BANXING_HOME. Nothing is ever written next to the program.

Settings rules that must hold (tests/test_security.py and test_config.py cover them):
  * a saved API key is bound to the provider and endpoint it was entered for. It
    never follows a changed endpoint — not through the dialog, not through an
    edited settings file, not through BANXING_PROVIDER / BANXING_BASE_URL;
  * endpoints are https, except on this machine;
  * a consent names the effective origin (scheme, host, port — environment overrides
    included) and is void as soon as that changes. A watched conversation is agreed
    to one at a time (watch/driver.py); the general consent here serves --replay.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path

from . import APP_ID, llm, secretbox

FROZEN = bool(getattr(sys, "frozen", False))
ROOT = Path(__file__).resolve().parent

SETTINGS_SCHEMA = 3      # 1, 2 = the draft-routes generations; their model fields carry over unchanged

DEFAULTS = dict(
    schema=SETTINGS_SCHEMA,
    provider="",                 # "" = not configured yet. This is the SUPERVISING model.
    model="",
    base_url="",
    effort="medium",             # reading a long conversation for what it rests on is worth some thought
    api_key="",                  # protected form, see secretbox
    key_for="",                  # the provider and endpoint that key was entered for
    consent="",                  # a destination agreed to in general (used by --replay); a watched
    #                              conversation is agreed to one at a time, in its own dialog
    language="auto",             # auto | zh | en
    pet_free=None,               # [x, y] of the figure on screen
    codex_command="",            # override for `codex app-server`
    sessions_dir="",             # override for ~/.codex/sessions
    model_timeout_seconds=60.0,
    watch_poll_seconds=5.0,      # how often a watched conversation's file is looked at again
)


def home():
    explicit = os.environ.get("BANXING_HOME")
    if explicit:
        return Path(explicit)
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / APP_ID
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "banxing"


def write_json(path, data):
    """Write atomically, UTF-8, private to the user where the platform allows."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
        if sys.platform != "win32":
            os.chmod(temp, 0o600)
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8-sig") as stream:
            return json.load(stream)
    except (OSError, ValueError):
        return default


class Settings:
    """settings.json plus the environment overrides used by CI and demos:
    BANXING_PROVIDER, BANXING_MODEL, BANXING_BASE_URL, BANXING_API_KEY."""

    def __init__(self, folder=None, environ=None):
        self.folder = Path(folder) if folder else home()
        self.path = self.folder / "settings.json"
        self._environ = os.environ if environ is None else environ
        self.data = dict(DEFAULTS)
        stored = read_json(self.path, {})
        if isinstance(stored, dict):
            schema = stored.get("schema", stored.get("version", 1))
            # A file written by a newer version is not guessed at: defaults are used
            # (and the file is only overwritten when the user changes a setting).
            if isinstance(schema, int) and not isinstance(schema, bool) and 1 <= schema <= SETTINGS_SCHEMA:
                self.data.update({k: v for k, v in stored.items() if k in DEFAULTS and k != "schema"})

    # ------------------------------------------------------------ basics
    def get(self, name):
        return self.data.get(name, DEFAULTS.get(name))

    def set(self, **values):
        for name, value in values.items():
            if name not in DEFAULTS:
                raise KeyError(name)
            self.data[name] = value
        self.save()

    def save(self):
        write_json(self.path, self.data)

    def number(self, name, low, high):
        try:
            value = float(self.get(name))
        except (TypeError, ValueError):
            value = float(DEFAULTS[name])
        return max(low, min(high, value))

    # ------------------------------------------------------------ model
    def _saved_key(self):
        stored = self.get("api_key") or ""
        cached = getattr(self, "_key_cache", None)
        if cached is None or cached[0] != stored:
            cached = self._key_cache = (stored, secretbox.reveal(stored))
        return cached[1]

    @property
    def env_locked(self):
        """Where requests go is decided by BANXING_* variables; the dialog cannot change it."""
        return bool(self._environ.get("BANXING_PROVIDER") or self._environ.get("BANXING_BASE_URL"))

    def model_config(self):
        """The configuration that would actually be used: saved settings with the
        environment applied on top.

        The saved API key is released only for the exact provider and endpoint it
        was entered for (`key_for`). Any override that changes where requests go —
        BANXING_PROVIDER, or BANXING_BASE_URL on its own — must bring its own
        BANXING_API_KEY; otherwise there is no key and nothing is sent.
        """
        env = self._environ
        saved_provider = str(self.get("provider") or "").strip().lower()
        provider = str(env.get("BANXING_PROVIDER") or saved_provider).strip().lower()
        if not provider:
            return llm.ModelConfig(provider="")                 # nothing configured: not ready
        same = provider == saved_provider
        config = llm.ModelConfig(
            provider=provider,
            model=env.get("BANXING_MODEL") or (self.get("model") if same else "") or "",
            base_url=env.get("BANXING_BASE_URL") or (self.get("base_url") if same else "") or "",
            effort=self.get("effort") or "medium")
        key = env.get("BANXING_API_KEY") or ""
        if not key and config.target and config.target == self.get("key_for"):
            key = self._saved_key()
        return replace(config, api_key=key)

    def destination(self, config=None):
        """What a consent names: the service kind and the origin (scheme, host, port)
        requests really go to — environment overrides included. "" when nothing
        leaves the machine."""
        return (config or self.model_config()).destination

    def consented(self, config=None):
        """Has the user agreed, in general, to send text to where `config` (default: the
        effective configuration) sends them?"""
        destination = self.destination(config)
        return destination == "" or self.get("consent") == destination

    def give_consent(self, shown=None):
        """Record the user's yes for the effective destination.

        `shown` is the destination the user was looking at when they agreed. If it is
        not the one that would really be used, nothing is recorded and False is
        returned — the caller must show the real one and ask again.
        """
        destination = self.destination()
        if shown is not None and shown != destination:
            return False
        self.set(consent=destination)
        return True

    def save_model(self, provider, model="", base_url="", api_key="", effort="medium"):
        """Apply the model dialog. Raises ValueError with a reason the dialog shows."""
        def tidy(value):
            return " ".join(str(value or "").split())

        provider = tidy(provider).lower()
        if provider not in llm.PROVIDERS:
            raise ValueError("provider")
        base_url = tidy(base_url).rstrip("/")
        api_key = tidy(api_key)
        effort = tidy(effort).lower()
        updates = {"provider": provider, "model": tidy(model),
                   "effort": effort if effort in ("low", "medium", "high", "none") else "medium"}
        if provider == "mock":
            updates.update(base_url="", api_key="", key_for="")
        else:
            try:
                llm.check_base_url(base_url)
            except ValueError as error:
                raise ValueError("base_url") from error
            target = llm.ModelConfig(provider, base_url=base_url).target
            if not target:
                raise ValueError("base_url")
            has_saved = bool(self._saved_key())
            # The saved key was entered for one provider and endpoint (`key_for`). It
            # must never follow a changed endpoint: whoever can change the URL would
            # otherwise receive the key with the next request.
            if not api_key and not (has_saved and target == self.get("key_for")):
                raise ValueError("api_key_again" if has_saved else "api_key")
            updates["base_url"] = base_url
            if api_key:
                try:
                    stored = secretbox.protect(api_key)
                except secretbox.SecretError as error:
                    # Windows could not encrypt it. Nothing is saved — not the key in a
                    # readable form, and not the rest of the dialog either.
                    raise ValueError("encrypt") from error
                updates.update(api_key=stored, key_for=target)
        self.data.update(updates)
        if self.data.get("consent") != self.destination():
            self.data["consent"] = ""           # a new destination needs a new yes
        self.save()
        return self.model_config()


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")
