"""Imported first by every test module.

Tests must never touch the user's own data or credentials, even by accident (a new
test that forgets to pass a folder, a developer machine with a real key in its
environment). So before any test runs:

  * CODEX_HOME and BANXING_HOME point into an empty temporary folder — a default
    session reader or settings object can only ever find nothing;
  * every BANXING_* variable that selects a model, a key or a process is removed.

tests/test_security.py checks that this is in force.
"""
import atexit
import os
import shutil
import tempfile

SANDBOX = tempfile.mkdtemp(prefix="banxing-sandbox-")
CLEARED = ("BANXING_PROVIDER", "BANXING_MODEL", "BANXING_BASE_URL", "BANXING_API_KEY", "BANXING_PROCESS_NAMES")

os.environ["CODEX_HOME"] = os.path.join(SANDBOX, "codex-home")
os.environ["BANXING_HOME"] = os.path.join(SANDBOX, "banxing-home")
for _name in CLEARED:
    os.environ.pop(_name, None)

atexit.register(shutil.rmtree, SANDBOX, True)
