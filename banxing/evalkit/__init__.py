"""Evaluation: labelled conversations, and what the watcher does with them.

  replay_cases  conversations in pairs that differ in the one thing that matters:
                where a concern is warranted and by which turn, where silence is
                right and why
  replay        play them to the watcher one turn at a time: timing, aim,
                interruptions, how much the supervisor went looking, and a one-read
                control; `timeline` does the same for any conversation without labels

What this gives depends on the model behind it. With a real supervising model the
report is structural evidence about its judgement on synthetic conversations (level
E4 in docs/verification.md). With the offline stand-in it only shows that the
plumbing works, and the report says so. A judge model and human rating of real
conversations are not implemented.

All case text was written for this repository.
"""
