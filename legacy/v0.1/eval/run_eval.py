#!/usr/bin/env python3
"""Offline comparison of agents: simulated learners, pre/post tests, blind transcript judging.

For every (scenario, agent) pair:

  1. PRE   the simulated learner writes what they understand of their own problem
  2. WORK  the learner works with the agent for a few turns
           (scaffolded agents: framing -> the learner picks threads -> plan -> chat;
            the collaborator additionally shows bubbles the learner may use or let fade)
  3. POST  the learner writes their understanding again
  4. JUDGE both answers are graded on the same rubric (the judge does not know the agent),
           and the whole transcript is graded for scaffolding / agency / breadth / progress /
           efficiency

Usage:
    python3 eval/run_eval.py                       # all scenarios, all agents
    python3 eval/run_eval.py --agents baseline collaborator --turns 4 --scenarios waste-sorting
    COLLAB_PROVIDER=mock python3 eval/run_eval.py  # plumbing check without an API key

Results from the mock provider are canned and say nothing about quality.
Simulated learners are a cheap first filter, not a substitute for studies with real people —
use the in-app pre/post tests and the arena for that.
"""
import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from collab import config, llm  # noqa: E402
from collab.db import Store  # noqa: E402
from collab.engine import (AGENTS, RUBRIC, TRANSCRIPT_RUBRIC, Engine, frame_to_text,  # noqa: E402
                           is_zh, prompt)
from collab.jsonutil import as_list, as_str, clamp, extract_json  # noqa: E402

OPEN_QUESTION = ("In your own words: what is this problem trying to solve? What would count as "
                 "success? How do you plan to go about it? (Answer in the language of your problem, "
                 "in a few sentences, only from what you understand right now.)")


def student_system(scenario):
    return (prompt("sim_student") + "\n\n<character>\n" + scenario["persona"] + "\n</character>\n\n"
            "<your_problem>\n" + scenario["problem"] + "\n</your_problem>")


def render_transcript(lines):
    return "\n\n".join(f"{who.upper()}: {text}" for who, text in lines)


def student_answer(scenario, transcript, task, agent):
    """The learner answers the open question, before (no transcript) or after the session."""
    content = ""
    if transcript:
        content += ("Here is the conversation you just had with an AI assistant:\n\n<conversation>\n"
                    + render_transcript(transcript) + "\n</conversation>\n\n")
    content += OPEN_QUESTION
    return llm.complete(task, student_system(scenario), [{"role": "user", "content": content}],
                        max_tokens=500, meta={"agent": agent})


def student_choose(scenario, frame_text, thread_ids):
    content = ("The assistant read your problem and replied:\n\n<assistant>\n" + frame_text
               + "\n</assistant>\n\nThread ids available: " + ", ".join(thread_ids) + ".\n"
               "Decide as your character would. Reply with one JSON object only:\n"
               '{"thread_ids": ["ids you choose, one or two"], '
               '"answers": "your short answers to any of its questions you can answer, or empty"}')
    data = extract_json(llm.complete("sim_choose", student_system(scenario),
                                     [{"role": "user", "content": content}], max_tokens=400)) or {}
    chosen = [t for t in as_list(data.get("thread_ids")) if t in thread_ids][:3]
    return chosen or thread_ids[:1], as_str(data.get("answers"), 1500)


def student_turn(scenario, transcript, bubbles):
    content = ("The conversation so far:\n\n<conversation>\n" + render_transcript(transcript)
               + "\n</conversation>\n\n")
    if bubbles:
        content += ("While you work, these small optional hints floated up at the edge of your "
                    "screen. You may take one up, or ignore them:\n"
                    + "\n".join(f"[{i}] {b['text']}" for i, b in enumerate(bubbles)) + "\n\n")
    content += ("Write your next message to the assistant, as your character. Reply with one JSON "
                'object only: {"message": "...", "used_bubbles": [indexes of hints that actually '
                "influenced your message, or empty]}")
    raw = llm.complete("sim_turn", student_system(scenario), [{"role": "user", "content": content}],
                       max_tokens=500, meta={"bubbles": bubbles})
    data = extract_json(raw) or {"message": raw}
    used = [i for i in as_list(data.get("used_bubbles")) if isinstance(i, int) and 0 <= i < len(bubbles)]
    return as_str(data.get("message"), 3000) or "…", used


def judge_transcript(engine, scenario, transcript, agent):
    content = ("<problem>\n" + scenario["problem"] + "\n</problem>\n\n<interaction>\n"
               + render_transcript(transcript) + "\n</interaction>")
    data = engine._json_call("judge_transcript", prompt("judge_transcript"), content,
                             {"agent": agent}, max_tokens=600)
    raw = data.get("scores") if isinstance(data.get("scores"), dict) else {}
    scores = {dim: int(round(clamp(raw.get(dim), 0, 4, 0))) for dim in TRANSCRIPT_RUBRIC}
    scores["comment"] = as_str(data.get("comment"), 500)
    return scores


def run_one(scenario, agent, turns, log):
    engine = Engine(Store(":memory:"))
    transcript = []
    pre_answer = student_answer(scenario, None, "sim_pre", agent)
    transcript.append(("learner", scenario["problem"]))
    session = engine.create_session(scenario["problem"], agent=agent)
    sid = session["id"]
    bubble_log = {"shown": 0, "used": 0}

    if AGENTS[agent]["scaffold"]:
        frame = session["state"]["frame"]
        transcript.append(("assistant", frame_to_text(frame, is_zh(scenario["problem"]))))
        if session["phase"] == "frame":
            ids = [t["id"] for t in frame["threads"]]
            chosen, answers = student_choose(scenario, transcript[-1][1], ids)
            titles = [t["title"] for t in frame["threads"] if t["id"] in chosen]
            transcript.append(("learner", "; ".join(titles) + (("\n" + answers) if answers else "")))
            session = engine.plan(sid, chosen, "", answers or None)
        card = session["state"]["card"] or {}
        tasks = session["state"]["tasks"]
        opening = next((m["content"] for m in engine.store.messages(sid) if m["role"] == "assistant"), "")
        transcript.append(("assistant", "[problem card] goal: " + card.get("goal", "")
                           + " | success criteria: " + "; ".join(card.get("success_criteria", []))
                           + " | tasks: " + "; ".join(t["title"] for t in tasks)
                           + ("\n" + opening if opening else "")))
    else:
        transcript.append(("assistant", engine.store.messages(sid)[-1]["content"]))

    for _ in range(turns):
        bubbles = engine.bubbles(sid, trigger="manual") if AGENTS[agent]["bubbles"] else []
        message, used = student_turn(scenario, transcript, bubbles)
        for index, bubble in enumerate(bubbles):
            if index in used:
                engine.feedback(bubble["id"], "accept")
                transcript.append(("learner", f"(kept the hint: {bubble['text']})"))
            else:
                engine.feedback(bubble["id"], "fade")
        bubble_log["shown"] += len(bubbles)
        bubble_log["used"] += len(used)
        reply = engine.chat(sid, message)[1]["content"]
        transcript += [("learner", message), ("assistant", reply)]
        log(".")

    post_answer = student_answer(scenario, transcript, "sim_post", agent)
    pre = engine.judge_answer(scenario["problem"], pre_answer)
    post = engine.judge_answer(scenario["problem"], post_answer)
    return {
        "scenario": scenario["id"], "kind": scenario.get("kind", ""), "agent": agent,
        "pre": pre, "post": post, "gain": post.get("total", 0) - pre.get("total", 0),
        "transcript_scores": judge_transcript(engine, scenario, transcript, agent),
        "bubbles": bubble_log, "pre_answer": pre_answer, "post_answer": post_answer,
        "transcript": transcript,
    }


def mean(values):
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else 0.0


def build_report(results, args):
    agents = [a for a in args.agents if any(r["agent"] == a for r in results)]
    out = ["# Agent comparison report", "",
           f"- date: {time.strftime('%Y-%m-%d %H:%M')}",
           f"- provider / model: {config.provider()} / {config.model()}",
           f"- scenarios: {len({r['scenario'] for r in results})} · turns per session: {args.turns}",
           ""]
    if config.provider() == "mock":
        out += ["> **Mock provider.** These numbers are canned and only prove the pipeline runs.", ""]
    out += ["## Pre/post test (learner's own problem statement, rubric 0–16)", "",
            "| agent | pre | post | gain | " + " | ".join(RUBRIC) + " (post) |",
            "|---|---|---|---|" + "---|" * len(RUBRIC)]
    for agent in agents:
        rows = [r for r in results if r["agent"] == agent]
        dims = " | ".join(f"{mean([r['post'].get(d) for r in rows]):.2f}" for d in RUBRIC)
        out.append(f"| {agent} | {mean([r['pre'].get('total') for r in rows]):.2f} "
                   f"| {mean([r['post'].get('total') for r in rows]):.2f} "
                   f"| {mean([r['gain'] for r in rows]):+.2f} | {dims} |")
    out += ["", "## Interaction quality (blind transcript judging, 0–4)", "",
            "| agent | " + " | ".join(TRANSCRIPT_RUBRIC) + " | mean |",
            "|---|" + "---|" * (len(TRANSCRIPT_RUBRIC) + 1)]
    for agent in agents:
        rows = [r for r in results if r["agent"] == agent]
        values = [mean([r["transcript_scores"].get(d) for r in rows]) for d in TRANSCRIPT_RUBRIC]
        out.append(f"| {agent} | " + " | ".join(f"{v:.2f}" for v in values) + f" | {mean(values):.2f} |")
    out += ["", "## Gain by problem type", "", "| agent | " + " | ".join(("ill", "mixed", "well")) + " |",
            "|---|---|---|---|"]
    for agent in agents:
        cells = []
        for kind in ("ill", "mixed", "well"):
            rows = [r["gain"] for r in results if r["agent"] == agent and r["kind"] == kind]
            cells.append(f"{mean(rows):+.2f} (n={len(rows)})" if rows else "—")
        out.append(f"| {agent} | " + " | ".join(cells) + " |")
    shown = sum(r["bubbles"]["shown"] for r in results)
    if shown:
        used = sum(r["bubbles"]["used"] for r in results)
        out += ["", f"Bubbles: {shown} shown, {used} taken up by the simulated learners "
                    f"({100 * used / shown:.0f}%)."]
    out += ["", "## Per scenario", "", "| scenario | agent | pre | post | gain | judge comment |",
            "|---|---|---|---|---|---|"]
    for r in results:
        comment = r["transcript_scores"].get("comment", "").replace("|", "/").replace("\n", " ")
        out.append(f"| {r['scenario']} | {r['agent']} | {r['pre'].get('total', 0)} "
                   f"| {r['post'].get('total', 0)} | {r['gain']:+d} | {comment} |")
    out += ["", "Caveat: simulated learners and an LLM judge. Treat as a screening signal; "
                "confirm with real learners using the in-app pre/post tests and arena."]
    return "\n".join(out) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--agents", nargs="+", default=list(AGENTS), choices=list(AGENTS))
    parser.add_argument("--scenarios", nargs="*", help="scenario ids (default: all)")
    parser.add_argument("--file", default=str(ROOT / "eval" / "scenarios.json"))
    parser.add_argument("--turns", type=int, default=3)
    parser.add_argument("--out", default=str(ROOT / "eval" / "reports"))
    args = parser.parse_args()

    scenarios = json.loads(Path(args.file).read_text(encoding="utf-8"))
    if args.scenarios:
        scenarios = [s for s in scenarios if s["id"] in args.scenarios]
    if not scenarios:
        sys.exit("no scenarios selected")

    def log(text):
        sys.stderr.write(text)
        sys.stderr.flush()

    results, failures = [], []
    for scenario in scenarios:
        for agent in args.agents:
            log(f"\n{scenario['id']} × {agent} ")
            try:
                results.append(run_one(scenario, agent, args.turns, log))
            except Exception as error:  # one failed pair should not lose the whole run
                failures.append(f"{scenario['id']} × {agent}: {error}")
                log(f" FAILED: {error}")
    log("\n")
    if not results:
        sys.exit("every run failed:\n" + "\n".join(failures))

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    report = build_report(results, args)
    if failures:
        report += "\n## Failed runs\n\n" + "\n".join(f"- {f}" for f in failures) + "\n"
    (out_dir / f"report-{stamp}.md").write_text(report, encoding="utf-8")
    (out_dir / f"report-{stamp}.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(report)
    print(f"saved to {out_dir}/report-{stamp}.md (+ .json with full transcripts)")
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
