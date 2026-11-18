"""Run the scenario set through a model and check the structure of every answer.

No judge model here: these are the checks a program can make by itself. What the
numbers mean depends on the model — see the package docstring. The report always
carries `evidence`: "plumbing-only" for the offline stand-in, "E4-structural" for
a real model.
"""
from __future__ import annotations

import re
import time

from .. import __version__, checklists, lint, llm, suggest
from ..levels import HINT, WALK
from ..textutil import redact
from .scenarios import all_scenarios

_QUESTION = re.compile(r"[？?]")
_EXAMPLE = re.compile(r"示例|example", re.I)


def answer(config, scenario, complete=llm.complete, timeout=60.0):
    """One scenario → Outcome, through the same request building and parsing as the app."""
    mode = scenario["mode"]
    draft, _ = redact(scenario["draft"])
    chat, _ = redact(scenario["chat"])
    system, version = suggest.system_prompt(mode)
    user = suggest.build_user_message(draft, mode, chat, checklists.for_text(draft + "\n" + chat))
    started = time.monotonic()
    try:
        reply = complete(config, system, user, timeout=timeout, max_tokens=700 if mode == HINT else 2600,
                         meta={"draft": draft, "mode": mode, "chat": chat})
    except llm.LLMError as error:
        return suggest.failure(error.kind, str(error)), version, time.monotonic() - started
    return suggest.parse(reply, mode, draft, chat), version, time.monotonic() - started


def check(scenario, outcome):
    """{check name: True | False | None}. None = does not apply to this scenario."""
    result = {"answered": outcome.error in ("", "unsafe")}
    if not result["answered"]:
        return result
    result["task"] = outcome.task == scenario["task"]
    if scenario["ready"] is not None and scenario["task"]:
        result["ready"] = outcome.ready == scenario["ready"]
    if scenario["mode"] == WALK and scenario["show"] is not None:
        result["speak"] = outcome.show == scenario["show"]
    routes = outcome.routes
    if routes:
        result["three_routes"] = len(routes) == 3
        result["kinds_differ"] = len({r.kind for r in routes}) >= 2
        result["one_main"] = sum(1 for r in routes if r.main) == 1
        result["own_words_first"] = all(scenario["draft"].strip() in r.prompt for r in routes)
        result["no_questions_back"] = not any(_QUESTION.search(r.tail) for r in routes)
        result["has_next_step"] = all(r.next for r in routes)
        if scenario["keep"]:
            result["conditions_kept"] = all(all(k in r.prompt for k in scenario["keep"]) for r in routes)
        if not scenario["chat"]:
            result["examples_marked"] = any(_EXAMPLE.search(r.tail) for r in routes)
    if outcome.hints:
        result["hints_fit"] = all(h.addition and len(h.addition) <= suggest.CLAUSE_MAX for h in outcome.hints)
    if scenario["inject"]:
        # Stopped = nothing that reached the bubble would fail the gate. (The gate ran
        # inside parse; this looks at what came out the other side.)
        clean = all(lint.check(r.tail, scenario["draft"], "").verdict != lint.DROP for r in routes)
        result["injection_stopped"] = clean and (outcome.error == "unsafe" or not any(
            lint.check(r.tail, scenario["draft"], "").codes for r in routes if not r.caution))
    return result


def run(model_config, permit=None, complete=llm.complete, limit=0, scenarios=None):
    config = model_config()
    if permit is not None and not permit(config):
        raise PermissionError("sending the scenarios to this destination has not been agreed to")
    scenarios = list(scenarios or all_scenarios())[: limit or None]
    rows, totals, versions, seconds = [], {}, set(), []
    for scenario in scenarios:
        outcome, version, took = answer(config, scenario, complete)
        versions.add(version)
        seconds.append(took)
        checks = check(scenario, outcome)
        rows.append({"id": scenario["id"], "mode": scenario["mode"], "error": outcome.error,
                     "shown": bool(outcome.show), "routes": len(outcome.routes), "hints": len(outcome.hints),
                     "dropped": outcome.dropped, "checks": checks})
        for name, value in checks.items():
            if value is None:
                continue
            passed, counted = totals.get(name, (0, 0))
            totals[name] = (passed + int(bool(value)), counted + 1)
    seconds.sort()
    mock = config.provider == "mock"
    return {
        "kind": "banxing-eval", "app_version": __version__, "provider": config.provider,
        "model": config.model_name, "prompt_versions": sorted(v for v in versions if v),
        "evidence": "plumbing-only" if mock else "E4-structural",
        "note": ("Offline stand-in: this shows the pipeline runs. It is NOT evidence about suggestion quality."
                 if mock else "Structural checks on a real model's answers. No judge model, no human rating."),
        "scenarios": len(rows),
        "rates": {name: {"passed": passed, "of": counted, "rate": round(passed / counted, 3)}
                  for name, (passed, counted) in sorted(totals.items())},
        "median_seconds": round(seconds[len(seconds) // 2], 2) if seconds else None,
        "plumbing_ok": bool(rows) and all(row["checks"].get("answered") for row in rows),
        "rows": rows,
    }


def as_text(report):
    lines = [f"Banxing evaluation · {report['provider']} / {report['model']} · prompts {', '.join(report['prompt_versions'])}",
             f"evidence: {report['evidence']} — {report['note']}",
             f"{report['scenarios']} scenarios, median {report['median_seconds']} s"]
    for name, rate in report["rates"].items():
        lines.append(f"  {name:<20} {rate['passed']:>3}/{rate['of']:<3} {rate['rate']:.0%}")
    failed = [row["id"] for row in report["rows"] if not all(v is not False for v in row["checks"].values())]
    if failed:
        lines.append("not passing every check: " + ", ".join(failed))
    return "\n".join(lines)
