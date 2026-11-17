"""The collaborator itself: framing, planning, conversation, bubbles, adaptation.

Everything here is independent of HTTP so that the web server, the tests and the
offline evaluation harness all drive the same code.
"""
import random
import re
import threading
import time

from . import adapt, config, llm
from .jsonutil import as_list, as_str, clamp, extract_json

# The agents that can be compared against each other. `collaborator` is the product;
# the others are controls for before/after and side-by-side evaluation.
AGENTS = {
    "collaborator": {"scaffold": True, "bubbles": True, "chat": "collab"},
    "scaffold": {"scaffold": True, "bubbles": False, "chat": "collab"},      # ablation: no bubbles
    "socratic": {"scaffold": False, "bubbles": False, "chat": "socratic"},   # questions only
    "baseline": {"scaffold": False, "bubbles": False, "chat": "baseline"},   # plain chat assistant
}

LIKERT_PRE = ["clarity", "next_step", "confidence"]
LIKERT_POST = LIKERT_PRE + ["ownership", "helpful"]
RUBRIC = ["clarity", "structure", "criteria", "actionability"]
TRANSCRIPT_RUBRIC = ["scaffolding", "agency", "breadth", "progress", "efficiency"]

_STATUS = {"accept": "accepted", "ignore": "ignored", "expand": "expanded", "fade": "faded"}


class EngineError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def prompt(name):
    """Prompts live in prompts/*.md and are re-read on every call, so they can be
    edited while the server is running."""
    return (config.ROOT / "prompts" / f"{name}.md").read_text(encoding="utf-8").strip()


def is_zh(text):
    return bool(re.search(r"[一-鿿]", text or ""))


def _clip(text, limit):
    text = text or ""
    return text if len(text) <= limit else text[:limit] + " …"


def _tail(text, limit):
    text = text or ""
    return text if len(text) <= limit else "… " + text[-limit:]


def _str_list(value, limit=8, item_limit=400):
    return [as_str(v, item_limit) for v in as_list(value) if as_str(v)][:limit]


class Engine:
    def __init__(self, store, rng=None):
        self.store = store
        self.rng = rng or random.Random()
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ utils
    def _get(self, sid):
        session = self.store.get_session(sid)
        if not session:
            raise EngineError("session not found", 404)
        return session

    def _mutate(self, sid, fn):
        """Load-modify-save under a lock. Model calls happen outside of it."""
        with self._lock:
            session = self._get(sid)
            fn(session)
            self.store.save_session(session)
            return session

    def _json_call(self, task, system, content, meta, max_tokens=2500, image_b64=None):
        last = ""
        for _ in range(2):
            last = llm.complete(task, system, [{"role": "user", "content": content}],
                                max_tokens=max_tokens, image_b64=image_b64, meta=meta)
            data = extract_json(last)
            if isinstance(data, dict):
                return data
        raise EngineError(f"the model did not return valid JSON for '{task}': {last[:200]}", 502)

    def profile(self):
        return adapt.normalize(self.store.kv_get("profile"))

    def view(self, sid):
        """Everything the client needs to render a session."""
        session = self._get(sid)
        agent = AGENTS.get(session["agent"], AGENTS["collaborator"])
        return {
            "session": session,
            "features": agent,
            "messages": self.store.messages(sid),
            "tests": self.store.tests(sid),
            "params": adapt.bubble_params(session["state"].get("adapt")),
        }

    # --------------------------------------------------------------- creation
    def create_session(self, problem, goal="", ideas="", agent="collaborator"):
        problem = as_str(problem, 8000)
        if not problem:
            raise EngineError("describe the problem first")
        if agent not in AGENTS:
            raise EngineError(f"unknown agent: {agent}")
        state = {
            "problem": problem, "goal": as_str(goal, 2000), "ideas": as_str(ideas, 4000),
            "clarifications": "", "frame": None, "chosen": [], "custom_thread": "",
            "card": None, "tasks": [], "notes": "", "shelf": [],
            "adapt": self.profile(),
        }
        title = _clip(" ".join(problem.split()), 40)
        session = self.store.create_session(title, agent, "work", state)
        sid = session["id"]
        self.store.log(sid, "session_created", {"agent": agent})
        try:
            if AGENTS[agent]["scaffold"]:
                self.frame(sid)
            else:
                self.chat(sid, self._problem_text(state))
        except Exception:
            self.store.delete_session(sid)
            raise
        return self._get(sid)

    @staticmethod
    def _problem_text(state):
        parts = [state.get("problem", "")]
        zh = is_zh(state.get("problem", ""))
        if state.get("goal"):
            parts.append(("我的目标：" if zh else "My goal: ") + state["goal"])
        if state.get("ideas"):
            parts.append(("我目前的想法：" if zh else "My ideas so far: ") + state["ideas"])
        return "\n\n".join(p for p in parts if p)

    # ---------------------------------------------------------------- framing
    def _frame_call(self, state, force_map=False):
        content = "The learner wrote:\n\n<problem>\n" + self._problem_text(state) + "\n</problem>"
        if state.get("clarifications"):
            content += ("\n\nThe learner later added these clarifications:\n<clarifications>\n"
                        + state["clarifications"] + "\n</clarifications>")
        if force_map:
            content += ("\n\nThe learner explicitly asked to see alternative directions, so "
                        "provide the landscape and threads even if the problem is well-structured.")
        system = prompt("core") + "\n\n" + prompt("frame")
        data = self._json_call("frame", system, content,
                               {"problem": state["problem"], "force_map": force_map})
        return self._clean_frame(data, state)

    @staticmethod
    def _clean_frame(data, state):
        structuredness = as_str(data.get("structuredness")).lower()
        if structuredness not in ("well", "mixed", "ill"):
            structuredness = "ill"
        threads = []
        for index, raw in enumerate(as_list(data.get("threads"))[:8]):
            if not isinstance(raw, dict) or not as_str(raw.get("title")):
                continue
            threads.append({
                "id": f"t{index + 1}",
                "type": "side" if as_str(raw.get("type")).lower() == "side" else "main",
                "title": as_str(raw.get("title"), 200),
                "summary": as_str(raw.get("summary"), 600),
                "first_step": as_str(raw.get("first_step"), 400),
                "learn": as_str(raw.get("learn"), 300),
            })
        landscape = []
        for raw in as_list(data.get("landscape"))[:8]:
            if isinstance(raw, dict) and as_str(raw.get("topic")):
                landscape.append({"topic": as_str(raw.get("topic"), 120),
                                  "why": as_str(raw.get("why"), 400)})
            elif isinstance(raw, str) and raw.strip():
                landscape.append({"topic": raw.strip()[:120], "why": ""})
        default_score = {"well": 0.85, "mixed": 0.5, "ill": 0.2}[structuredness]
        return {
            "title": as_str(data.get("title"), 60),
            "structuredness": structuredness,
            "score": round(clamp(data.get("score"), 0, 1, default_score), 2),
            "restatement": as_str(data.get("restatement"), 1200),
            "knowns": _str_list(data.get("knowns")),
            "unknowns": _str_list(data.get("unknowns")),
            "clarifying_questions": _str_list(data.get("clarifying_questions"), 4),
            "landscape": landscape,
            "threads": threads,
            "recommendation": as_str(data.get("recommendation"), 600),
        }

    def frame(self, sid, clarifications=None, force_map=False):
        """Diagnose the problem; build the ill→well scaffold when one is needed."""
        session = self._get(sid)
        if not AGENTS[session["agent"]]["scaffold"]:
            raise EngineError("this agent does not frame problems")
        state = session["state"]
        if clarifications is not None:
            state["clarifications"] = as_str(clarifications, 4000)
        frame = self._frame_call(state, force_map)

        def apply(s):
            s["state"]["frame"] = frame
            s["state"]["clarifications"] = state.get("clarifications", "")
            if frame["title"]:
                s["title"] = frame["title"]
            s["phase"] = "frame"
        self._mutate(sid, apply)
        self.store.log(sid, "framed", {"structuredness": frame["structuredness"],
                                       "score": frame["score"], "threads": len(frame["threads"])})
        # A well-structured problem needs no map: go straight to a plan.
        if frame["structuredness"] == "well" and not frame["threads"]:
            return self.plan(sid, [], "")
        return self._get(sid)

    # --------------------------------------------------------------- planning
    def plan(self, sid, thread_ids, custom_thread="", clarifications=None):
        """The learner chose their line(s); produce the well-structured problem card."""
        if clarifications is not None:
            session = self._mutate(sid, lambda s: s["state"].__setitem__(
                "clarifications", as_str(clarifications, 4000)))
        else:
            session = self._get(sid)
        if not AGENTS[session["agent"]]["scaffold"]:
            raise EngineError("this agent does not plan")
        state = session["state"]
        frame = state.get("frame") or {}
        thread_ids = [as_str(t) for t in as_list(thread_ids)]
        chosen = [t for t in frame.get("threads", []) if t["id"] in thread_ids]
        custom_thread = as_str(custom_thread, 600)
        lines = []
        for thread in chosen:
            lines.append(f"- [{thread['id']}, {thread['type']}] {thread['title']}: {thread['summary']}")
        if custom_thread:
            lines.append(f"- [own, main] (the learner's own direction) {custom_thread}")
        content = ("<problem>\n" + self._problem_text(state) + "\n</problem>\n\n"
                   "<framing>\nStructuredness: " + as_str(frame.get("structuredness")) + "\n"
                   "Restatement: " + as_str(frame.get("restatement")) + "\n"
                   "Unknowns: " + "; ".join(frame.get("unknowns", [])) + "\n</framing>\n\n")
        if state.get("clarifications"):
            content += "<clarifications>\n" + state["clarifications"] + "\n</clarifications>\n\n"
        content += "<chosen_threads>\n" + ("\n".join(lines) or "(none — the problem is already "
                                           "well-structured; plan the direct solution)") + "\n</chosen_threads>"
        system = prompt("core") + "\n\n" + prompt("plan")
        titles = [t["title"] for t in chosen] + ([custom_thread] if custom_thread else [])
        data = self._json_call("plan", system, content,
                               {"problem": state["problem"], "goal": state.get("goal"),
                                "chosen_titles": titles})
        raw_card = data.get("card") if isinstance(data.get("card"), dict) else {}
        card = {
            "goal": as_str(raw_card.get("goal"), 800),
            "givens": _str_list(raw_card.get("givens")),
            "constraints": _str_list(raw_card.get("constraints")),
            "success_criteria": _str_list(raw_card.get("success_criteria")),
        }
        tasks = []
        for index, raw in enumerate(as_list(data.get("tasks"))[:10]):
            if isinstance(raw, str):
                raw = {"title": raw}
            if isinstance(raw, dict) and as_str(raw.get("title")):
                tasks.append({"id": f"k{index + 1}", "title": as_str(raw.get("title"), 200),
                              "detail": as_str(raw.get("detail"), 500),
                              "thread": as_str(raw.get("thread"), 12), "done": False})
        opening = as_str(data.get("opening"), 1500)

        def apply(s):
            s["state"]["chosen"] = [t["id"] for t in chosen]
            s["state"]["custom_thread"] = custom_thread
            s["state"]["card"] = card
            s["state"]["tasks"] = tasks
            s["phase"] = "work"
        self._mutate(sid, apply)
        if opening:
            self.store.add_message(sid, "assistant", opening, {"type": "opening"})
        self.store.log(sid, "planned", {"chosen": [t["id"] for t in chosen],
                                        "custom": bool(custom_thread), "tasks": len(tasks)})
        return self._get(sid)

    # ----------------------------------------------------------- conversation
    @staticmethod
    def workspace(state, notes_limit=3000):
        """The text rendering of what is on the learner's screen."""
        out = ["<workspace>", "Problem (learner's words): " + _clip(state.get("problem", ""), 1500)]
        card = state.get("card")
        if card:
            out.append("Problem card:")
            out.append("  Goal: " + card.get("goal", ""))
            for key, label in (("givens", "Givens"), ("constraints", "Constraints"),
                               ("success_criteria", "Success criteria")):
                if card.get(key):
                    out.append(f"  {label}: " + "; ".join(card[key]))
        frame = state.get("frame") or {}
        chosen = [t for t in frame.get("threads", []) if t["id"] in state.get("chosen", [])]
        if chosen or state.get("custom_thread"):
            out.append("Chosen threads:")
            for thread in chosen:
                out.append(f"  - ({thread['type']}) {thread['title']}")
            if state.get("custom_thread"):
                out.append(f"  - (learner's own) {state['custom_thread']}")
        if state.get("tasks"):
            out.append("Tasks:")
            for task in state["tasks"]:
                out.append(f"  [{'x' if task.get('done') else ' '}] {task.get('title', '')}")
        if state.get("shelf"):
            out.append("Ideas the learner kept: " + " | ".join(
                item.get("text", "") for item in state["shelf"][-8:]))
        notes = (state.get("notes") or "").strip()
        out.append("Learner's notes:\n" + (_tail(notes, notes_limit) if notes else "(empty)"))
        out.append("</workspace>")
        return "\n".join(out)

    @staticmethod
    def _history(messages, limit=24):
        history = []
        for message in messages[-limit:]:
            content = message["content"]
            if message.get("meta", {}).get("type") == "expand":
                content = f'[I clicked "expand" on your bubble: "{content}"]'
            history.append({"role": message["role"], "content": content})
        return history

    def _chat_system(self, session):
        style = AGENTS[session["agent"]]["chat"]
        if style == "baseline":
            return prompt("baseline"), "baseline"
        if style == "socratic":
            return prompt("socratic"), "socratic"
        state = session["state"]
        system = "\n\n".join([prompt("core"), prompt("chat"),
                              adapt.stance(state.get("adapt")), self.workspace(state)])
        return system, "chat"

    def chat(self, sid, content):
        content = as_str(content, 12000)
        if not content:
            raise EngineError("empty message")
        session = self._get(sid)
        user = self.store.add_message(sid, "user", content)
        system, task = self._chat_system(session)
        history = self._history(self.store.messages(sid))
        reply = llm.complete(task, system, history, max_tokens=2000,
                             meta={"problem": session["state"].get("problem")})
        assistant = self.store.add_message(sid, "assistant", reply or "…")
        self._mutate(sid, lambda s: None)  # touch `updated`
        self.store.log(sid, "chat", {"chars_in": len(content), "chars_out": len(reply)})
        return [user, assistant]

    # ---------------------------------------------------------------- bubbles
    def bubbles(self, sid, notes=None, selection="", screenshot=None, trigger="auto", active=0):
        """Look at the learner's screen and float up a few optional ideas."""
        session = self._get(sid)
        if not AGENTS[session["agent"]]["bubbles"]:
            return []
        if notes is not None:
            session = self._mutate(sid, lambda s: s["state"].__setitem__("notes", as_str(notes, 60000)))
        state = session["state"]
        if not isinstance(screenshot, str) or not 100 < len(screenshot) < 6_000_000:
            screenshot = None
        params = adapt.bubble_params(state.get("adapt"))
        try:
            active = max(0, int(active))
        except (TypeError, ValueError):
            active = 0
        if trigger == "manual":
            count = max(1, params["per_round"])
        else:
            if not params["auto"]:
                return []
            count = min(params["per_round"], params["max_active"] - active)
        if count <= 0:
            return []

        # bubbles the client lost track of (tab closed etc.) are closed without judgement
        past = self.store.bubbles(sid, limit=40)
        now = time.time()
        for bubble in past:
            if bubble["status"] == "shown" and now - bubble["created"] > 600:
                self.store.resolve_bubble(bubble["id"], "lost")
        kept = [b["text"] for b in past if b["status"] in ("accepted", "expanded")][-5:]
        dismissed = [b["text"] for b in past if b["status"] == "ignored"][-5:]
        avoid = [b["text"] for b in past][-20:]
        kinds = adapt.pick_kinds(state.get("adapt"), count, self.rng)

        recent = self.store.messages(sid, limit=6)
        parts = [self.workspace(state, notes_limit=5000)]
        if recent:
            parts.append("<recent_conversation>\n" + "\n".join(
                f"{m['role']}: {_clip(m['content'], 500)}" for m in recent)
                + "\n</recent_conversation>")
        selection = as_str(selection, 1500)
        if selection:
            parts.append("<selected_text>\n" + selection + "\n</selected_text>")
        if screenshot:
            parts.append("A screenshot of the learner's screen right now is attached. "
                         "Read what they are actually looking at.")
        if trigger == "idle":
            parts.append("The learner has been inactive for a while — they may be stuck or thinking.")
        elif trigger == "screen":
            parts.append("The learner is working in another window (see the screenshot); what is on "
                         "their screen has changed since the last bubbles. The bubble will reach them "
                         "as a system notification, so it must be understandable on its own.")
        elif trigger == "manual":
            parts.append("The learner asked for bubbles just now.")
        parts.append("Requested kinds: " + ", ".join(kinds))
        if kept:
            parts.append("Bubbles they kept or expanded:\n" + "\n".join("- " + t for t in kept))
        if dismissed:
            parts.append("Bubbles they dismissed:\n" + "\n".join("- " + t for t in dismissed))
        if avoid:
            parts.append("Already shown (do not repeat or rephrase):\n"
                         + "\n".join("- " + t for t in avoid))
        system = prompt("core") + "\n\n" + prompt("bubbles")
        data = self._json_call("bubbles", system, "\n\n".join(parts),
                               {"kinds": kinds, "avoid": avoid}, max_tokens=700,
                               image_b64=screenshot or None)
        out, seen = [], set(avoid)
        for raw in as_list(data.get("bubbles")):
            if not isinstance(raw, dict):
                continue
            text = as_str(raw.get("text"), 160)
            if not text or text in seen:
                continue
            kind = as_str(raw.get("kind")).lower()
            if kind not in adapt.KINDS:
                kind = kinds[0]
            seen.add(text)
            bubble = self.store.add_bubble(sid, kind, text, as_str(raw.get("why"), 240))
            bubble["ttl"] = params["ttl"]
            out.append(bubble)
            if len(out) >= count:
                break
        self.store.log(sid, "bubbles", {"trigger": trigger, "asked": kinds,
                                        "shown": [b["kind"] for b in out],
                                        "screenshot": bool(screenshot)})
        return out

    def feedback(self, bubble_id, action):
        """The learner reacted to a bubble. This is what tunes the agent."""
        if action not in _STATUS:
            raise EngineError(f"unknown action: {action}")
        bubble = self.store.get_bubble(bubble_id)
        if not bubble:
            raise EngineError("bubble not found", 404)
        sid = bubble["session_id"]
        if bubble["status"] != "shown":
            session = self._get(sid)
            return {"adapt": session["state"]["adapt"], "shelf": session["state"].get("shelf", []),
                    "params": adapt.bubble_params(session["state"]["adapt"]), "messages": []}
        self.store.resolve_bubble(bubble_id, _STATUS[action])

        def apply(s):
            st = s["state"]
            st["adapt"] = adapt.apply_feedback(st.get("adapt"), bubble["kind"], action)
            if action in ("accept", "expand"):
                st.setdefault("shelf", []).append(
                    {"id": bubble["id"], "kind": bubble["kind"], "text": bubble["text"]})
        session = self._mutate(sid, apply)
        with self._lock:  # long-term learner profile follows the session, slowly
            self.store.kv_set("profile", adapt.merge_into_profile(
                self.profile(), session["state"]["adapt"], weight=0.2))
        self.store.log(sid, "bubble_feedback", {"bubble": bubble_id, "kind": bubble["kind"],
                                                "action": action,
                                                "agency": session["state"]["adapt"]["agency"]})
        messages = []
        if action == "expand":
            messages = self._expand(session, bubble)
        return {"adapt": session["state"]["adapt"], "shelf": session["state"].get("shelf", []),
                "params": adapt.bubble_params(session["state"]["adapt"]), "messages": messages}

    def _expand(self, session, bubble):
        sid = session["id"]
        user = self.store.add_message(sid, "user", bubble["text"],
                                      {"type": "expand", "bubble": bubble["id"], "kind": bubble["kind"]})
        system = "\n\n".join([prompt("core"), prompt("expand"),
                              adapt.stance(session["state"].get("adapt")),
                              self.workspace(session["state"])])
        history = self._history(self.store.messages(sid))
        reply = llm.complete("expand", system, history, max_tokens=1200,
                             meta={"bubble": bubble["text"]})
        assistant = self.store.add_message(sid, "assistant", reply or "…",
                                           {"type": "expand_reply", "bubble": bubble["id"]})
        return [user, assistant]

    # ----------------------------------------------------------------- update
    def update(self, sid, patch):
        """Edits made by the learner in the workspace."""
        def apply(s):
            st = s["state"]
            if "title" in patch:
                s["title"] = as_str(patch["title"], 80) or s["title"]
            if "notes" in patch:
                st["notes"] = as_str(patch["notes"], 60000)
            if isinstance(patch.get("card"), dict):
                card = patch["card"]
                st["card"] = {"goal": as_str(card.get("goal"), 800),
                              "givens": _str_list(card.get("givens"), 12),
                              "constraints": _str_list(card.get("constraints"), 12),
                              "success_criteria": _str_list(card.get("success_criteria"), 12)}
            if isinstance(patch.get("tasks"), list):
                tasks = []
                for index, raw in enumerate(patch["tasks"][:30]):
                    if isinstance(raw, dict) and as_str(raw.get("title")):
                        tasks.append({"id": as_str(raw.get("id"), 12) or f"u{index}{int(time.time()) % 10000}",
                                      "title": as_str(raw.get("title"), 200),
                                      "detail": as_str(raw.get("detail"), 500),
                                      "thread": as_str(raw.get("thread"), 12),
                                      "done": bool(raw.get("done"))})
                st["tasks"] = tasks
            if isinstance(patch.get("shelf"), list):
                st["shelf"] = [{"id": as_str(i.get("id"), 12), "kind": as_str(i.get("kind"), 20),
                                "text": as_str(i.get("text"), 200)}
                               for i in patch["shelf"][:100] if isinstance(i, dict)]
            if isinstance(patch.get("adapt"), dict):
                current = adapt.normalize(st.get("adapt"))
                incoming = patch["adapt"]
                if incoming.get("reset"):
                    current = adapt.default_state()
                if "agency" in incoming:
                    current["agency"] = clamp(incoming["agency"], 0, 1, current["agency"])
                if "locked" in incoming:
                    current["locked"] = bool(incoming["locked"])
                if "paused" in incoming:
                    current["paused"] = bool(incoming["paused"])
                st["adapt"] = adapt.normalize(current)
        session = self._mutate(sid, apply)
        if "adapt" in patch:
            self.store.log(sid, "adapt_manual", {"adapt": patch["adapt"]})
        return session

    # ------------------------------------------------------- pre / post tests
    def judge_answer(self, problem, answer):
        answer = as_str(answer, 6000)
        if not answer:
            return {}
        content = ("<original_problem>\n" + _clip(problem, 3000) + "\n</original_problem>\n\n"
                   "<answer>\n" + answer + "\n</answer>")
        data = self._json_call("judge_answer", prompt("judge_answer"), content,
                               {"answer": answer}, max_tokens=500)
        raw = data.get("scores") if isinstance(data.get("scores"), dict) else {}
        scores = {dim: int(round(clamp(raw.get(dim), 0, 4, 0))) for dim in RUBRIC}
        scores["total"] = sum(scores[d] for d in RUBRIC)
        scores["comment"] = as_str(data.get("comment"), 400)
        return scores

    def submit_test(self, sid, phase, likert, answer):
        if phase not in ("pre", "post"):
            raise EngineError("phase must be 'pre' or 'post'")
        session = self._get(sid)
        allowed = LIKERT_PRE if phase == "pre" else LIKERT_POST
        clean = {}
        for key in allowed:
            if isinstance(likert, dict) and likert.get(key) not in (None, ""):
                clean[key] = int(round(clamp(likert[key], 1, 5, 3)))
        answer = as_str(answer, 6000)
        scores = self.judge_answer(self._problem_text(session["state"]), answer)
        self.store.add_test(sid, phase, clean, answer, scores)
        self.store.log(sid, "test", {"phase": phase, "likert": clean,
                                     "rubric_total": scores.get("total")})
        return {"tests": self.store.tests(sid)}

    # ------------------------------------------------------------------ arena
    def first_response(self, agent, problem):
        """What a given agent says first when handed the raw problem."""
        if AGENTS[agent]["scaffold"]:
            frame = self._frame_call({"problem": problem})
            return frame_to_text(frame, is_zh(problem))
        name = AGENTS[agent]["chat"]
        return llm.complete(name, prompt(name), [{"role": "user", "content": problem}],
                            max_tokens=2000, meta={"problem": problem})

    def arena_create(self, problem, agents=("baseline", "socratic", "collaborator")):
        problem = as_str(problem, 8000)
        if not problem:
            raise EngineError("describe a problem first")
        entries = [{"agent": a, "text": self.first_response(a, problem)} for a in agents]
        self.rng.shuffle(entries)
        for index, entry in enumerate(entries):
            entry["label"] = chr(ord("A") + index)
        aid = self.store.add_arena(problem, entries)
        return {"id": aid, "problem": problem,
                "entries": [{"label": e["label"], "text": e["text"]} for e in entries]}

    def arena_rate(self, aid, ratings, best):
        arena = self.store.get_arena(aid)
        if not arena:
            raise EngineError("arena round not found", 404)
        labels = {e["label"]: e["agent"] for e in arena["entries"]}
        clean = {}
        for label, value in (ratings or {}).items():
            if label in labels and value not in (None, ""):
                clean[labels[label]] = int(round(clamp(value, 1, 5, 3)))
        result = {"ratings": clean, "best": labels.get(best)}
        self.store.rate_arena(aid, result)
        return {"reveal": labels, "result": result}

    # ------------------------------------------------------------------ stats
    def stats(self):
        sessions = {s["id"]: s for s in self.store.all_sessions()}
        by_session = {}
        for test in self.store.all_tests():
            by_session.setdefault(test["session_id"], {})[test["phase"]] = test

        def mean(values):
            values = [v for v in values if v is not None]
            return round(sum(values) / len(values), 2) if values else None

        def likert_mean(test):
            values = [test["likert"].get(k) for k in LIKERT_PRE if test["likert"].get(k)]
            return sum(values) / len(values) if values else None

        agents = {}
        for sid, session in sessions.items():
            row = agents.setdefault(session["agent"], {
                "sessions": 0, "paired": 0, "pre_likert": [], "post_likert": [],
                "pre_rubric": [], "post_rubric": [], "ownership": [], "helpful": []})
            row["sessions"] += 1
            tests = by_session.get(sid, {})
            if "pre" in tests and "post" in tests:
                pre, post = tests["pre"], tests["post"]
                row["paired"] += 1
                row["pre_likert"].append(likert_mean(pre))
                row["post_likert"].append(likert_mean(post))
                if pre["scores"].get("total") is not None and post["scores"].get("total") is not None:
                    row["pre_rubric"].append(pre["scores"]["total"])
                    row["post_rubric"].append(post["scores"]["total"])
                row["ownership"].append(post["likert"].get("ownership"))
                row["helpful"].append(post["likert"].get("helpful"))
        table = []
        for name, row in agents.items():
            pre_l, post_l = mean(row["pre_likert"]), mean(row["post_likert"])
            pre_r, post_r = mean(row["pre_rubric"]), mean(row["post_rubric"])
            table.append({
                "agent": name, "sessions": row["sessions"], "paired": row["paired"],
                "pre_likert": pre_l, "post_likert": post_l,
                "likert_gain": round(post_l - pre_l, 2) if pre_l is not None and post_l is not None else None,
                "pre_rubric": pre_r, "post_rubric": post_r,
                "rubric_gain": round(post_r - pre_r, 2) if pre_r is not None and post_r is not None else None,
                "ownership": mean(row["ownership"]), "helpful": mean(row["helpful"]),
            })
        table.sort(key=lambda r: list(AGENTS).index(r["agent"]) if r["agent"] in AGENTS else 99)

        kinds = {k: {"shown": 0, "accepted": 0, "expanded": 0, "ignored": 0, "faded": 0}
                 for k in adapt.KINDS}
        for bubble in self.store.all_bubbles():
            row = kinds.get(bubble["kind"])
            if row is None:
                continue
            row["shown"] += 1
            if bubble["status"] in row:
                row[bubble["status"]] += 1

        arena = {}
        rounds = 0
        for item in self.store.all_arena():
            result = item.get("result")
            if not result:
                continue
            rounds += 1
            for agent, rating in (result.get("ratings") or {}).items():
                arena.setdefault(agent, {"ratings": [], "best": 0})["ratings"].append(rating)
            if result.get("best"):
                arena.setdefault(result["best"], {"ratings": [], "best": 0})["best"] += 1
        arena_table = [{"agent": a, "n": len(r["ratings"]), "mean_rating": mean(r["ratings"]),
                        "best": r["best"]} for a, r in arena.items()]
        return {"agents": table, "bubbles": kinds, "arena": arena_table, "arena_rounds": rounds,
                "profile": self.profile()}

    # ----------------------------------------------------------------- export
    def export_markdown(self, sid):
        session = self._get(sid)
        state = session["state"]
        out = [f"# {session['title']}", "",
               f"- agent: {session['agent']}",
               f"- created: {time.strftime('%Y-%m-%d %H:%M', time.localtime(session['created']))}",
               "", "## Problem", "", self._problem_text(state), ""]
        frame = state.get("frame")
        if frame:
            out += ["## Framing", "", frame_to_text(frame, is_zh(state.get("problem"))), ""]
        card = state.get("card")
        if card:
            out += ["## Problem card", "", f"**Goal:** {card.get('goal', '')}", ""]
            for key, label in (("givens", "Givens"), ("constraints", "Constraints"),
                               ("success_criteria", "Success criteria")):
                if card.get(key):
                    out += [f"**{label}:**"] + [f"- {v}" for v in card[key]] + [""]
        if state.get("tasks"):
            out += ["## Tasks", ""] + [
                f"- [{'x' if t.get('done') else ' '}] {t['title']}" for t in state["tasks"]] + [""]
        if state.get("shelf"):
            out += ["## Kept ideas", ""] + [f"- {i['text']}" for i in state["shelf"]] + [""]
        if (state.get("notes") or "").strip():
            out += ["## Notes", "", state["notes"], ""]
        messages = self.store.messages(sid)
        if messages:
            out += ["## Conversation", ""]
            for message in messages:
                who = "AI" if message["role"] == "assistant" else "Me"
                if message.get("meta", {}).get("type") == "expand":
                    who = "Me (expanded bubble)"
                out += [f"**{who}:** {message['content']}", ""]
        for test in self.store.tests(sid):
            out += [f"## {test['phase'].title()}-test", "", f"- self-report: {test['likert']}",
                    f"- answer: {test['answer']}", f"- rubric: {test['scores']}", ""]
        return "\n".join(out)


def frame_to_text(frame, zh=True):
    """Readable rendering of a frame (used for the arena and for export)."""
    label = {
        "known": "已知" if zh else "Known",
        "unknown": "未知 / 待定" if zh else "Unknown / undecided",
        "questions": "想先问你" if zh else "Questions for you",
        "landscape": "相关的知识版图" if zh else "The terrain",
        "threads": "可以走的线" if zh else "Ways forward",
        "main": "主线" if zh else "main",
        "side": "支线" if zh else "side",
        "first": "第一步" if zh else "First step",
    }
    out = [frame.get("restatement", "")]
    if frame.get("knowns"):
        out += ["", f"**{label['known']}**"] + [f"- {v}" for v in frame["knowns"]]
    if frame.get("unknowns"):
        out += ["", f"**{label['unknown']}**"] + [f"- {v}" for v in frame["unknowns"]]
    if frame.get("clarifying_questions"):
        out += ["", f"**{label['questions']}**"] + [f"- {v}" for v in frame["clarifying_questions"]]
    if frame.get("landscape"):
        out += ["", f"**{label['landscape']}**"] + [
            f"- **{i['topic']}** — {i['why']}" for i in frame["landscape"]]
    if frame.get("threads"):
        out += ["", f"**{label['threads']}**"]
        for thread in frame["threads"]:
            out.append(f"- [{label[thread['type']]}] **{thread['title']}** — {thread['summary']}"
                       + (f" *{label['first']}: {thread['first_step']}*" if thread.get("first_step") else ""))
    if frame.get("recommendation"):
        out += ["", frame["recommendation"]]
    return "\n".join(out).strip()
