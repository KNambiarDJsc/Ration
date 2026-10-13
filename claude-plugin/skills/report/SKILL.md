---
name: report
description: Show the latest RATION Autopilot run in this project - what was accepted, what was discarded and why, and which branches to review.
argument-hint: "[run id]"
disable-model-invocation: true
allowed-tools: Bash(ration autopilot status:*), Bash(ration autopilot report:*), Bash(git diff:*), Bash(git log:*)
---

1. Run `ration autopilot status`. If there are no runs yet, say so and suggest `/ration:autopilot`.
2. Run `ration autopilot report` (add `--run $ARGUMENTS` when a run id was given) and show it as it is.
3. For each branch listed under "Branches to review", offer to walk through its diff with the `git diff` command the report prints.
   Review only. Do not merge, rebase, push or delete any branch unless the user asks for that specific action.
