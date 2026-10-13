---
name: status
description: Show how much of each usage window (5-hour, weekly) is used, the burn pressure, and RATION's recommendation.
disable-model-invocation: true
allowed-tools: Bash(ration status:*)
---

Run `ration status` and show the output as it is. If it reports no usage data, tell the user that `ration statusline install`
(run in their own terminal) lets RATION record the official usage figures from Claude Code's status line.
Do not estimate usage yourself.
