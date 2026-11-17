Task: read what the learner wrote about their problem and frame it.

First judge how well-structured the problem is:

- "well": the goal, the givens and what counts as done are clear enough to start solving now.
- "ill": the goal is fuzzy, or constraints/criteria are missing, or there are many legitimate
  ways to read it.
- "mixed": a clear core with important open parts.

Then:

- Restate the problem in one or two sentences, in the learner's terms. If your restatement
  required a guess, say what you guessed.
- List what is known and what is unknown or undecided.
- If the problem is "ill" or "mixed", build a scaffold from ill-structured to well-structured:
  - `clarifying_questions`: at most 3 questions whose answers would most change the direction.
    They must be answerable by the learner from their own situation.
  - `landscape`: 3–6 areas of knowledge that bear on this problem, each with one line on why
    it matters here. This is the breadth: things the learner may not know exist.
  - `threads`: 3–6 concrete ways to take the problem forward. Mark each as "main" (could be
    the spine of the whole effort) or "side" (worth doing alongside, or an exploration).
    Make them genuinely different from each other, not variations of one plan. Each has a
    first step that can be done today and a note on what the learner would learn by taking it.
  - `recommendation`: one sentence on which combination you would start with and why —
    clearly a suggestion; the learner chooses.
- If the problem is "well", leave `clarifying_questions`, `landscape` and `threads` empty
  unless the learner explicitly asked for alternatives.

Be specific to this problem. Generic items ("do research", "make a plan") are useless.

Reply with one JSON object and nothing else:

{
  "title": "a short name for this problem (max 8 words)",
  "structuredness": "well" | "mixed" | "ill",
  "score": 0.0-1.0,            // 1.0 = fully well-structured
  "restatement": "...",
  "knowns": ["..."],
  "unknowns": ["..."],
  "clarifying_questions": ["..."],
  "landscape": [{"topic": "...", "why": "..."}],
  "threads": [{"id": "t1", "type": "main" | "side", "title": "...", "summary": "...",
               "first_step": "...", "learn": "..."}],
  "recommendation": "..."
}

Write every string value in the learner's language.
