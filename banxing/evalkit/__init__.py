"""Evaluation: a labelled set of drafts, and checks on what the model answers.

  scenarios  the drafts (with optional chat excerpts) and what is expected of them
  checks     structural checks that need no judge: format, three routes, kinds, one
             main line, kept conditions, marked examples, no questions back, safety
  run        run a model over the set and report

What this gives depends on the model behind it. With a real model the report is
evidence about suggestion quality for that model and prompt version (level E4 in
docs/banxing-generation2.md). With the offline stand-in it only shows that the
plumbing works, and the report says so. A judge model and blind human rating are
planned (milestone M4) and not implemented.

All scenario text was written for this repository.
"""
