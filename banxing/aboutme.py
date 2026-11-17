"""关于我: standing facts the user wrote about themselves, and which kinds of route
they tend to choose.

  facts     the user's own sentences ("我没有编程基础", "家里没有电钻"). They go
            with every request and all three routes must respect them. This is
            content, so it is kept encrypted (vault/about.bin) and only exists
            because the user typed it; clearing it deletes the file.
  weights   one number per route kind: up when a route of that kind is chosen,
            down when a bubble is turned away. Never below a floor — tastes change.
            Used only as a tie-break for the main line; the rule that three routes
            span at least two kinds is not affected. Numbers only (state.json).

Both are visible and editable in the "关于我" window.
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Tuple

from . import secretbox
from .config import read_json
from .suggest import KINDS

FLOOR, CEILING, START = 0.2, 3.0, 1.0
UP, DOWN = 0.25, 0.08
MAX_FACTS, MAX_FACT_CHARS = 12, 120


def default_weights() -> Dict[str, float]:
    return {kind: START for kind in KINDS}


def adjust(weights, kind, delta):
    """A new weights dict after one signal: delta > 0 chosen, delta < 0 turned down."""
    weights = dict(default_weights(), **{k: v for k, v in (weights or {}).items() if k in KINDS})
    if kind in weights:
        step = UP if delta > 0 else -DOWN
        weights[kind] = round(max(FLOOR, min(CEILING, weights[kind] + step)), 3)
    return weights


def preferred(weights, count=2) -> Tuple[str, ...]:
    """Kinds clearly above the starting weight, strongest first."""
    weights = dict(default_weights(), **(weights or {}))
    ranked = sorted(((value, kind) for kind, value in weights.items()
                     if kind != "other" and value >= START + UP), reverse=True)
    return tuple(kind for _, kind in ranked[:count])


def band(value):
    """How a weight is shown to the user: often | sometimes | rarely."""
    if value >= START + UP:
        return "often"
    if value <= START - 2 * DOWN:
        return "rarely"
    return "sometimes"


def tidy_facts(lines) -> List[str]:
    facts = []
    for line in lines or []:
        line = " ".join(str(line).split()).lstrip("-•· ").strip()
        if line and line not in facts:
            facts.append(line[:MAX_FACT_CHARS])
    return facts[:MAX_FACTS]


class AboutMe:
    """Loads and saves both parts. `folder` is the user's data folder."""

    def __init__(self, folder):
        self.folder = Path(folder)
        self.facts_path = self.folder / "vault" / "about.bin"
        self.weights_path = self.folder / "state.json"
        self._facts = None

    # ------------------------------------------------------------ facts
    def facts(self) -> List[str]:
        if self._facts is None:
            try:
                stored = self.facts_path.read_text(encoding="utf-8").strip()
            except OSError:
                stored = ""
            text = secretbox.reveal(stored) if stored else ""
            self._facts = tidy_facts(text.split("\n")) if text else []
        return list(self._facts)

    def save_facts(self, lines):
        """Raises secretbox.SecretError when Windows cannot encrypt: then nothing is
        written and what was stored before stays as it was."""
        facts = tidy_facts(lines)
        if not facts:
            self._facts = facts
            try:
                self.facts_path.unlink()
            except OSError:
                pass
            return facts
        stored = secretbox.protect("\n".join(facts))        # before anything is touched
        self._facts = facts
        self.facts_path.parent.mkdir(parents=True, exist_ok=True)
        self.facts_path.write_text(stored, encoding="utf-8")
        return facts

    # ------------------------------------------------------------ what a request gets
    def for_request(self, weights=None):
        return tuple(self.facts()), preferred(weights)


def weights_from(data):
    stored = data.get("weights") if isinstance(data, dict) else None
    clean = default_weights()
    if isinstance(stored, dict):
        for kind, value in stored.items():
            if kind in clean and isinstance(value, (int, float)) and not isinstance(value, bool):
                clean[kind] = max(FLOOR, min(CEILING, float(value)))
    return clean


def load_weights(path):
    return weights_from(read_json(path, {}))
