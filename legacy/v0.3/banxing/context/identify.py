"""Which stored thread is the chat on screen?

There is no supported way to ask the Codex window for its thread id, so the chat
is recognised by what is visible in it: lines from a thread's last turns are
looked for in the window's text. The rule is deliberately conservative.

A thread is a candidate only if
  * something from its NEWEST turn is on screen (the bottom of a chat is what sits
    above the input box),
  * at least two independent pieces of evidence match (MIN_SCORE), and
  * a fair share of what it should show is there (MIN_COVERAGE).
A thread's title or first message weighs little: the sidebar shows those for
every chat.

Among candidates the one whose newest turn appears LATEST in the window wins.
That is what separates a chat from a fork or continuation of it: both match the
shared part, but only the real one has its newest turn at the end. If two
candidates end at the same place the answer is "not sure".

"Not sure" is a normal result. The pet then works from the draft alone — a wrong
chat's context would be worse than none. A brand-new chat is never guessed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..textutil import loose

MIN_SCORE = 4
MIN_COVERAGE = 0.34
PROBE_CHARS = 36
MIN_PROBE = 8
_FENCE = re.compile(r"```.*?(?:```|\Z)", re.S)
_MARKER = re.compile(r"^\s*(?:[-*+•]|\d+[.)、]|#{1,6}|>)\s*")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")


@dataclass(frozen=True)
class Identification:
    thread_id: str = ""
    confident: bool = False
    score: int = 0
    runner_up: int = 0
    candidates: int = 0


def _probe(line):
    """A stretch of text likely to appear verbatim in the rendered chat."""
    line = _LINK.sub(r"\1", _MARKER.sub("", line))
    probe = loose(line)[:PROBE_CHARS]
    return probe if len(probe) >= MIN_PROBE else ""


def _prose_lines(text):
    lines = []
    for line in _FENCE.sub("\n", text or "").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("|") and _probe(stripped):
            lines.append(stripped)
    return lines


def probes_for(turns):
    """[(probe, weight, from_newest_turn)] for a thread, from its last turns (oldest first)."""
    found = {}

    def add(line, weight, newest):
        probe = _probe(line)
        if probe and weight >= found.get(probe, (0, False))[0]:
            found[probe] = (weight, newest or found.get(probe, (0, False))[1])

    recent = turns[-3:]
    first_index = len(turns) - len(recent)
    for offset, turn in enumerate(recent):
        newest = offset == len(recent) - 1
        lines = _prose_lines(turn.user)
        if lines:
            # The very first message of a thread doubles as its sidebar title.
            add(lines[0], 1 if first_index + offset == 0 else 3, newest)
        lines = _prose_lines(turn.answer)
        if lines:
            add(lines[0], 3 if newest else 2, newest)
            if len(lines) > 1:
                add(lines[-1], 3 if newest else 2, newest)
            if len(lines) > 4:
                add(lines[len(lines) // 2], 2, newest)
    return sorted((probe, weight, newest) for probe, (weight, newest) in found.items())


def evaluate(signals, thread):
    """(score, coverage, tail) — tail is where the thread's newest turn ends in the
    window text, or -1 if nothing of it is visible."""
    blob = "".join(loose(text) for text in signals.texts)
    score = total = 0
    tail = -1
    for probe, weight, newest in thread.probes or []:
        total += weight
        position = blob.rfind(probe)
        if position < 0:
            continue
        score += weight
        if newest:
            tail = max(tail, position + len(probe))
    coverage = score / total if total else 0.0
    name = loose(thread.name)
    if len(name) >= 4:
        focus = "".join(loose(text) for text in (signals.title,) + tuple(signals.selected))
        if name in focus:
            score += 2
    return score, coverage, tail


def score(signals, thread):
    return evaluate(signals, thread)[0]


def identify(signals, threads, min_score=MIN_SCORE, min_coverage=MIN_COVERAGE):
    threads = list(threads or [])
    if signals is None or not threads:
        return Identification(candidates=len(threads))
    # If the page address carries a thread id, that settles it without reading content.
    address = (signals.url or "").casefold()
    if address:
        named = [t.id for t in threads if len(t.id) >= 16 and t.id.casefold() in address]
        if len(named) == 1:
            return Identification(thread_id=named[0], confident=True, score=99, candidates=len(threads))
    rated =[(evaluate(signals, thread), thread) for thread in threads]
    best_score = max(result[0] for result, _ in rated)
    eligible = sorted(((tail, points, thread.id) for (points, coverage, tail), thread in rated
                       if tail >= 0 and points >= min_score and coverage >= min_coverage), reverse=True)
    if not eligible:
        return Identification(score=best_score, candidates=len(threads))
    tail, points, thread_id = eligible[0]
    runner_up = eligible[1][1] if len(eligible) > 1 else 0
    if len(eligible) > 1 and eligible[1][0] == tail:
        return Identification(score=points, runner_up=runner_up, candidates=len(threads))   # same ending
    return Identification(thread_id=thread_id, confident=True, score=points, runner_up=runner_up,
                          candidates=len(threads))
