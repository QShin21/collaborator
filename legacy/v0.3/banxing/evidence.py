"""The evidence bundle: what was found on this machine, in a form that can be handed
to a developer — and that the user can read in full before deciding to.

It is the bridge from "we assume" to "it was seen": calibration results, the
compatibility report, a structure recording of the host window, the self-test and
the versions involved. By construction it holds no draft, no chat text, no prompt:
yes/no answers, counts, timings, control types, text lengths. `scrub` enforces that
again on the way out and removes the user's name from any path.

Nothing is ever uploaded. The bundle is written where the user says.
"""
from __future__ import annotations

import json
import os
import platform
import re
import sys
import time
import zipfile
from pathlib import Path

from . import __version__
from .hosts import replay

MAX_STRING = 200
_CJK_RUN = re.compile(r"[一-鿿]{4,}")


def _home_names():
    names = set()
    for value in (os.environ.get("USERNAME"), os.environ.get("USER"), Path.home().name):
        if value and len(value) >= 2:
            names.add(value)
    return names


def scrub(value, _names=None):
    """A copy that is safe to share: user names in paths replaced, long strings cut,
    anything that looks like a sentence of Chinese text removed (no field of a report
    is supposed to hold prose)."""
    names = _home_names() if _names is None else _names
    if isinstance(value, dict):
        return {str(k)[:60]: scrub(v, names) for k, v in value.items() if k != "placeholders"}
    if isinstance(value, (list, tuple)):
        return [scrub(v, names) for v in value]
    if isinstance(value, str):
        text = value
        for name in names:
            text = re.sub(re.escape(name), "~user~", text, flags=re.I)
        text = _CJK_RUN.sub("…", text)
        return text[:MAX_STRING]
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return str(type(value).__name__)


def build(calibration_entry=None, probe=None, structure=None, selftest=None, summary=None, wall=time.time):
    """Collect the parts into one dict (already scrubbed)."""
    structure = list(structure or [])
    return scrub({
        "kind": "banxing-evidence", "schema": 1, "made_at": round(wall(), 1),
        "app_version": __version__, "python": sys.version.split()[0],
        "system": {"platform": sys.platform, "release": platform.release(), "version": platform.version(),
                   "machine": platform.machine()},
        "calibration": calibration_entry, "probe": probe,
        "structure": structure if replay.is_clean(structure) else {"withheld": "recording had unexpected fields"},
        "selftest": selftest, "usage_summary": summary,
        "note": "No draft, chat or prompt text is included. Lengths, counts, timings and yes/no answers only.",
    })


def as_text(bundle):
    """What the user is shown before saving: the whole thing, nothing hidden."""
    return json.dumps(bundle, ensure_ascii=False, indent=2)


def write(path, bundle):
    """Save as a zip with one readable JSON file inside (and the recording separately,
    ready to be dropped into tests/fixtures/)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("evidence.json", as_text(bundle))
        if isinstance(bundle.get("structure"), list):
            archive.writestr("structure.json", json.dumps(bundle["structure"], ensure_ascii=False, indent=2))
        archive.writestr("README.txt",
                         "Banxing evidence bundle. Contains no draft, chat or prompt text.\n"
                         "evidence.json  calibration, compatibility report, versions\n"
                         "structure.json interface structure of the host window (types, positions, text lengths)\n")
    return path
