Task: turn the framed problem plus the learner's choices into a well-structured working
definition that the learner will own and edit.

You are given the original problem, the framing, the threads the learner chose (main line and
side branches, possibly one they wrote themselves) and any answers to clarifying questions.

Produce:

- `card`: the problem card.
  - `goal`: one sentence, concrete enough that someone could tell whether it was reached.
  - `givens`: what is known or available.
  - `constraints`: real limits (time, resources, rules, scope). Where the learner has not
    said, write the assumption you are making and mark it with "(assumed)" — in the learner's
    language — so they can correct it.
  - `success_criteria`: 2–4 observable checks for "done".
- `tasks`: 3–7 tasks in a sensible order along the chosen main line, with side-branch tasks
  marked by their thread. Each task is something the learner does (you help), small enough to
  finish in one sitting. `detail` says what finishing it looks like.
- `opening`: 2–3 sentences to start the working phase. Say what the card captures, name one
  thing in it you are least sure about, and ask where they want to begin. Do not start
  solving yet.

Reply with one JSON object and nothing else:

{
  "card": {"goal": "...", "givens": ["..."], "constraints": ["..."], "success_criteria": ["..."]},
  "tasks": [{"title": "...", "detail": "...", "thread": "t1"}],
  "opening": "..."
}

Write every string value in the learner's language.
