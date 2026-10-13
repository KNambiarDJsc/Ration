---
name: autopilot
description: Plan safe, verifiable unattended work for idle usage capacity with RATION Autopilot. Shows the plan only; it never starts a run.
argument-hint: "[--mode safe|build|research] [--max-tasks N]"
disable-model-invocation: true
allowed-tools: Bash(ration autopilot --dry-run:*), Bash(ration autopilot status:*), Bash(ration --version)
---

Show the user what RATION Autopilot would do in this project. This skill plans; it does not run anything.

1. Run `ration autopilot --dry-run $ARGUMENTS` from the project root. If the command is not found, tell the user to install RATION
   (`uv tool install ration-cli`) and stop.
2. Show the output as it is. Do not re-rank the tasks, do not add tasks of your own, and do not restate estimates as facts:
   the budget figures marked "estimate" are estimates.
3. If the plan says autopilot will not run, explain the stated reason in one sentence and stop. That outcome is correct, not an error.
4. Otherwise, tell the user the exact command that would start it, for them to run in a separate terminal:
   `ration autopilot $ARGUMENTS`. Mention that each task lands on its own `ration/auto/...` branch and nothing is merged or pushed.

Never start a real run yourself, and never pass `--yes`. Starting unattended work is the user's decision, made in their own terminal.
