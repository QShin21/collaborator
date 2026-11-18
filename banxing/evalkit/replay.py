"""Replay: play recorded conversations to the watcher one turn at a time and see
whether it would have spoken at the right moment, about the right thing.

This separates the quality of the supervision from the live hook-up: no window, no
host, only "given what had been said by turn N, what did the watcher do".

For every case the conversation is revealed step by step — first the user's new
message with the main model still working, then the finished turn — exactly as the
session would have seen it. Measured, without a judge:

  timing        was the concern raised before more work was built on the mistake
                (inside the labelled window)? How many turns to spare; how many missed.
  aim           is it the kind of concern that was warranted, and do its passages
                contain the words they should (it points at the right place)?
  interruption  bubbles in cases where staying quiet was right (the claim had a
                record, the user changed the goal themselves, a justified deep dive,
                a carefully worded result, open exploration); bubbles before the
                evidence was there; bubbles repeated after the user had answered.
  agency        how often the supervisor went back to look, searched, or asked for
                a blind reading — and what that cost in requests.

and one control: the same model reads the conversation ONCE at the key turn, with
no ledger and no memory of earlier bubbles. If that does as well, watching the
conversation turn by turn buys nothing.

What the numbers mean depends on the model. With the offline stand-in they show that
the pipeline runs ("plumbing-only") and nothing about judgement; with a real model
they are structural evidence (E4-structural) on synthetic conversations — not a
replay of real ones, which is still to be done by someone who has them.
"""
from __future__ import annotations

import time

from .. import __version__, llm
from ..watch import audit as au
from ..watch import session as ss
from ..watch import transcript as ts
from .replay_cases import all_cases, transcript_of

QUIET_KINDS = ("checked", "user_pivot", "deep_dive", "hedged", "exploring")
ANSWERS = ("intended", "took_back", "wrong", "dismiss")


def _pending(transcript, number):
    """The conversation at the moment the user has just sent turn `number`: that turn
    holds the user's message only and the main model is still working."""
    turn = transcript.turn(number)
    earlier = tuple(t for t in transcript.turns if t.number < number)
    waiting = ts.Turn(number=number, user=turn.user, state=ts.RUNNING)
    return ts.Transcript(turns=earlier + (waiting,), gaps=tuple(g for g in transcript.gaps if g.turn < number),
                         source=transcript.source, conversation_id=transcript.conversation_id)


def _fresh():
    return {"requests": 0, "chars": 0, "seconds": [], "versions": set(), "errors": [], "unlocated": 0,
            "looks": 0, "searches": 0, "independent": 0, "decided": []}


def _drain(session, auditor, effects, seen):
    """Carry out effects until nothing is left to run. Returns the bubbles shown."""
    shown, queue = [], list(effects)
    while queue:
        effect = queue.pop(0)
        if isinstance(effect, ss.Run):
            audit = auditor.run(effect.job)
            seen["requests"] += audit.sent.requests
            seen["chars"] += audit.sent.chars
            seen["seconds"].append(audit.seconds)
            seen["versions"].add(audit.prompt_version)
            seen["looks"] += audit.trace.looks
            seen["searches"] += audit.trace.searches
            seen["independent"] += int(audit.trace.independent)
            if not audit.ok:
                seen["errors"].append(audit.error)
            seen["unlocated"] += int((audit.notes or {}).get("unlocated", 0))
            queue += session.audited(effect.job.id, audit)
        elif isinstance(effect, ss.ShowBubble):
            shown.append(effect.bubble)
        elif isinstance(effect, ss.Log) and effect.event == "watch_decided":
            seen["decided"].append(effect.data)
    return shown


def play(case, config, complete=llm.complete, permit=None):
    """One case, turn by turn. Returns what happened, in numbers and names."""
    whole = transcript_of(case)
    auditor = au.Auditor(lambda: config, complete=complete, permit=permit)
    session = ss.Session(destination=config.destination)
    seen = _fresh()
    bubbles, answered = [], set()
    actions = case.get("actions") or {}
    for turn in whole.turns:
        number = turn.number
        _drain(session, auditor, session.observe(_pending(whole, number)), seen)
        for bubble in _drain(session, auditor, session.observe(whole.upto(number)), seen):
            finding = bubble.finding
            bubbles.append({
                "turn": number, "kind": finding.kind, "after_answer": finding.kind in answered,
                "refs": len(finding.basis), "now": len(finding.now), "has_ask": bool(finding.ask),
                "quotes": " ".join(ref.quote for ref in finding.basis + finding.now)})
            action = actions.get(number)
            if action in ANSWERS:
                _drain(session, auditor, getattr(session, action)(), seen)
                answered.add(finding.kind)
    return {"bubbles": bubbles, "seen": seen, "usage": session.usage,
            "unchecked": sum(1 for a in session.ledger.anchors if a.status == "unchecked")}


def one_shot(case, config, turn, complete=llm.complete, permit=None):
    """The control: a fresh reader looks at the conversation once, at `turn`."""
    auditor = au.Auditor(lambda: config, complete=complete, permit=permit)
    session = ss.Session(destination=config.destination)
    seen = _fresh()
    shown = _drain(session, auditor, session.observe(transcript_of(case).upto(turn)), seen)
    return (shown[0].finding.kind if shown else ""), seen


def timeline(transcript, config, complete=llm.complete, permit=None, first=1):
    """Any conversation, replayed turn by turn with no labels: after which turns the
    watcher would have audited, and where a bubble would have appeared. This is how
    a REAL conversation is looked at (the user's own export): the rows hold turn
    numbers and names; the bubbles are returned for display and are not written
    anywhere by this function."""
    auditor = au.Auditor(lambda: config, complete=complete, permit=permit)
    session = ss.Session(destination=config.destination, first=first)
    seen = _fresh()
    rows, bubbles = [], []
    for turn in transcript.turns:
        if turn.number < first:
            continue
        before, errors, requests = len(seen["decided"]), len(seen["errors"]), seen["requests"]
        _drain(session, auditor, session.observe(_pending(transcript, turn.number)), seen)
        shown = _drain(session, auditor, session.observe(transcript.upto(turn.number)), seen) if turn.settled else []
        last = seen["decided"][before:][-1] if seen["decided"][before:] else {}
        rows.append({"turn": turn.number, "action": last.get("action", ""), "why": last.get("why", ""),
                     "bubble": shown[-1].finding.kind if shown else "", "requests": seen["requests"] - requests,
                     "error": seen["errors"][-1] if len(seen["errors"]) > errors else ""})
        bubbles += shown
    return {"rows": rows, "bubbles": bubbles, "session": session, "requests": seen["requests"],
            "chars": seen["chars"], "looks": seen["looks"], "searches": seen["searches"],
            "independent": seen["independent"], "prompt_versions": sorted(v for v in seen["versions"] if v)}


def score(case, played):
    """{check name: True | False | None} for one case, and the first fitting bubble."""
    bubbles = played["bubbles"]
    expect, window = case.get("expect"), case.get("window")
    result = {"answered": not played["seen"]["errors"]}
    hit = None
    if expect:
        first, last = window
        hit = next((b for b in bubbles if b["kind"] == expect and first <= b["turn"] <= last), None)
        result["in_time"] = hit is not None
        result["not_early"] = not any(b["turn"] < first for b in bubbles)
        if hit is not None:
            result["traceable"] = hit["refs"] > 0
            result["bears_on_now"] = hit["now"] > 0
            result["question_to_take_back"] = hit["has_ask"]
            if case.get("quotes"):
                result["points_at_the_place"] = all(word in hit["quotes"] for word in case["quotes"])
        result["no_other_bubble"] = all(b is hit or b["kind"] == expect for b in bubbles)
    else:
        result["stayed_quiet"] = not bubbles
    if case.get("actions"):
        result["answer_respected"] = not any(b["after_answer"] for b in bubbles)
    return result, hit


def run(model_config, permit=None, complete=llm.complete, limit=0, cases=None):
    config = model_config()
    if permit is not None and not permit(config):
        raise PermissionError("sending the conversations to this destination has not been agreed to")
    cases = list(cases or all_cases())[: limit or None]
    rows, totals, versions, seconds = [], {}, set(), []
    spare, interruptions, control_requests = [], {}, 0
    agency = {"requests": 0, "chars_sent": 0, "looks": 0, "searches": 0, "independent_readings": 0}
    control = {"agrees": 0, "of": 0}
    started = time.monotonic()
    for case in cases:
        played = play(case, config, complete, permit)
        checks, hit = score(case, played)
        seen = played["seen"]
        versions |= seen["versions"]
        seconds += seen["seconds"]
        for name, key in (("requests", "requests"), ("chars_sent", "chars"), ("looks", "looks"),
                          ("searches", "searches"), ("independent_readings", "independent")):
            agency[name] += seen[key]
        if hit is not None:
            spare.append(case["window"][1] - hit["turn"])
        if not case.get("expect") and played["bubbles"]:
            kind = case.get("quiet") or "other"
            interruptions[kind] = interruptions.get(kind, 0) + len(played["bubbles"])
        # The control reads once, at the last moment speaking up would still be in time
        # (or at the end, for the cases where silence is right).
        key_turn = case["window"][1] if case.get("expect") else len(case["turns"])
        said, control_seen = one_shot(case, config, key_turn, complete, permit)
        control_requests += control_seen["requests"]
        control["of"] += 1
        control["agrees"] += int(said == (case.get("expect") or ""))
        rows.append({"id": case["id"], "expect": case.get("expect") or "quiet", "quiet": case.get("quiet", ""),
                     "bubbles": [{"turn": b["turn"], "kind": b["kind"]} for b in played["bubbles"]],
                     "one_shot": said or "quiet", "requests": seen["requests"], "looks": seen["looks"],
                     "errors": seen["errors"], "unlocated": seen["unlocated"], "checks": checks})
        for name, value in checks.items():
            if value is None:
                continue
            passed, counted = totals.get(name, (0, 0))
            totals[name] = (passed + int(bool(value)), counted + 1)
    seconds.sort()
    mock = config.provider == "mock"
    expected = [row for row in rows if row["expect"] != "quiet"]
    return {
        "kind": "banxing-replay", "app_version": __version__, "provider": config.provider, "model": config.model_name,
        "prompt_versions": sorted(v for v in versions if v),
        "evidence": "plumbing-only" if mock else "E4-structural",
        "note": ("Offline stand-in: this shows the replay pipeline runs. It is NOT evidence about the quality of "
                 "the supervision or of its timing." if mock else
                 "Structural checks on a real model's audits of SYNTHETIC conversations. No judge model, no human "
                 "rating, no real conversation."),
        "cases": len(rows),
        "timing": {"expected": len(expected), "in_time": sum(1 for row in expected if row["checks"].get("in_time")),
                   "missed": sum(1 for row in expected if not row["checks"].get("in_time")),
                   "turns_to_spare": spare},
        "interruptions": {"in_quiet_cases": interruptions,
                          "early": sum(1 for row in expected if row["checks"].get("not_early") is False),
                          "repeated_after_answer": sum(1 for row in rows
                                                       if row["checks"].get("answer_respected") is False)},
        "agency": agency,
        "control_one_shot": dict(control, requests=control_requests),
        "rates": {name: {"passed": passed, "of": counted, "rate": round(passed / counted, 3)}
                  for name, (passed, counted) in sorted(totals.items())},
        "median_seconds": round(seconds[len(seconds) // 2], 2) if seconds else None,
        "total_seconds": round(time.monotonic() - started, 2),
        "plumbing_ok": bool(rows) and all(row["checks"].get("answered") for row in rows),
        "rows": rows,
    }


def as_text(report):
    timing, noise, control, agency = (report["timing"], report["interruptions"], report["control_one_shot"],
                                      report["agency"])
    lines = [f"Banxing replay · {report['provider']} / {report['model']} · prompts {', '.join(report['prompt_versions'])}",
             f"evidence: {report['evidence']} — {report['note']}",
             f"{report['cases']} conversations, {agency['requests']} requests, {agency['chars_sent']} characters sent",
             f"timing: {timing['in_time']}/{timing['expected']} in time, {timing['missed']} missed, "
             f"turns to spare {timing['turns_to_spare']}",
             f"interruptions: in quiet cases {noise['in_quiet_cases'] or 0}, early {noise['early']}, "
             f"repeated after an answer {noise['repeated_after_answer']}",
             f"agency: went back to look {agency['looks']} times, {agency['searches']} searches, "
             f"{agency['independent_readings']} blind readings",
             f"control (one read at the key turn): agrees with the label in {control['agrees']}/{control['of']} cases, "
             f"{control['requests']} requests"]
    for name, rate in report["rates"].items():
        lines.append(f"  {name:<24} {rate['passed']:>3}/{rate['of']:<3} {rate['rate']:.0%}")
    for row in report["rows"]:
        bubbles = ", ".join(f"{b['kind']}@{b['turn']}" for b in row["bubbles"]) or "—"
        mark = "ok " if all(value is not False for value in row["checks"].values()) else "!! "
        lines.append(f"  {mark}{row['id']:<28} expect {row['expect']:<10} got {bubbles:<16} one-shot {row['one_shot']}")
    return "\n".join(lines)
