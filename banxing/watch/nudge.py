"""When a bubble may appear. Pure: no I/O, no clock.

A supervisor that speaks often is switched off. So a concern appears by itself only
when all of this holds:

  traceable   it rests on passages found in the conversation (audit.py made sure);
  bearing     it bears on where the work is going NOW — it could quote the step that
              rests on it. Anything else is a note in the ledger;
  timely      the main model has finished a turn and the user has not moved on:
              the next decision is still open;
  once        the same concern on the same evidence is shown once;
  respected   a kind of concern the user closed, called intended, called wrong, took
              back to the main model, or simply walked past stays away until the
              goal or the footing of the conversation has really changed
              (`ledger.situation`).

One bubble at a time: the first finding that passes. The rest are in the ledger.

When the USER asks ("look now"), there is always an answer — the weightiest
finding, or a plain "nothing is off, because …" — neither counted nor suppressed.
"""
from __future__ import annotations

from . import audit as au

SHOW, NOTE, QUIET = "show", "note", "quiet"

# Why nothing appeared (logged as a name, shown nowhere as prose).
MANUAL, TOO_LATE, NOTHING, NOTES_ONLY, REPEAT, MUTED = "manual", "too_late", "nothing", "notes_only", "repeat", "muted"


def decide(findings, manual, timely, shown=(), muted=None, situation=None):
    """(what to do, the finding or None, why).

    `shown`: keys of findings already shown for this conversation.
    `muted`: {kind: the situation it was turned away in}."""
    if manual:
        return (SHOW, findings[0], MANUAL) if findings else (NOTE, None, NOTHING)
    if not findings:
        return QUIET, None, NOTHING
    if not timely:
        return QUIET, None, TOO_LATE
    why = NOTES_ONLY
    for finding in findings:
        if finding.weight != au.DIRECTION:
            continue
        if au.finding_key(finding) in shown:
            why = REPEAT
        elif muted and finding.kind in muted and muted[finding.kind] == situation:
            why = MUTED
        else:
            return SHOW, finding, finding.kind
    return QUIET, None, why
