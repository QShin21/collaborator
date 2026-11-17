"""Adaptation of the agent's agency and bubble preferences from human feedback.

Pure functions, no I/O. Every reaction to a bubble moves two things:

* ``agency``  (0..1) — how proactive the agent is: how often it bubbles, how
  many bubbles at once, how long they linger, how forward it is in chat.
* ``prefs[kind]`` (0..1) — how much this learner values each kind of bubble.

Actions and what they mean:
  expand — the learner opened a discussion from the bubble (strongest yes)
  accept — the learner kept the idea (yes)
  fade   — the bubble timed out untouched (weak no: maybe just busy)
  ignore — the learner dismissed it (clear no)
"""
import copy
import random

KINDS = ["question", "perspective", "knowledge", "next_step", "risk", "connection"]
ACTIONS = ["expand", "accept", "fade", "ignore"]

# reward target for the preference weight, and its learning rate
_PREF = {"expand": (1.0, 0.30), "accept": (0.85, 0.25), "fade": (0.30, 0.08), "ignore": (0.0, 0.25)}
# additive agency deltas
_AGENCY = {"expand": 0.06, "accept": 0.04, "fade": -0.015, "ignore": -0.06}

PREF_FLOOR = 0.05  # no kind ever dies completely: tastes change, keep exploring


def default_state():
    return {
        "agency": 0.5,
        "locked": False,
        "paused": False,
        "prefs": {k: 0.5 for k in KINDS},
        "counts": {a: 0 for a in ACTIONS},
    }


def normalize(state):
    """Fill in anything missing so older saved states keep working."""
    base = default_state()
    if not isinstance(state, dict):
        return base
    base["agency"] = _clip(state.get("agency", 0.5))
    base["locked"] = bool(state.get("locked", False))
    base["paused"] = bool(state.get("paused", False))
    for kind in KINDS:
        base["prefs"][kind] = max(PREF_FLOOR, _clip((state.get("prefs") or {}).get(kind, 0.5)))
    for action in ACTIONS:
        base["counts"][action] = int((state.get("counts") or {}).get(action, 0))
    return base


def _clip(value):
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.5


def apply_feedback(state, kind, action):
    """Return a new state after the learner reacted to a bubble of `kind`."""
    state = normalize(copy.deepcopy(state))
    if action not in ACTIONS:
        raise ValueError(f"unknown action: {action}")
    if kind in state["prefs"]:
        target, rate = _PREF[action]
        weight = state["prefs"][kind]
        state["prefs"][kind] = round(max(PREF_FLOOR, _clip(weight + rate * (target - weight))), 4)
    if not state["locked"]:
        state["agency"] = round(_clip(state["agency"] + _AGENCY[action]), 4)
    state["counts"][action] += 1
    return state


def bubble_params(state):
    """Translate agency into concrete bubbling behaviour for the client."""
    state = normalize(state)
    agency = state["agency"]
    auto = agency >= 0.08 and not state["paused"]
    return {
        "auto": auto,
        "interval": round(_lerp(150, 25, agency)),      # seconds between rounds
        "per_round": 1 if agency < 0.4 else (2 if agency < 0.8 else 3),
        "max_active": 2 if agency < 0.5 else (3 if agency < 0.85 else 4),
        "ttl": round(_lerp(18, 45, agency)),             # seconds before fading
        "idle_trigger": round(_lerp(300, 60, agency)),   # nudge when stuck this long
    }


def _lerp(a, b, t):
    return a + (b - a) * t


def pick_kinds(state, n, rng=None):
    """Sample `n` distinct kinds, weighted by preference (with a floor to explore)."""
    rng = rng or random
    prefs = normalize(state)["prefs"]
    pool = {k: max(PREF_FLOOR, w) ** 1.5 for k, w in prefs.items()}
    chosen = []
    for _ in range(min(n, len(pool))):
        total = sum(pool.values())
        mark = rng.random() * total
        acc = 0.0
        for kind, weight in pool.items():
            acc += weight
            if mark <= acc:
                chosen.append(kind)
                del pool[kind]
                break
        else:  # float edge case
            kind = next(iter(pool))
            chosen.append(kind)
            del pool[kind]
    return chosen


def stance(state):
    """A sentence for the system prompt describing how forward to be."""
    agency = normalize(state)["agency"]
    if agency < 0.25:
        return ("Agency: LOW. The learner prefers to drive. Answer what is asked, concisely. "
                "Do not volunteer extra directions unless there is a serious blind spot.")
    if agency < 0.6:
        return ("Agency: MEDIUM. Respond to what is asked, then offer at most one optional "
                "suggestion or question that could move the work forward.")
    return ("Agency: HIGH. The learner welcomes initiative. Proactively surface alternatives, "
            "risks and next steps — still as offers the learner chooses among, never as orders.")


def merge_into_profile(profile, session_state, weight=0.5):
    """Blend a session's learned state into the long-term learner profile."""
    profile = normalize(profile)
    session_state = normalize(session_state)
    profile["agency"] = round(_lerp(profile["agency"], session_state["agency"], weight), 4)
    for kind in KINDS:
        profile["prefs"][kind] = round(
            _lerp(profile["prefs"][kind], session_state["prefs"][kind], weight), 4)
    profile["counts"] = {a: 0 for a in ACTIONS}
    profile["locked"] = False
    profile["paused"] = False
    return profile
