"""Runtime configuration, read from the environment (and an optional .env file).

Two locations matter:

  ROOT — where the bundled resources live (prompts/, web/). In the packaged Windows
         build this is PyInstaller's unpack directory and is read-only.
  HOME — where the user's own files live (.env, data/). From source it is the repo;
         packaged, it is the folder containing the .exe (or %LOCALAPPDATA% if that
         folder is not writable). Override with COLLAB_HOME.
"""
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

FROZEN = bool(getattr(sys, "frozen", False))
if FROZEN:
    ROOT = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
else:
    ROOT = Path(__file__).resolve().parent.parent


def _writable(folder):
    try:
        folder.mkdir(parents=True, exist_ok=True)
        probe = folder / ".write-test"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def _home():
    explicit = os.environ.get("COLLAB_HOME")
    if explicit:
        return Path(explicit)
    if not FROZEN:
        return ROOT
    beside_exe = Path(sys.executable).resolve().parent
    if _writable(beside_exe):
        return beside_exe
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "AICollaborator"


HOME = _home()
ENV_FILE = HOME / ".env"


def load_dotenv(path=None):
    path = Path(path or ENV_FILE)
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_dotenv()


def write_env(path, updates):
    """Set (or, for empty values, remove) keys in a .env file, keeping other lines."""
    path = Path(path)
    lines = path.read_text(encoding="utf-8-sig").splitlines() if path.exists() else []
    pending = dict(updates)
    out = []
    for line in lines:
        stripped = line.strip()
        key = stripped.split("=", 1)[0].strip() if "=" in stripped and not stripped.startswith("#") else None
        if key in pending:
            value = pending.pop(key)
            if value:
                out.append(f"{key}={value}")
        else:
            out.append(line)
    out += [f"{key}={value}" for key, value in pending.items() if value]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def env(name, default=None):
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def provider():
    explicit = env("COLLAB_PROVIDER")
    if explicit:
        return explicit.lower()
    if env("ANTHROPIC_API_KEY"):
        return "anthropic"
    if env("OPENAI_API_KEY"):
        return "openai"
    return "mock"


def model():
    default = {"anthropic": "claude-fable-5-2", "openai": "gpt-4o", "mock": "mock"}
    return env("COLLAB_MODEL", default.get(provider(), "mock"))


def effort():
    """Reasoning effort for Anthropic models. Empty string disables the field."""
    return env("COLLAB_EFFORT", "medium")


def db_path():
    return env("COLLAB_DB", str(HOME / "data" / "collab.db"))


def host():
    return env("COLLAB_HOST", "127.0.0.1")


def port():
    return int(env("COLLAB_PORT", "8765"))


# ------------------------------------------------------------- in-app settings
def settings():
    name = provider()
    key_var = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}.get(name)
    url_var = {"anthropic": "ANTHROPIC_BASE_URL", "openai": "OPENAI_BASE_URL"}.get(name)
    return {
        "provider": name, "model": model(), "effort": effort(),
        "has_key": bool(key_var and env(key_var)),
        "base_url": env(url_var, "") if url_var else "",
        "env_file": str(ENV_FILE),
    }


def check_base_url(url):
    """An endpoint receives the API key: https only, except on this machine."""
    if not url:
        return
    parts = urlparse(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        raise ValueError("base URL must look like https://host[:port][/path]")
    if parts.scheme == "http" and parts.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("base URL must use https (http is allowed only for localhost)")


def save_settings(data):
    """Apply settings from the UI: persist to HOME/.env and to this process."""
    def clean(value):
        return " ".join(str(value or "").split())

    name = clean(data.get("provider")).lower()
    if name not in ("anthropic", "openai", "mock"):
        raise ValueError("provider must be anthropic, openai or mock")
    updates = {"COLLAB_PROVIDER": name, "COLLAB_MODEL": clean(data.get("model"))}
    effort_value = clean(data.get("effort")).lower()
    if effort_value in ("low", "medium", "high", "none"):
        updates["COLLAB_EFFORT"] = effort_value
    prefix = {"anthropic": "ANTHROPIC", "openai": "OPENAI"}.get(name)
    if prefix:
        key = clean(data.get("api_key"))
        if key:  # an empty field means "keep the key already saved"
            updates[f"{prefix}_API_KEY"] = key
        base_url = clean(data.get("base_url")).rstrip("/")
        check_base_url(base_url)
        # The saved key was entered for the endpoint that is configured now. It must
        # never follow a changed endpoint: whoever can change the URL would otherwise
        # receive the key on the next model call. A new endpoint needs the key again.
        current_url = (env(f"{prefix}_BASE_URL", "") or "").rstrip("/")
        if base_url != current_url and not key:
            raise ValueError("changing the endpoint requires entering the API key again")
        updates[f"{prefix}_BASE_URL"] = base_url
        if not (key or env(f"{prefix}_API_KEY")):
            raise ValueError("an API key is required for this provider")
    write_env(ENV_FILE, updates)
    for key, value in updates.items():
        if value:
            os.environ[key] = value
        else:
            os.environ.pop(key, None)
    return settings()
